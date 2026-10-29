"""Runs a team's process() on this machine, with the same contract as a GPU job.

Two uses: a student checks their script before spending one of 35 teams' GPU
turns, and we develop the platform without an Azure backend existing yet. The
mounts are directories and the "image" is the current interpreter, but the
contract a student codes against -- data_dir, out_dir, HABS_SCRATCH, full logs,
an exit code -- is identical.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from habs_hack.backends.base import RunResult

#: Runs the student's process() in a child interpreter rather than importing it,
#: so a script that calls sys.exit, segfaults or leaks memory fails the job
#: instead of taking the CLI down with it -- which is how it behaves on a GPU.
_RUNNER = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, {source!r})
    from process import process

    process(Path({data!r}), Path({out!r}))
    """
)


def run_local(source: Path, data_dir: Path, out_dir: Path, *, team: str = "local",
              job_id: str = "local") -> RunResult:
    """Execute *source*/process.py against *data_dir*, writing into *out_dir*."""
    script = source / "process.py"
    if not script.is_file():
        raise FileNotFoundError(f"no process.py in {source}")
    if not data_dir.is_dir():
        raise FileNotFoundError(f"data directory not found: {data_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="habs-scratch-") as scratch:
        env = {
            **os.environ,
            "HABS_TEAM": team,
            "HABS_JOB_ID": job_id,
            "HABS_DATA_DIR": str(data_dir),
            "HABS_OUT_DIR": str(out_dir),
            "HABS_SCRATCH": scratch,
            "PYTHONUNBUFFERED": "1",
        }
        proc = subprocess.run(
            [sys.executable, "-c",
             _RUNNER.format(source=str(source), data=str(data_dir), out=str(out_dir))],
            check=False,  # a student's script failing is data, not an exception
            capture_output=True,
            text=True,
            env=env,
        )

    # stdout and stderr interleaved and kept whole, in that order, because a
    # student debugging a crash needs the print() lines that led to it.
    log = proc.stdout + proc.stderr
    outputs = sorted(p for p in out_dir.rglob("*") if p.is_file())
    return RunResult(exit_code=proc.returncode, log=log, outputs=outputs)
