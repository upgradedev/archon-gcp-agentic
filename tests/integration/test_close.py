"""The chore itself, over the bundled month.

These are the assertions that would go red if the product stopped doing what it
claims to do, so they are written against the claim rather than against the
implementation.
"""
from __future__ import annotations

from archon.adapters.store import LocalStore
from archon.domain.models import DocType, ExceptionKind
from archon.runtime.close import documents_summary, run_close, run_id_for
from archon.runtime.journal import FixedClock
from tests.conftest import PERIOD


def close(documents, raw=None, store=None, clock=None):
    return run_close(period=PERIOD, documents=documents, company="Bell Ridge Haulage",
                     store=store or LocalStore(), clock=clock, raw_texts=raw)


# ── the claim: it finishes, unattended ───────────────────────────────────────

def test_the_month_closes_end_to_end(documents):
    result = close(documents)

    assert result.outcome == "closed"
    assert result.closed


def test_the_run_is_eleven_steps_and_every_one_completed(documents):
    result = close(documents)

    assert len(result.journal.steps) == 11
    assert [s.name for s in result.journal.steps] == [
        "intake", "post", "allocate", "reconcile", "triage",
        "decide", "draft", "verify", "report", "file", "notify",
    ]
    assert {s.status for s in result.journal.steps} == {"ok"}


def test_every_step_records_what_it_touched_not_merely_that_it_ran(documents):
    """A step that reports only that it happened proves only that it was called."""
    for step in close(documents).journal.steps:
        assert step.detail.strip()
        assert step.counts


# ── the claim: the books are right ───────────────────────────────────────────

def test_every_journal_entry_balances(documents):
    assert close(documents).ledger.all_entries_balanced()


def test_all_five_gates_pass_on_the_bundled_month(documents):
    result = close(documents)

    assert all(gate.passed for gate in result.gates)
    assert len(result.gates) == 5


def test_the_month_reads_like_a_real_thin_margin_haulier(documents):
    statements = close(documents).statements

    assert statements.revenue == 23_005.00
    assert statements.operating_expenses == 21_010.76
    assert statements.net_profit == 1_994.24
    assert statements.total_miles == 10_810.0
    assert statements.revenue_per_mile == 2.128
    assert statements.cost_per_mile == 1.944


def test_three_trucks_each_carry_their_own_miles_and_direct_cost(documents):
    per_truck = close(documents).statements.per_truck

    assert sorted(per_truck) == ["T-101", "T-102", "T-103"]
    assert sum(row["miles"] for row in per_truck.values()) == 10_810.0
    assert all(row["cost_per_mile"] is not None for row in per_truck.values())


# ── the claim: it splits one payment across many loads ───────────────────────

def test_the_remittance_is_split_across_every_load_it_settles(documents):
    result = close(documents)

    assert len(result.allocations) == 1
    allocation = result.allocations[0]
    assert len(allocation.allocations) == 8
    assert allocation.remittance_total == 18_667.65
    assert allocation.factoring_fee == 577.35


def test_the_remittance_identity_closes(documents):
    """What landed equals what the lines pay, less the fee charged once."""
    allocation = close(documents).allocations[0]

    assert allocation.residual == 0.0
    assert allocation.reconciles


def test_the_load_paid_from_a_month_we_have_no_confirmation_for_is_flagged(documents):
    assert close(documents).allocations[0].unmatched_load_refs == ["L-7099"]


# ── the claim: it finds the mess ─────────────────────────────────────────────

def test_every_detector_fires_on_the_bundled_month(documents):
    """The corpus was built to contain one instance of each defect, so a
    detector that stops working shows up here rather than in a demo."""
    kinds = {finding.kind for finding in close(documents).findings}

    assert kinds == set(ExceptionKind)


def test_the_errors_are_the_ones_worth_money(documents):
    errors = [f for f in close(documents).findings if f.severity == "error"]

    assert {f.kind for f in errors} == {
        ExceptionKind.PAYMENT_WITHOUT_DOCUMENT,
        ExceptionKind.DUPLICATE_CHARGE,
        ExceptionKind.SHORT_PAY,
    }
    assert round(sum(f.amount for f in errors), 2) == 2_477.85


def test_findings_are_ordered_worst_first(documents):
    """An owner with ten minutes reads the top of the list, so the top of the
    list has to be the money."""
    findings = close(documents).findings
    rank = {"error": 0, "warning": 1, "info": 2}

    assert findings == sorted(findings, key=lambda f: (rank[f.severity], -f.amount))


# ── the claim: it writes the letters, and does not send them ─────────────────

def test_a_corrective_document_is_written_for_every_actionable_finding(documents):
    result = close(documents)
    actionable = [f for f in result.findings if f.actionable and f.amount > 0]

    assert len(result.drafts) == len(actionable) == 5
    assert result.leakage == 612.85          # what would have been lost
    assert result.outstanding == 4_900.00    # owed already, not found
    assert result.undocumented == 1_865.00   # recovers nothing


def test_each_draft_names_a_real_counterparty_not_a_reference(documents):
    for draft in close(documents).drafts:
        assert draft.recipient
        assert not draft.recipient.startswith(("INV-", "L-7", "FCN-"))


def test_no_draft_is_ever_sent(documents):
    """The one human gate. If this goes red, the product changed shape."""
    assert {draft.status for draft in close(documents).drafts} == {"filed"}


# ── the claim: nothing unreadable was invented ───────────────────────────────

def test_the_unreadable_scan_is_reported_and_posts_nothing(documents):
    result = close(documents)

    unreadable = [f for f in result.findings
                  if f.kind is ExceptionKind.UNREADABLE_DOCUMENT]
    assert len(unreadable) == 1

    scans = result.ledger.documents_of(DocType.UNREADABLE)
    posted = [e for e in result.ledger.entries
              if e.source_doc in {d.source_file for d in scans} and e.lines]
    assert posted == []


# ── persistence and idempotency ──────────────────────────────────────────────

def test_the_close_the_drafts_and_the_trail_are_all_persisted(documents, corpus):
    store = LocalStore()
    result = close(documents, raw=corpus[1], store=store)

    assert store.load_close("Bell Ridge Haulage", PERIOD)["outcome"] == "closed"
    assert store.load_run(result.run_id)["outcome"] == "closed"
    assert len(store.load_drafts(result.run_id)) == 5


def test_the_persisted_trail_is_the_whole_trail(documents, corpus):
    """The record used to stop two steps short, because it was written from
    inside step 9. A stored trail missing its own filing and notification is a
    trail that hides the last thing the agent did."""
    store = LocalStore()
    result = close(documents, raw=corpus[1], store=store)

    persisted_run = store.load_run(result.run_id)
    persisted_close = store.load_close("Bell Ridge Haulage", PERIOD)

    assert len(result.journal.steps) == 11
    assert len(persisted_run["steps"]) == 11
    assert len(persisted_close["journal"]["steps"]) == 11
    assert persisted_run["steps"][-1]["name"] == "notify"
    assert persisted_run["finished_at"] is not None


def test_the_persisted_close_carries_the_digest_and_its_receipt(documents, corpus):
    """Written during step 10, so the record from step 9 could not have them."""
    store = LocalStore()
    close(documents, raw=corpus[1], store=store)

    persisted = store.load_close("Bell Ridge Haulage", PERIOD)

    assert persisted["digest"]["subject"]
    assert persisted["receipt"]["channel"] == "filed"


def test_re_running_the_same_month_reuses_the_same_run_id(documents):
    """Idempotent by derivation: the same mail overwrites, it does not litter."""
    assert close(documents).run_id == close(documents).run_id


def test_changing_the_mail_changes_the_run_id(documents):
    assert run_id_for(PERIOD, documents) != run_id_for(PERIOD, documents[:-1])


def test_a_run_is_byte_stable_under_a_fixed_clock(documents):
    """Without this, no golden assertion over a whole run is possible."""
    first = close(documents, clock=FixedClock()).journal.to_dict()
    second = close(documents, clock=FixedClock()).journal.to_dict()

    assert first == second


# ── failure behaviour ────────────────────────────────────────────────────────

def test_a_month_that_fails_a_gate_is_blocked_rather_than_filed_as_good(documents):
    """A close that finds its own books untrustworthy must say so, not hide it."""
    broken = list(documents)
    remittance = next(d for d in broken if d.doc_type is DocType.BROKER_REMITTANCE)
    remittance.remittance_total = 1.0            # the identity can no longer close

    result = close(broken)

    assert result.outcome == "blocked"
    assert not result.closed
    verify = next(s for s in result.journal.steps if s.name == "verify")
    assert verify.status == "blocked"


def test_a_blocked_close_still_did_the_work_and_still_says_so(documents):
    broken = list(documents)
    next(d for d in broken
         if d.doc_type is DocType.BROKER_REMITTANCE).remittance_total = 1.0

    result = close(broken)

    assert result.drafts                          # the letters were still written
    assert "not trustworthy" in result.summary


def test_an_empty_month_closes_without_raising():
    result = close([])

    assert result.outcome == "closed"
    assert result.findings == []
    assert result.statements.revenue == 0.0


# ── the narrator seam ────────────────────────────────────────────────────────

def test_the_summary_is_deterministic_when_no_narrator_is_supplied(documents):
    assert close(documents).summary == close(documents).summary


def test_an_injected_narrator_writes_the_summary(documents):
    result = run_close(period=PERIOD, documents=documents, store=LocalStore(),
                       narrator=lambda facts: "A different sentence entirely.")

    assert result.summary == "A different sentence entirely."
    report = next(s for s in result.journal.steps if s.name == "report")
    assert report.counts["source"] == "gemini"


def test_a_narrator_that_fails_does_not_fail_the_close(documents):
    """A model being down is not a reason for a month not to close."""
    def broken(_facts):
        raise RuntimeError("quota exhausted")

    result = run_close(period=PERIOD, documents=documents, store=LocalStore(),
                       narrator=broken)

    assert result.outcome == "closed"
    assert result.summary.startswith(PERIOD)      # the deterministic one


def test_a_narrator_returning_nothing_leaves_the_deterministic_summary(documents):
    result = run_close(period=PERIOD, documents=documents, store=LocalStore(),
                       narrator=lambda facts: "   ")

    assert result.summary.startswith(PERIOD)


# ── the serialised shape the API and the page depend on ─────────────────────

def test_the_serialised_close_carries_everything_the_page_renders(documents):
    payload = close(documents).to_dict()

    assert set(payload) >= {
        "run_id", "period", "outcome", "summary", "statements", "allocations",
        "findings", "gates", "drafts", "journal", "recoverable", "facts",
        "digest", "receipt",
    }
    assert payload["allocations"][0]["lines"][0]["load_ref"] == "L-7101"
    assert payload["journal"]["steps"][0]["title"]


def test_the_serialised_close_is_json_safe(documents):
    import json

    json.dumps(close(documents).to_dict())        # raises if an enum leaked through


def test_documents_summary_counts_every_family_including_the_empty_ones(documents):
    counts = documents_summary(documents)

    assert counts["load_confirmation"] == 9
    assert counts["broker_remittance"] == 1
    assert counts["unknown"] == 0
    assert set(counts) == {t.value for t in DocType}
