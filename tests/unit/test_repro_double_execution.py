"""Reproduction: does the ADK path run the whole close twice?

Audit claim (section 2): on the agent path `post_journal` calls `_ensure_run`,
which runs the entire deterministic `run_close`; `decide_actions` then throws
that result away (`self.result = None`) and calls `_ensure_run` again. If that
is true, every side effect of a close happens twice per agent run: the
Firestore writes and the owner's month-end letter.

These tests do not reason about the code. They count. A store that tallies
every write and a deliverer that tallies every delivery are injected, the real
ADK function-calling loop is driven by a scripted model (no key, no network,
the same `ScriptedLlm` mechanism `tests/integration/test_agents.py` uses), and
the counters are asserted.

`test_the_deterministic_close_writes_and_delivers_once` is the control: it pins
what one close costs, so a number in the agent test can be read as a multiple
rather than as a bare figure.
"""
from __future__ import annotations

from collections import Counter

import pytest

from archon.adapters import delivery as delivery_mod
from archon.adapters.delivery import Receipt
from archon.adapters.store import LocalStore
from archon.runtime.close import run_close
from archon.runtime.journal import FixedClock
from archon.runtime.mailbox import read_period
from tests.conftest import PERIOD

COMPANY = "Bell Ridge Haulage"


class CountingStore:
    """A real `LocalStore` that keeps a tally of every write through it.

    Wrapping rather than mocking: the close gets genuine persistence with
    genuine return values, and the count is a by-product rather than the
    behaviour under test.
    """

    backend = "memory"

    def __init__(self) -> None:
        self._inner = LocalStore()
        self.calls: Counter = Counter()
        self.close_keys: list[str] = []

    def put_document(self, name: str, content: str) -> str:
        self.calls["put_document"] += 1
        return self._inner.put_document(name, content)

    def save_run(self, run: dict) -> str:
        self.calls["save_run"] += 1
        return self._inner.save_run(run)

    def save_close(self, company, period: str, payload: dict) -> str:
        self.calls["save_close"] += 1
        self.close_keys.append(period)
        return self._inner.save_close(company, period, payload)

    def save_drafts(self, run_id: str, drafts: list, company=None,
                    period=None) -> list[str]:
        # Exactly one call per completed `run_close`, so this is the cleanest
        # available count of "how many times did the whole chore execute".
        self.calls["save_drafts"] += 1
        return self._inner.save_drafts(run_id, drafts)

    def load_close(self, company, period: str):
        return self._inner.load_close(company, period)

    def load_run(self, run_id: str):
        return self._inner.load_run(run_id)


class CountingDeliverer:
    """Stands in for whatever `get_deliverer()` would have returned.

    In the deployed shape with `ARCHON_SMTP_HOST` set this position is held by
    `SmtpDelivery`, so one entry in `self.sent` is one email leaving the
    machine and reaching the owner's inbox.
    """

    channel = "counting"

    def __init__(self) -> None:
        self.sent: list = []

    def deliver(self, digest) -> Receipt:
        self.sent.append(digest)
        return Receipt(channel=self.channel, delivered=True,
                       detail=f"delivered to {digest.recipient}",
                       recipient=digest.recipient)


class CountingNarrator:
    """A narrator that counts how often the reporting pipeline would have run.

    Returns "" so `run_close` keeps its deterministic summary and the books are
    unaffected. In production this position is held by `gemini_narrator()`,
    which is a three-stage `SequentialAgent`: one call here is three Gemini
    calls of real spend.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, facts: str) -> str:
        self.calls.append(facts)
        return ""


def _observed(store: CountingStore, deliverer: CountingDeliverer,
              narrator: CountingNarrator) -> dict:
    """Every counter in one dict, so a failure prints all of them at once."""
    return {
        "whole-close executions": store.calls["save_drafts"],
        "owner letters delivered": len(deliverer.sent),
        "store.save_close writes": store.calls["save_close"],
        "store.save_run writes": store.calls["save_run"],
        "narrator (Gemini pipeline) invocations": len(narrator.calls),
    }


#: What exactly one close costs. Pinned by the control test below.
ONE_CLOSE = {
    "whole-close executions": 1,
    "owner letters delivered": 1,
    # `run_close` writes the close, then the digest, then rewrites the close
    # with the complete trail: three `save_close` calls, two distinct keys.
    "store.save_close writes": 3,
    "store.save_run writes": 2,
    "narrator (Gemini pipeline) invocations": 1,
}


def test_the_deterministic_close_writes_and_delivers_once(monkeypatch):
    """The control. One `run_close` costs one of everything."""
    store, deliverer, narrator = CountingStore(), CountingDeliverer(), CountingNarrator()
    monkeypatch.setattr(delivery_mod, "get_deliverer", lambda: deliverer)
    documents, raw = read_period(PERIOD)

    result = run_close(period=PERIOD, documents=documents, company=COMPANY,
                       store=store, clock=FixedClock(), raw_texts=raw,
                       narrator=narrator)

    assert result.outcome == "closed"
    assert _observed(store, deliverer, narrator) == ONE_CLOSE


def test_the_adk_agent_close_runs_the_whole_chore_only_once(monkeypatch):
    """The claim, driven through the real ADK function-calling loop.

    The scripted model calls the seven tools in the order `CLOSE_INSTRUCTION`
    tells the agent to call them. That is the ordinary, well-behaved run: no
    retry, no confusion, no out-of-order call. One close was asked for, so one
    close is what the counters should show.
    """
    pytest.importorskip("google.adk")
    from archon.adapters.agents import run_agent_close
    from tests.adk_fakes import ScriptedLlm

    store, deliverer, narrator = CountingStore(), CountingDeliverer(), CountingNarrator()
    monkeypatch.setattr(delivery_mod, "get_deliverer", lambda: deliverer)

    model = ScriptedLlm([
        ("call", "take_in_mail", {}),
        ("call", "post_journal", {}),
        ("call", "allocate_remittances", {}),
        ("call", "triage_exceptions", {}),
        ("call", "decide_actions", {"actions": {"0": "draft", "1": "escalate"}}),
        ("call", "draft_corrections", {}),
        ("call", "verify_and_file", {}),
        ("text", "July is closed."),
    ])

    result, final = run_agent_close(period=PERIOD, company=COMPANY, model=model,
                                    store=store, clock=FixedClock(),
                                    narrator=narrator)

    # The close itself succeeded; this test is about how many times it happened.
    assert result is not None
    assert result.outcome == "closed"
    assert final

    assert _observed(store, deliverer, narrator) == ONE_CLOSE


def test_the_owner_is_not_written_to_twice_for_one_month(monkeypatch):
    """The consequence, stated on its own.

    Every delivery here is the same month-end letter to the same address. With
    `ARCHON_SMTP_HOST` configured this is `SmtpDelivery`, so more than one
    entry is more than one email in the owner's inbox for one close.
    """
    pytest.importorskip("google.adk")
    from archon.adapters.agents import run_agent_close
    from tests.adk_fakes import ScriptedLlm

    store, deliverer, narrator = CountingStore(), CountingDeliverer(), CountingNarrator()
    monkeypatch.setattr(delivery_mod, "get_deliverer", lambda: deliverer)

    model = ScriptedLlm([
        ("call", "take_in_mail", {}),
        ("call", "post_journal", {}),
        ("call", "triage_exceptions", {}),
        ("call", "decide_actions", {"actions": {"0": "draft"}}),
        ("call", "verify_and_file", {}),
        ("text", "done"),
    ])
    run_agent_close(period=PERIOD, company=COMPANY, model=model, store=store,
                    clock=FixedClock(), narrator=narrator)

    subjects = [d.subject for d in deliverer.sent]
    assert subjects == subjects[:1], (
        f"the owner was written to {len(subjects)} times for one close: {subjects}"
    )


def test_decide_actions_after_post_journal_does_not_re_execute_the_close():
    """The mechanism, at the tool surface, with no ADK involved.

    `post_journal` is step 2 of the instructed order and `decide_actions` is
    step 5, so this is the sequence the agent is told to produce. Isolating it
    from the runner shows the doubling is in `CloseSession`, not in ADK.
    """
    from archon.adapters.agents import CloseSession

    store = CountingStore()
    session = CloseSession(period=PERIOD, company=COMPANY, store=store,
                           clock=FixedClock())

    session.take_in_mail()
    session.post_journal()          # _ensure_run: the whole close, run 1
    before = store.calls["save_drafts"]
    session.triage_exceptions()
    session.decide_actions({"0": "draft"})
    after = store.calls["save_drafts"]

    assert session.result is not None
    assert after - before == 0, (
        "decide_actions re-executed the whole close after post_journal had "
        f"already executed it ({after - before} extra full execution(s))"
    )
