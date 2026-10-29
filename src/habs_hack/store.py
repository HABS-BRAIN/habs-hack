"""Where a job lives between a student's push and their result.

The layout is the security model, so it is worth stating plainly. A team's
identity can write exactly one place -- its own container -- and read two: its
own container and `shared`. So a submission is a file a team drops in its own
container, and everything authoritative is written by the platform into a
`control` container no team has any role on.

That split is what stops a team editing its own GPU-hour accounting. If the
queue lived somewhere teams could write, fair-share would be advisory.

    <team>/                       team writes, team reads
      _submit/<id>.json           "please run this commit"
      _submit/<id>.tar.gz         the source, until the platform takes it
      jobs/<job_id>.json          status, written by the platform for them
      logs/<job_id>/std*.txt      whole logs, uploaded by the backend
      out/<job_id>/...            whatever process() wrote
      work/...                    carries from one of their jobs to the next

    control/                      platform only
      jobs/<job_id>.json          the record of every job, kept
      active/<job_id>.json        the same record WHILE IN FLIGHT, then deleted
      usage.json                  GPU seconds already spent, per team
      code/<job_id>.tar.gz        the exact tree that will run

Two of those need a word.

`active/` exists so that one scheduling decision costs a listing of what is in
flight rather than a download of every job of the event. With 35 teams pushing
for four weeks `jobs/` ends up in the thousands, and the tick ran against all
of them, every minute. What the scheduler needs from the jobs it cannot see is
one number per team, and `usage.json` is that number.

`code/` is here, and not in the team's own container, because a team can write
its own container. The tarball a job runs is the tree we recorded the sha of,
and it has to stay out of reach between the push and the GPU -- otherwise "we
ran your commit" is something we hope rather than something we enforce.

`LocalStore` is the same tree on disk. It exists so the dispatcher can be run
and tested end to end without Azure, which is the only way the fairness rules
get exercised before 35 teams depend on them.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from habs_hack.model import TERMINAL_STATES, Job, JobState

CONTROL = "control"
SHARED = "shared"
#: Everything else in the account is a team. Team containers are named after
#: the team's client id, so there is no prefix to match on -- and matching one
#: is how the dispatcher silently stopped seeing submissions when the naming
#: changed. Excluding the two we own is the rule that cannot drift.
PLATFORM_CONTAINERS = frozenset({CONTROL, SHARED})

#: In the order they happen: the build, then the program's own output, then
#: anything the platform has to say about why it stopped.
LOG_PARTS = ("build.txt", "stdout.txt", "stderr.txt", "platform.txt")

#: The same list, cut where the CI workflow is cut. The build half and the run
#: half are separate GitHub jobs, and each must show its own output only --
#: replaying 400 lines of pip resolution above a student's own `print()` is how
#: the thing they are looking for gets lost.
BUILD_PARTS = ("build.txt",)
RUN_PARTS = ("stdout.txt", "stderr.txt", "platform.txt")

#: Where the platform's own copies live inside `control`.
ACTIVE = "active"
CODE = "code"
USAGE = "usage.json"
LOCK = "dispatcher.lock"

#: How many finished job ids `usage.json` remembers. Its only job is to make
#: banking idempotent across a crash, so it needs to outlast one tick, not the
#: event -- and an unbounded list would recreate the problem it exists to fix.
COUNTED_KEPT = 1000

#: A submitted tree is `git archive HEAD` of a student repository. Anything
#: past this is a mistake (a committed dataset, usually) and is worth refusing
#: with a sentence rather than discovering as a dispatcher OOM.
MAX_SOURCE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class Submission:
    """A team asking for a commit to be run. Written by the team, never trusted.

    `team` is taken from the container the file was found in, not from the file
    body -- a team that writes `{"team": "team-07"}` into its own container must
    not thereby submit as team 07.
    """

    team: str
    repo: str
    sha: str
    submitted_at: datetime
    source: str  # name of the accompanying tarball, in the same container


def new_job_id(team: str, now: datetime) -> str:
    """`<team>--<utc>-<rand>`: sorts by time, and the team is readable from it.

    The backend recovers the team by splitting on `--`, so the separator must
    not appear in a team name; `team-01` style names are safe.
    """
    return f"{team}--{now.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"


def job_to_dict(job: Job) -> dict[str, Any]:
    d = asdict(job)
    d["state"] = job.state.value
    for key in ("submitted_at", "started_at", "finished_at", "state_since"):
        value = getattr(job, key)
        d[key] = value.isoformat() if value else None
    return d


def job_from_dict(d: dict[str, Any]) -> Job:
    def when(key: str) -> datetime | None:
        raw = d.get(key)
        return datetime.fromisoformat(raw) if raw else None

    return Job(
        job_id=d["job_id"],
        team=d["team"],
        image=d["image"],
        submitted_at=when("submitted_at") or datetime.now(UTC),
        state=JobState(d["state"]),
        started_at=when("started_at"),
        finished_at=when("finished_at"),
        wait_reason=d.get("wait_reason", ""),
        queue_position=d.get("queue_position", 0),
        exit_code=d.get("exit_code"),
        state_since=when("state_since"),
    )


class JobStore(Protocol):
    # -- the team's side, run with the team's own identity in CI --------------
    def request(self, team: str, repo: str, sha: str, source: Path) -> str: ...
    def request_cancel(self, team: str, job_id: str) -> None: ...
    def team_jobs(self, team: str) -> list[Job]: ...
    def read_log(self, team: str, job_id: str, parts: Sequence[str] = LOG_PARTS) -> str: ...
    def write_log(self, team: str, job_id: str, name: str, text: str) -> None: ...
    def list_outputs(self, team: str, job_id: str) -> list[str]: ...
    def fetch_outputs(self, team: str, job_id: str, dest: Path) -> list[Path]: ...

    # -- the platform's side --------------------------------------------------
    def pending(self) -> list[Submission]: ...
    def take(self, submission: Submission) -> bytes: ...
    def drop(self, submission: Submission) -> None: ...
    def discard_source(self, submission: Submission) -> None: ...
    def stage_code(self, job_id: str, source: bytes) -> None: ...
    def code_url(self, job_id: str, hours: int = 6) -> str: ...
    def cancellations(self) -> list[tuple[str, str]]: ...
    def clear_cancel(self, team: str, job_id: str) -> None: ...
    def jobs(self) -> list[Job]: ...
    def active(self) -> list[Job]: ...
    def usage(self) -> dict[str, float]: ...
    def put(self, job: Job) -> None: ...
    def lock(self, seconds: int = 60) -> Any: ...


class LocalStore:
    """The same tree on a filesystem. Used by tests and by `habs dispatch --local`."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # -- team side ------------------------------------------------------------

    def request(self, team: str, repo: str, sha: str, source: Path) -> str:
        now = datetime.now(UTC)
        name = f"{now.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
        drop = self.root / team / "_submit"
        drop.mkdir(parents=True, exist_ok=True)
        (drop / f"{name}.tar.gz").write_bytes(source.read_bytes())
        (drop / f"{name}.json").write_text(
            json.dumps(
                {"repo": repo, "sha": sha, "submitted_at": now.isoformat(),
                 "source": f"{name}.tar.gz"},
                indent=2,
            )
        )
        return name

    def request_cancel(self, team: str, job_id: str) -> None:
        """A team cannot write to `control`, so stopping a job is a request too."""
        folder = self.root / team / "_cancel"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / job_id).write_text("")

    def team_jobs(self, team: str) -> list[Job]:
        folder = self.root / team / "jobs"
        jobs = [job_from_dict(json.loads(p.read_text())) for p in sorted(folder.glob("*.json"))]
        return sorted(jobs, key=lambda j: j.submitted_at)

    def read_log(self, team: str, job_id: str, parts: Sequence[str] = LOG_PARTS) -> str:
        folder = self.root / team / "logs" / job_id
        chunks = [
            (folder / name).read_text(errors="replace")
            for name in parts
            if (folder / name).is_file()
        ]
        return "".join(chunks)

    def write_log(self, team: str, job_id: str, name: str, text: str) -> None:
        folder = self.root / team / "logs" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)

    def list_outputs(self, team: str, job_id: str) -> list[str]:
        folder = self.root / team / "out" / job_id
        if not folder.is_dir():
            return []
        return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())

    def fetch_outputs(self, team: str, job_id: str, dest: Path) -> list[Path]:
        folder = self.root / team / "out" / job_id
        written = []
        for name in self.list_outputs(team, job_id):
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((folder / name).read_bytes())
            written.append(target)
        return written

    # -- platform side --------------------------------------------------------

    def pending(self) -> list[Submission]:
        found: list[Submission] = []
        for meta in sorted(self.root.glob("*/_submit/*.json")):
            team = meta.parents[1].name
            body = json.loads(meta.read_text())
            found.append(
                Submission(
                    team=team,
                    repo=body.get("repo", ""),
                    sha=body.get("sha", ""),
                    submitted_at=datetime.fromisoformat(body["submitted_at"]),
                    source=body.get("source", ""),
                )
            )
        return found

    def take(self, submission: Submission) -> bytes:
        """Claim a submission: remove the request and return the tree it names.

        Claimed before anything slow happens, so a submission that crashes the
        dispatcher does not get retried forever on the next tick.
        """
        drop = self.root / submission.team / "_submit"
        name = submission.source.removesuffix(".tar.gz")
        (drop / f"{name}.json").unlink(missing_ok=True)
        return (drop / submission.source).read_bytes()

    def drop(self, submission: Submission) -> None:
        folder = self.root / submission.team / "_submit"
        (folder / f"{submission.source.removesuffix('.tar.gz')}.json").unlink(missing_ok=True)
        (folder / submission.source).unlink(missing_ok=True)

    def discard_source(self, submission: Submission) -> None:
        (self.root / submission.team / "_submit" / submission.source).unlink(missing_ok=True)

    def stage_code(self, job_id: str, source: bytes) -> None:
        folder = self.root / CONTROL / CODE
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{job_id}.tar.gz").write_bytes(source)

    def code_url(self, job_id: str, hours: int = 6) -> str:
        return str(self.root / CONTROL / CODE / f"{job_id}.tar.gz")

    def cancellations(self) -> list[tuple[str, str]]:
        return [(p.parents[1].name, p.name) for p in sorted(self.root.glob("*/_cancel/*"))]

    def clear_cancel(self, team: str, job_id: str) -> None:
        (self.root / team / "_cancel" / job_id).unlink(missing_ok=True)

    def jobs(self) -> list[Job]:
        return self._read_dir(self.root / CONTROL / "jobs")

    def active(self) -> list[Job]:
        return self._read_dir(self.root / CONTROL / ACTIVE)

    @staticmethod
    def _read_dir(folder: Path) -> list[Job]:
        if not folder.is_dir():
            return []
        jobs = [job_from_dict(json.loads(p.read_text())) for p in sorted(folder.glob("*.json"))]
        return sorted(jobs, key=lambda j: j.submitted_at)

    def usage(self) -> dict[str, float]:
        return dict(self._usage_doc()["teams"])

    def _usage_doc(self) -> dict[str, Any]:
        path = self.root / CONTROL / USAGE
        if not path.is_file():
            return {"teams": {}, "counted": []}
        return json.loads(path.read_text())

    def _bank(self, job: Job) -> None:
        doc = self._usage_doc()
        if job.job_id in doc["counted"]:
            return
        doc["teams"][job.team] = doc["teams"].get(job.team, 0.0) + job.gpu_seconds(
            job.finished_at or job.submitted_at
        )
        doc["counted"] = (doc["counted"] + [job.job_id])[-COUNTED_KEPT:]
        path = self.root / CONTROL / USAGE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2))

    def put(self, job: Job) -> None:
        """Write the record, then the team's copy. Control first: it is the truth."""
        terminal = job.state in TERMINAL_STATES
        # Banked before the record says terminal, and idempotently, so that a
        # dispatcher dying between the two re-finishes the job next tick and
        # charges it once rather than twice or not at all.
        if terminal:
            self._bank(job)
        body = json.dumps(job_to_dict(job), indent=2)
        for folder in (self.root / CONTROL / "jobs", self.root / job.team / "jobs"):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{job.job_id}.json").write_text(body)
        marker = self.root / CONTROL / ACTIVE / f"{job.job_id}.json"
        if terminal:
            marker.unlink(missing_ok=True)
        else:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(body)

    @contextlib.contextmanager
    def lock(self, seconds: int = 60) -> Any:
        """One dispatcher at a time, locally too, so the tests can exercise it."""
        import fcntl

        path = self.root / CONTROL / LOCK
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def reindex(self) -> int:
        return 0


def active(jobs: list[Job]) -> list[Job]:
    return [j for j in jobs if j.state not in TERMINAL_STATES]


class BlobStore:
    """The same tree in the hackathon storage account.

    Azure imports are lazy and the client is built per call site rather than at
    construction, so `habs run` and the tests keep working with no Azure
    packages and no credentials present.

    Note which identity is doing what: `request` runs in a team's GitHub
    Actions job under that team's federated identity, and the rest runs on the
    dispatcher under the platform identity. The same class serves both because
    the RBAC, not the code, is what separates them -- a team calling `jobs()`
    gets a 403, which is the intended answer.
    """

    def __init__(self, account: str, credential: Any | None = None) -> None:
        self.account = account
        self._credential = credential
        self._service: Any = None
        #: Read once per process, written through. One dispatcher holds the
        #: lease for the length of a tick, so there is no second writer to
        #: invalidate it underneath us.
        self._usage_cache: dict[str, Any] | None = None

    @property
    def account_url(self) -> str:
        return f"https://{self.account}.blob.core.windows.net"

    @property
    def service(self) -> Any:
        if self._service is None:
            from azure.storage.blob import BlobServiceClient

            self._service = BlobServiceClient(self.account_url, credential=self._auth())
        return self._service

    def _auth(self) -> Any:
        """A SAS string if one was handed to us, otherwise Entra.

        The SAS is how a student's own laptop reads their container. They have
        no Azure account -- the team identity is federated to GitHub Actions
        and nothing else can present it -- so without this the only place their
        results are visible is a CI log.
        """
        if self._credential is not None:
            return self._credential
        sas = os.environ.get("HABS_SAS", "").strip()
        if sas:
            return sas.split("?", 1)[-1]

        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential()

    def _container(self, name: str) -> Any:
        return self.service.get_container_client(name)

    @staticmethod
    def _is_dir(blob: Any) -> bool:
        """The storage account has a hierarchical namespace, so a listing
        contains the directories as well as the files in them: zero-byte
        entries carrying `hdi_isfolder`. Without this, `habs ls` shows a
        student's output folders as if they were files."""
        return bool((getattr(blob, "metadata", None) or {}).get("hdi_isfolder"))

    # -- team side ------------------------------------------------------------

    def request(self, team: str, repo: str, sha: str, source: Path) -> str:
        now = datetime.now(UTC)
        name = f"{now.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
        container = self._container(team)
        with source.open("rb") as fh:
            container.upload_blob(f"_submit/{name}.tar.gz", fh, overwrite=True)
        # The json lands second and is what the dispatcher looks for, so it can
        # never see a request whose source has not finished uploading.
        container.upload_blob(
            f"_submit/{name}.json",
            json.dumps(
                {"repo": repo, "sha": sha, "submitted_at": now.isoformat(),
                 "source": f"{name}.tar.gz"},
                indent=2,
            ),
            overwrite=True,
        )
        return name

    def request_cancel(self, team: str, job_id: str) -> None:
        self._container(team).upload_blob(f"_cancel/{job_id}", b"", overwrite=True)

    def team_jobs(self, team: str) -> list[Job]:
        container = self._container(team)
        jobs = []
        for blob in container.list_blobs(name_starts_with="jobs/", include=["metadata"]):
            if self._is_dir(blob):
                continue
            raw = container.download_blob(blob.name).readall()
            jobs.append(job_from_dict(json.loads(raw)))
        return sorted(jobs, key=lambda j: j.submitted_at)

    def read_log(self, team: str, job_id: str, parts: Sequence[str] = LOG_PARTS) -> str:
        container = self._container(team)
        chunks = []
        for name in parts:
            # Absent is normal -- there is no build log for a job that never
            # built, and no stdout until the task runs -- so a missing part is
            # skipped rather than reported as a problem.
            with contextlib.suppress(Exception):
                raw = container.download_blob(f"logs/{job_id}/{name}").readall()
                chunks.append(raw.decode("utf-8", errors="replace"))
        return "".join(chunks)

    def write_log(self, team: str, job_id: str, name: str, text: str) -> None:
        self._container(team).upload_blob(f"logs/{job_id}/{name}", text, overwrite=True)

    def fetch_outputs(self, team: str, job_id: str, dest: Path) -> list[Path]:
        """Download a finished job's results, so CI can hand them back."""
        container = self._container(team)
        written = []
        for name in self.list_outputs(team, job_id):
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(container.download_blob(f"out/{job_id}/{name}").readall())
            written.append(target)
        return written

    def list_outputs(self, team: str, job_id: str) -> list[str]:
        prefix = f"out/{job_id}/"
        return sorted(
            blob.name[len(prefix):]
            for blob in self._container(team).list_blobs(
                name_starts_with=prefix, include=["metadata"]
            )
            if not self._is_dir(blob)
        )

    # -- platform side --------------------------------------------------------

    def _team_containers(self) -> list[str]:
        return [c.name for c in self.service.list_containers()
                if c.name not in PLATFORM_CONTAINERS]

    def pending(self) -> list[Submission]:
        found: list[Submission] = []
        for name in self._team_containers():
            client = self._container(name)
            for blob in client.list_blobs(name_starts_with="_submit/"):
                # The directory entries an HNS account returns never end in
                # .json, so this filter already excludes them.
                if not blob.name.endswith(".json"):
                    continue
                body = json.loads(client.download_blob(blob.name).readall())
                found.append(
                    Submission(
                        team=name,
                        repo=body.get("repo", ""),
                        sha=body.get("sha", ""),
                        submitted_at=datetime.fromisoformat(body["submitted_at"]),
                        source=body.get("source", ""),
                    )
                )
        return found

    def take(self, submission: Submission) -> bytes:
        """Claim the submission and return the tree it names.

        The request json goes first and the tarball is read second: claiming
        before anything slow happens is what stops a submission that kills the
        dispatcher from being retried forever.
        """
        container = self._container(submission.team)
        name = submission.source.removesuffix(".tar.gz")
        container.delete_blob(f"_submit/{name}.json")
        # `source` is the bare filename, as LocalStore stores it too; the
        # prefix belongs to the layout, not to the submission record.
        blob = container.get_blob_client(f"_submit/{submission.source}")
        size = blob.get_blob_properties().size
        if size > MAX_SOURCE_BYTES:
            raise ValueError(
                f"your repository is {size / 1e6:.0f} MB, and the limit is "
                f"{MAX_SOURCE_BYTES / 1e6:.0f} MB. Large files belong in your "
                f"own container or in $HABS_WORK, not in git"
            )
        return blob.download_blob().readall()

    def drop(self, submission: Submission) -> None:
        """Throw a submission away without downloading it.

        For a submission we are refusing outright -- a team already at its
        queue limit. Pulling a repository through the dispatcher only to
        delete it is work nobody needs.
        """
        container = self._container(submission.team)
        for name in (f"_submit/{submission.source.removesuffix('.tar.gz')}.json",
                     f"_submit/{submission.source}"):
            with contextlib.suppress(Exception):
                container.delete_blob(name)

    def stage_code(self, job_id: str, source: bytes) -> None:
        """Put the submitted tree somewhere the team cannot reach.

        A team has Storage Blob Data Contributor on its own container, so a
        tarball left there could be swapped between the push and the GPU. In
        `control` nobody but the platform has a role at all, which is what
        makes "we ran the commit you pushed" a fact rather than a hope.
        """
        self._container(CONTROL).upload_blob(f"{CODE}/{job_id}.tar.gz", source, overwrite=True)

    def code_url(self, job_id: str, hours: int = 6) -> str:
        """A read token for one blob -- this job's tree -- and nothing else."""
        return self._read_url(CONTROL, f"{CODE}/{job_id}.tar.gz", minutes=hours * 60)

    def _read_url(self, container: str, blob: str, minutes: int = 30) -> str:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        now = datetime.now(UTC)
        # A few minutes of backdating: a token minted now is otherwise
        # occasionally not yet valid when the other end presents it.
        start, expiry = now - timedelta(minutes=5), now + timedelta(minutes=minutes)
        token = generate_blob_sas(
            account_name=self.account,
            container_name=container,
            blob_name=blob,
            user_delegation_key=self.service.get_user_delegation_key(start, expiry),
            permission=BlobSasPermissions(read=True),
            start=start,
            expiry=expiry,
        )
        return f"{self._container(container).get_blob_client(blob).url}?{token}"

    def discard_source(self, submission: Submission) -> None:
        """Drop the tarball once its build is over, win or lose.

        Nothing else ever deletes it: `take` only removes the request that
        points at it. Left alone, every submission any team ever makes stays in
        storage for the month, and the source of a run nobody will repeat is
        not worth keeping.
        """
        with contextlib.suppress(Exception):
            self._container(submission.team).delete_blob(f"_submit/{submission.source}")

    def cancellations(self) -> list[tuple[str, str]]:
        found = []
        for name in self._team_containers():
            client = self._container(name)
            for blob in client.list_blobs(name_starts_with="_cancel/", include=["metadata"]):
                if self._is_dir(blob):
                    continue
                found.append((name, blob.name.split("/", 1)[1]))
        return found

    def clear_cancel(self, team: str, job_id: str) -> None:
        # Already gone is the desired state, so a missing blob is not an error.
        with contextlib.suppress(Exception):
            self._container(team).delete_blob(f"_cancel/{job_id}")

    def jobs(self) -> list[Job]:
        """Every job of the event. The audit trail, and nothing a tick reads.

        Deliberately not what the scheduler calls: this downloads one blob per
        job, which by the end of a four-week event is thousands of round trips
        a minute. `active()` is the one on the hot path.
        """
        return self._read_prefix("jobs/")

    def active(self) -> list[Job]:
        """The jobs still in flight. Bounded by what is queued, not by history."""
        return self._read_prefix(f"{ACTIVE}/")

    def _read_prefix(self, prefix: str) -> list[Job]:
        container = self._container(CONTROL)
        jobs = []
        for blob in container.list_blobs(name_starts_with=prefix, include=["metadata"]):
            if self._is_dir(blob):
                continue
            jobs.append(job_from_dict(json.loads(container.download_blob(blob.name).readall())))
        return sorted(jobs, key=lambda j: j.submitted_at)

    def usage(self) -> dict[str, float]:
        """GPU seconds each team has already spent on jobs that are over.

        One blob, read once a tick. The alternative -- adding up every finished
        job -- is the same answer for a thousand times the work, recomputed
        every minute for four weeks.
        """
        return dict(self._usage_doc()["teams"])

    def _usage_doc(self) -> dict[str, Any]:
        if self._usage_cache is None:
            try:
                raw = self._container(CONTROL).download_blob(USAGE).readall()
                self._usage_cache = json.loads(raw)
            except Exception:  # noqa: BLE001 - absent on the first tick of the event
                self._usage_cache = {"teams": {}, "counted": []}
        return self._usage_cache

    def _bank(self, job: Job) -> None:
        doc = self._usage_doc()
        if job.job_id in doc["counted"]:
            return
        doc["teams"][job.team] = doc["teams"].get(job.team, 0.0) + job.gpu_seconds(
            job.finished_at or job.submitted_at
        )
        doc["counted"] = (doc["counted"] + [job.job_id])[-COUNTED_KEPT:]
        self._container(CONTROL).upload_blob(USAGE, json.dumps(doc, indent=2), overwrite=True)

    def put(self, job: Job) -> None:
        terminal = job.state in TERMINAL_STATES
        # Banked before the record says terminal, and idempotently, so that a
        # dispatcher dying between the two re-finishes the job next tick and
        # charges it once rather than twice or not at all.
        if terminal:
            self._bank(job)
        body = json.dumps(job_to_dict(job), indent=2)
        control = self._container(CONTROL)
        control.upload_blob(f"jobs/{job.job_id}.json", body, overwrite=True)
        self._container(job.team).upload_blob(f"jobs/{job.job_id}.json", body, overwrite=True)
        if terminal:
            with contextlib.suppress(Exception):
                control.delete_blob(f"{ACTIVE}/{job.job_id}.json")
        else:
            control.upload_blob(f"{ACTIVE}/{job.job_id}.json", body, overwrite=True)

    @contextlib.contextmanager
    def lock(self, seconds: int = 60) -> Any:
        """Hold the dispatcher lease, or yield False and let this tick pass.

        Container Apps starts a scheduled job on the minute whether or not the
        previous one has finished, and `parallelism` bounds replicas inside one
        execution, not executions. So two dispatchers overlap whenever a tick
        runs long -- and two dispatchers both ingest the same submission, both
        write a job record for it, and one of them then fails to claim the
        source, leaving a job that is BUILDING and will never build.

        A blob lease rather than a flag, because a lease expires on its own:
        a dispatcher killed mid-tick blocks the next one for at most `seconds`,
        where a flag would block it until somebody noticed.
        """
        import threading

        blob = self._container(CONTROL).get_blob_client(LOCK)
        with contextlib.suppress(Exception):
            blob.upload_blob(b"", overwrite=False)
        try:
            lease = blob.acquire_lease(lease_duration=seconds)
        except Exception:  # noqa: BLE001 - held by another tick, which is the point
            yield False
            return

        # Renewed rather than taken as an infinite lease: an infinite one
        # outlives the process that took it, and a dispatcher that is OOM-killed
        # during a build would then lock the event out until somebody broke it
        # by hand.
        stop = threading.Event()

        def renew() -> None:
            while not stop.wait(seconds / 3):
                try:
                    lease.renew()
                except Exception:  # noqa: BLE001 - nothing useful to do here
                    return

        threading.Thread(target=renew, daemon=True).start()
        try:
            yield True
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                lease.release()

    def reindex(self) -> int:
        """Build `active/` and `usage.json` from `jobs/` once, for an account
        that predates them. Returns how many jobs it had to read.

        Called only when `usage.json` is missing, so the expensive read of the
        whole history happens once in the life of the account rather than once
        a minute.
        """
        control = self._container(CONTROL)
        with contextlib.suppress(Exception):
            control.download_blob(USAGE).readall()
            return 0

        doc: dict[str, Any] = {"teams": {}, "counted": []}
        jobs = self.jobs()
        for job in jobs:
            body = json.dumps(job_to_dict(job), indent=2)
            if job.state in TERMINAL_STATES:
                doc["teams"][job.team] = doc["teams"].get(job.team, 0.0) + job.gpu_seconds(
                    job.finished_at or job.submitted_at
                )
                doc["counted"].append(job.job_id)
            else:
                control.upload_blob(f"{ACTIVE}/{job.job_id}.json", body, overwrite=True)
        doc["counted"] = doc["counted"][-COUNTED_KEPT:]
        control.upload_blob(USAGE, json.dumps(doc, indent=2), overwrite=True)
        self._usage_cache = doc
        return len(jobs)
