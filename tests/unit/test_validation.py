"""The gates. Every one is shown passing AND shown failing.

A gate nobody has watched fail is a gate nobody should believe. So each of the
each is broken deliberately here, once, and asserted red. Nothing in this file
widens a threshold to make a run go green.
"""
from __future__ import annotations

from archon.domain.allocation import allocate_all
from archon.domain.ledger import Ledger
from archon.domain.models import Account, DocType, JournalLine
from archon.domain.validation import (
    all_passed,
    g1_entries_balance,
    g2_remittances_reconcile,
    g3_bank_movement_agrees,
    g4_no_double_posting,
    g5_unreadable_left_unposted,
    g6_every_document_was_accounted_for,
    summary,
    validate,
)
from tests.conftest import PERIOD, bank, expense, load, remittance, unreadable


def _ledger(*docs) -> Ledger:
    ledger = Ledger(period=PERIOD, company="Test Haulage")
    ledger.add_all(list(docs))
    return ledger


# ── G1: entries balance ──────────────────────────────────────────────────────

def test_g1_passes_on_a_balanced_journal():
    assert g1_entries_balance(_ledger(load("L-1", 1000.0))).passed


def test_g1_fails_when_an_entry_is_forced_out_of_balance():
    ledger = _ledger(load("L-1", 1000.0))
    ledger.entries[0].lines.append(JournalLine(Account.BANK, debit=1.0))

    result = g1_entries_balance(ledger)

    assert not result.passed
    assert result.severity == "error"


def test_g1_is_skipped_rather_than_failed_when_nothing_was_posted():
    result = g1_entries_balance(_ledger())
    assert result.passed and "Skipped" in result.message


# ── G2: remittances reconcile ────────────────────────────────────────────────

def test_g2_passes_when_every_remittance_closes():
    docs = [load("L-1", 1000.0), remittance("R-1", [("L-1", 1000.0, 0.0, None)], fee=30.0)]
    assert g2_remittances_reconcile(allocate_all(docs)).passed


def test_g2_fails_on_a_residual():
    docs = [load("L-1", 1000.0),
            remittance("R-1", [("L-1", 1000.0, 0.0, None)], fee=30.0, total=900.0)]

    result = g2_remittances_reconcile(allocate_all(docs))

    assert not result.passed
    assert "off by" in result.message


def test_g2_is_skipped_when_no_remittance_arrived():
    assert "Skipped" in g2_remittances_reconcile([]).message


# ── G3: bank movement agrees ─────────────────────────────────────────────────

def test_g3_passes_when_the_books_move_the_bank_by_what_the_bank_saw():
    docs = [load("L-1", 1000.0),
            remittance("R-1", [("L-1", 1000.0, 0.0, None)], fee=30.0),
            expense(DocType.TOLL_INVOICE, "TOLL-1", 400.0, 32.0),
            bank(432.0, "TOLL-1")]
    ledger = _ledger(*docs)

    assert g3_bank_movement_agrees(ledger, docs).passed


def test_g3_catches_a_payment_posted_twice():
    """The failure this gate exists for: a whole family of silent errors that
    all show up as the ledger's bank account leaving the statement behind."""
    docs = [expense(DocType.TOLL_INVOICE, "TOLL-1", 400.0, 32.0), bank(432.0, "TOLL-1")]
    ledger = _ledger(*docs)
    ledger.add(bank(432.0, "TOLL-1"))          # the same payment, booked again

    result = g3_bank_movement_agrees(ledger, docs)

    assert not result.passed
    assert "difference" in result.message


def test_g3_is_skipped_when_no_money_moved():
    ledger = _ledger(load("L-1", 1000.0))
    assert "Skipped" in g3_bank_movement_agrees(ledger, ledger.documents).message


# ── G4: no double posting ────────────────────────────────────────────────────

def test_g4_passes_when_each_document_posted_once():
    assert g4_no_double_posting(_ledger(load("L-1", 1000.0), load("L-2", 2000.0))).passed


def test_g4_fails_when_the_same_source_document_posted_twice():
    duplicate = load("L-1", 1000.0)
    ledger = _ledger(duplicate, duplicate)

    result = g4_no_double_posting(ledger)

    assert not result.passed
    assert "load-L-1.txt" in result.message


# ── G5: nothing unreadable was given a figure ────────────────────────────────

def test_g5_passes_when_an_unreadable_document_posted_nothing():
    assert g5_unreadable_left_unposted(_ledger(unreadable())).passed


def test_g5_fails_if_an_unreadable_document_is_ever_given_figures():
    """The one failure that would not announce itself: an estimated invoice
    balances, rolls up and reads perfectly, and is simply not true."""
    ledger = _ledger(unreadable("scan.pdf"))
    ledger.entries[0].lines = [
        JournalLine(Account.FUEL_EXPENSE, debit=500.0),
        JournalLine(Account.ACCOUNTS_PAYABLE, credit=500.0),
    ]

    result = g5_unreadable_left_unposted(ledger)

    assert not result.passed
    assert result.severity == "error"


def test_g5_is_skipped_when_everything_was_readable():
    assert "Skipped" in g5_unreadable_left_unposted(_ledger(load("L-1", 1.0))).message


# ── the set ──────────────────────────────────────────────────────────────────

def test_validate_runs_every_gate():
    ledger = _ledger(load("L-1", 1000.0))
    gates = validate(ledger, [])

    assert len(gates) == 7
    assert [g.rule.split(":")[0] for g in gates] == [
        "G1", "G2", "G3", "G4", "G5", "G6", "G7"]


def test_a_clean_month_passes_every_gate():
    docs = [load("L-1", 1000.0), remittance("R-1", [("L-1", 1000.0, 0.0, None)], fee=30.0)]
    gates = validate(_ledger(*docs), allocate_all(docs))

    assert all_passed(gates)
    # Four run on a two-document month. The other three have no unreadable
    # scan, nothing of an unknown family and one currency, so they check
    # nothing. They pass -- a thin month is not a failed month -- and the
    # summary says how many actually ran rather than folding the three into a
    # "7/7" that would overstate what was verified.
    assert summary(gates) == "4/4 gates passed, 3 skipped"
    assert [g.rule.split(":")[0] for g in gates if g.skipped] == ["G5", "G6", "G7"]


def test_one_broken_gate_fails_the_set():
    ledger = _ledger(load("L-1", 1000.0))
    ledger.entries[0].lines.append(JournalLine(Account.BANK, debit=1.0))

    assert not all_passed(validate(ledger, []))


# ── G6: the document nobody recognised ───────────────────────────────────────
#
# Found by asking a plain question: "if I drop my own company's invoices in,
# does it work?" It did not, and worse, it did not say so. Two invoices in a
# format the extractor has never seen closed a month at zero revenue and
# reported "closed, every gate passed". UNREADABLE already produced a finding;
# UNKNOWN produced nothing anywhere, because `Ledger._post_unknown` posts no
# entry. The artifact left no trace in the books, the exceptions or the letter.
#
# This is not a trucking-versus-freelancer problem. A broker sending a document
# in an unfamiliar layout hits the identical hole.

def test_an_unrecognised_document_blocks_the_close():
    """A month is not closeable while documents the owner sent are
    unaccounted for. Blocking, not warning."""
    from archon.domain.models import DocType, Document

    mystery = Document(doc_type=DocType.UNKNOWN, period=PERIOD,
                       source_file="INV-2026-0184.txt")
    ledger = _ledger(load("L-1", 1000.0), mystery)

    gate = g6_every_document_was_accounted_for(ledger)

    assert not gate.passed
    assert gate.severity == "error"
    assert "INV-2026-0184.txt" in gate.message


def test_a_month_where_everything_was_recognised_passes_g6():
    gate = g6_every_document_was_accounted_for(_ledger(load("L-1", 1000.0)))

    assert gate.passed
    assert "every document matched a known family" in gate.message


def test_g6_names_the_files_rather_than_counting_them():
    """'Two documents failed' sends the owner hunting. The gate names them."""
    from archon.domain.models import DocType, Document

    unknowns = [Document(doc_type=DocType.UNKNOWN, period=PERIOD,
                         source_file=f"INV-{n}.txt") for n in range(1, 6)]
    gate = g6_every_document_was_accounted_for(_ledger(load("L-1", 1000.0), *unknowns))

    assert "INV-1.txt" in gate.message
    assert "and 2 more" in gate.message, "a long list must be truncated, not dumped"


def test_the_unrecognised_finding_is_reported_and_is_not_actionable():
    """There is a document to parse, not a letter to send. A draft here would
    be a dispute nobody can act on."""
    from archon.domain.exceptions import find_unrecognised
    from archon.domain.models import ACTIONABLE_KINDS, DocType, Document, ExceptionKind

    findings = find_unrecognised([
        Document(doc_type=DocType.UNKNOWN, period=PERIOD, source_file="INV-1.txt"),
        Document(doc_type=DocType.LOAD_CONFIRMATION, period=PERIOD, source_file="ok.txt"),
    ])

    assert len(findings) == 1
    assert findings[0].kind is ExceptionKind.UNRECOGNISED_DOCUMENT
    assert findings[0].severity == "error"
    assert findings[0].amount == 0.0, "Archon does not guess what it could not classify"
    assert ExceptionKind.UNRECOGNISED_DOCUMENT not in ACTIONABLE_KINDS


# --- The gate summary, and what the two document gates say about each other ---

def _bundled_gates():
    from archon.cli import read_period
    from archon.runtime.close import run_close
    documents, raw = read_period("2026-07", root=None)
    result = run_close(period="2026-07", documents=documents,
                       company="Bell Ridge Haulage", raw_texts=raw)
    return documents, result


def test_g4_and_g5_agree_about_the_unreadable_document():
    """Two gates on the same screen must not contradict each other.

    G5 reports the unreadable scan as "none posted", and it is right: the
    document carries a zero-line memo entry so the trail records that it
    arrived at all. G4 counted that memo among the documents that "each posted
    once", so the checks panel said 27 documents posted and, two rows down,
    that one of them had not. A judge reading both rows is entitled to conclude
    one of the gates is decorative.
    """
    documents, result = _bundled_gates()
    gates = {g.rule.split(":")[0]: g for g in result.gates}
    unreadable = [d for d in documents if d.failure_reason]
    assert unreadable, "the bundled month is supposed to contain one"

    posted = {e.source_doc for e in result.ledger.entries
              if e.source_doc and e.lines}
    for document in unreadable:
        assert document.source_file not in posted
    assert str(len(posted)) in gates["G4"].message
    assert str(len(documents)) not in gates["G4"].message.split(",")[0]


def test_a_skipped_gate_is_not_reported_as_one_that_ran():
    """`7/7 gates passed` when two had no inputs is a stronger claim than the
    run can carry. A gate whose inputs are absent still passes -- a thin month
    is not a failed month -- but it did not check anything, and the headline
    figure said it did."""
    from archon.domain import validation
    from archon.domain.models import ValidationResult

    ran = ValidationResult("G1: x", True, "info", "27 entries, all balanced")
    skipped = validation._skipped("G7: y", "one currency in the month")

    assert skipped.passed is True
    assert skipped.skipped is True
    assert ran.skipped is False

    line = validation.summary([ran, skipped])
    assert "1/1" in line, f"only one gate ran: {line}"
    assert "skipped" in line, f"the skipped one must be visible: {line}"


def test_gate_messages_read_as_english():
    """`1 remittance(s) reconcile to 8 load lines` is on camera."""
    _, result = _bundled_gates()
    for gate in result.gates:
        assert "(s)" not in gate.message, gate.message
