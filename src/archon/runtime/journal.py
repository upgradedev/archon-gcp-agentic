"""The run journal: what the agent did while nobody was watching.

An unattended agent has an evidence problem. The owner was not there. By the
time they look, the work is finished and the only thing on offer is a report
asking to be believed. So Archon writes down every step as it takes it, with
what it acted on, what it produced, and how long it took, and that trail is
persisted alongside the books.

The trail is not logging. Logs are for whoever is debugging; this is a
first-class product surface. It is what the owner scrolls through on Monday
morning, what a judge watches replay in the browser, and what makes "the agent
closed the month" a claim you can check rather than one you accept.

Three properties are deliberate:

**Steps are appended as they complete, not assembled at the end.** A run that
crashes half way still has a journal up to the crash, which is exactly when a
trail is worth most.

**Every step carries a count of what it touched.** Not "reconciled bank" but
"reconciled bank: 14 lines in, 3 unmatched". A step that reports only that it
happened proves only that it was called.

**Timings come from a clock passed in.** The default is the real one; tests
pass a fake, so a golden-output assertion over a whole run is stable to the
byte. An orchestrator whose output changes every run cannot be regression
tested, and an agent that cannot be regression tested will drift.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime


@dataclass
class Step:
    """One completed step of a run."""

    index: int
    name: str
    title: str                       # one line, written for the owner
    detail: str                      # what it touched, with counts
    started_at: str
    duration_ms: int
    counts: dict = field(default_factory=dict)
    status: str = "ok"               # "ok" | "blocked" | "failed"

    def to_dict(self) -> dict:
        return asdict(self)


class Clock:
    """Wall clock, injectable so a run can be made byte-stable in tests."""

    def now_iso(self) -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    def monotonic_ms(self) -> int:
        return int(time.monotonic() * 1000)


class FixedClock(Clock):
    """A clock that does not move. Used by the golden-output test."""

    def __init__(self, at: str = "2026-08-01T00:00:00+00:00"):
        self._at = at

    def now_iso(self) -> str:
        return self._at

    def monotonic_ms(self) -> int:
        return 0


class RunJournal:
    """The append-only trail of one close.

    Use it as a context manager per step so a step that raises is still
    recorded, with the failure on it:

        with journal.step("allocate", "Split the remittance") as step:
            ...
            step.note("9 loads settled", loads=9)
    """

    def __init__(self, run_id: str, period: str, clock: Clock | None = None):
        self.run_id = run_id
        self.period = period
        self.clock = clock or Clock()
        self.started_at = self.clock.now_iso()
        self.steps: list[Step] = []
        self.finished_at: str | None = None
        self.outcome: str = "running"

    def step(self, name: str, title: str) -> _StepContext:
        return _StepContext(self, name, title)

    def _append(self, step: Step) -> None:
        self.steps.append(step)

    def finish(self, outcome: str) -> None:
        """Close the run. `outcome` is one of closed, blocked, failed."""
        self.outcome = outcome
        self.finished_at = self.clock.now_iso()

    @property
    def total_ms(self) -> int:
        return sum(s.duration_ms for s in self.steps)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "period": self.period,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome,
            "total_ms": self.total_ms,
            "steps": [s.to_dict() for s in self.steps],
        }

    def transcript(self) -> str:
        """The trail as plain text, for a terminal or a video caption track."""
        lines = [f"run {self.run_id} - closing {self.period}"]
        for step in self.steps:
            mark = {"ok": "+", "blocked": "!", "failed": "x"}.get(step.status, "?")
            lines.append(f" {mark} {step.index}. {step.title}")
            lines.append(f"     {step.detail}")
        lines.append(f" = {self.outcome} in {self.total_ms} ms")
        return "\n".join(lines)


class _StepContext:
    """Records one step, whether it succeeds or raises."""

    def __init__(self, journal: RunJournal, name: str, title: str):
        self._journal = journal
        self._name = name
        self._title = title
        self._detail = ""
        self._counts: dict = {}
        self._status = "ok"
        self._started_at = ""
        self._started_ms = 0

    def __enter__(self) -> _StepContext:
        self._started_at = self._journal.clock.now_iso()
        self._started_ms = self._journal.clock.monotonic_ms()
        return self

    def note(self, detail: str, **counts) -> None:
        """Say what this step touched. Counts land in the machine-readable trail."""
        self._detail = detail
        self._counts.update(counts)

    def block(self, detail: str, **counts) -> None:
        """The step ran but its result is not trustworthy, so the run is blocked."""
        self.note(detail, **counts)
        self._status = "blocked"

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self._status = "failed"
            self._detail = f"{exc_type.__name__}: {exc}"
        self._journal._append(
            Step(
                index=len(self._journal.steps) + 1,
                name=self._name,
                title=self._title,
                detail=self._detail,
                started_at=self._started_at,
                duration_ms=self._journal.clock.monotonic_ms() - self._started_ms,
                counts=dict(self._counts),
                status=self._status,
            )
        )
        return False  # never swallow; close.py decides what a failure means
