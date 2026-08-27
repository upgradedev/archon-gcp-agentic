"""Independent verification of audit finding 1.3 - bank direction parsing.

Written to REFUTE a prior agent's reproduction, not to echo it. The fixtures
here are deliberately different: where the prior test always stamped the
artifact with archon's own ``Document Type: Bank Transaction`` label, the
tests below also drive a statement that uses NO archon vocabulary at all, to
establish whether the exposure needs a document already speaking archon's
convention or arrives from ordinary bank text.

The blamed line is `src/archon/domain/extract.py:426-427`::

    direction = (_pick(labels, "direction") or "").lower()
    doc.direction = "in" if direction.startswith("in") else "out"

`_pick` is an EXACT key lookup - unlike `reference`/`net`/`gross`, the field
`direction` has no entry in `_ALIASES`. So a statement naming its column
``Type``, ``Dr/Cr`` or ``Transaction Type``, or naming no column at all,
produces no `direction` label and takes the unconditional `out` branch.

Tests are split:

* the FAILING ones are the defect, reproduced independently.
* ``test_documents_*`` and ``test_g3_*`` PASS and pin the blast radius and
  the reachability.
"""
from __future__ import annotations

import pytest

from archon.adapters.store import LocalStore
from archon.domain.extract import extract_document
from archon.domain.ledger import Ledger
from archon.domain.models import Account, DocType, ExceptionKind
from archon.runtime.close import run_close
from archon.runtime.mailbox import CORPUS_ROOT, read_period

PERIOD = "2026-07"

#: Bank movement the bundled corpus month produces on its own, before any
#: document of ours is added. Measured, not assumed.
CORPUS_BANK_MOVEMENT = 707.16


def _plain_statement(amount: str, reference: str, *, direction: str | None = None,
                     type_label: str | None = None) -> str:
    """A statement carrying NO archon-specific vocabulary by default.

    There is no ``Document Type:`` line. Classification has to come from
    `_KEYWORDS[BANK_TRANSACTION]`, which contains the literal
    ``account statement`` - i.e. from ordinary bank letterhead.
    """
    lines = [
        "FIRST PLAINS BANK",
        "ACCOUNT STATEMENT",
        "Account: ****4417",
        "",
        "Date: 2026-07-29",
        "Description: Redline Freight Brokerage",
        f"Reference: {reference}",
    ]
    if direction is not None:
        lines.append(f"Direction: {direction}")
    if type_label is not None:
        lines.append(f"Type: {type_label}")
    lines.append(f"Amount: {amount}")
    return "\n".join(lines) + "\n"


def _bank_movement(ledger: Ledger) -> float:
    debits, credits = ledger.balances().get(Account.BANK, (0.0, 0.0))
    return round(debits - credits, 2)


# -- reachability, established first: no archon vocabulary is required --------

def test_documents_that_plain_bank_letterhead_alone_reaches_the_parser() -> None:
    """PASSES. The exposure does not need archon's own label convention.

    A statement with no ``Document Type:`` line and no ``Direction:`` line is
    still classified BANK_TRANSACTION off the words "ACCOUNT STATEMENT", is
    still handed to `_bank_transaction`, and still comes out `out`. So the
    unconditional default is reached by ordinary bank text, not only by a
    half-archon artifact.
    """
    doc = extract_document(_plain_statement("10,000.00", "RM-2026-07-02"))

    assert doc.doc_type is DocType.BANK_TRANSACTION
    assert doc.net_amount == 10_000.00
    assert doc.reference == "RM-2026-07-02"
    # No evidence of direction was present anywhere on the artifact.
    assert doc.direction == "out"


def test_documents_that_direction_has_no_alias_table() -> None:
    """PASSES. `Type: CR` and `Type: Credit` are simply not seen.

    Every other messy-key field goes through `_ALIASES`. `direction` does not,
    so the commonest real column headings never reach the decision at all.
    """
    for label in ("CR", "Credit", "DR", "Debit"):
        doc = extract_document(
            _plain_statement("10,000.00", "RM-1", type_label=label))
        assert doc.direction == "out", label


def test_documents_that_direction_is_never_left_unknown() -> None:
    """PASSES, and is the internal-consistency case against line 427.

    This module's contract everywhere else is refuse-rather-than-guess:
    `_iso_date` returns None on an ambiguous 03/04/2026, `_alias` returns None
    when two labels disagree, `_HAS_FIGURE` keeps a document UNKNOWN rather
    than invent a figure, and `EXTRACTION_INSTRUCTION` says "unreadable" beats
    a plausible guess. `models.Document.direction` is typed `str | None`.

    Line 427 is the exception: it never yields None, so no gate downstream can
    tell "the statement said out" from "we had nothing to go on".
    """
    assert extract_document(_plain_statement("10,000.00", "R")).direction is not None


# -- the defect, reproduced independently ------------------------------------

@pytest.mark.parametrize("spelling", ["Credit", "CR", "Deposit", "Funds received"])
def test_a_credit_line_is_booked_as_money_leaving(spelling: str) -> None:
    """FAILS. A credit to a bank account is money arriving."""
    doc = extract_document(
        _plain_statement("10,000.00", "RM-2026-07-02", direction=spelling))

    assert doc.direction == "in", (
        f"'Direction: {spelling}' is inbound. extract.py:427 tests "
        f"str.startswith('in'), so it classified this {doc.direction!r}."
    )


def test_a_month_encoded_by_sign_alone_comes_out_exactly_inverted() -> None:
    """FAILS. `_money` reads the signs correctly; nothing consults them.

    Note what is and is not asserted here. The amounts are asserted first, so
    the failure cannot be blamed on a mis-parsed figure: the parser gets
    -1,200.00 and +3,400.00 exactly right and then throws the information away.
    """
    ledger = Ledger(period=PERIOD)
    for amount, reference in [("-1,200.00", "FUEL-OUT"), ("3,400.00", "BROKER-IN")]:
        ledger.add(extract_document(_plain_statement(amount, reference),
                                    source_file=f"{reference}.txt"))

    assert [d.net_amount for d in ledger.documents] == [-1200.00, 3400.00]

    assert _bank_movement(ledger) == 2_200.00, (
        f"The two lines sum to +2,200.00 on the parser's own figures. The "
        f"books moved the bank {_bank_movement(ledger):,.2f} - the sign is "
        f"inverted, because both lines took the 'out' branch and the negative "
        f"amount was then posted as a negative credit to Bank."
    )


def test_the_real_close_reports_green_on_a_twenty_thousand_swing(tmp_path) -> None:
    """FAILS. Severity: this is end to end through the shipped runtime.

    The whole path runs: `mailbox.read_period` (the same function the GCS
    adapter mirrors at `adapters/gcs.py:96`) over a real directory of `.txt`,
    then `runtime.close.run_close` - intake, post, allocate, reconcile,
    triage, decide, draft, verify, report, file, notify - over the bundled
    month plus ONE inbound bank line.

    The assertion is not about the parser. It is that the close must not
    report a clean month over books it has inverted.
    """
    inbox = tmp_path / PERIOD
    inbox.mkdir()
    for path in (CORPUS_ROOT / PERIOD).glob("*.txt"):
        (inbox / path.name).write_text(path.read_text(encoding="utf-8"),
                                       encoding="utf-8")
    (inbox / "bank-credit-in.txt").write_text(
        _plain_statement("10,000.00", "RM-2026-07-02", direction="Credit"),
        encoding="utf-8",
    )

    documents, raw = read_period(PERIOD, root=tmp_path)
    result = run_close(period=PERIOD, documents=documents,
                       company="Bell Ridge Haulage", store=LocalStore(),
                       raw_texts=raw)

    booked = _bank_movement(result.ledger)
    green = [g.rule for g in result.gates if g.passed]
    bogus = [(f.reference, f.amount) for f in result.findings
             if f.kind == ExceptionKind.PAYMENT_WITHOUT_DOCUMENT]

    assert booked == CORPUS_BANK_MOVEMENT + 10_000.00, (
        f"A 10,000.00 credit must leave the month's bank movement at "
        f"{CORPUS_BANK_MOVEMENT + 10_000.00:,.2f}. It came out {booked:,.2f}, "
        f"a 20,000.00 swing. The close still returned outcome "
        f"{result.outcome!r} with {len(green)} of {len(result.gates)} gates "
        f"green, including G3 - validation.py:89 derives the 'observed' bank "
        f"movement from doc.direction, the same field the parser got wrong, "
        f"so both sides of that gate move together and the drift is always "
        f"zero. Payments-without-documents now reads {bogus}."
    )


def test_g3_cannot_see_it_because_it_reads_the_same_field() -> None:
    """PASSES. Why the corruption is silent rather than caught.

    G3's docstring names "a fuel statement booked to the wrong side" as
    exactly what it exists to catch. It cannot catch this one.
    """
    from archon.domain.validation import g3_bank_movement_agrees

    ledger = Ledger(period=PERIOD)
    ledger.add(extract_document(
        _plain_statement("10,000.00", "RM-2026-07-02", direction="Credit"),
        source_file="bank-credit-in.txt"))

    result = g3_bank_movement_agrees(ledger, ledger.documents)

    assert result.passed is True
    assert "-10,000.00" in result.message
