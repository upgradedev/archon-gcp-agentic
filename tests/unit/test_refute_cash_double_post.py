"""Independent attempt to REFUTE audit finding 1.1 (cash counted twice).

The other agent's repro hand-built its three documents with the conftest
helpers. The obvious refutation is "no real intake path ever produces that
input", so nothing below uses a helper. Every document here is raw bank/broker
text written into a directory and read back through `runtime.mailbox
.read_period`, which is the same `extract_document` call the GCS mailbox makes
(`adapters/gcs.read_gcs_period`) when a bookkeeper's objects land in the bucket.

Two scenarios, both from text:

    * `test_bundled_month_*` takes the shipped `corpus/2026-07/` month exactly
      as it is and adds ONE more line of the same bank statement: the deposit
      for remittance MFX-RA-4417, which the remittance advice says landed on
      2026-07-24 at 18,667.65. Nothing else changes.
    * `test_minimal_month_*` writes a three-document month from scratch, so the
      arithmetic is small enough to read.

The refutation failed. Both doubled the cash, and every gate still passed, so
the finding stood. `ledger.matching_remittance` closed it: a bank line that is
the arrival of a remittance already in the period posts nothing, and G3 leaves
it out of the observed sum it checks the books against. These tests are what
hold that in place.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from archon.adapters.store import LocalStore
from archon.domain.extract import extract_document
from archon.domain.models import DocType
from archon.domain.validation import summary as gate_summary
from archon.runtime.close import run_close
from archon.runtime.journal import FixedClock
from archon.runtime.mailbox import CORPUS_ROOT, read_period

PERIOD = "2026-07"

#: The deposit half of remittance MFX-RA-4417, exactly as the bank prints it.
#: Same layout as every bank line already in `corpus/2026-07/`, only the
#: direction differs. The amount is the "Amount Credited" the broker's own
#: advice states.
DEPOSIT_LINE = """FIRST PLAINS BANK
ACCOUNT ACTIVITY

Document Type: Bank Transaction
Date: 2026-07-24
Direction: in
Amount: 18,667.65
Reference: MFX-RA-4417
Description: Midwest Freight Exchange
"""

REMITTED = 18_667.65


def close_mailbox(root: Path):
    """Drive the real intake and the real close over a directory of text."""
    documents, _raw = read_period(PERIOD, root=root)
    return documents, run_close(period=PERIOD, documents=documents,
                                company="Bell Ridge Haulage",
                                store=LocalStore(), clock=FixedClock())


def books(result) -> str:
    s = result.statements
    return (
        f"\n  outcome             = {result.outcome!r} ({result.outcome_reason})"
        f"\n  gates               = {gate_summary(result.gates)}"
        f"\n  revenue             = {s.revenue:,.2f}"
        f"\n  cash_in             = {s.cash_in:,.2f}"
        f"\n  cash_out            = {s.cash_out:,.2f}"
        f"\n  net_cash            = {s.net_cash:,.2f}"
        f"\n  accounts_receivable = {s.accounts_receivable:,.2f}"
        f"\n  findings            = {len(result.findings)}"
        + "".join(f"\n  {g.rule} -> {g.passed}: {g.message}" for g in result.gates)
    )


# ── fixtures: two mailboxes made of text, nothing hand-built ─────────────────

@pytest.fixture
def bundled(tmp_path) -> Path:
    """The shipped month, byte for byte, in a directory we may add to."""
    root = tmp_path / "mail"
    shutil.copytree(CORPUS_ROOT / PERIOD, root / PERIOD)
    return root


@pytest.fixture
def minimal(tmp_path) -> Path:
    """One load earned, one remittance settling it, one deposit of that money.

    100.00 was earned and 100.00 arrived. Three artifacts, all of them the kind
    a bookkeeper forwards without thinking about it.
    """
    root = tmp_path / "small"
    period = root / PERIOD
    period.mkdir(parents=True)
    (period / "load-L-1.txt").write_text(
        "MIDWEST FREIGHT EXCHANGE\nLOAD CONFIRMATION\n\n"
        "Document Type: Load Confirmation\n"
        "Date: 2026-07-10\nLoad Number: L-1\nBroker: Midwest Freight Exchange\n"
        "Unit: T-1\nMiles: 1000\nLinehaul Rate: 100.00\nAccessorial: 0.00\n"
        "Total: 100.00\n", encoding="utf-8")
    (period / "remittance-RM-1.txt").write_text(
        "MIDWEST FREIGHT EXCHANGE\nREMITTANCE ADVICE\n\n"
        "Document Type: Broker Remittance\n"
        "Remittance Number: RM-1\nDate: 2026-07-24\n"
        "Broker: Midwest Freight Exchange\nLoads Settled: 1\n"
        "Factoring Fee: 0.00\nAmount Credited: 100.00\n\n"
        "LOAD LINES\nLoad L-1  Gross 100.00  Deduction 0.00  Reason -\n",
        encoding="utf-8")
    (period / "bank-2026-07-24-1.txt").write_text(
        "FIRST PLAINS BANK\nACCOUNT ACTIVITY\n\n"
        "Document Type: Bank Transaction\n"
        "Date: 2026-07-24\nDirection: in\nAmount: 100.00\n"
        "Reference: RM-1\nDescription: Midwest Freight Exchange\n", encoding="utf-8")
    return root


# ── the input is one the shipped parser produces, not a fixture artefact ─────

def test_the_deposit_line_parses_into_the_document_the_ledger_acts_on():
    """Refutation attempt 1: "no real document ever looks like that."

    It does. The shipped extractor turns an ordinary deposit line into a
    BANK_TRANSACTION with direction 'in', which is the exact branch
    `Ledger._post_bank_transaction` used to post Dr Bank / Cr AR from without
    once asking whether that money had already arrived. This passes: the input
    the other agent hand-built is the input the parser produces.
    """
    doc = extract_document(DEPOSIT_LINE, source_file="bank-2026-07-24-7.txt",
                           period=PERIOD)

    assert doc.doc_type is DocType.BANK_TRANSACTION
    assert doc.direction == "in"
    assert doc.net_amount == REMITTED
    assert doc.reference == "MFX-RA-4417"


# ── the bundled month, plus the one line the bank statement really carries ───

def test_bundled_month_counts_the_remittance_cash_once(bundled):
    """Adding the deposit for money already remitted must not add cash.

    Baseline is the shipped month: cash_in 18,667.65, the remittance. The
    deposit line is the same 18,667.65 arriving -- the bank's record of the
    event the advice describes. Cash in must not move.
    """
    _docs, before = close_mailbox(bundled)
    baseline = before.statements.cash_in

    (bundled / PERIOD / "bank-2026-07-24-7.txt").write_text(DEPOSIT_LINE,
                                                            encoding="utf-8")
    _docs, after = close_mailbox(bundled)

    assert after.statements.cash_in == baseline, (
        f"\n  cash_in without the deposit line = {baseline:,.2f}"
        f"\n  cash_in with it                  = {after.statements.cash_in:,.2f}"
        f"\n  the deposit was for              = {REMITTED:,.2f}" + books(after))


def test_bundled_month_receivables_do_not_go_negative(bundled):
    """The month is owed 3,760.00 at close. Adding a deposit cannot owe less
    than nothing: a negative receivable is not a judgement call."""
    (bundled / PERIOD / "bank-2026-07-24-7.txt").write_text(DEPOSIT_LINE,
                                                            encoding="utf-8")
    _docs, result = close_mailbox(bundled)

    assert result.statements.accounts_receivable >= 0.0, books(result)


def test_bundled_month_passes_its_gates_without_double_counting(bundled):
    """The half of the claim that made it dangerous: nothing objected.

    A clean close is only worth anything if it is clean for a reason. When the
    deposit line doubled the cash, this month still reported every gate passed,
    and that silence was not luck. `g3_bank_movement_agrees` rebuilt its
    "observed" figure by adding the remittance total to every bank line, the
    deposit included, which is the same double count the ledger had just made:
    booked and observed committed one error each and the drift between them was
    always zero. G4 was no help either, because it keys on `source_doc`
    filenames and a remittance advice and the bank credit for it are two files.

    The gates pass today for the opposite reason, and this test pins down which
    one. `_post_bank_transaction` posts no lines at all when
    `matching_remittance` ties an inbound line to a remittance already in the
    period, and G3 drops that same line out of observed, so the bank movement
    the close reports does not move at all when a bookkeeper forwards the
    statement as well as the advice. Two errors cancelling would not survive
    that comparison; one honest figure does.
    """
    _docs, before = close_mailbox(bundled)
    g3_before = next(g for g in before.gates if g.rule.startswith("G3"))

    (bundled / PERIOD / "bank-2026-07-24-7.txt").write_text(DEPOSIT_LINE,
                                                            encoding="utf-8")
    _docs, result = close_mailbox(bundled)

    assert result.outcome == "closed", books(result)
    # Deliberately not a gate count. Gates get added for unrelated reasons and
    # this test's claim is that none of them object, however many there are.
    assert all(g.passed for g in result.gates), books(result)

    # The deposit still produces an entry, so the trail shows the document was
    # seen and read rather than quietly dropped, but the entry carries no lines
    # and names the remittance that already brought the money in.
    seen = [e for e in result.ledger.entries
            if e.source_doc == "bank-2026-07-24-7.txt"]
    assert len(seen) == 1, [e.memo for e in seen]
    assert seen[0].lines == [], seen[0].lines
    # "MFX-RA-4417" alone would prove nothing: the old memo was "Receipt
    # MFX-RA-4417" and carried the reference too. The refusal is the new half.
    assert "not posted again" in seen[0].memo, seen[0].memo
    assert "MFX-RA-4417" in seen[0].memo, seen[0].memo

    # The one that could not have passed before: G3 reports the identical
    # movement with and without the deposit, which it can only do by leaving
    # the matched line out of observed rather than doubling to meet a doubled
    # ledger.
    g3 = next(g for g in result.gates if g.rule.startswith("G3"))
    assert g3.message == g3_before.message, (g3_before.message, g3.message)


def test_the_same_month_requires_the_bank_statement_on_the_way_out(bundled):
    """Why forwarding the whole statement is the taught behaviour, not an error.

    The shipped month contains BOTH the fuel card statement FCN-2026-07 and the
    outbound bank line that paid it, and it needs both: the statement raises the
    payable, the bank line clears it. Drop the bank line and 7,281.00 stays
    owed. So on the way out, "send the bank statement as well as the invoice" is
    exactly right -- which is what made doing the same on the way in, where it
    used to double count, an ordinary thing for a bookkeeper to do rather than
    an exotic one. This is context, not a defect: it passed before the fix and
    passes after it, because outbound lines were never the ones at risk.
    """
    _docs, with_line = close_mailbox(bundled)
    (bundled / PERIOD / "bank-2026-07-30-4.txt").unlink()      # paid FCN-2026-07
    _docs, without_line = close_mailbox(bundled)

    paid = round(without_line.statements.cash_out - with_line.statements.cash_out, 2)
    still_owed = round(without_line.statements.accounts_payable
                       - with_line.statements.accounts_payable, 2)
    assert paid == -7281.00, paid
    assert still_owed == 7281.00, still_owed


# ── the same thing at a size you can check in your head ──────────────────────

def test_minimal_month_cash_in_is_the_hundred_that_arrived(minimal):
    """100.00 earned, 100.00 remitted, 100.00 landed. Cash in is 100.00."""
    _docs, result = close_mailbox(minimal)

    assert result.statements.cash_in == 100.00, books(result)


def test_minimal_month_settled_load_leaves_no_receivable(minimal):
    """One load, settled in full, nothing else touching AR: AR is zero."""
    _docs, result = close_mailbox(minimal)

    assert result.statements.accounts_receivable == 0.00, books(result)


def test_minimal_month_cash_in_cannot_beat_revenue(minimal):
    """No capital, no loan, no other inflow in the period."""
    s = close_mailbox(minimal)[1].statements

    assert s.cash_in <= s.revenue, f"cash_in {s.cash_in:,.2f} > revenue {s.revenue:,.2f}"
