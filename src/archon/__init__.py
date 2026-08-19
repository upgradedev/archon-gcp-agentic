"""Archon GCP (Agentic) — an agent that closes a haulier's month unattended.

The public surface is deliberately narrow: `run_close` is the chore, and
everything else is the machinery it drives.

    from archon import run_close
    result = run_close(period="2026-07", documents=docs)

Nothing in this package imports Google ADK, Firestore or a web framework at
module level. Those live behind `agents.py`, `store.py` and `service.py` and
are imported lazily, so the deterministic engine and its whole test suite run
on the standard library with no key, no credential and no network.
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["run_close", "CloseResult", "PERIOD"]

#: The month the bundled corpus covers, and the period the demo closes.
PERIOD = "2026-07"


def __getattr__(name):  # pragma: no cover - thin lazy re-export
    if name in ("run_close", "CloseResult"):
        from .runtime import close

        return getattr(close, name)
    raise AttributeError(name)
