"""Audit 1.2, reproduced: a document from another period reaches July's figures.

The claim under test is not "the close should refuse a June invoice". It should
not, and `exceptions.find_out_of_period` says so in its own words: a June
invoice arriving in July mail is ordinary. That docstring also states the
contract this file checks:

    "What it must not do is post it silently into the wrong month, which is
     how a period gets reopened three weeks later."

So the assertions below are about the *figures*, not the outcome. A June
document may arrive, may be reported, and may leave the month closeable. Its
money may not turn up in July's revenue, July's costs, July's profit or July's
miles.

Two of these tests are expected to fail on this commit. They are the
reproduction. The rest pass and are the guard rails around any fix:

  * the close outcome stays `closed` and the finding stays a `warning`
    (test_the_close_reports_it_as_a_warning_and_still_closes)
  * nothing in `validation.validate` blocks on the period
    (test_no_gate_looks_at_the_period_at_all)
  * a document dated on the first or last day of the month is still accepted
    and still counted (test_a_document_on_either_edge_of_the_month_is_kept)

That last one matters most. A fix that rejects out-of-period documents wholesale,
or that gets the month's last day wrong by one, would be worse than the defect
it replaces.
"""
from __future__ import annotations

import pytest

from archon.adapters.store import LocalStore
from archon.domain import validation as validation_mod
from archon.domain.ledger import Ledger
from archon.domain.models import DocType, Document, ExceptionKind
from archon.domain.policy import Disposition
from archon.runtime.close import run_close
from archon.runtime.journal import FixedClock

JULY = "2026-07"
JUNE = "2026-06"

#: July's own month, in full. These are the only figures July's books may show.
JULY_LOAD_RATE = 5_000.00
JULY_LOAD_MILES = 1_000.0
JULY_TOLL_NET = 100.00
JULY_TOLL_TAX = 8.00

#: June's, which arrived in July's mail and belongs to a period already gone.
JUNE_LOAD_RATE = 9_000.00
JUNE_LOAD_MILES = 2_000.0
JUNE_TOLL_NET = 4_000.00
JUNE_TOLL_TAX = 320.00


# ── documents, built by hand so each one self-identifies its period ──────────
#
# The helpers in tests/conftest.py hard-code period="2026-07". Building these
# directly means the June documents carry period="2026-06" on their own face,
# which closes the "well, the document said it was July" reading of the claim.

def load_confirmation(ref: str, rate: float, *, period: str, date: str,
                      miles: float, truck: str = "T-1") -> Document:
    return Document(
        doc_type=DocType.LOAD_CONFIRMATION, period=period,
        source_file=f"load-{ref}.txt", date=date, load_ref=ref,
        broker="Halstead Freight", truck=truck, miles=miles,
        net_amount=rate, gross_amount=rate,
    )


def toll_invoice(number: str, net: float, tax: float, *, period: str, date: str,
                 supplier: str) -> Document:
    return Document(
        doc_type=DocType.TOLL_INVOICE, period=period,
        source_file=f"toll-{number}.txt", date=date, document_number=number,
        counterparty=supplier, net_amount=net, tax_amount=tax,
        gross_amount=round(net + tax, 2),
    )


def july_only() -> list[Document]:
    """A clean July: one load run, one toll bill. Nothing else."""
    return [
        load_confirmation("L-JUL", JULY_LOAD_RATE, period=JULY,
                          date="2026-07-10", miles=JULY_LOAD_MILES),
        toll_invoice("T-JUL", JULY_TOLL_NET, JULY_TOLL_TAX, period=JULY,
                     date="2026-07-15", supplier="Interstate Tolling"),
    ]


def june_in_the_july_mail() -> list[Document]:
    """The same July, plus two documents dated and stamped June."""
    return july_only() + [
        load_confirmation("L-JUN", JUNE_LOAD_RATE, period=JUNE,
                          date="2026-06-15", miles=JUNE_LOAD_MILES, truck="T-2"),
        toll_invoice("T-JUN", JUNE_TOLL_NET, JUNE_TOLL_TAX, period=JUNE,
                     date="2026-06-28", supplier="Riverside Bridge Authority"),
    ]


def close_july(documents: list[Document]):
    """Drive the real entry point, offline, with a store and clock of our own."""
    return run_close(period=JULY, documents=documents, company="Bell Ridge Haulage",
                     store=LocalStore(), clock=FixedClock())


#: What July's books would show if only July were in them.
JULY_ALONE = {
    "revenue": JULY_LOAD_RATE,
    "tolls": JULY_TOLL_NET,
    "net_profit": round(JULY_LOAD_RATE - JULY_TOLL_NET, 2),
    "total_miles": JULY_LOAD_MILES,
}


def figures(statements) -> dict:
    return {
        "revenue": statements.revenue,
        "tolls": statements.tolls,
        "net_profit": statements.net_profit,
        "total_miles": statements.total_miles,
    }


# ── the control: July alone produces July's figures ──────────────────────────

def test_july_alone_produces_julys_figures():
    """Pins the expected numbers, so the failures below are about June only."""
    result = close_july(july_only())

    assert result.statements.period == JULY
    assert figures(result.statements) == JULY_ALONE
    assert result.outcome == "closed"


# ── the reproduction ─────────────────────────────────────────────────────────

def test_a_june_document_is_recorded_by_the_ledger_and_posts_nothing():
    """What the defect looked like, and what replaced it.

    `_post` stamped `entry.period` from the document and `balances()` never read
    it, so a June entry in a July ledger was indistinguishable from a July one
    at roll-up time and June's figures became July's.

    The stamp was never going to save it: `extract_document(text, period=...)`
    writes the month being CLOSED onto every document it produces, so a June
    invoice read during a July close carries `period="2026-07"` and any
    comparison against the ledger's period is true by construction. The date is
    the only field that still knows, which is what `belongs_to` reads.

    Both halves are asserted here. The entry still EXISTS, carrying its reason,
    because G6 has to account for an artifact the owner sent whatever its date;
    and it posts no lines, so it cannot reach the books.
    """
    ledger = Ledger(period=JULY, company="Bell Ridge Haulage")
    ledger.add_all(june_in_the_july_mail())

    june_entries = [e for e in ledger.entries if e.date.startswith(JUNE)]
    assert len(june_entries) == 2, "both June documents must stay on the trail"
    assert all(e.lines == [] for e in june_entries), "and neither may post a line"
    assert all("outside 2026-07" in e.memo for e in june_entries), (
        "the entry has to say why it posted nothing"
    )

    assert ledger.period == JULY
    assert [d.source_file for d in ledger.documents] != [d.source_file for d in ledger.posted], (
        "the arrived list and the posted list must differ, or nothing was excluded"
    )
    assert figures(ledger.statements()) == JULY_ALONE


def test_a_june_document_does_not_reach_julys_revenue_and_costs():
    """End to end through `run_close`, which is how a real month arrives."""
    result = close_july(june_in_the_july_mail())

    assert result.statements.period == JULY
    assert figures(result.statements) == JULY_ALONE


# ── what the close actually does about it, verified rather than assumed ──────

def test_the_close_reports_it_as_a_warning_and_still_closes():
    """The documented half of the behaviour: reported, not refused.

    Passes today and must keep passing. A fix that turns this into a blocked
    month contradicts `find_out_of_period`'s own stated intent.
    """
    result = close_july(june_in_the_july_mail())

    out_of_period = [f for f in result.findings
                     if f.kind is ExceptionKind.OUT_OF_PERIOD]
    assert len(out_of_period) == 2
    assert {f.severity for f in out_of_period} == {"warning"}

    assert result.outcome == "closed"
    assert result.closed


def test_nothing_acts_on_the_out_of_period_finding():
    """No letter, no escalation: the policy notes it and moves on.

    OUT_OF_PERIOD is not in `ALWAYS_ESCALATE` and not in `ACTIONABLE_KINDS`, and
    it carries `severity == "warning"`, so `_clamp` leaves the default NOTE
    alone. Verified here rather than read off the source.
    """
    result = close_july(june_in_the_july_mail())

    dispositions = {d.applied for d in result.decisions
                    if d.finding.kind is ExceptionKind.OUT_OF_PERIOD}
    assert dispositions == {Disposition.NOTE}

    assert [d for d in result.drafts
            if d.finding_kind is ExceptionKind.OUT_OF_PERIOD] == []


def test_an_out_of_period_load_is_still_chased_as_an_open_receivable():
    """The other half of the rail: being out of period is not being irrelevant.

    A June load nobody has paid is still money owed at the end of July, and
    `register.py` says so in its own words: age is measured to the end of the
    period being closed. So the payment reminder is correct and must survive.

    This is here because the cheapest wrong fix is to drop out-of-period
    documents from the close entirely. That would take June's 9,000 out of
    July's revenue, which is right, and also stop anyone ever chasing it, which
    is worse than the defect. The figures and the open items are different
    questions and only the figures are wrong.
    """
    result = close_july(june_in_the_july_mail())

    chased = [d.reference for d in result.drafts
              if d.finding_kind is ExceptionKind.LOAD_UNPAID]
    assert "L-JUN" in chased
    assert "L-JUL" in chased


def test_no_gate_looks_at_the_period_at_all():
    """G1-G6 over a July ledger holding June entries: every one of them passes."""
    ledger = Ledger(period=JULY, company="Bell Ridge Haulage")
    ledger.add_all(june_in_the_july_mail())

    gates = validation_mod.validate(ledger, [])

    assert validation_mod.all_passed(gates)
    assert not [g for g in gates if "period" in g.rule.lower()]


# ── the boundary, which a careless fix would break ───────────────────────────

@pytest.mark.parametrize("date", ["2026-07-01", "2026-07-31"])
def test_a_document_on_either_edge_of_the_month_is_kept(date):
    """The first and last day of July are July. Both are in, and both count."""
    documents = july_only() + [
        toll_invoice("T-EDGE", 250.00, 20.00, period=JULY, date=date,
                     supplier="Riverside Bridge Authority"),
    ]

    result = close_july(documents)

    assert [f for f in result.findings
            if f.kind is ExceptionKind.OUT_OF_PERIOD] == []
    assert result.statements.tolls == round(JULY_TOLL_NET + 250.00, 2)
    assert result.statements.revenue == JULY_LOAD_RATE
    assert result.outcome == "closed"


@pytest.mark.parametrize("period, date", [
    ("2026-12", "2026-12-01"),
    ("2026-12", "2026-12-31"),
])
def test_december_edges_are_kept_too(period, date):
    """The year roll-over is the edge case an off-by-one fix loses first."""
    documents = [
        toll_invoice("T-DEC", 250.00, 20.00, period=period, date=date,
                     supplier="Riverside Bridge Authority"),
    ]

    result = run_close(period=period, documents=documents,
                       company="Bell Ridge Haulage", store=LocalStore(),
                       clock=FixedClock())

    assert [f for f in result.findings
            if f.kind is ExceptionKind.OUT_OF_PERIOD] == []
    assert result.statements.tolls == 250.00
    assert result.outcome == "closed"
