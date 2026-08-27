"""Repro for audit finding 1.1: one cash event, posted twice.

The month below is the smallest one that can hold the claim:

    * one load, earned at 100.00, so receivables carry 100.00
    * one broker remittance of 100.00 that settles exactly that load
    * the inbound bank line that same remittance produced when it landed --
      same reference, same counterparty, same amount, same date

Only 100.00 ever arrived. The question these tests ask is what the real close
does with it: `run_close` is driven end to end and the assertions are on the
books it produced, not on any internal.
"""
from __future__ import annotations

from archon.adapters.store import LocalStore
from archon.domain.validation import summary as gate_summary
from archon.runtime.close import run_close
from archon.runtime.journal import FixedClock
from tests.conftest import PERIOD, bank, load, remittance

BROKER = "Test Broker"
SETTLED_ON = "2026-07-24"
REMITTANCE_REF = "RM-1"


def one_cash_event() -> list:
    """A period holding exactly one earned 100.00 and exactly one receipt of it."""
    return [
        load("L-1", 100.00, broker=BROKER, date="2026-07-10"),
        remittance(REMITTANCE_REF, [("L-1", 100.00, 0.00, "")], fee=0.00,
                   broker=BROKER, date=SETTLED_ON),
        # The bank's own record of that same remittance landing.
        bank(100.00, REMITTANCE_REF, direction="in", counterparty=BROKER,
             date=SETTLED_ON),
    ]


def close_it():
    return run_close(period=PERIOD, documents=one_cash_event(),
                     company="Repro Haulage", store=LocalStore(), clock=FixedClock())


def books(result) -> str:
    """Every number that matters, in one string, so a failure carries the finding."""
    s = result.statements
    return (
        f"\n  outcome           = {result.outcome!r} ({result.outcome_reason})"
        f"\n  gates             = {gate_summary(result.gates)}"
        f"\n  revenue           = {s.revenue:,.2f}"
        f"\n  cash_in           = {s.cash_in:,.2f}"
        f"\n  cash_out          = {s.cash_out:,.2f}"
        f"\n  net_cash          = {s.net_cash:,.2f}"
        f"\n  accounts_receivable = {s.accounts_receivable:,.2f}"
        f"\n  findings          = {len(result.findings)}"
        f"\n  entries           = {[(e.memo, [(line.account.value, line.debit, line.credit) for line in e.lines]) for e in result.ledger.entries]}"
    )


def test_one_hundred_that_arrived_once_is_counted_once():
    """100.00 landed in the bank. The books must show 100.00 of cash in."""
    result = close_it()

    assert result.statements.cash_in == 100.00, books(result)


def test_a_fully_settled_load_leaves_no_receivable():
    """One load of 100.00, paid in full. Receivables must land on zero.

    A negative receivable here is not a matter of taste: there is one load, it
    was settled once, and nothing else in the period touches AR.
    """
    result = close_it()

    assert result.statements.accounts_receivable == 0.00, books(result)


def test_cash_in_never_exceeds_what_the_month_earned():
    """No capital, no loan, no other inflow: cash in cannot beat revenue."""
    result = close_it()

    s = result.statements
    assert s.cash_in <= s.revenue, books(result)


def test_the_close_reports_itself_healthy_either_way():
    """The silent half of the claim: nothing in the run objects.

    This passes on the current code and must keep passing once the double
    posting is fixed -- correct books close too. It is here so the failure
    output above cannot be read as "the gates would have caught it".
    """
    result = close_it()

    assert result.outcome == "closed", books(result)
    assert len(result.gates) == 6
    assert [g.passed for g in result.gates] == [True] * 6, books(result)
    g4 = next(g for g in result.gates if g.rule.startswith("G4"))
    assert g4.passed, g4.message
