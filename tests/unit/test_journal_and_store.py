"""The run journal and the persistence seam."""
from __future__ import annotations

import pytest

from archon.journal import FixedClock, RunJournal, Step
from archon.models import Draft, DraftKind, ExceptionKind
from archon.store import LocalStore, _plain, close_key

# ── the run journal ──────────────────────────────────────────────────────────

def test_a_step_records_what_it_touched():
    run = RunJournal("r-1", "2026-07", clock=FixedClock())

    with run.step("post", "Post the journal") as step:
        step.note("26 entries posted", entries=26)

    assert run.steps[0].title == "Post the journal"
    assert run.steps[0].counts == {"entries": 26}
    assert run.steps[0].status == "ok"


def test_steps_are_numbered_in_the_order_they_completed():
    run = RunJournal("r-1", "2026-07", clock=FixedClock())
    for name in ("a", "b", "c"):
        with run.step(name, name) as step:
            step.note(name)

    assert [s.index for s in run.steps] == [1, 2, 3]
    assert [s.name for s in run.steps] == ["a", "b", "c"]


def test_a_blocked_step_is_recorded_as_blocked_not_as_success():
    run = RunJournal("r-1", "2026-07", clock=FixedClock())

    with run.step("verify", "Check the gates") as step:
        step.block("2 gates failed", failed=2)

    assert run.steps[0].status == "blocked"


def test_a_step_that_raises_is_still_recorded_and_the_error_propagates():
    """A run that dies half way still has a trail up to where it died, which is
    exactly when a trail is worth the most."""
    run = RunJournal("r-1", "2026-07", clock=FixedClock())

    with pytest.raises(ValueError), run.step("post", "Post the journal"):
        raise ValueError("the ledger exploded")

    assert run.steps[0].status == "failed"
    assert "the ledger exploded" in run.steps[0].detail


def test_a_fixed_clock_makes_a_whole_run_byte_stable():
    """An orchestrator whose output changes every run cannot be regression
    tested, and an agent that cannot be regression tested will drift."""
    def build():
        run = RunJournal("r-1", "2026-07", clock=FixedClock())
        with run.step("a", "Step A") as step:
            step.note("did a thing", n=1)
        run.finish("closed")
        return run.to_dict()

    assert build() == build()


def test_the_transcript_reads_as_a_trail_a_person_can_follow():
    run = RunJournal("r-1", "2026-07", clock=FixedClock())
    with run.step("a", "Take in the mail") as step:
        step.note("27 artifacts")
    with run.step("b", "Check the gates") as step:
        step.block("1 gate failed")
    run.finish("blocked")

    text = run.transcript()

    assert "+ 1. Take in the mail" in text
    assert "! 2. Check the gates" in text
    assert "= blocked" in text


def test_total_time_is_the_sum_of_the_steps():
    run = RunJournal("r-1", "2026-07", clock=FixedClock())
    run.steps.extend([
        Step(1, "a", "A", "", "t", 120), Step(2, "b", "B", "", "t", 80),
    ])
    assert run.total_ms == 200


# ── the store ────────────────────────────────────────────────────────────────

def test_the_local_store_round_trips_a_close():
    store = LocalStore()
    store.save_close("Bell Ridge Haulage", "2026-07", {"outcome": "closed"})

    assert store.load_close("Bell Ridge Haulage", "2026-07") == {"outcome": "closed"}


def test_a_missing_close_reads_back_as_none_rather_than_raising():
    assert LocalStore().load_close("Nobody", "1999-01") is None


def test_runs_and_drafts_are_keyed_by_run_id():
    store = LocalStore()
    draft = Draft(kind=DraftKind.PAYMENT_REMINDER, recipient="Broker", subject="s",
                  body="b", amount=100.0, reference="L-1",
                  finding_kind=ExceptionKind.LOAD_UNPAID)

    store.save_run({"run_id": "r-1", "outcome": "closed"})
    paths = store.save_drafts("r-1", [draft])

    assert store.load_run("r-1")["outcome"] == "closed"
    assert paths == ["memory://drafts/r-1::0"]
    assert store.load_drafts("r-1")[0]["status"] == "filed"


def test_documents_are_stored_under_their_own_name():
    store = LocalStore()
    assert store.put_document("load-L-1.txt", "body") == "memory://documents/load-L-1.txt"


def test_the_key_scheme_scopes_a_close_to_a_company_and_a_period():
    assert close_key("Bell Ridge Haulage", "2026-07") == "Bell Ridge Haulage::2026-07"
    assert close_key(None, "2026-07") == "default::2026-07"


def test_dataclasses_and_enums_flatten_to_plain_documents():
    """Firestore takes documents, not dataclasses. The same conversion runs for
    the local store, so a test proves the calling code rather than a mock."""
    draft = Draft(kind=DraftKind.SHORT_PAY_DISPUTE, recipient="Broker", subject="s",
                  body="b", amount=200.0, reference="L-1",
                  finding_kind=ExceptionKind.SHORT_PAY)

    flat = _plain(draft)

    assert flat["kind"] == "short_pay_dispute"
    assert flat["finding_kind"] == "short_pay"
    assert isinstance(flat, dict)


def test_get_store_falls_back_to_memory_without_a_project(monkeypatch):
    from archon.store import get_store

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    assert get_store().backend == "memory"
