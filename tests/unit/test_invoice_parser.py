"""Invoices: the document family every business has and a haulier's set did not.

The gap this closes was found by asking the plainest possible question: if the
owner drops their own company's files in, does it work? It did not. Two
consulting invoices came back UNKNOWN, posted nothing, and the month closed at
zero revenue reporting every gate passed. G6 now blocks that, and this file is
the other half: recognising and posting the documents rather than only refusing
them.

Three ideas here are recreated as patterns from the author's earlier financial
extractors, disclosed in the README, with no donor code:

- a diacritic fold, so one unaccented literal matches accented and uppercase
  text without a table of variants per language;
- an alias table where a DISAGREEMENT between two spellings of the same field
  returns nothing, instead of first-key-wins silently inventing a figure;
- the evidence-versus-candidate boundary: prose that merely mentions an invoice
  is never handed to a field extractor, because a model or a regex asked to
  find an invoice number in a sentence will find one.
"""
from __future__ import annotations

import pathlib

import pytest

from archon.domain.extract import _fold, _iso_date, _money, classify, extract_document
from archon.domain.models import DocType


def invoice(kind="SALES INVOICE", *, number="INV-2026-0184", date="14/07/2026",
            party_label="Bill To", party="Kestrel Software SA",
            net="5,890.00", vat="1,413.60", total="7,303.60"):
    return (f"NORTHWIND CONSULTING LTD\n{kind}\n\n"
            f"Invoice Number: {number}\n"
            f"Invoice Date: {date}\n"
            f"{party_label}: {party}\n"
            f"Net Amount: {net}\n"
            f"VAT 24%: {vat}\n"
            f"Total Due: {total}\n")


# ── the money parser, one function for two notations ─────────────────────────

@pytest.mark.parametrize("written, expected", [
    ("5,890.00", 5890.00),          # US thousands
    ("5.890,00", 5890.00),          # European thousands
    ("€1.234,56", 1234.56),
    ("$1,234.56", 1234.56),
    ("7 303,60 EUR", 7303.60),      # space as a thousands separator
    ("1234.56", 1234.56),
    ("1,500", 1500.00),             # one separator, three digits: thousands
    ("1,50", 1.50),                 # one separator, two digits: decimal
    ("(250.00)", -250.00),          # accounting negative
    ("-99.99", -99.99),
])
def test_one_money_parser_reads_both_notations(written, expected):
    """Deliberately ONE function. Two locale parsers sharing call sites is how
    the same string becomes two different amounts depending on which path
    reached it, which is worse than either being wrong consistently."""
    assert _money(written) == expected


def test_money_that_is_not_a_number_is_none_rather_than_zero():
    for junk in ("see attached", "", "n/a", None):
        assert _money(junk) is None


# ── classification ───────────────────────────────────────────────────────────

def test_a_sales_invoice_is_recognised_without_a_document_type_label():
    assert classify(invoice()) is DocType.SALES_INVOICE


def test_a_purchase_invoice_is_recognised_and_pointed_the_other_way():
    text = invoice("PURCHASE INVOICE", party_label="Supplier", party="Cloudhost Ltd")

    assert classify(text) is DocType.PURCHASE_INVOICE


def test_direction_is_decided_by_who_the_document_addresses():
    """A bare `INVOICE` with no family word. Getting this backwards books
    revenue as a cost, so the tie breaks on evidence, never on a default."""
    assert classify(invoice("INVOICE", party_label="Bill To")) is DocType.SALES_INVOICE
    assert classify(invoice("INVOICE", party_label="Supplier")) is DocType.PURCHASE_INVOICE


def test_accented_and_uppercase_greek_still_classifies():
    """The fold is what makes one literal match every casing and accenting."""
    greek = ("ΝΟΡΘΓΟΥΪΝΤ ΕΠΕ\nΤΙΜΟΛΟΓΙΟ ΠΩΛΗΣΗΣ\n\n"
             "Invoice Number: INV-1\nNet Amount: 100,00\nTotal Due: 124,00\n")

    assert classify(greek) is DocType.SALES_INVOICE
    assert "τιμολογιο πωλησης" in _fold(greek)


# ── refusals, which are the point rather than a gap ─────────────────────────

@pytest.mark.parametrize("heading", [
    "CREDIT NOTE", "CREDIT MEMO", "PROFORMA INVOICE",
    "QUOTATION", "PURCHASE ORDER", "STATEMENT OF ACCOUNT",
])
def test_documents_that_look_like_invoices_and_are_not_stay_unknown(heading):
    """Every downstream detector in this product is sign-naive: duplicate
    charges skip amounts at or below zero, the payables register skips
    anything under a cent, outliers skip non-positive. So a credit note booked
    as a purchase invoice does not merely misfile, it OVERSTATES the expense
    and then hides from every check that would have caught it.

    A proforma or an order creates no obligation at all, and posting one
    invents a debt. These are recognised precisely well enough to be refused:
    they stay UNKNOWN, G6 blocks the month, and a person looks.
    """
    assert classify(invoice(heading)) is DocType.UNKNOWN


def test_prose_that_merely_mentions_an_invoice_is_not_an_invoice():
    """Evidence, never a candidate. A cover note is a sentence about a
    document, not the document."""
    note = "Hi Maria, please find the invoice attached for July. Thanks, Tom"

    assert classify(note) is DocType.UNKNOWN


def test_a_document_with_no_figure_anywhere_is_refused():
    assert classify("SALES INVOICE\nBill To: Someone\nTerms: 30 days") is DocType.UNKNOWN


# ── field extraction ─────────────────────────────────────────────────────────

def test_every_field_comes_off_a_european_invoice():
    doc = extract_document(
        invoice(net="5.890,00", vat="1.413,60", total="7.303,60 EUR"),
        source_file="INV-0184.txt", period="2026-07")

    assert doc.doc_type is DocType.SALES_INVOICE
    assert doc.reference == "INV-2026-0184"
    assert doc.date == "2026-07-14"
    assert doc.counterparty == "Kestrel Software SA"
    assert doc.net_amount == 5890.00
    assert doc.tax_amount == 1413.60
    assert doc.gross_amount == 7303.60
    assert round(doc.net_amount + doc.tax_amount, 2) == doc.gross_amount


def test_a_vat_label_carrying_its_rate_is_still_the_vat_field():
    """Real invoices write `VAT 24%`, not `VAT`. The prefix match is guarded so
    that `Invoice Date` never satisfies the `invoice` alias for the reference,
    which would file the date as the invoice number."""
    doc = extract_document(invoice(), period="2026-07")

    assert doc.tax_amount == 1413.60
    assert doc.reference == "INV-2026-0184"     # not the date


def test_two_labels_disagreeing_leave_the_field_empty():
    """First-alias-wins is the obvious implementation and it is wrong. A
    document carrying `Total Due` and `Grand Total` with different figures is
    ambiguous, and picking whichever the table listed first invents a number."""
    text = (invoice(total="7,303.60") + "Grand Total: 9,999.99\n")

    assert extract_document(text, period="2026-07").gross_amount is None


def test_the_same_number_written_twice_is_not_a_disagreement():
    text = (invoice(total="7,303.60") + "Amount Due: 7303.60\n")

    assert extract_document(text, period="2026-07").gross_amount == 7303.60


def test_the_net_is_derived_when_only_the_total_and_the_vat_are_stated():
    """Arithmetic on figures the document stated is not a guess."""
    text = ("SALES INVOICE\nInvoice Number: INV-9\nBill To: X\n"
            "VAT 24%: 240.00\nTotal Due: 1,240.00\n")

    assert extract_document(text, period="2026-07").net_amount == 1000.00


# ── dates ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("written, iso", [
    ("2026-07-14", "2026-07-14"),
    ("14/07/2026", "2026-07-14"),
    ("14.07.2026", "2026-07-14"),
    ("14 July 2026", "2026-07-14"),
])
def test_unambiguous_dates_are_read(written, iso):
    assert _iso_date(written) == iso


def test_an_ambiguous_slashed_date_is_refused_rather_than_guessed():
    """03/04/2026 is two real readings thirty days apart, and thirty days moves
    an invoice across a period boundary and across an ageing bucket. Guessing
    day-first because most of the world writes that way is a coin flip dressed
    up as a default."""
    assert _iso_date("03/04/2026") is None
    assert _iso_date("12/11/2026") is None
    # Unambiguous because no month is the fourteenth.
    assert _iso_date("14/07/2026") == "2026-07-14"


def test_an_unreadable_date_leaves_the_document_undated_not_stamped_with_today():
    doc = extract_document(invoice(date="sometime in July"), period="2026-07")

    assert doc.date != "sometime in July"
    assert doc.reference == "INV-2026-0184"     # the rest still parsed


# ── the guarantee that the haulage corpus is untouched ──────────────────────

def test_every_bundled_artifact_classifies_on_its_declared_label():
    """The zero-regression proof, structural rather than argumentative.

    Every bundled file carries a `Document Type:` line and is therefore decided
    by the declared-label lookup, which none of the invoice machinery can
    reach. Keyword scoring is unreachable for the corpus BY CONSTRUCTION, so
    the guarantee cannot rot as the keyword table grows.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "corpus"
    files = sorted(root.glob("*/*.txt"))

    assert len(files) >= 40, "the corpus shrank; this guarantee covers nothing"
    for path in files:
        text = path.read_text(encoding="utf-8")
        labels = [ln for ln in text.splitlines()
                  if ln.lower().startswith("document type:")]
        assert labels, f"{path.name} has no Document Type line and would fall to scoring"
        assert classify(text) is not DocType.UNKNOWN, path.name
