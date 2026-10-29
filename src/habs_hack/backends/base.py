"""What a compute backend has to provide.

Kept to four methods so the Azure Batch and Kubernetes implementations can be
written and swapped without touching the scheduler, the CLI or the student
contract. That decision is still open; this interface is what keeps it open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass
class BuildResult:
    """A built image, and the log of building it.

    The log comes back on success as well as failure. It is what the `build`
    job of a team's workflow has to show for itself -- without it that job is a
    spinner followed by a green tick, and a student waiting ninety seconds for
    pip to resolve has no way to know that is what is happening.
    """

    image: str
    log: str = ""
    #: True when the image for these requirements already existed and nothing
    #: was built. The dispatcher reports the two differently, because "reused"
    #: taking four seconds is the system working and "built" taking four
    #: seconds would be the system lying.
    reused: bool = False


@dataclass
class RunResult:
    exit_code: int
    #: The WHOLE log, never a tail. A truncated error is the most expensive
    #: thing we can hand a student: it turns a self-service failure into a
    #: mentor conversation, and with 35 teams that does not scale.
    log: str
    outputs: list[Path]


class Backend(Protocol):
    def ensure_image(self, requirements: str) -> BuildResult:
        """The image for these dependencies, built only if it is not there yet.

        Keyed on the requirements alone, not on the submission: two teams that
        asked for the same packages get the same image, and a push that changes
        only a team's code builds nothing. Their code is not in the image -- it
        travels to the task, as *code_url* below.
        """

    def start(self, job_id: str, team: str, image: str, code_url: str) -> None:
        """Begin running an admitted job. Returns as soon as it is launched.

        *code_url* is a read token for this job's tree and that blob only. It
        is what the entrypoint unpacks into /job before importing process.py.
        """

    def started(self, job_id: str) -> datetime | None:
        """When the job actually reached a machine, or None if it has not yet.

        Separate from `poll` because the gap between "we admitted it" and "it is
        on a GPU" is real -- a spot pool can take minutes to hand over a node --
        and a team must not be billed GPU time for waiting through it.
        """

    def live_log(self, job_id: str) -> dict[str, str]:
        """stdout and stderr of a running job, keyed by file name.

        The platform writes these to the team's container on every tick. Without
        it a student following a long job sees nothing until it ends.
        """

    def poll(self, job_id: str) -> RunResult | None:
        """Result if the job has finished, None while it is still running."""

    def cancel(self, job_id: str) -> None: ...
