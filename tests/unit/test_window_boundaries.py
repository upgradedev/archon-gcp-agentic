"""C6: every window pinned by a fixture that does not come from the constant.

A test whose fixture is derived from the same parameter the code reads cannot
fail. Change the constant and the fixture follows it, the assertion still holds,
and the window is no longer tested by anything.

This repository already had one: the digest test asserted
`f"and {5 - TOP_ACTIONS} more"`, which is true for any value of `TOP_ACTIONS`.

So every window is pinned here with literal offsets chosen against the value the
product ships with, and each one is checked on both sides of its edge. Changing
a constant now breaks a test, which is the point: a threshold is a product
decision and it should not be possible to move one quietly.
"""
from __future__ import annotations

import pytest

from archon.domain import exceptions as exc
from archon.domain.allocation import allocate_all, allocate_remittance
from archon.domain.digest import TOP_ACTIONS
from archon.domain.models import DocType
from tests.conftest import PERIOD, expense, fuel, load, remittance

# ── the shipped values, written out so a change is visible in the diff ───────

def test_the_windows_are_the_values_every_fixture_below_assumes():
    """If this fails, the fixtures underneath stopped testing their edges."""
    assert exc.DUPLICATE_WINDOW_DAYS == 10
    assert exc.OUTLIER_MULTIPLE == 4.0
    assert exc.OUTLIER_MIN_HISTORY == 3
    assert exc.TAX_MIN_INVOICES == 3
    assert TOP_ACTIONS == 3


# ── the duplicate window, 10 days ────────────────────────────────────────────

@pytest.mark.parametrize("second_date, expected", [
    ("2026-07-11", 1),   # 10 days, exactly the edge, inside
    ("2026-07-12", 0),   # 11 days, one past it, outside
])
def test_the_duplicate_window_holds_at_its_edge(second_date, expected):
    """Dates are literal. Neither is computed from DUPLICATE_WINDOW_DAYS."""
    statement = fuel("F-1", [
        ("2026-07-01", "T-1", "Effingham IL", 100.0, 412.85, 33.03),
        (second_date, "T-1", "Effingham IL", 100.0, 412.85, 33.03),
    ])

    assert len(exc.find_duplicate_charges([statement])) == expected


# ── the outlier multiple, 4x, and its minimum history of 3 ───────────────────

@pytest.mark.parametrize("outlier_amount, expected", [
    (1600.00, 1),   # 4.0x a median of 400.00, exactly the edge, reported
    (1599.00, 0),   # a shade under, not reported
])
def test_the_outlier_multiple_holds_at_its_edge(outlier_amount, expected):
    """The median of the other four charges is 400.00 by construction."""
    statement = fuel("F-1", [
        ("2026-07-02", "T-1", "A", 100.0, 400.00, 32.0),
        ("2026-07-04", "T-1", "B", 100.0, 400.00, 32.0),
        ("2026-07-06", "T-1", "C", 100.0, 400.00, 32.0),
        ("2026-07-08", "T-1", "D", 100.0, 400.00, 32.0),
        ("2026-07-20", "T-1", "E", 100.0, outlier_amount, 0.0),
    ])

    assert len(exc.find_amount_outliers([statement])) == expected


@pytest.mark.parametrize("prior_charges, expected", [
    (3, 1),   # four charges in total, one more than the minimum, a norm exists
    (2, 0),   # three in total, no norm, and nothing is claimed
])
def test_no_outlier_is_claimed_below_the_minimum_history(prior_charges, expected):
    fills = [(f"2026-07-0{i + 1}", "T-1", "A", 100.0, 400.00, 32.0)
             for i in range(prior_charges)]
    fills.append(("2026-07-20", "T-1", "A", 100.0, 4000.00, 0.0))

    assert len(exc.find_amount_outliers([fuel("F-1", fills)])) == expected


# ── the tax minimum, 3 invoices before a rate is inferred ────────────────────

@pytest.mark.parametrize("consistent_invoices, expected", [
    (2, 1),   # three invoices in total, exactly the minimum, a rate is inferred
    (1, 0),   # two in total, one short, and nothing is claimed
])
def test_a_rate_is_only_inferred_once_there_are_enough_invoices(
        consistent_invoices, expected):
    """The edge is the total count of taxed invoices, not the consistent ones.

    Two 8% invoices plus the 20% one give a median of 8%, so the odd invoice is
    still reported. Drop to two invoices in total and the detector has fewer
    than TAX_MIN_INVOICES, so it declines to infer a rate at all, which is the
    honest answer rather than a guess from a sample of two.
    """
    documents = [expense(DocType.TOLL_INVOICE, f"T-{i}", 400.0, 32.0)
                 for i in range(consistent_invoices)]
    documents.append(expense(DocType.TOLL_INVOICE, "ODD", 355.40, 71.08))

    assert len(exc.find_tax_inconsistencies(documents)) == expected


# ── the settlement tolerance, one dollar ─────────────────────────────────────

@pytest.mark.parametrize("paid, settled", [
    (999.00, True),    # one dollar light, exactly the edge, still settled
    (998.99, False),   # a cent past it, a short pay
])
def test_the_settlement_tolerance_holds_at_its_edge(paid, settled):
    documents = [load("L-1", 1000.0)]
    remit = remittance("R-1", [("L-1", paid, 0.0, None)], fee=0.0)
    documents.append(remit)

    assert allocate_remittance(remit, documents).allocations[0].settled_in_full is settled


@pytest.mark.parametrize("paid, findings", [(999.00, 0), (998.99, 1)])
def test_the_short_pay_detector_agrees_with_that_edge(paid, findings):
    """The tolerance must mean the same thing in both places it is felt."""
    documents = [load("L-1", 1000.0)]
    documents.append(remittance("R-1", [("L-1", paid, 0.0, None)], fee=0.0))

    assert len(exc.find_short_pays(allocate_all(documents))) == findings


# ── the digest's listing cap, 3 ──────────────────────────────────────────────

@pytest.mark.parametrize("letters, tail", [
    (3, None),                        # exactly the cap: nothing is elided
    (4, "and 1 more, all in the app."),
    (6, "and 3 more, all in the app."),
])
def test_the_digest_lists_three_then_counts_the_rest(letters, tail):
    """The literals here are written out rather than derived from TOP_ACTIONS,
    which is the exact mistake this file exists to stop repeating."""
    from archon.adapters.store import LocalStore
    from archon.runtime.close import run_close

    documents = []
    for index in range(letters):
        documents.append(load(f"L-{index}", 1000.0 + index))
    # No remittance at all, so every load is unpaid and every one earns a letter.
    result = run_close(period=PERIOD, documents=documents,
                       company="Test Haulage", store=LocalStore())

    assert len(result.drafts) == letters
    listed = [line for line in result.digest.body.splitlines()
              if line.startswith("  ") and "L-" in line]
    assert len(listed) == min(letters, 3)
    if tail is None:
        assert "more, all in the app" not in result.digest.body
    else:
        assert tail in result.digest.body


# ── the period boundary, which is a window too ───────────────────────────────

@pytest.mark.parametrize("date, out_of_period", [
    ("2026-06-30", True),    # the day before
    ("2026-07-01", False),   # the first day, inside
    ("2026-07-31", False),   # the last day, inside
    ("2026-08-01", True),    # the day after
])
def test_the_period_boundary_holds_on_both_edges(date, out_of_period):
    documents = [expense(DocType.TOLL_INVOICE, "T-1", 100.0, 8.0, date=date)]

    found = exc.find_out_of_period(documents, "2026-07")

    assert bool(found) is out_of_period
