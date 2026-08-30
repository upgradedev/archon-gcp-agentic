"""The second audit round, reproduced before anything is fixed.

Four gaps, each one a way for a real document or a real decision to disappear
without the close saying so. Every test here fails on 218f1b6.

The shape of all four is the same and it is worth naming: a rule that was right
for the case it was written for, applied to a case nobody checked.
"""
from __future__ import annotations

from archon.adapters import gcs
from archon.adapters.store import LocalStore
from archon.domain.models import DocType
from archon.domain.periods import belongs_to
from archon.runtime.close import run_close
from tests.conftest import PERIOD, bank, load, remittance


class FakeBlob:
    def __init__(self, name: str, data: bytes, generation: int = 1) -> None:
        self.name, self._data = name, data
        self.size, self.generation = len(data), generation

    def download_as_bytes(self) -> bytes:
        return self._data


class FakeClient:
    def __init__(self, blobs) -> None:
        self._blobs = blobs

    def list_blobs(self, bucket, prefix=""):
        return [b for b in self._blobs if b.name.startswith(prefix)]


LOAD_TEXT = (b"BELL RIDGE HAULAGE\nDocument Type: Load Confirmation\n\n"
             b"Load Number: L-1\nDate: 2026-07-02\nBroker: Test Broker\n"
             b"Linehaul: 1,000.00\nMiles: 100\nUnit: T-101\n")

INVOICE_TEXT = (b"NORTHWIND SUPPLY\nDocument Type: Purchase Invoice\n\n"
                b"Invoice Number: PI-1\nInvoice Date: 2026-07-14\n"
                b"Supplier: Northwind Supply\nNet Amount: 500.00\n"
                b"VAT 24%: 0.00\nTotal Due: 500.00\n")


def _mail(*pairs):
    return FakeClient([FakeBlob("mail/" + PERIOD + "/" + n, d) for n, d in pairs])


# -- 1. a real document whose name begins with an underscore ------------------

def test_an_underscore_named_invoice_is_not_treated_as_a_control_object():
    """`_invoice.txt` is a document. `_READY` is a signal. Only one is mail.

    The control-object rule was written for the batch marker and then widened
    to every name starting with an underscore, which is a convention and not a
    guarantee. A bookkeeper whose export names files `_invoice.txt` loses them
    silently: not read, not posted, and NON-blocking, so the month closes green
    over a purchase invoice nobody opened. That is the exact failure the
    fail-closed intake exists to prevent, reintroduced by the fix for something
    else.
    """
    client = _mail(("_invoice.txt", INVOICE_TEXT), ("load-L-1.txt", LOAD_TEXT))

    documents, _raw, manifest = gcs.read_gcs_period("b", PERIOD, client=client)

    names = {d.source_file for d in documents}
    assert "_invoice.txt" in names, (
        "an invoice whose filename starts with an underscore vanished before "
        "any gate could see it"
    )

    folded = [s for s in manifest["skipped"] if s["object"].endswith("_invoice.txt")]
    assert not folded or folded[0]["blocking"], (
        "if it is skipped at all it must BLOCK, never be waved through"
    )


def test_the_marker_family_is_still_recognised(monkeypatch):
    """The fix must not re-break what the rule was written for."""
    monkeypatch.setenv("ARCHON_BATCH_MARKER", "_READY")
    client = _mail(("_READY", b""), ("_READY2", b""), ("load-L-1.txt", LOAD_TEXT))

    documents, _raw, manifest = gcs.read_gcs_period("b", PERIOD, client=client)

    assert [d.source_file for d in documents] == ["load-L-1.txt"]
    controls = {s["object"].rsplit("/", 1)[-1] for s in manifest["skipped"]
                if not s["blocking"]}
    assert controls == {"_READY", "_READY2"}


# -- 2. a document nobody could date ------------------------------------------

def test_an_undated_document_does_not_quietly_belong_to_every_month():
    """`belongs_to` returned True when the date could not be read.

    Fail-OPEN, on the one question that decides whether money lands in this
    month or another one. An invoice dated "sometime in July", or in a format
    the parser refuses, was posted into whatever month happened to be closing.
    """
    assert belongs_to(PERIOD, "2026-07-14") is True
    assert belongs_to(PERIOD, "2026-06-30") is False
    assert belongs_to(PERIOD, None) is False, "undated is not 'this month'"
    assert belongs_to(PERIOD, "sometime in July") is False, (
        "an unreadable date is not a date"
    )


def test_an_undated_invoice_stays_out_of_the_books_and_blocks_the_month():
    """End to end: recorded, not posted, and the month refuses to close."""
    from archon.domain.extract import extract_document

    dated = load("L-1", 1000.0)
    undated = extract_document(
        "NORTHWIND SUPPLY\nDocument Type: Purchase Invoice\n\n"
        "Invoice Number: PI-9\nInvoice Date: sometime in July\n"
        "Supplier: Northwind\nNet Amount: 4,000.00\nVAT 24%: 0.00\n"
        "Total Due: 4,000.00\n",
        source_file="undated.txt", period=PERIOD)
    assert undated.doc_type is DocType.PURCHASE_INVOICE

    result = run_close(period=PERIOD, documents=[dated, undated],
                       company="Bell Ridge Haulage", store=LocalStore())

    assert result.statements.operating_expenses == 0.0, (
        "an undated invoice reached this month's costs"
    )
    assert result.outcome == "blocked", (
        "the month closed over a document nobody could date"
    )
    assert any("undated.txt" in (f.source_file or "") or "undated.txt" in f.message
               for f in result.findings), "no finding names the undated document"


# -- 3. a remittance from another month ---------------------------------------

def test_a_prior_period_remittance_does_not_swallow_this_months_bank_line():
    """`matching_remittance` looked at every arrived document, not this month's.

    June's remittance advice arrives late and sits in July's mail. July's bank
    line carries the same reference, because factors reuse their numbering. The
    inbound line was then read as the arrival of June's remittance and posted
    nothing, so July's cash was short by the whole credit and no gate said a
    word: G3 reconciles against the same wrong reading.
    """
    june_remittance = remittance("RA-1", [("L-99", 5000.0, 0.0, None)],
                                 date="2026-06-28", total=5000.0)
    july_load = load("L-1", 5000.0)
    july_credit = bank(5000.0, "RA-1", direction="in", date="2026-07-24",
                       counterparty="Test Broker")

    result = run_close(period=PERIOD,
                       documents=[june_remittance, july_load, july_credit],
                       company="Bell Ridge Haulage", store=LocalStore())

    assert result.statements.cash_in == 5000.00, (
        "July's own bank credit was folded into June's remittance; cash in "
        "reads " + format(result.statements.cash_in, ",.2f")
    )


def test_a_same_period_pair_is_still_deduped():
    """The fix must not reopen the double-post it was built to close."""
    docs = [load("L-1", 1000.0),
            remittance("RA-2", [("L-1", 1000.0, 0.0, None)], date="2026-07-24",
                       total=1000.0),
            bank(1000.0, "RA-2", direction="in", date="2026-07-25",
                 counterparty="Test Broker")]

    result = run_close(period=PERIOD, documents=docs,
                       company="Bell Ridge Haulage", store=LocalStore())

    assert result.statements.cash_in == 1000.00, "the same money was posted twice"


def test_the_same_amount_under_a_different_reference_is_a_different_payment():
    docs = [load("L-1", 1000.0), load("L-2", 1000.0),
            remittance("RA-3", [("L-1", 1000.0, 0.0, None)], date="2026-07-24",
                       total=1000.0),
            bank(1000.0, "RA-OTHER", direction="in", date="2026-07-25",
                 counterparty="Someone Else")]

    result = run_close(period=PERIOD, documents=docs,
                       company="Bell Ridge Haulage", store=LocalStore())

    assert result.statements.cash_in == 2000.00, (
        "two different payments of the same size were treated as one"
    )


# -- 4. drafts that cannot be read back ---------------------------------------

def test_saved_drafts_carry_the_run_they_belong_to():
    """`load_drafts` queries `where("run_id", "==", ...)`. `save_drafts` never
    wrote that field, so on Firestore the query matched nothing and the drafts
    were unreachable by the only method that reads them.

    The local store hides it by keying a dict on `run_id`, which is why the
    suite never noticed. Asserted on what is PERSISTED rather than on the
    retrieval, so it holds for both stores without an emulator.
    """
    from archon.domain.drafts import draft_all
    from archon.domain.models import ExceptionKind, Finding

    finding = Finding(kind=ExceptionKind.DUPLICATE_CHARGE, severity="error",
                      reference="INV-1", amount=100.0, message="x",
                      counterparty="Someone")
    drafts = draft_all([finding], "Bell Ridge Haulage")
    assert drafts

    store = LocalStore()
    store.save_drafts("2026-07-abc123", drafts)
    stored = store.load_drafts("2026-07-abc123")

    assert stored, "the drafts could not be read back at all"
    assert stored[0].get("run_id") == "2026-07-abc123", (
        "a stored draft does not say which run produced it, so the Firestore "
        "query that reads them back cannot find it"
    )
