"""Reproduction for audit finding 1.3 — bank direction parsing.

The whole decision lives in one line, `src/archon/domain/extract.py:427`::

    direction = (_pick(labels, "direction") or "").lower()
    doc.direction = "in" if direction.startswith("in") else "out"

That is a prefix test against the literal string ``in`` with ``out`` as the
catch-all. It is the ONLY place a bank line's direction is decided; nothing
downstream normalises it (`ledger.py:142`, `validation.py:89` and
`exceptions.py:97` all read `doc.direction` as gospel).

The corpus only ever spells the label ``in`` / ``out``, so the bundled demo
never exercises the failure. Real statements do not restrict themselves to
that vocabulary, and the amount's own sign is never consulted at all.

Tests below are split into two groups:

* ``test_credit_*`` / ``test_outgoing_*`` / ``test_*_end_to_end`` FAIL on the
  current code. They are the defect.
* ``test_spellings_that_do_work`` and the two ``test_*_is_silent`` /
  ``test_*_raises_a_bogus`` characterisation tests PASS. They pin down the
  blast radius: which spellings are safe, and the fact that no existing gate
  reports the corruption.
"""
from __future__ import annotations

import pytest

from archon.domain.exceptions import find_payments_without_documents
from archon.domain.extract import extract_document
from archon.domain.ledger import Ledger
from archon.domain.models import Account
from archon.domain.validation import g1_entries_balance, g3_bank_movement_agrees


def _statement(direction: str | None = None, amount: str = "1,200.00",
               reference: str = "REF-1") -> str:
    """One bank line in the exact shape the corpus uses.

    `direction=None` omits the label entirely, which is how an export that
    encodes direction purely in the sign of the amount arrives.
    """
    lines = [
        "FIRST PLAINS BANK",
        "ACCOUNT ACTIVITY",
        "",
        "Document Type: Bank Transaction",
        "Date: 2026-07-15",
    ]
    if direction is not None:
        lines.append(f"Direction: {direction}")
    lines += [
        f"Amount: {amount}",
        f"Reference: {reference}",
        "Paid To: Sunrise Logistics",
    ]
    return "\n".join(lines) + "\n"


def _bank_movement(ledger: Ledger) -> float:
    debits, credits = ledger.balances().get(Account.BANK, (0.0, 0.0))
    return round(debits - credits, 2)


# ── the defect: money that ARRIVED, booked as money that LEFT ────────────────

#: Spellings every real statement uses for an inbound line. A credit to the
#: account is money arriving; none of these start with the letters "in".
CREDIT_SPELLINGS = [
    "CREDIT", "Credit", "credit",
    "CR", "Cr",
    "DEPOSIT", "Deposit",
    "PAYMENT RECEIVED", "Payment received",
    "Received", "Funds received",
]


@pytest.mark.parametrize("spelling", CREDIT_SPELLINGS)
def test_credit_spellings_are_read_as_incoming(spelling: str) -> None:
    """A credit line is money arriving. The parser calls all of these "out"."""
    doc = extract_document(_statement(spelling))

    assert doc.direction == "in", (
        f"Direction: {spelling!r} is an INBOUND bank line, but the parser "
        f"classified it {doc.direction!r}. extract.py:427 only accepts a "
        f"string starting with 'in'; everything else falls through to 'out', "
        f"so arriving money is booked as money leaving the account."
    )


#: Unambiguously OUTBOUND wordings that happen to begin with the letters "in".
#: The prefix test has no way to tell these from "incoming".
OUTGOING_SPELLINGS_BEGINNING_WITH_IN = [
    "Internal transfer out",
    "Instant payment sent",
    "Interest charge",
]


@pytest.mark.parametrize("spelling", OUTGOING_SPELLINGS_BEGINNING_WITH_IN)
def test_outgoing_spellings_beginning_with_in_are_read_as_outgoing(
    spelling: str,
) -> None:
    """The mirror failure: money that left, booked as money that arrived."""
    doc = extract_document(_statement(spelling))

    assert doc.direction == "out", (
        f"Direction: {spelling!r} is an OUTBOUND bank line, but the parser "
        f"classified it {doc.direction!r}. extract.py:427 tests "
        f"str.startswith('in'), which matches 'internal', 'instant' and "
        f"'interest' just as happily as it matches 'incoming'."
    )


def test_a_credit_increases_the_bank_balance_end_to_end() -> None:
    """Drive the real path: statement text -> extract -> Ledger -> balances."""
    ledger = Ledger(period="2026-07")
    doc = extract_document(
        _statement("CREDIT", amount="10,000.00", reference="BROKER-PAYMENT-9"),
        source_file="bank-credit.txt",
    )
    entry = ledger.add(doc)

    # The entry balances either way, so G1 is no help here.
    assert g1_entries_balance(ledger).passed

    assert _bank_movement(ledger) == 10_000.00, (
        f"A 10,000.00 CREDIT must move the bank +10,000.00. The books moved "
        f"it {_bank_movement(ledger):,.2f} — a 20,000.00 swing — and posted "
        f"{entry.memo!r} clearing Accounts Payable instead of a Receipt "
        f"clearing Accounts Receivable."
    )


def test_direction_by_sign_alone_end_to_end() -> None:
    """A statement that encodes direction only in the sign of the amount.

    `_money` parses the sign correctly, so the figures are right. Nothing ever
    consults that sign to decide direction, so both lines default to "out" and
    the month's bank movement comes out exactly inverted.
    """
    ledger = Ledger(period="2026-07")
    for amount, reference in [("-1,200.00", "FUEL-OUT"), ("3,400.00", "BROKER-IN")]:
        ledger.add(
            extract_document(
                _statement(direction=None, amount=amount, reference=reference),
                source_file=f"{reference}.txt",
            )
        )

    # The amounts themselves parsed fine; only the direction is lost.
    assert [d.net_amount for d in ledger.documents] == [-1200.00, 3400.00]

    assert _bank_movement(ledger) == 2_200.00, (
        f"1,200.00 left and 3,400.00 arrived, so the bank moved +2,200.00. "
        f"The books moved it {_bank_movement(ledger):,.2f}: the sign is "
        f"inverted because every line defaulted to 'out' and the negative "
        f"amount was then posted as a negative credit to Bank."
    )


def test_a_credit_inverts_the_close_through_the_real_mailbox(tmp_path) -> None:
    """Reachability: the defect fires through the shipped ingest path.

    `mailbox.read_period` is what the close actually calls, and its GCS twin
    (`adapters/gcs.py:96`) feeds `extract_document` every UTF-8 `.txt` object
    a bookkeeper drops in the bucket. Neither restricts the Direction
    vocabulary to the corpus's `in`/`out`, so this is live input, not a
    hypothetical.
    """
    from archon.runtime.mailbox import read_period

    period = "2026-07"
    inbox = tmp_path / period
    inbox.mkdir()
    (inbox / "bank-credit.txt").write_text(
        _statement("CREDIT", amount="10,000.00", reference="BROKER-PAYMENT-9"),
        encoding="utf-8",
    )

    documents, _ = read_period(period, root=tmp_path)
    ledger = Ledger(period=period)
    ledger.add_all(documents)

    assert _bank_movement(ledger) == 10_000.00, (
        f"A statement forwarded to the mailbox saying 'Direction: CREDIT' for "
        f"10,000.00 moved the books {_bank_movement(ledger):,.2f}. The close "
        f"ingests arbitrary bookkeeper text, so nothing upstream constrains "
        f"the Direction wording to the corpus's 'in'/'out'."
    )


# ── characterisation: what works, and why the failure is silent ──────────────

@pytest.mark.parametrize(
    "spelling,expected",
    [
        # These genuinely work.
        ("in", "in"), ("IN", "in"), ("In", "in"),
        ("Incoming", "in"), ("INCOMING", "in"),
        ("Inbound", "in"), ("Inward", "in"),
        # These are correct, but only because "out" is the catch-all: the
        # parser is not recognising them, it is failing to recognise them and
        # landing on the right answer anyway.
        ("out", "out"), ("OUT", "out"),
        ("Outgoing", "out"), ("Outbound", "out"), ("Outward", "out"),
        ("DEBIT", "out"), ("Debit", "out"), ("DR", "out"), ("Dr", "out"),
        ("WITHDRAWAL", "out"), ("Withdrawal", "out"),
        ("PAYMENT SENT", "out"), ("Sent", "out"),
    ],
)
def test_spellings_that_do_work(spelling: str, expected: str) -> None:
    """Documents the vocabulary the current parser handles correctly."""
    assert extract_document(_statement(spelling)).direction == expected


def test_a_missing_direction_label_defaults_to_outgoing() -> None:
    """No label at all is silently treated as money leaving the account."""
    doc = extract_document(_statement(direction=None))
    assert doc.direction == "out"
    assert doc.net_amount == 1200.00


def test_the_misread_is_silent_because_g3_reads_the_same_field() -> None:
    """No gate reports the corruption, which is what makes it dangerous.

    G3 exists to catch "a fuel statement booked to the wrong side". It cannot
    catch this one: `validation.py:89` derives the "observed" bank movement
    from `doc.direction` — the very field the parser got wrong — and compares
    it to a ledger built from that same field. Both sides move together, so
    the drift is always zero.
    """
    ledger = Ledger(period="2026-07")
    ledger.add(
        extract_document(
            _statement("CREDIT", amount="10,000.00", reference="BROKER-PAYMENT-9"),
            source_file="bank-credit.txt",
        )
    )

    result = g3_bank_movement_agrees(ledger, ledger.documents)

    assert result.passed is True
    assert "-10,000.00" in result.message
    # i.e. the close reports "bank movement agrees" on a 20,000.00 error.


def test_a_misread_credit_raises_a_bogus_payment_exception() -> None:
    """The misread does not just corrupt the books, it invents an exception.

    Because the arriving money is now an outgoing payment with no matching
    document, the operator is handed a "payment without document" finding to
    chase on cash that was never spent.
    """
    doc = extract_document(
        _statement("CREDIT", amount="10,000.00", reference="BROKER-PAYMENT-9"),
        source_file="bank-credit.txt",
    )
    findings = find_payments_without_documents([doc])

    assert [(f.reference, f.amount) for f in findings] == [
        ("BROKER-PAYMENT-9", 10_000.00)
    ]
