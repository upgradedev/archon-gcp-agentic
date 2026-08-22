"""The corrective documents, and the rule that none of them is ever sent."""
from __future__ import annotations

from archon.domain.drafts import (
    DRAFT_FOR_FINDING,
    draft_all,
    draft_for,
    leakage,
    outstanding,
    recoverable,
    undocumented,
)
from archon.domain.models import ACTIONABLE_KINDS, DraftKind, ExceptionKind, Finding


def finding(kind: ExceptionKind, amount: float = 100.0, *,
            counterparty: str | None = "Acme Freight",
            reference: str = "REF-1") -> Finding:
    return Finding(kind=kind, severity="error", reference=reference, amount=amount,
                   message="something is wrong", counterparty=counterparty)


def test_every_actionable_kind_has_a_draft_and_no_other_kind_does():
    """The two sides of this must not drift apart."""
    assert set(DRAFT_FOR_FINDING) == set(ACTIONABLE_KINDS)


def test_an_orphan_payment_becomes_a_document_request():
    draft = draft_for(finding(ExceptionKind.PAYMENT_WITHOUT_DOCUMENT, 1865.0))

    assert draft.kind is DraftKind.DOCUMENT_REQUEST
    assert draft.recipient == "Acme Freight"
    assert "1,865.00" in draft.body


def test_a_short_pay_becomes_a_dispute_quoting_the_evidence_back():
    draft = draft_for(finding(ExceptionKind.SHORT_PAY, 200.0))

    assert draft.kind is DraftKind.SHORT_PAY_DISPUTE
    assert "something is wrong" in draft.body       # the detector's own message


def test_a_duplicate_becomes_a_refund_request():
    assert draft_for(finding(ExceptionKind.DUPLICATE_CHARGE)).kind is DraftKind.DUPLICATE_REFUND


def test_an_unpaid_load_becomes_a_reminder():
    assert draft_for(finding(ExceptionKind.LOAD_UNPAID)).kind is DraftKind.PAYMENT_REMINDER


def test_kinds_with_no_honest_letter_produce_none():
    for kind in (ExceptionKind.AMOUNT_OUTLIER, ExceptionKind.TAX_INCONSISTENCY,
                 ExceptionKind.OUT_OF_PERIOD, ExceptionKind.UNREADABLE_DOCUMENT,
                 ExceptionKind.REMITTANCE_UNRECONCILED):
        assert draft_for(finding(kind)) is None


def test_a_zero_amount_finding_produces_no_draft():
    """A dispute for nothing wastes a counterparty the owner depends on."""
    assert draft_for(finding(ExceptionKind.SHORT_PAY, 0.0)) is None


def test_a_missing_counterparty_falls_back_to_a_readable_placeholder():
    draft = draft_for(finding(ExceptionKind.LOAD_UNPAID, counterparty=None))
    assert draft.recipient == "the broker"


def test_every_draft_is_filed_and_never_sent():
    """The single human gate in the product, asserted rather than assumed."""
    drafts = draft_all([finding(kind) for kind in ACTIONABLE_KINDS])

    assert drafts
    assert {d.status for d in drafts} == {"filed"}


def test_no_send_path_exists_in_the_drafts_module():
    """If someone adds one, this test is where they have to think about it."""
    import archon.domain.drafts as module

    assert not [name for name in dir(module) if "send" in name.lower()]


def test_the_three_figures_are_kept_apart():
    """They used to be added together, and the total read as money found.

    A receivable the broker has not paid is not a discovery: an invoice ledger
    shows it without any agent. Blending it with a genuine leak overstated the
    bundled month by nine times.
    """
    drafts = draft_all([
        finding(ExceptionKind.PAYMENT_WITHOUT_DOCUMENT, 1000.0),
        finding(ExceptionKind.SHORT_PAY, 200.0),
        finding(ExceptionKind.DUPLICATE_CHARGE, 50.0),
        finding(ExceptionKind.LOAD_UNPAID, 2780.0),
    ])

    assert leakage(drafts) == 250.0        # only what would have been lost
    assert outstanding(drafts) == 2780.0   # owed already
    assert undocumented(drafts) == 1000.0  # recovers nothing at all


def test_recoverable_no_longer_counts_receivables():
    """The name is kept for the API shape; the meaning is now leakage only."""
    drafts = draft_all([finding(ExceptionKind.SHORT_PAY, 200.0),
                        finding(ExceptionKind.LOAD_UNPAID, 2780.0)])

    assert recoverable(drafts) == leakage(drafts) == 200.0


def test_the_company_name_signs_every_draft():
    draft = draft_for(finding(ExceptionKind.LOAD_UNPAID), company="Bell Ridge Haulage")
    assert draft.body.rstrip().endswith("Bell Ridge Haulage")
