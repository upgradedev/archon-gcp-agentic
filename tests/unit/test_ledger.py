"""Double-entry posting and the period roll-up."""
from __future__ import annotations

from archon.domain.ledger import Ledger
from archon.domain.models import Account, DocType
from tests.conftest import (
    PERIOD,
    bank,
    expense,
    fuel,
    load,
    remittance,
    settlement,
    unreadable,
)


def _ledger(*docs) -> Ledger:
    ledger = Ledger(period=PERIOD, company="Test Haulage")
    ledger.add_all(list(docs))
    return ledger


def test_every_posting_shape_balances():
    ledger = _ledger(
        load("L-1", 2000.0, accessorial=150.0),
        remittance("R-1", [("L-1", 2150.0, 0.0, None)], fee=64.5),
        fuel("F-1", [("2026-07-03", "T-1", "Somewhere", 100.0, 300.0, 24.0)]),
        expense(DocType.TOLL_INVOICE, "TOLL-1", 400.0, 32.0),
        expense(DocType.MAINTENANCE_INVOICE, "MNT-1", 900.0, 72.0),
        expense(DocType.INSURANCE_INVOICE, "INS-1", 1000.0, 80.0),
        settlement("DS-1", 2000.0, 360.0, 80.0, 1560.0),
        bank(500.0, "TOLL-1"),
        bank(1560.0, "DS-1 SETTLEMENT", driver="Driver"),
    )
    assert ledger.all_entries_balanced()


def test_a_load_is_revenue_and_a_receivable_before_anyone_pays():
    ledger = _ledger(load("L-1", 2000.0, accessorial=150.0))
    statements = ledger.statements()

    assert statements.revenue_linehaul == 2000.0
    assert statements.revenue_accessorial == 150.0
    assert statements.accounts_receivable == 2150.0
    assert statements.cash_in == 0.0


def test_a_remittance_clears_receivables_at_gross_not_at_what_landed():
    """Book it at the credited amount and receivables never clear."""
    ledger = _ledger(
        load("L-1", 2000.0),
        remittance("R-1", [("L-1", 2000.0, 0.0, None)], fee=60.0),
    )
    statements = ledger.statements()

    assert statements.cash_in == 1940.0            # what the bank saw
    assert statements.factoring_fees == 60.0       # what the factor took
    assert statements.accounts_receivable == 0.0   # cleared at the full 2000


def test_fuel_tax_is_held_apart_from_the_fuel_cost():
    """Recoverable tax must not inflate cost per mile."""
    ledger = _ledger(fuel("F-1", [
        ("2026-07-03", "T-1", "A", 100.0, 300.0, 24.0),
        ("2026-07-09", "T-1", "B", 100.0, 300.0, 24.0),
    ]))
    statements = ledger.statements()

    assert statements.fuel == 552.0                       # 600 gross less 48 tax
    assert statements.accounts_payable == 600.0           # the supplier is owed gross


def test_a_settlement_expenses_the_gross_and_leaves_the_rest_payable():
    ledger = _ledger(settlement("DS-1", 3000.0, 540.0, 120.0, 2340.0))
    balances = ledger.balances()

    assert ledger.statements().driver_pay == 3000.0
    assert balances[Account.DRIVER_PAY_PAYABLE] == (0.0, 2340.0)
    assert balances[Account.TAX_WITHHELD_PAYABLE] == (0.0, 540.0)


def test_an_unreadable_document_posts_no_lines():
    """The refusal is the feature. An estimated invoice would balance and lie."""
    ledger = _ledger(unreadable())

    assert ledger.entries[0].lines == []
    assert "not posted" in ledger.entries[0].memo


def test_an_unknown_document_posts_no_lines():
    ledger = Ledger(period=PERIOD)
    from archon.domain.models import Document

    ledger.add(Document(doc_type=DocType.UNKNOWN, period=PERIOD, source_file="odd.txt"))

    assert ledger.entries[0].lines == []


def test_profit_is_revenue_less_every_operating_line():
    ledger = _ledger(
        load("L-1", 10_000.0),
        expense(DocType.TOLL_INVOICE, "TOLL-1", 500.0, 40.0),
        expense(DocType.MAINTENANCE_INVOICE, "MNT-1", 1_000.0, 80.0),
        expense(DocType.INSURANCE_INVOICE, "INS-1", 2_000.0, 160.0),
        settlement("DS-1", 4_000.0, 720.0, 160.0, 3_120.0),
    )
    statements = ledger.statements()

    assert statements.operating_expenses == 7_500.0
    assert statements.net_profit == 2_500.0


def test_per_mile_figures_come_from_the_miles_actually_run():
    ledger = _ledger(
        load("L-1", 2000.0, miles=1000, truck="T-1"),
        load("L-2", 3000.0, miles=1000, truck="T-2"),
        expense(DocType.INSURANCE_INVOICE, "INS-1", 1000.0, 0.0),
    )
    statements = ledger.statements()

    assert statements.total_miles == 2000.0
    assert statements.revenue_per_mile == 2.5
    assert statements.cost_per_mile == 0.5


def test_per_mile_figures_are_none_rather_than_zero_when_no_miles_ran():
    """Dividing by nothing is not a figure, and a zero here would read as one."""
    statements = _ledger(expense(DocType.INSURANCE_INVOICE, "INS-1", 1000.0, 0.0)).statements()

    assert statements.cost_per_mile is None
    assert statements.revenue_per_mile is None


def test_per_truck_attributes_only_what_can_honestly_be_attributed():
    ledger = _ledger(
        load("L-1", 2000.0, miles=1000, truck="T-1"),
        load("L-2", 1000.0, miles=500, truck="T-2"),
        fuel("F-1", [
            ("2026-07-03", "T-1", "A", 100.0, 300.0, 24.0),
            ("2026-07-05", "T-2", "B", 100.0, 200.0, 16.0),
        ]),
        expense(DocType.MAINTENANCE_INVOICE, "MNT-1", 400.0, 32.0, truck="T-1"),
        # Firm-level, and deliberately not spread across the trucks.
        expense(DocType.INSURANCE_INVOICE, "INS-1", 900.0, 72.0),
        settlement("DS-1", 1000.0, 180.0, 40.0, 780.0, truck="T-1"),
    )
    per_truck = ledger.per_truck()

    assert per_truck["T-1"] == {
        "miles": 1000.0, "revenue": 2000.0, "fuel": 276.0, "maintenance": 400.0,
        "direct_cost": 676.0, "cost_per_mile": 0.676, "revenue_per_mile": 2.0,
    }
    assert per_truck["T-2"]["direct_cost"] == 184.0
    assert "insurance" not in per_truck["T-1"]


def test_a_note_names_what_factoring_took_before_the_bank_saw_it():
    ledger = _ledger(
        load("L-1", 2000.0),
        remittance("R-1", [("L-1", 2000.0, 0.0, None)], fee=60.0),
    )
    assert any("Factoring took" in note for note in ledger.statements().notes)


def test_a_note_names_documents_left_unposted():
    assert any("could not be read" in note
               for note in _ledger(unreadable()).statements().notes)


def test_documents_of_filters_by_family():
    ledger = _ledger(load("L-1", 100.0), load("L-2", 200.0), unreadable())
    assert len(ledger.documents_of(DocType.LOAD_CONFIRMATION)) == 2
    assert len(ledger.documents_of(DocType.UNREADABLE)) == 1


def test_settled_and_outstanding_loads_add_up_to_the_loads_there_were():
    """"8 of 9 loads settled, 2 still outstanding" is ten loads out of nine.

    `settled_load_refs` returns every reference any remittance paid towards,
    and TFX-RA-4417 pays L-7099, which arrived in nobody's mail this month.
    Counting that as a settled LOAD inflated the numerator against a
    denominator built from the load confirmations actually received, and put a
    sum that does not add up on the first line a judge reads in the terminal.

    The stray reference is not being hidden by this: G2 reconciles it and the
    `remittance_unreconciled` detector reports it by name. It is simply not one
    of this month's loads, so it cannot be counted as one of them being paid.
    """
    from archon.cli import read_period
    from archon.domain import allocation as allocation_mod
    from archon.runtime.close import run_close

    documents, raw = read_period("2026-07", root=None)
    result = run_close(period="2026-07", documents=documents,
                       company="Bell Ridge Haulage", raw_texts=raw)

    known = allocation_mod.loads_by_ref(documents)
    results = result.allocations
    settled = allocation_mod.settled_load_refs(results)
    outstanding = allocation_mod.unsettled_loads(documents, results)

    # The bundled month is built to contain this case, so the test is not
    # hypothetical: a reference is paid that has no load behind it.
    assert settled - known.keys(), "the month lost its unreconciled remittance line"

    reconcile = next(s for s in result.journal.steps if s.name == "reconcile")
    assert reconcile.counts["settled"] + reconcile.counts["outstanding"] == len(known), (
        f"{reconcile.counts['settled']} settled + {reconcile.counts['outstanding']} "
        f"outstanding != {len(known)} loads. The step note reads: "
        f"{reconcile.detail!r}"
    )
    assert len(outstanding) == 2
