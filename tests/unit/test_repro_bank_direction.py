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

    `_money` always parsed the sign correctly; nothing consulted it. Both lines
    defaulted to "out" and the month's bank movement came out exactly
    inverted -- a 4,400.00 error on 4,600.00 of real movement. `_direction`
    now falls back to the sign when the statement carries no word for it,
    which is the statement's other way of saying which way the money went.
    """
    ledger = Ledger(period="2026-07")
    for amount, reference in [("-1,200.00", "FUEL-OUT"), ("3,400.00", "BROKER-IN")]:
        ledger.add(
            extract_document(
                _statement(direction=None, amount=amount, reference=reference),
                source_file=f"{reference}.txt",
            )
        )

    # `_money` always parsed the sign; the direction is where it now lands.
    # The amount is stored as a magnitude so the sign is not applied twice.
    assert [d.direction for d in ledger.documents] == ["out", "in"]
    assert [d.net_amount for d in ledger.documents] == [1200.00, 3400.00]

    assert _bank_movement(ledger) == 2_200.00, (
        f"1,200.00 left and 3,400.00 arrived, so the bank moved +2,200.00, "
        f"and the books must agree. They moved it {_bank_movement(ledger):,.2f}."
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


def test_a_missing_direction_label_with_a_positive_amount_is_an_arrival() -> None:
    """No word for it, but a positive amount still says which way it went.

    This used to read "out" because "out" was the catch-all for everything the
    prefix test did not recognise, which was almost everything.
    """
    doc = extract_document(_statement(direction=None))
    assert doc.direction == "in"
    assert doc.net_amount == 1200.00


def test_a_statement_that_says_nothing_at_all_refuses_to_guess() -> None:
    """No word, no sign, no direction. A guess here is a number in the owner's
    books pointing the wrong way, so the field is left empty and the close
    raises it rather than picking a side."""
    doc = extract_document(_statement(direction=None, amount="0.00"))
    assert doc.direction is None


def test_a_credit_now_moves_the_bank_the_way_the_statement_says() -> None:
    """Why no gate could ever have caught this, and why the parser had to be
    the fix rather than the gate.

    G3 derives its "observed" bank movement from `doc.direction` -- the very
    field the parser got wrong -- and compares it to a ledger built from that
    same field. Both sides moved together, so the drift was always zero and a
    20,000.00 error reported "bank movement agrees". A gate cannot audit its
    own input; the reading had to be right.
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
    assert _bank_movement(ledger) == 10_000.00, "a CREDIT must move the bank up"
    assert "-10,000.00" not in result.message


def test_a_credit_no_longer_invents_a_payment_to_chase() -> None:
    """The misread did not just corrupt the books, it invented work.

    An arrival read as an outgoing payment with no matching document handed the
    owner a "payment without document" finding, and sent them chasing cash that
    was never spent. Read correctly, there is nothing to chase.
    """
    doc = extract_document(
        _statement("CREDIT", amount="10,000.00", reference="BROKER-PAYMENT-9"),
        source_file="bank-credit.txt",
    )
    findings = find_payments_without_documents([doc])

    assert findings == [], (
        "money arriving is not a payment without a document"
    )
