"""Making a CI run readable: collapsible groups, annotations, the summary card.

Every function here is a no-op when `GITHUB_ACTIONS` is unset, so the CLI has
one code path rather than two. A student running `habs watch` on a laptop sees
plain headings; the same call in Actions produces a foldable log section and a
rendered table on the run page.

None of this is decoration. With 35 teams the Actions page is the entire support
surface -- a student who cannot tell "your requirements.txt is wrong" from "your
code crashed" from "the queue is long" becomes a mentor conversation, and the
whole point of the split build/run workflow is that the two failures land on
different lines of the UI.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from datetime import datetime


def on() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


@contextlib.contextmanager
def group(title: str) -> Iterator[None]:
    """A foldable section in the Actions log; a plain heading anywhere else.

    Always closed, including when the body raises -- an unclosed `::group::`
    swallows the rest of the run's log into the fold, which is exactly the
    output somebody needs when something has just raised.
    """
    print(f"::group::{title}" if on() else f"\n--- {title} ---", flush=True)
    try:
        yield
    finally:
        if on():
            print("::endgroup::", flush=True)


def notice(message: str) -> None:
    print(f"::notice::{message}" if on() else message, flush=True)


def error(message: str) -> None:
    """An annotation at the top of the run page, not just a line in the log."""
    print(f"::error::{message}" if on() else f"error: {message}", flush=True)


def output(name: str, value: str) -> None:
    """Hand a value to a later job. This is how `run` learns the job id."""
    _append("GITHUB_OUTPUT", f"{name}={value}\n")


def summary(markdown: str) -> None:
    """The card rendered on the run page, above the logs.

    Where a student actually looks: it holds the result, the GPU time it cost
    and what the job wrote, so the common case needs no log reading at all.
    """
    _append("GITHUB_STEP_SUMMARY", markdown.rstrip() + "\n\n")


def table(rows: list[tuple[str, str]]) -> str:
    """Two-column markdown, header-less -- a fact sheet rather than a dataset."""
    body = "\n".join(f"| {name} | {value} |" for name, value in rows)
    return f"| | |\n| --- | --- |\n{body}"


def _append(variable: str, text: str) -> None:
    path = os.environ.get(variable)
    if not path:
        return
    # Never fatal: a summary that cannot be written must not fail a job whose
    # actual work succeeded.
    with contextlib.suppress(OSError):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def duration(seconds: float) -> str:
    """`2m 04s` -- short enough for a table cell, exact enough to compare runs."""
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m {total % 60:02d}s"


def span(start: datetime | None, end: datetime | None) -> str:
    """How far apart two timestamps are, or `--` when either never happened."""
    if start is None or end is None:
        return "--"
    return duration((end - start).total_seconds())
