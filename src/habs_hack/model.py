"""Job and limit types shared by the scheduler, the CLI and every backend.

Deliberately free of Azure, Kubernetes and HTTP: the scheduler is the one piece
of genuinely new logic in this platform, so it is kept pure and testable without
a cluster. A backend turns an admitted job into a running container; nothing in
here knows how.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class JobState(str, Enum):
    """The six states a team sees in `habs status`, and the two internal ones."""

    QUEUED = "queued"          # waiting its turn behind other teams
    BUILDING = "building"      # their image is being built from requirements.txt
    ADMITTED = "admitted"      # past the queue, waiting for a machine
    RUNNING = "running"        # on a GPU
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED})

#: States that occupy a GPU slot. ADMITTED counts: the slot is already promised,
#: and double-counting it is what lets a cluster oversubscribe between ticks.
OCCUPYING_STATES = frozenset({JobState.ADMITTED, JobState.RUNNING})


@dataclass
class Job:
    job_id: str
    team: str
    image: str
    submitted_at: datetime
    state: JobState = JobState.QUEUED
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: Why the job is not running, in words a student can act on. The CLI prints
    #: this verbatim: "queued" with no reason is what turns 35 teams into a
    #: support queue.
    wait_reason: str = ""
    queue_position: int = 0
    exit_code: int | None = None
    #: When the job last changed state. The reaper needs it: "has this been
    #: BUILDING for forty minutes" cannot be answered from submitted_at, which
    #: is when the *team* pushed -- a dispatcher that was down for an hour would
    #: otherwise declare every submission it then picks up already stuck.
    state_since: datetime | None = None

    def __post_init__(self) -> None:
        if self.state_since is None:
            self.state_since = self.submitted_at

    def move(self, state: JobState, now: datetime) -> None:
        """Change state and remember when. The only way state should be set."""
        self.state = state
        self.state_since = now

    def in_state_for(self, now: datetime) -> float:
        return max(0.0, (now - (self.state_since or self.submitted_at)).total_seconds())

    def gpu_seconds(self, now: datetime) -> float:
        """GPU time consumed so far. Counts a running job from its start."""
        if self.started_at is None:
            return 0.0
        end = self.finished_at or now
        return max(0.0, (end - self.started_at).total_seconds())


@dataclass(frozen=True)
class Limits:
    """What the platform refuses to let a team do. Policy, not physics.

    An earlier version of this docstring said these came from a design doc.
    There is no design doc. Three of them were picked on the first day of the
    repository by whoever was typing, which is a bad way to decide how much of
    a shared GPU pool a team may have, so the provenance of each is written
    down here and the guesses are named as guesses.

    Each stops a specific failure: a team filling the pool, a retry loop
    building an unbounded backlog, a team burning the month in week one, an
    infinite loop holding a card until December. That the failures are real
    does not make the numbers right.
    """

    #: Derived, not configured: the dispatcher asks the pool how many tasks it
    #: can run (`BatchBackend.pool_capacity`). This default is only the
    #: fallback for when the pool cannot be read.
    gpu_slots: int = 8
    #: Reasoned: with four cards and 35 teams, more than one each would let two
    #: teams hold half the pool. The only number here that follows from the
    #: shape of the event rather than from taste.
    max_running_per_team: int = 1
    #: A GUESS. It bounds a retry loop, and any number does that; ten was
    #: picked on day one and nothing has tested whether it is the right ten.
    max_queued_per_team: int = 10
    #: A GUESS, and the one that most deserves a decision rather than a
    #: default, because students are shown it and plan against it.
    #:
    #: For scale: four cards over the 29 days of the event is about 2,800
    #: GPU-hours, so 35 teams at 40 sums to half of what exists. It is
    #: therefore not rationing a scarce thing -- it is a backstop against one
    #: team running long jobs all month. A team hitting it would have to run
    #: forty full-length hours; a team running jobs of minutes never will.
    gpu_hours_per_team: float = 40.0
    #: One hour, not four. Throughput is nodes x (24 / this), and at four
    #: hours four nodes give 35 teams fewer than one job each per day.
    max_runtime: timedelta = timedelta(hours=1)
    #: How long a job may sit ADMITTED -- past the queue, waiting for a machine
    #: -- before we call it stuck and give the slot back. ADMITTED occupies a
    #: slot, so without this one task Batch never schedules costs the whole
    #: event a GPU. Generous on purpose: autoscale evaluates every five minutes
    #: and an H100 node takes ten more to boot, so a healthy cold start can be
    #: twenty minutes.
    max_admitted: timedelta = timedelta(minutes=45)
    #: How long a job may stay BUILDING. The registry's own build timeout is
    #: thirty minutes; past this the build is not slow, the dispatcher that was
    #: waiting for it died, and nothing else will ever finish the job.
    max_building: timedelta = timedelta(minutes=40)
    #: How much one `habs get` may pull onto a laptop. Results live in Azure
    #: and are readable with the team's link; `habs get` is a convenience, and
    #: a convenience that quietly moves gigabytes out of the region is one
    #: nobody meant to use. Over this it says so and points at the link.
    max_download_mb: float = 25.0

    def gpu_seconds_per_team(self) -> float:
        return self.gpu_hours_per_team * 3600.0


@dataclass
class TeamUsage:
    """What a team is using now and has used so far, over the whole event."""

    team: str
    running: int = 0
    queued: int = 0
    gpu_seconds_used: float = 0.0


@dataclass
class Admission:
    """One scheduling tick's decision.

    *start* is ordered: the backend starts them in this order, so a short slot
    shortage degrades predictably rather than at random.
    """

    start: list[str] = field(default_factory=list)
    #: job_id -> the sentence the CLI shows for a job that stays queued.
    waiting: dict[str, str] = field(default_factory=dict)
