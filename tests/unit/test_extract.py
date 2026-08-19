"""Turning a raw artifact into a structured document."""
from __future__ import annotations

from archon.extract import EXTRACTION_SCHEMA, classify, extract_document
from archon.models import DocType

LOAD = """MIDWEST FREIGHT EXCHANGE
RATE CONFIRMATION

Document Type: Load Confirmation
Load Number: L-7101
Date: 2026-07-02
Broker: Midwest Freight Exchange
Carrier Unit: T-101
Miles: 1180
Linehaul Rate: 2,450.00
Accessorial: 150.00
Total Payable: 2,600.00
"""

REMITTANCE = """Document Type: Broker Remittance
Remittance Number: MFX-RA-4417
Date: 2026-07-24
Broker: Midwest Freight Exchange
Factoring Fee: 577.35
Amount Credited: 18,667.65

LOAD LINES
Load L-7101  Gross 2,450.00  Deduction 0.00  Reason -
Load L-7102  Gross 3,100.00  Deduction 150.00  Reason Lumper fee
"""

FUEL = """Document Type: Fuel Card Statement
Statement Number: FCN-2026-07
Date: 2026-07-31
Supplier: Roadway Fuel Network

FUEL LINES
2026-07-02  T-101  Effingham IL  Gallons 142.6  Gross 428.30  Tax 34.26
2026-07-04  T-102  Texarkana AR  Gallons 138.9  Gross 416.70  Tax 33.34
"""

TOLL = """Document Type: Toll Invoice
Invoice Number: TOLL-88231
Date: 2026-06-28
Supplier: E-Z Pass Mid-Atlantic
Net Amount: 412.60
Tax: 33.01
Total: 445.61
"""

SETTLEMENT = """Document Type: Driver Settlement
Settlement Number: DS-2026-07-01
Date: 2026-07-31
Driver: Driver 1
Unit: T-101
Gross Pay: 2,179.10
Tax Withheld: 392.24
Other Deductions: 87.16
Net Pay: 1,699.70
"""

BANK = """Document Type: Bank Transaction
Date: 2026-07-28
Direction: out
Amount: 1,865.00
Reference: INV-2291
Paid To: Vandalay Trailer Repair
"""

BAD_SCAN = """Document Type: Unreadable
Date: 2026-07-19
Source: scan_20260719_113402.pdf
Failure: no text layer, 3 pages, image only
"""


def test_every_family_classifies_from_its_declared_type():
    assert classify(LOAD) is DocType.LOAD_CONFIRMATION
    assert classify(REMITTANCE) is DocType.BROKER_REMITTANCE
    assert classify(FUEL) is DocType.FUEL_CARD_STATEMENT
    assert classify(TOLL) is DocType.TOLL_INVOICE
    assert classify(SETTLEMENT) is DocType.DRIVER_SETTLEMENT
    assert classify(BANK) is DocType.BANK_TRANSACTION
    assert classify(BAD_SCAN) is DocType.UNREADABLE


def test_an_artifact_with_no_declared_type_is_unknown_not_guessed():
    assert classify("some text about a truck and an invoice") is DocType.UNKNOWN


def test_a_load_confirmation_carries_its_rate_miles_and_unit():
    doc = extract_document(LOAD, "load.txt", "2026-07")

    assert doc.load_ref == "L-7101"
    assert doc.net_amount == 2450.0
    assert doc.accessorial == 150.0
    assert doc.gross_amount == 2600.0
    assert doc.miles == 1180.0
    assert doc.truck == "T-101"
    assert doc.broker == "Midwest Freight Exchange"


def test_a_remittance_carries_every_load_line_with_its_reason():
    doc = extract_document(REMITTANCE, "remit.txt", "2026-07")

    assert doc.remittance_total == 18667.65
    assert doc.factoring_fee == 577.35
    assert [line.load_ref for line in doc.lines] == ["L-7101", "L-7102"]
    assert doc.lines[1].deduction == 150.0
    assert doc.lines[1].reason == "Lumper fee"


def test_a_dash_is_read_as_no_reason_rather_than_as_a_reason():
    doc = extract_document(REMITTANCE, "remit.txt", "2026-07")
    assert doc.lines[0].reason is None


def test_a_fuel_statement_carries_each_fill_and_totals_them():
    doc = extract_document(FUEL, "fuel.txt", "2026-07")

    assert len(doc.fuel_lines) == 2
    assert doc.fuel_lines[0].truck == "T-101"
    assert doc.fuel_lines[0].location == "Effingham IL"
    assert doc.gross_amount == 845.0
    assert doc.tax_amount == 67.6
    assert doc.net_amount == 777.4


def test_an_expense_invoice_carries_net_tax_and_gross():
    doc = extract_document(TOLL, "toll.txt", "2026-07")

    assert (doc.net_amount, doc.tax_amount, doc.gross_amount) == (412.60, 33.01, 445.61)
    assert doc.counterparty == "E-Z Pass Mid-Atlantic"
    assert doc.date == "2026-06-28"


def test_a_settlement_carries_the_gross_the_withholding_and_the_net():
    doc = extract_document(SETTLEMENT, "set.txt", "2026-07")

    assert doc.driver_gross == 2179.10
    assert doc.tax_withheld == 392.24
    assert doc.driver_net == 1699.70
    assert doc.truck == "T-101"


def test_a_bank_line_carries_its_direction_reference_and_who_was_paid():
    doc = extract_document(BANK, "bank.txt", "2026-07")

    assert doc.direction == "out"
    assert doc.net_amount == 1865.0
    assert doc.reference == "INV-2291"
    assert doc.counterparty == "Vandalay Trailer Repair"


def test_an_unreadable_scan_keeps_its_real_filename_and_its_reason():
    doc = extract_document(BAD_SCAN, "scan.txt", "2026-07")

    assert doc.doc_type is DocType.UNREADABLE
    assert doc.source_file == "scan_20260719_113402.pdf"
    assert "no text layer" in doc.failure_reason


def test_thousands_separators_are_parsed_not_truncated():
    """The bug that would silently divide a month by a thousand."""
    doc = extract_document(REMITTANCE, "remit.txt", "2026-07")
    assert doc.remittance_total == 18667.65


def test_the_first_occurrence_of_a_label_wins():
    """A repeated footer total must not overwrite the header it was read from."""
    text = TOLL + "\nNet Amount: 9,999.99\n"
    assert extract_document(text, "toll.txt", "2026-07").net_amount == 412.60


def test_garbage_returns_a_document_rather_than_raising():
    """One bad scan in forty must not cost the owner their month end."""
    doc = extract_document("\x00\x01 not a document at all", "junk.bin", "2026-07")

    assert doc.doc_type is DocType.UNKNOWN
    assert doc.source_file == "junk.bin"


def test_an_empty_artifact_returns_a_document():
    assert extract_document("", "empty.txt", "2026-07").doc_type is DocType.UNKNOWN


def test_the_gemini_schema_names_only_fields_the_document_model_has():
    """The two extraction paths must not drift apart unnoticed."""
    from archon.models import Document

    fields = set(Document.__dataclass_fields__)
    assert set(EXTRACTION_SCHEMA["properties"]) <= fields | {"doc_type"}


def test_the_schema_enumerates_exactly_the_known_document_types():
    assert set(EXTRACTION_SCHEMA["properties"]["doc_type"]["enum"]) == {
        t.value for t in DocType
    }
