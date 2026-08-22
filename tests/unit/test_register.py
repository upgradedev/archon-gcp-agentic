"""The open items register: who has not paid us, and who we have not paid.

The gap this closed: the product reported one accounts-receivable total rolled
up from the journal. A total tells an owner the size of the problem and nothing
about its shape, and a total cannot be chased. These assert the shape.
"""
from __future__ import annotations

import pytest

from archon.domain import register
from archon.domain.allocation import allocate_all
from archon.domain.models import DocType
from tests.conftest import PERIOD, bank, expense, fuel, load, remittance, settlement


def build(documents):
    return register.build(documents, allocate_all(documents), PERIOD)


# ── receivables ──────────────────────────────────────────────────────────────

def test_a_load_nobody_paid_is_open_in_full():
    documents = [load("L-1", 2000.0, accessorial=150.0, date="2026-07-10")]

    item = build(documents).receivables[0]

    assert item.open_amount == 2150.0
    assert item.paid == 0.0
    assert item.note == "no payment received"
    assert item.counterparty == "Test Broker"


def test_a_load_paid_in_full_is_not_open():
    documents = [load("L-1", 2000.0)]
    documents.append(remittance("R-1", [("L-1", 2000.0, 0.0, None)], fee=60.0))

    assert build(documents).receivables == []


def test_a_short_paid_load_leaves_the_difference_open():
    """The case a total hides. Money arrived, so the load looks settled, and
    the 200 that did not arrive disappears with it."""
    documents = [load("L-1", 2260.0, accessorial=200.0)]
    documents.append(remittance("R-1", [("L-1", 2260.0, 0.0, None)], fee=0.0))

    item = build(documents).receivables[0]

    assert item.invoiced == 2460.0
    assert item.paid == 2260.0
    assert item.open_amount == 200.0
    assert item.partially_paid
    assert item.note == "paid short"


def test_a_stated_deduction_counts_as_settled_not_as_open():
    """The broker held back a lumper fee and said so. That is an argument to
    have, not an open receivable to chase."""
    documents = [load("L-1", 2000.0)]
    documents.append(remittance("R-1", [("L-1", 1850.0, 150.0, "Lumper fee")], fee=0.0))

    assert build(documents).receivables == []


# ── payables ─────────────────────────────────────────────────────────────────

def test_a_bill_with_no_payment_against_it_is_open():
    documents = [expense(DocType.TOLL_INVOICE, "TOLL-1", 400.0, 32.0)]

    item = build(documents).payables[0]

    assert item.open_amount == 432.0
    assert item.counterparty == "Test Supplier"
    assert item.note == "no payment recorded against it"


def test_a_bill_a_bank_line_references_is_settled():
    documents = [expense(DocType.TOLL_INVOICE, "TOLL-1", 400.0, 32.0),
                 bank(432.0, "TOLL-1")]

    assert build(documents).payables == []


def test_a_fuel_statement_is_a_payable_until_it_is_paid():
    statement = fuel("FCN-1", [("2026-07-03", "T-1", "A", 100.0, 300.0, 24.0)])

    assert build([statement]).payables[0].open_amount == 300.0
    assert build([statement, bank(300.0, "FCN-1")]).payables == []


def test_a_driver_settlement_is_owed_until_the_net_leaves_the_bank():
    sheet = settlement("DS-1", 2000.0, 360.0, 80.0, 1560.0, driver="Driver A")

    item = build([sheet]).payables[0]
    assert item.open_amount == 1560.0          # the net, not the gross
    assert item.counterparty == "Driver A"

    paid = bank(1560.0, "DS-1 SETTLEMENT", driver="Driver A")
    assert build([sheet, paid]).payables == []


# ── the totals an owner reads first ─────────────────────────────────────────

def test_both_sides_and_the_net_position():
    documents = [
        load("L-1", 3000.0),
        expense(DocType.TOLL_INVOICE, "TOLL-1", 400.0, 32.0),
        expense(DocType.MAINTENANCE_INVOICE, "MNT-1", 900.0, 72.0),
    ]

    reg = build(documents)

    assert reg.owed_to_us == 3000.0
    assert reg.owed_by_us == 1404.0
    assert reg.net_position == 1596.0


def test_open_amounts_group_by_counterparty_because_that_is_who_gets_called():
    documents = [
        load("L-1", 1000.0, broker="Broker A"),
        load("L-2", 2000.0, broker="Broker A"),
        load("L-3", 500.0, broker="Broker B"),
    ]

    by_broker = build(documents).by_counterparty("receivable")

    assert by_broker == {"Broker A": 3000.0, "Broker B": 500.0}
    assert list(by_broker) == ["Broker A", "Broker B"]      # largest first


def test_the_biggest_open_item_is_listed_first():
    documents = [load("L-small", 100.0), load("L-big", 9000.0)]

    assert [i.reference for i in build(documents).receivables] == ["L-big", "L-small"]


# ── ageing ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("document_date, bucket", [
    ("2026-07-25", "current"),        # 6 days to period end
    ("2026-07-17", "current"),        # 14 days, the edge
    ("2026-07-16", "15-30 days"),     # 15 days, one past it
    ("2026-06-20", "31-60 days"),
    ("2026-04-10", "over 60 days"),
])
def test_age_is_bucketed_from_the_document_date(document_date, bucket):
    documents = [load("L-1", 1000.0, date=document_date)]

    assert build(documents).receivables[0].bucket == bucket


def test_age_is_measured_to_the_period_end_not_to_today():
    """A close is a statement about a month. Re-running last July in December
    must not silently age everything by five months, or the same run gives two
    answers and the books stop being reproducible."""
    documents = [load("L-1", 1000.0, date="2026-07-01")]

    item = build(documents).receivables[0]

    assert item.age_days == 30          # 1 July to 31 July, whenever this runs


def test_an_undated_document_is_reported_rather_than_guessed_at():
    documents = [load("L-1", 1000.0, date=None)]

    item = build(documents).receivables[0]

    assert item.age_days is None
    assert item.bucket == "undated"
    assert item.open_amount == 1000.0   # still owed, still listed


def test_the_aged_summary_only_names_buckets_that_hold_something():
    documents = [load("L-1", 1000.0, date="2026-07-25")]

    assert build(documents).aged("receivable") == {"current": 1000.0}


def test_the_register_serialises_for_the_page():
    documents = [load("L-1", 1000.0), expense(DocType.TOLL_INVOICE, "T-1", 100.0, 8.0)]

    payload = build(documents).to_dict()

    assert set(payload) >= {"owed_to_us", "owed_by_us", "net_position", "receivables",
                            "payables", "receivables_by_counterparty",
                            "receivables_aged", "payables_aged"}
    assert payload["receivables"][0]["reference"] == "L-1"


def test_an_empty_month_has_an_empty_register():
    reg = build([])

    assert reg.owed_to_us == 0.0
    assert reg.owed_by_us == 0.0
    assert reg.net_position == 0.0
