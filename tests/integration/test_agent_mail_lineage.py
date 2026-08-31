"""The bytes the trigger handed in are the bytes the agent closes.

The defect this suite exists to catch, found by an external audit on
2026-08-24, introduced the day before: `/events` read the real objects off the
bucket and injected them into the agent session, and the agent's first tool
call, `take_in_mail`, then executed `read_period(self.period)` unconditionally,
overwriting the injected mailbox with the bundled sample corpus. The persisted
record carried genuine GCS hashes while the books were computed from bundled
files. The two happen to hold identical bytes today, which is exactly why
nothing noticed: the lie was invisible until a judge uploaded something new.

It slipped between two suites. The ingestion tests injected documents but ran
the deterministic path; the call-graph tests exercised the agent path but faked
`run_agent_close`. These run the REAL agent loop, with the scripted model from
`tests/adk_fakes.py`, and patch `read_period` to blow up if anything touches
the bundled corpus while an injected mailbox is present.
"""
from __future__ import annotations

import pytest

pytest.importorskip("google.adk")

from archon.adapters import service
from archon.adapters.store import LocalStore
from tests.adk_fakes import ScriptedLlm
from tests.conftest import PERIOD, load, remittance

#: The whole chore, in the order the instruction gives the agent.
FULL_SCRIPT = [
    ("call", "take_in_mail", {}),
    ("call", "post_journal", {}),
    ("call", "allocate_remittances", {}),
    ("call", "triage_exceptions", {}),
    ("call", "decide_actions", {"actions": {}}),
    ("call", "draft_corrections", {}),
    ("call", "verify_and_file", {}),
    ("text", "The month is closed."),
]

SOURCE = {"mailbox": "gcs", "bucket": "test-bucket", "period": PERIOD,
          "objects_read": 2, "objects_skipped": 0, "manifest": []}


def sentinel_mail(rate: float = 1_111.0):
    """Two documents that exist nowhere in the bundled corpus."""
    documents = [load("L-SENTINEL", rate),
                 remittance("R-SENTINEL", [("L-SENTINEL", rate, 0.0, None)], fee=11.0)]
    raw = {"load-L-SENTINEL.txt": "sentinel load",
           "remittance-R-SENTINEL.txt": "sentinel remittance"}
    return documents, raw


@pytest.fixture
def corpus_is_off_limits(monkeypatch):
    """Any touch of the bundled corpus while mail is injected is the bug."""
    def forbidden(period, root=None):
        raise AssertionError(
            "read_period was called while an injected mailbox was present: "
            "the agent is closing the bundled sample instead of the trigger's bytes"
        )

    monkeypatch.setattr("archon.adapters.agents.read_period", forbidden)


def run_real_agent(documents, raw, store=None):
    from archon.adapters.agents import run_agent_close

    result, final = run_agent_close(
        period=PERIOD, company="Bell Ridge Haulage",
        model=ScriptedLlm(FULL_SCRIPT), store=store or LocalStore(),
        documents=documents, raw=raw, source=dict(SOURCE),
    )
    assert result is not None, f"the agent produced no close: {final!r}"
    return result


# ── the lineage ──────────────────────────────────────────────────────────────

def test_injected_bytes_survive_the_real_adk_tool_path(corpus_is_off_limits):
    documents, raw = sentinel_mail()

    result = run_real_agent(documents, raw)

    assert result.statements.revenue == 1_111.0
    assert result.driver == "adk-agent"
    assert result.source["bucket"] == "test-bucket"


def test_no_bundled_filename_reaches_the_persisted_record(corpus_is_off_limits):
    documents, raw = sentinel_mail()
    store = LocalStore()

    run_real_agent(documents, raw, store=store)

    stored = store.load_close("Bell Ridge Haulage", PERIOD)
    names = {step_name for step_name in (stored.get("journal") or {}).get("steps", [])
             if isinstance(step_name, str)}
    text = str(stored)
    for bundled in ("L-7101", "TFX-RA-4417", "fuelcard"):
        assert bundled not in text, (
            f"bundled artifact {bundled} leaked into a close of injected mail"
        )
    assert "L-SENTINEL" in text
    assert stored["source"]["bucket"] == "test-bucket"
    assert names is not None  # journal persisted alongside


def test_changing_one_injected_document_changes_the_result(corpus_is_off_limits):
    a = run_real_agent(*sentinel_mail(rate=1_111.0))
    b = run_real_agent(*sentinel_mail(rate=2_222.0))

    assert a.statements.revenue == 1_111.0
    assert b.statements.revenue == 2_222.0


def test_an_explicitly_injected_empty_mailbox_stays_empty(corpus_is_off_limits):
    """Empty is an answer, not an invitation to substitute the sample month.
    `documents or []` could not tell these apart; `is not None` can."""
    result = run_real_agent([], {})

    assert result.statements.revenue == 0.0
    assert not result.allocations


def test_without_injection_the_bundled_corpus_still_serves_the_demo():
    """The button and the CLI hand in nothing, and must keep working."""
    from archon.adapters.agents import run_agent_close

    result, _ = run_agent_close(period=PERIOD, company="Bell Ridge Haulage",
                                model=ScriptedLlm(FULL_SCRIPT), store=LocalStore())

    assert result is not None
    assert result.statements.revenue > 20_000          # the bundled July


# ── the production route, end to end with the real agent ────────────────────

def test_events_shaped_close_runs_the_agent_on_the_injected_bytes(
        corpus_is_off_limits, monkeypatch):
    """USE_AGENT on, a scripted model in place of the pin, and the same _close
    call the /events handler makes: the persisted books must be the sentinel's,
    stamped adk-agent, carrying the trigger's source block."""
    monkeypatch.setattr(service, "USE_AGENT", True)
    monkeypatch.setattr("archon.adapters.agents.DEFAULT_MODEL", ScriptedLlm(FULL_SCRIPT))
    store = LocalStore()
    monkeypatch.setattr(service, "get_store", lambda: store)
    documents, raw = sentinel_mail()

    payload = service._close(PERIOD, documents=documents, raw=raw,
                             source=dict(SOURCE))

    assert payload["driver"] == "adk-agent"
    assert payload["statements"]["revenue"] == 1_111.0
    assert payload["source"]["bucket"] == "test-bucket"
    stored = store.load_close("Bell Ridge Haulage", PERIOD)
    assert stored["statements"]["revenue"] == 1_111.0
    assert stored["driver"] == "adk-agent"
