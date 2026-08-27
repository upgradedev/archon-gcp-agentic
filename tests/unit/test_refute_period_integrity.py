"""Independent check of audit 1.2, driven from the real mailbox and the real close.

The prior repro built its June documents by hand with ``period="2026-06"`` and
concluded that ``Ledger._post`` writes ``entry.period`` and ``Ledger.balances``
never reads it. That conclusion is about a field, and the field is not where
this defect lives. Both readers the product actually ships --
``mailbox.read_period`` (mailbox.py:44) and ``gcs.read_gcs_period``
(gcs.py:96) -- call ``extract_document(..., period=period)`` with the period
being CLOSED. Every document in a July close therefore carries
``period == "2026-07"`` no matter what date is printed on its face, and so does
every journal entry posted from it. A ``Document`` with ``period="2026-06"``
sitting in a July close is not an input this product can produce.

So this file does not use hand-built periods. It uses the month the repository
ships, ``corpus/2026-07/``, which already contains the scenario:

    corpus/2026-07/expense-toll-88231.txt   Date: 2026-06-28   Net: 412.60

A June-dated toll invoice, in July's mail, in the bundled demo a judge runs.

What is checked here:

  * the money does land in July's figures (test_july_tolls_are_julys_tolls,
    expected to FAIL -- this is the reproduction, from a real entry point)
  * the period field carries no June signal at all, so the fix the audit's root
    cause implies is a no-op
    (test_every_journal_entry_carries_the_close_period,
     test_filtering_the_roll_up_by_entry_period_would_change_nothing)
  * the one thing that does carry the signal is ``doc.date``, which is what
    ``find_out_of_period`` already reads (test_the_only_surviving_signal_is_the_date)
  * reported as a warning, month still closes -- documented, not a bug
    (test_the_june_invoice_is_reported_and_the_month_still_closes)
"""
from __future__ import annotations

from archon.adapters.store import LocalStore
from archon.domain.exceptions import parse_date
from archon.domain.models import Account, DocType, ExceptionKind
from archon.runtime.close import run_close
from archon.runtime.journal import FixedClock
from archon.runtime.mailbox import read_period

JULY = "2026-07"

#: The three toll invoices in July's mail, by net amount and the date on each.
TOLL_88231_NET = 412.60      # dated 2026-06-28 -- June, in July's mail
TOLL_88409_NET = 388.20      # dated 2026-07-15
TOLL_88512_NET = 355.40      # dated 2026-07-29

#: The June invoice's gross, which is what lands in accounts payable.
TOLL_88231_GROSS = 445.61

#: What July's toll cost is if July's toll cost is July's.
JULY_TOLLS_ALONE = round(TOLL_88409_NET + TOLL_88512_NET, 2)          # 743.60
#: What the close actually reports today.
JULY_TOLLS_WITH_JUNE = round(JULY_TOLLS_ALONE + TOLL_88231_NET, 2)    # 1156.20
#: Accounts payable as the close reports it today, June's gross included.
JULY_PAYABLES_WITH_JUNE = 2032.69


def july_mail():
    """The month this repository ships, read the way production reads it."""
    documents, _raw = read_period(JULY)
    return documents


def close_july():
    """The real entry point, offline: in-memory store, frozen clock."""
    return run_close(period=JULY, documents=july_mail(),
                     company="Bell Ridge Haulage",
                     store=LocalStore(), clock=FixedClock())


# ── the corpus really does contain the scenario ──────────────────────────────

def test_julys_shipped_mail_contains_a_june_dated_invoice():
    """No fixture, no hand-built document: this is what is on disk."""
    tolls = [d for d in july_mail() if d.doc_type is DocType.TOLL_INVOICE]
    june = [d for d in tolls
            if (when := parse_date(d.date)) is not None and when.month == 6]

    assert len(june) == 1
    assert june[0].source_file == "expense-toll-88231.txt"
    assert june[0].date == "2026-06-28"
    assert june[0].net_amount == TOLL_88231_NET


# ── the reproduction, from the real entry point ──────────────────────────────

def test_july_tolls_are_julys_tolls():
    """412.60 of June's toll cost is reported as July's cost of running.

    Expected to FAIL on this commit. This is the defect, reached without
    constructing anything the product does not construct itself: read the
    shipped July mail, run the shipped close, look at the toll line.
    """
    result = close_july()

    assert result.statements.period == JULY
    assert result.statements.tolls == JULY_TOLLS_ALONE


def test_july_does_not_owe_junes_supplier_bill():
    """The other half of the same entry: 445.61 gross sits in July's payables.

    Also expected to FAIL. Worth its own test because accounts payable is the
    figure a bookkeeper reconciles against a supplier statement, so this one
    is visible outside the P&L.
    """
    result = close_july()

    assert TOLL_88231_GROSS not in (0.0, None)
    assert result.statements.accounts_payable == round(
        JULY_PAYABLES_WITH_JUNE - TOLL_88231_GROSS, 2
    )


# ── why the audit's stated root cause is the wrong lever ─────────────────────

def test_the_real_intake_stamps_the_close_period_on_every_document():
    """``read_period`` sets ``period=period`` on all 27 artifacts, June one included.

    This is the fact that makes the prior repro's hand-built
    ``Document(period="2026-06")`` unreachable from any real intake path.
    """
    documents = july_mail()

    assert {d.period for d in documents} == {JULY}

    june = [d for d in documents if d.source_file == "expense-toll-88231.txt"][0]
    assert june.period == JULY, "the June invoice is stamped July by the reader"
    assert june.date == "2026-06-28", "only the date still says June"


def test_every_journal_entry_carries_the_close_period():
    """``_post`` copies ``doc.period``, and ``doc.period`` is always the close period.

    So ``entry.period`` is a constant across the ledger. It is not that the
    field is 'written and never read' -- it is that the field has nothing to
    say, because the June signal was flattened at intake, before posting.
    """
    result = close_july()

    assert {e.period for e in result.ledger.entries} == {JULY}


def test_filtering_the_roll_up_by_entry_period_would_change_nothing():
    """The fix implied by the audit's root cause, applied: a no-op.

    If ``balances()`` grew the period predicate the audit says it lacks, it
    would admit every entry it admits today, June invoice included, and July's
    toll line would still read 1,156.20.
    """
    ledger = close_july().ledger

    kept = [e for e in ledger.entries if e.period == ledger.period]
    assert len(kept) == len(ledger.entries), "the predicate excludes nothing"

    tolls_under_the_fix = round(
        sum(line.debit - line.credit
            for entry in kept for line in entry.lines
            if line.account is Account.TOLLS_EXPENSE),
        2,
    )
    assert tolls_under_the_fix == JULY_TOLLS_WITH_JUNE
    assert tolls_under_the_fix != JULY_TOLLS_ALONE


def test_the_only_surviving_signal_is_the_date():
    """``doc.date`` is the one field that still distinguishes June from July.

    ``find_out_of_period`` already reads it, which is why the close notices the
    invoice at all while the roll-up does not. Any real fix has to key off the
    date, not the period stamp.
    """
    documents = july_mail()

    by_period = {d.period for d in documents}
    by_month = {(d.date or "")[:7] for d in documents if d.date}

    assert by_period == {JULY}, "period cannot separate them"
    assert "2026-06" in by_month, "the date can"


# ── the documented half, which must survive any fix ──────────────────────────

def test_the_june_invoice_is_reported_and_the_month_still_closes():
    """Reported, not refused. ``find_out_of_period`` says so in its own docstring.

    Passes today and must keep passing: the finding is a warning and July still
    closes. The defect is that the figure is wrong, not that the month closed.
    """
    result = close_july()

    out_of_period = [f for f in result.findings
                     if f.kind is ExceptionKind.OUT_OF_PERIOD]
    assert len(out_of_period) == 1
    assert out_of_period[0].reference == "TOLL-88231"
    assert out_of_period[0].severity == "warning"
    assert out_of_period[0].source_file == "expense-toll-88231.txt"

    assert result.outcome == "closed"
    assert result.closed
