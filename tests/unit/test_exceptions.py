"""The nine detectors. Each must fire on the defect and stay quiet without it.

Both halves matter. A detector that never fires is dead; a detector that always
fires is noise, and noise is what makes an owner stop reading the list.
"""
from __future__ import annotations

from archon.allocation import allocate_all
from archon.exceptions import (
    exposure,
    find_all,
    find_amount_outliers,
    find_duplicate_charges,
    find_out_of_period,
    find_payments_without_documents,
    find_short_pays,
    find_tax_inconsistencies,
    find_unpaid_loads,
    find_unreadable,
    find_unreconciled_remittances,
    parse_date,
)
from archon.models import DocType, ExceptionKind
from tests.conftest import (
    PERIOD,
    bank,
    expense,
    fuel,
    load,
    remittance,
    unreadable,
)

# ── payment without a document ───────────────────────────────────────────────

def test_a_payment_with_no_document_behind_it_is_reported():
    docs = [bank(1865.0, "INV-2291", counterparty="Vandalay Trailer Repair")]

    findings = find_payments_without_documents(docs)

    assert len(findings) == 1
    assert findings[0].kind is ExceptionKind.PAYMENT_WITHOUT_DOCUMENT
    assert findings[0].amount == 1865.0
    assert findings[0].counterparty == "Vandalay Trailer Repair"


def test_a_payment_matching_a_document_number_is_not_reported():
    docs = [expense(DocType.TOLL_INVOICE, "TOLL-1", 400.0, 32.0), bank(432.0, "TOLL-1")]
    assert find_payments_without_documents(docs) == []


def test_a_driver_settlement_payment_is_not_an_orphan():
    """The settlement sheet is the document. Reporting it would be noise."""
    docs = [bank(1560.0, "DS-1 SETTLEMENT", driver="Driver")]
    assert find_payments_without_documents(docs) == []


def test_money_arriving_is_never_an_orphan_payment():
    assert find_payments_without_documents([bank(500.0, "X", direction="in")]) == []


# ── short pay ────────────────────────────────────────────────────────────────

def test_a_silent_short_pay_is_reported_with_the_shortfall():
    docs = [load("L-1", 2260.0, accessorial=200.0)]
    docs.append(remittance("R-1", [("L-1", 2260.0, 0.0, None)], fee=0.0))

    findings = find_short_pays(allocate_all(docs))

    assert len(findings) == 1
    assert findings[0].amount == 200.0
    assert "No reason was given" in findings[0].message


def test_a_declared_deduction_is_quoted_back_rather_than_treated_as_silent():
    docs = [load("L-1", 2000.0)]
    docs.append(remittance("R-1", [("L-1", 1800.0, 100.0, "Late fee")], fee=0.0))

    findings = find_short_pays(allocate_all(docs))

    assert findings[0].amount == 100.0
    assert "Late fee" in findings[0].message


def test_a_load_paid_in_full_produces_no_short_pay():
    docs = [load("L-1", 2000.0)]
    docs.append(remittance("R-1", [("L-1", 2000.0, 0.0, None)], fee=0.0))
    assert find_short_pays(allocate_all(docs)) == []


def test_an_overpaid_load_is_not_reported_as_a_short_pay():
    docs = [load("L-1", 2000.0)]
    docs.append(remittance("R-1", [("L-1", 2100.0, 0.0, None)], fee=0.0))
    assert find_short_pays(allocate_all(docs)) == []


# ── remittance that does not reconcile ───────────────────────────────────────

def test_a_residual_is_reported_as_an_error():
    docs = [load("L-1", 1000.0)]
    docs.append(remittance("R-1", [("L-1", 1000.0, 0.0, None)], fee=30.0, total=900.0))

    findings = find_unreconciled_remittances(allocate_all(docs))

    assert findings[0].kind is ExceptionKind.REMITTANCE_UNRECONCILED
    assert findings[0].severity == "error"
    assert findings[0].amount == 70.0


def test_an_unknown_load_is_reported_even_when_the_arithmetic_closes():
    """The regression this exists for: a balanced remittance can still be
    paying for a load nobody here has a confirmation for."""
    docs = [load("L-1", 1000.0)]
    docs.append(remittance("R-1", [("L-1", 1000.0, 0.0, None), ("L-OLD", 500.0, 0.0, None)],
                           fee=0.0))
    results = allocate_all(docs)
    assert results[0].reconciles

    findings = find_unreconciled_remittances(results)

    assert [f.reference for f in findings] == ["L-OLD"]
    assert findings[0].severity == "warning"


def test_a_clean_remittance_reports_nothing():
    docs = [load("L-1", 1000.0)]
    docs.append(remittance("R-1", [("L-1", 1000.0, 0.0, None)], fee=30.0))
    assert find_unreconciled_remittances(allocate_all(docs)) == []


# ── unpaid loads ─────────────────────────────────────────────────────────────

def test_a_load_no_remittance_touched_is_reported_at_its_full_value():
    docs = [load("L-1", 1000.0), load("L-2", 2780.0)]
    docs.append(remittance("R-1", [("L-1", 1000.0, 0.0, None)], fee=0.0))

    findings = find_unpaid_loads(docs, allocate_all(docs))

    assert [f.reference for f in findings] == ["L-2"]
    assert findings[0].amount == 2780.0


def test_a_short_paid_load_is_not_also_reported_as_unpaid():
    """Detectors must not overlap; one problem, one line."""
    docs = [load("L-1", 2000.0)]
    docs.append(remittance("R-1", [("L-1", 1800.0, 0.0, None)], fee=0.0))
    assert find_unpaid_loads(docs, allocate_all(docs)) == []


# ── duplicate charge ─────────────────────────────────────────────────────────

def test_the_same_charge_twice_in_a_short_window_is_reported_once():
    docs = [fuel("F-1", [
        ("2026-07-06", "T-1", "Effingham IL", 137.6, 412.85, 33.03),
        ("2026-07-09", "T-1", "Effingham IL", 137.6, 412.85, 33.03),
    ])]

    findings = find_duplicate_charges(docs)

    assert len(findings) == 1
    assert findings[0].amount == 412.85


def test_the_same_charge_far_apart_is_not_a_duplicate():
    docs = [fuel("F-1", [
        ("2026-07-01", "T-1", "Effingham IL", 137.6, 412.85, 33.03),
        ("2026-07-28", "T-1", "Effingham IL", 137.6, 412.85, 33.03),
    ])]
    assert find_duplicate_charges(docs) == []


def test_the_same_amount_at_different_places_is_not_a_duplicate():
    docs = [fuel("F-1", [
        ("2026-07-06", "T-1", "Effingham IL", 137.6, 412.85, 33.03),
        ("2026-07-07", "T-2", "Jackson MS", 137.6, 412.85, 33.03),
    ])]
    assert find_duplicate_charges(docs) == []


# ── outlier ──────────────────────────────────────────────────────────────────

def test_a_charge_far_above_the_firms_own_norm_is_reported():
    docs = [fuel("F-1", [
        ("2026-07-02", "T-1", "A", 100.0, 400.00, 32.0),
        ("2026-07-04", "T-1", "B", 100.0, 410.00, 32.8),
        ("2026-07-06", "T-1", "C", 100.0, 390.00, 31.2),
        ("2026-07-08", "T-1", "D", 100.0, 405.00, 32.4),
        ("2026-07-17", "T-1", "E", 630.0, 1890.00, 151.2),
    ])]

    findings = find_amount_outliers(docs)

    assert [f.amount for f in findings] == [1890.00]


def test_no_outlier_is_claimed_without_enough_history_to_have_a_norm():
    docs = [fuel("F-1", [
        ("2026-07-02", "T-1", "A", 100.0, 400.00, 32.0),
        ("2026-07-17", "T-1", "B", 630.0, 1890.00, 151.2),
    ])]
    assert find_amount_outliers(docs) == []


def test_a_firm_that_habitually_spends_more_is_not_nagged():
    docs = [fuel("F-1", [(f"2026-07-0{i}", "T-1", "A", 600.0, 1800.00, 144.0)
                         for i in range(1, 6)])]
    assert find_amount_outliers(docs) == []


# ── tax inconsistency ────────────────────────────────────────────────────────

def test_a_tax_figure_contradicting_the_firms_own_rate_is_reported():
    docs = [
        expense(DocType.TOLL_INVOICE, "T-1", 400.0, 32.0),
        expense(DocType.TOLL_INVOICE, "T-2", 500.0, 40.0),
        expense(DocType.MAINTENANCE_INVOICE, "M-1", 1000.0, 80.0),
        expense(DocType.TOLL_INVOICE, "T-3", 355.40, 71.08),   # 20 percent, not 8
    ]

    findings = find_tax_inconsistencies(docs)

    assert [f.reference for f in findings] == ["T-3"]
    assert findings[0].amount == 42.65


def test_no_rate_is_inferred_from_too_few_invoices():
    docs = [expense(DocType.TOLL_INVOICE, "T-1", 400.0, 32.0),
            expense(DocType.TOLL_INVOICE, "T-2", 355.40, 71.08)]
    assert find_tax_inconsistencies(docs) == []


def test_consistent_tax_reports_nothing():
    docs = [expense(DocType.TOLL_INVOICE, f"T-{i}", 400.0, 32.0) for i in range(4)]
    assert find_tax_inconsistencies(docs) == []


# ── out of period ────────────────────────────────────────────────────────────

def test_a_document_dated_outside_the_month_is_reported():
    docs = [expense(DocType.TOLL_INVOICE, "T-1", 412.60, 33.01, date="2026-06-28")]

    findings = find_out_of_period(docs, PERIOD)

    assert findings[0].kind is ExceptionKind.OUT_OF_PERIOD
    assert findings[0].amount == 445.61


def test_the_last_day_of_the_month_is_inside_the_period():
    docs = [expense(DocType.TOLL_INVOICE, "T-1", 100.0, 8.0, date="2026-07-31")]
    assert find_out_of_period(docs, PERIOD) == []


def test_the_first_day_of_the_next_month_is_outside_it():
    docs = [expense(DocType.TOLL_INVOICE, "T-1", 100.0, 8.0, date="2026-08-01")]
    assert len(find_out_of_period(docs, PERIOD)) == 1


def test_december_rolls_the_year_over_correctly():
    docs = [expense(DocType.TOLL_INVOICE, "T-1", 100.0, 8.0, date="2026-12-31")]
    assert find_out_of_period(docs, "2026-12") == []


def test_an_unparseable_date_is_not_reported_as_out_of_period():
    """Not knowing when something happened is different from knowing it is late."""
    docs = [expense(DocType.TOLL_INVOICE, "T-1", 100.0, 8.0, date="sometime in July")]
    assert find_out_of_period(docs, PERIOD) == []


def test_parse_date_returns_none_rather_than_guessing():
    assert parse_date("not a date") is None
    assert parse_date(None) is None
    assert parse_date("2026-07-04").isoformat() == "2026-07-04"


# ── unreadable ───────────────────────────────────────────────────────────────

def test_an_unreadable_document_is_reported_with_no_amount():
    findings = find_unreadable([unreadable("scan.pdf", "no text layer")])

    assert findings[0].amount == 0.0
    assert "no text layer" in findings[0].message


# ── ranking and roll-up ──────────────────────────────────────────────────────

def test_findings_are_ranked_errors_first_then_by_money():
    docs = [
        load("L-1", 2780.0),                                    # unpaid, warning
        bank(1865.0, "INV-2291", counterparty="Someone"),       # orphan, error
        unreadable(),                                           # warning, no amount
    ]
    findings = find_all(docs, allocate_all(docs), PERIOD)

    assert findings[0].severity == "error"
    warnings = [f for f in findings if f.severity == "warning"]
    assert warnings == sorted(warnings, key=lambda f: -f.amount)


def test_exposure_totals_the_money_sitting_in_findings():
    docs = [bank(1000.0, "A", counterparty="X"), bank(500.0, "B", counterparty="Y")]
    assert exposure(find_payments_without_documents(docs)) == 1500.0


def test_a_clean_month_produces_no_findings_at_all():
    docs = [
        load("L-1", 1000.0),
        remittance("R-1", [("L-1", 1000.0, 0.0, None)], fee=30.0),
        expense(DocType.TOLL_INVOICE, "TOLL-1", 400.0, 32.0),
        bank(432.0, "TOLL-1"),
    ]
    assert find_all(docs, allocate_all(docs), PERIOD) == []
