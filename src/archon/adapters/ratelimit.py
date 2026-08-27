"""What a stranger is allowed to spend.

The anonymous button on the judge's page runs a real close. With the agent
switched on that is a thinking-model conversation of seven tool calls, and the
page also fetched a close on every load. Nothing bounded either one: no limit
per caller, no ceiling on how many could run at once, no budget for the day. A
single visitor holding down refresh was an unbounded bill, and the URL is
public and printed in a submission.

This is deliberately small and in-process. It is not a distributed rate
limiter and does not pretend to be: Cloud Run runs up to four instances of this
service, so the real ceiling is four times what is configured here. That is a
bound, which is the thing that did not exist before, and it is stated rather
than implied. A shared counter would need Firestore on the request path for
every anonymous page load, which buys accuracy nobody is asking for at the cost
of the latency every judge would feel.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque


class Bounded:
    """A sliding window per caller, plus a ceiling on concurrent work."""

    def __init__(self, per_caller: int, window_seconds: float, concurrent: int) -> None:
        self.per_caller = per_caller
        self.window = window_seconds
        self._seen: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(concurrent)
        self.concurrent = concurrent

    def check(self, caller: str, now: float | None = None) -> tuple[bool, str]:
        """Has this caller room for one more? Records the attempt if so."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._seen.setdefault(caller, deque())
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.per_caller:
                wait = int(self.window - (now - hits[0])) + 1
                return False, (
                    f"this demo allows {self.per_caller} fresh closes every "
                    f"{int(self.window / 60)} minutes from one address, and a close "
                    f"runs a thinking model. Try again in about {wait}s, or read the "
                    f"saved close, which costs nothing and is the same books."
                )
            hits.append(now)
            # Callers that have gone quiet are dropped so the dict cannot grow
            # without limit on a public URL.
            if len(self._seen) > 4096:
                for key in [k for k, v in self._seen.items() if not v or now - v[-1] > self.window]:
                    self._seen.pop(key, None)
        return True, ""

    def reset(self) -> None:
        """Forget every caller. For tests, and for nothing else.

        The limiter is process-wide by design, which means a test that presses
        the button leaves its presses behind for the next one. Without this,
        the suite passes or fails on the order pytest happens to choose.
        """
        with self._lock:
            self._seen.clear()

    def slot(self):
        """A context manager holding one of the concurrent slots, or refusing."""
        return _Slot(self._slots)


class _Slot:
    def __init__(self, semaphore: threading.BoundedSemaphore) -> None:
        self._semaphore = semaphore
        self.acquired = False

    def __enter__(self) -> _Slot:
        self.acquired = self._semaphore.acquire(blocking=False)
        return self

    def __exit__(self, *exc) -> None:
        if self.acquired:
            self._semaphore.release()


def _setting(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


#: Three closes per address per ten minutes, two at a time. Chosen so a judge
#: can press the button, watch it, press it again to check it was not a
#: recording, and still have one spare, while a script gets almost nothing.
PUBLIC_CLOSES = Bounded(
    per_caller=_setting("ARCHON_PUBLIC_CLOSES_PER_WINDOW", 3),
    window_seconds=_setting("ARCHON_PUBLIC_WINDOW_SECONDS", 600),
    concurrent=_setting("ARCHON_PUBLIC_CONCURRENT_CLOSES", 2),
)
