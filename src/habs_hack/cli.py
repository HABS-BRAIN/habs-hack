"""The `habs` command -- the whole of what a team installs.

Six verbs, all of which act on one team's own container: run, submit, status,
logs, cancel, ls. The scheduler, the dispatcher, the image builder and the
Batch backend live in `habs_platform` and are not published, so nothing a
student can pip install contains them.

That is a hygiene boundary, not a security one. What actually stops a team
reaching another team's data is RBAC: their identity can write one container
and read `shared`, so even this client pointed somewhere else gets a 403.

Nothing here holds a credential. In CI the team's token comes from the OIDC
login step; on a laptop it comes from `az login`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from habs_hack import github
from habs_hack.backends.local import run_local
from habs_hack.model import TERMINAL_STATES, Job, JobState, Limits
from habs_hack.store import BUILD_PARTS, RUN_PARTS, BlobStore, LocalStore

POLL_SECONDS = 10


# -- wiring -------------------------------------------------------------------


def _store(args: argparse.Namespace):
    """BlobStore in the event, LocalStore when HABS_STORE_DIR is set.

    The local one is not a student feature; it is how the platform is rehearsed
    end to end without Azure, and how the tests run.
    """
    local = os.environ.get("HABS_STORE_DIR")
    if local:
        return LocalStore(Path(local))

    sas = os.environ.get("HABS_SAS", "").strip()
    account = getattr(args, "storage", None) or os.environ.get("HABS_STORAGE")
    if sas and not account:
        # The link HABS sent carries the account name, so there is nothing
        # else for a student to set up on their own machine.
        account = sas.split("//", 1)[-1].split(".", 1)[0]
    if not account:
        raise SystemExit(
            "habs: nothing to connect to. On your own machine, paste the link HABS "
            "sent you:\n    export HABS_SAS='https://...'\n"
            "In your team repo this is already set for you."
        )
    return BlobStore(account)


def _team(args: argparse.Namespace) -> str:
    """Which storage container this team writes to.

    It is the team's client id, and it is the whole of HABS_KEY. One value
    serves as both the identity GitHub signs in as and the name of the
    container, so a team pastes one opaque line and nothing anywhere reveals
    how many teams there are or what they are called.

    Neither use is a secret: the identity is worthless without a token GitHub
    mints for their repository alone.
    """
    team = getattr(args, "team", None) or os.environ.get("HABS_TEAM")
    if not team:
        team = os.environ.get("HABS_KEY", "").strip()
    if not team:
        # The read-only link HABS sends names the container in its path.
        sas = os.environ.get("HABS_SAS", "").strip()
        if sas:
            team = sas.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if not team:
        raise SystemExit(
            "habs: I don't know which team you are.\n"
            "In your repo this is HABS_KEY, the line HABS sent you.\n"
            "On your own machine: export HABS_KEY='<that line>'"
        )
    return team


def _out(args: argparse.Namespace) -> Path | None:
    """Where results land, or None for "list them, do not download".

    None is the default on purpose. In CI this runs on every push, and pulling
    every team's results through a GitHub runner moves the data out of Azure
    whether or not anybody ever looks at it -- egress we pay for, to produce a
    copy that is deleted when the runner is.
    """
    return Path(args.out) if args.out else None


def _pack(source: Path) -> Path:
    """The build context: the committed tree, or the directory if it is not a repo.

    `git archive` rather than a tar of the working directory, so what gets built
    is exactly the commit that was submitted -- an uncommitted edit that works on
    a laptop and is missing on the GPU is a confusing hour for everyone.
    """
    handle, name = tempfile.mkstemp(suffix=".tar.gz", prefix="habs-src-")
    os.close(handle)
    path = Path(name)
    try:
        with path.open("wb") as fh:
            subprocess.run(
                ["git", "archive", "--format=tar.gz", "HEAD"],
                cwd=source, stdout=fh, check=True, stderr=subprocess.PIPE,
            )
    except (subprocess.CalledProcessError, FileNotFoundError):
        with tarfile.open(path, "w:gz") as tar:
            for item in sorted(source.iterdir()):
                if item.name not in {".git", ".venv", "out", "__pycache__"}:
                    tar.add(item, arcname=item.name)
    return path


# -- student commands ---------------------------------------------------------


def _cmd_run(args: argparse.Namespace) -> int:
    source, data, out = Path(args.source), Path(args.data), Path(args.out)
    try:
        result = run_local(source, data, out, team=args.team, job_id="local")
    except FileNotFoundError as exc:
        print(f"habs: {exc}", file=sys.stderr)
        return 2

    # The log goes out whole and first: on a failure it is the only thing the
    # student needs, and burying it under a summary line is how people miss it.
    if result.log:
        print(result.log, end="" if result.log.endswith("\n") else "\n")

    if result.exit_code == 0:
        print(f"\nok - {len(result.outputs)} file(s) written to {out}")
        for path in result.outputs[:20]:
            print(f"  {path.relative_to(out)}")
        if len(result.outputs) > 20:
            print(f"  ... and {len(result.outputs) - 20} more")
    else:
        print(f"\nfailed - exit code {result.exit_code}, nothing was kept", file=sys.stderr)
    return result.exit_code


def _cmd_submit(args: argparse.Namespace) -> int:
    store, team = _store(args), _team(args)
    source = Path(args.source)
    if not (source / "process.py").is_file():
        print(f"habs: no process.py in {source}", file=sys.stderr)
        return 2

    submitted_at = datetime.now(UTC)
    archive = _pack(source)
    try:
        store.request(team, args.repo or "", args.sha or "", archive)
    finally:
        archive.unlink(missing_ok=True)

    print(f"submitted {team} @ {(args.sha or 'HEAD')[:7]}")
    until = "done" if args.wait else args.until
    if until == "submitted":
        print("  habs status      where it is, and why it is waiting")
        return 0

    job = _await_build(store, team, submitted_at, args.timeout)
    if job is None:
        return _gave_up("your image is still building")
    if job.state is JobState.FAILED:
        return _build_failed(job)
    if until == "built":
        _built(job, submitted_at)
        return 0
    return _await_result(store, team, job.job_id, _out(args), args.timeout)


def _cmd_watch(args: argparse.Namespace) -> int:
    """Follow a job somebody else queued -- in practice, the one the build job
    of this same workflow left behind. Splitting submit from watch is what lets
    a failed image and failed code be two different red crosses."""
    store, team = _store(args), _team(args)
    ref = (args.job or "").strip()
    job_id = _resolve(store, team, ref) if ref else _latest(store, team)
    if not job_id:
        # Loudly, and not by quietly watching some other job: a run that
        # reports the wrong job's result is worse than one that fails.
        print(f"habs: no job of yours matches {ref!r}" if ref else "habs: no job to watch",
              file=sys.stderr)
        return 2
    return _await_result(store, team, job_id, _out(args), args.timeout)


# -- the two halves of a run --------------------------------------------------


def _await_build(store, team: str, since: datetime, timeout: int):
    """Wait for the submission to become a job with an image.

    The dispatcher writes the job as BUILDING *before* it calls the registry,
    so this sees it within a tick and then has the build log to show for the
    minute or two that follows.
    """
    with github.group("building your image"):
        job = _track(store, team, lambda: _mine(store, team, since),
                     until=lambda j: j.state is not JobState.BUILDING,
                     parts=BUILD_PARTS, timeout=timeout)
    if job is not None:
        # The TAIL of the id, not the id. A job id begins with the team's
        # client id, azure/login registers that as a secret, and GitHub
        # redacts any job output containing a secret rather than passing it --
        # so handing over the whole id hands over an empty string, and the
        # next job silently falls back to "your latest", which after a second
        # push is the wrong job.
        github.output("job", job.job_id.split("--", 1)[-1])
    return job


def _await_result(store, team: str, job_id: str, out_dir: Path | None, timeout: int) -> int:
    """Wait for a built job to finish, and exit the way it did.

    This is what makes a red CI run mean a failed job. Without it a team
    pushes, the workflow goes green because the *upload* worked, and nothing
    anywhere tells them their code crashed an hour later.
    """
    print(f"watching {job_id}", flush=True)
    with github.group("your job"):
        job = _track(store, team, lambda: _by_id(store, team, job_id),
                     until=lambda j: j.state in TERMINAL_STATES,
                     parts=RUN_PARTS, timeout=timeout)
    if job is None:
        return _gave_up("your job is still going")
    return _finish(store, team, job, out_dir)


def _track(store, team: str, find, *, until, parts, timeout: int):
    """Poll one job, printing the log as it arrives, until *until* holds.

    Shared by both halves because the only things that differ between them are
    which log parts exist yet and what counts as finished. Polled at least once
    before the clock is consulted, so a job that is already over returns
    straight away rather than after a sleep.
    """
    deadline = time.monotonic() + timeout
    job, state, shown = None, None, 0

    while True:
        job = find() or job
        if job is not None:
            if job.state is not state:
                state = job.state
                note = f"  -- {job.wait_reason}" if job.wait_reason else ""
                print(f"[{datetime.now(UTC):%H:%M:%S}] {state.value}{note}", flush=True)
            log = store.read_log(team, job.job_id, parts)
            if len(log) > shown:
                print(log[shown:], end="", flush=True)
                shown = len(log)
            if until(job):
                return job
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_SECONDS)


def _mine(store, team: str, since: datetime):
    """The job this run submitted -- the first one newer than our request."""
    for job in store.team_jobs(team):
        if job.submitted_at >= since:
            return job
    return None


def _by_id(store, team: str, job_id: str):
    return next((j for j in store.team_jobs(team) if j.job_id == job_id), None)


def _resolve(store, team: str, ref: str) -> str | None:
    """A whole job id, or the tail of one. Newest wins if a tail is ambiguous."""
    ids = [j.job_id for j in store.team_jobs(team)]
    if ref in ids:
        return ref
    matched = [job_id for job_id in ids if job_id.endswith(ref)]
    return matched[-1] if matched else None


# -- how each ending is reported ----------------------------------------------


def _built(job: Job, since: datetime) -> None:
    took = github.duration((datetime.now(UTC) - since).total_seconds())
    print(f"\nimage built in {took}. {job.job_id} is {job.state.value}.")
    github.summary(
        "### Image built\n\n"
        + github.table([
            ("job", f"`{job.job_id.split('--', 1)[-1]}`"),
            ("took", took),
            ("now", job.wait_reason or job.state.value),
        ])
    )


def _build_failed(job: Job) -> int:
    reason = job.wait_reason or "image build failed"
    github.error(reason)
    print(f"\n{reason}", file=sys.stderr)
    github.summary(
        "### Image build failed\n\n"
        + github.table([("job", f"`{job.job_id}`"), ("why", reason)])
        + "\n\nThe log above is the registry's. It is almost always "
          "`requirements.txt`: a misspelt package, or a version that has no "
          "build for Python 3.11. **Your code never ran**, so this cost your "
          "team no GPU time."
    )
    return 1


def _gave_up(what: str) -> int:
    """Stop watching without stopping the job. Not a failure: Actions minutes
    are finite and a queue is not a crash."""
    print(f"\n{what}, and this run has stopped watching.")
    print("Nothing was cancelled.")
    print("  habs status      where it got to")
    print("  habs logs        the whole log")
    github.notice(f"{what} -- `habs status` to follow it from here")
    return 0


def _finish(store, team: str, job: Job, out_dir: Path | None) -> int:
    names = store.list_outputs(team, job.job_id)
    if names and out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        store.fetch_outputs(team, job.job_id, out_dir)
        print(f"\ndownloaded to {out_dir}/")

    if names:
        print(f"\n{len(names)} file(s) written")
        for name in names[:20]:
            print(f"  {name}")
        if len(names) > 20:
            print(f"  ... and {len(names) - 20} more")
    else:
        print("\nthe job wrote no files")

    ok = job.state is JobState.SUCCEEDED
    verdict = job.state.value + (f", exit code {job.exit_code}" if job.exit_code else "")
    print(f"\n{job.job_id} {verdict}", file=sys.stdout if ok else sys.stderr)
    _run_summary(store, team, job, names, ok, verdict)
    if not ok:
        github.error(f"{verdict} -- see the job log above")
    return 0 if ok else 1


def _run_summary(store, team: str, job: Job, names: list[str], ok: bool, verdict: str) -> None:
    """The card on the run page: what happened, what it cost, what it left.

    Deliberately the whole answer for the common case. A student who has to
    open the log to find out whether their job worked is one we have failed.
    """
    now = datetime.now(UTC)
    charged = job.gpu_seconds(now)
    used = sum(j.gpu_seconds(now) for j in store.team_jobs(team)) / 3600.0
    budget = Limits().gpu_hours_per_team

    listed = ", ".join(f"`{n}`" for n in names[:8]) or "none"
    if len(names) > 8:
        listed += f" and {len(names) - 8} more"

    github.summary(
        f"### {'Succeeded' if ok else 'Failed'}\n\n"
        + github.table([
            ("result", verdict),
            ("queued for", github.span(job.submitted_at, job.started_at)),
            ("ran for", github.span(job.started_at, job.finished_at)),
            ("GPU time charged", github.duration(charged)),
            ("your team has used", f"{used:.1f} of {budget:.0f} GPU-hours"),
            ("files written", listed),
        ])
        + "\n\nOpen the files with the results link HABS sent you -- "
          "Storage Explorer, or `export HABS_SAS=... && habs get`."
    )


def _cmd_status(args: argparse.Namespace) -> int:
    store, team = _store(args), _team(args)
    jobs = [j for j in store.team_jobs(team) if not args.job or j.job_id == args.job]
    if not jobs:
        print("no jobs yet - push to main, or `habs submit`")
        return 0

    now = datetime.now(UTC)
    used = sum(j.gpu_seconds(now) for j in jobs) / 3600.0
    for job in jobs[-args.limit:]:
        print(_status_line(job, now))
        # The reason a job is waiting is the whole point of this command, so it
        # gets its own line rather than being truncated into a column.
        if job.wait_reason and job.state not in TERMINAL_STATES:
            print(f"    {job.wait_reason}")
    print(f"\n{used:.1f} of {Limits().gpu_hours_per_team:.0f} GPU-hours used by {team}")
    return 0


def _status_line(job: Job, now: datetime) -> str:
    elapsed = timedelta(seconds=int(job.gpu_seconds(now)))
    mark = {
        JobState.SUCCEEDED: "ok",
        JobState.FAILED: "fail",
        JobState.CANCELLED: "cancelled",
    }.get(job.state, job.state.value)
    tail = f"exit {job.exit_code}" if job.exit_code not in (None, 0) else ""
    return f"  {job.job_id}  {mark:<10} {elapsed!s:>8}  {tail}"


def _cmd_logs(args: argparse.Namespace) -> int:
    store, team = _store(args), _team(args)
    job_id = args.job or _latest(store, team)
    if not job_id:
        print("habs: no job to show", file=sys.stderr)
        return 2

    shown = 0
    while True:
        log = store.read_log(team, job_id)
        if len(log) > shown:
            print(log[shown:], end="")
            shown = len(log)
        if not args.follow:
            break
        job = next((j for j in store.team_jobs(team) if j.job_id == job_id), None)
        if job and job.state in TERMINAL_STATES:
            break
        time.sleep(POLL_SECONDS)
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    store, team = _store(args), _team(args)
    job_id = args.job or _latest(store, team)
    if not job_id:
        print("habs: no job to cancel", file=sys.stderr)
        return 2
    store.request_cancel(team, job_id)
    print(f"cancel requested for {job_id}; it stops within a minute")
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    """Pull a job's results onto this machine, so they can be looked at."""
    store, team = _store(args), _team(args)
    job_id = args.job or _latest(store, team)
    if not job_id:
        print("habs: no job to download", file=sys.stderr)
        return 2

    dest = Path(args.out) / job_id
    written = store.fetch_outputs(team, job_id, dest)
    if not written:
        print(f"{job_id} wrote nothing")
        return 0
    for path in written[:20]:
        print(f"  {path}")
    if len(written) > 20:
        print(f"  ... and {len(written) - 20} more")
    print(f"\n{len(written)} file(s) in {dest}/")
    return 0


def _cmd_ls(args: argparse.Namespace) -> int:
    store, team = _store(args), _team(args)
    job_id = args.job or _latest(store, team)
    if not job_id:
        print("habs: no job to list", file=sys.stderr)
        return 2
    names = store.list_outputs(team, job_id)
    if not names:
        print(f"{job_id} wrote nothing (yet)")
        return 0
    for name in names:
        print(f"  {name}")
    print(f"\n{len(names)} file(s) in out/{job_id}")
    return 0


def _latest(store, team: str) -> str | None:
    jobs = store.team_jobs(team)
    return jobs[-1].job_id if jobs else None


# -- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="habs", description="HABS hackathon compute")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run process.py on this machine against a sample")
    run.add_argument("--source", default=".", help="directory holding process.py")
    run.add_argument("--data", required=True, help="sample data directory (read-only to you)")
    run.add_argument("--out", default="./out", help="where results are written")
    run.add_argument("--team", default="local")
    run.set_defaults(func=_cmd_run)

    submit = sub.add_parser("submit", help="queue a run on a GPU")
    submit.add_argument("--source", default=".")
    submit.add_argument("--repo", help="owner/name, for the record")
    submit.add_argument("--sha", help="the commit being submitted")
    # The three places a submission can reasonably be left. `built` is what the
    # build half of a team's workflow asks for: it returns as soon as the image
    # exists, so a broken requirements.txt is a red cross in ninety seconds
    # instead of a red cross at the end of an hour in a queue.
    submit.add_argument("--until", choices=("submitted", "built", "done"),
                        default="submitted",
                        help="how far to follow it (default: just queue it)")
    submit.add_argument("--wait", action="store_true", help="shorthand for --until done")
    submit.add_argument("--out", default="",
                        help="download results here when it ends (default: only list them)")
    # Every minute spent waiting is a GitHub Actions minute, and most of them
    # are spent in a queue rather than computing. Bounded, so one submission
    # cannot eat an afternoon of a team's allowance.
    submit.add_argument("--timeout", type=int, default=1200,
                        help="seconds to follow the job before letting go (default 20 min)")
    submit.set_defaults(func=_cmd_submit)

    watch = sub.add_parser("watch", help="follow a job already queued, and exit the way it does")
    watch.add_argument("job", nargs="?", help="defaults to your latest")
    watch.add_argument("--out", default="",
                       help="download results here when it ends (default: only list them)")
    watch.add_argument("--timeout", type=int, default=3600)
    watch.set_defaults(func=_cmd_watch)

    status = sub.add_parser("status", help="your team's jobs, and why they are waiting")
    status.add_argument("job", nargs="?")
    status.add_argument("--limit", type=int, default=20)
    status.set_defaults(func=_cmd_status)

    logs = sub.add_parser("logs", help="full stdout and stderr for a job")
    logs.add_argument("job", nargs="?")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.set_defaults(func=_cmd_logs)

    cancel = sub.add_parser("cancel", help="stop a job")
    cancel.add_argument("job", nargs="?")
    cancel.set_defaults(func=_cmd_cancel)

    ls = sub.add_parser("ls", help="what a job wrote")
    ls.add_argument("job", nargs="?")
    ls.set_defaults(func=_cmd_ls)

    get = sub.add_parser("get", help="download what a job wrote")
    get.add_argument("job", nargs="?")
    get.add_argument("--out", default="./results", help="where to put them")
    get.set_defaults(func=_cmd_get)

    for p in (submit, watch, status, logs, cancel, ls, get):
        p.add_argument("--team", help="defaults to $HABS_TEAM")
        p.add_argument("--storage", help="defaults to $HABS_STORAGE")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
