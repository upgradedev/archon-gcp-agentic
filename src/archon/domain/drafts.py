"""Turn a finding into the document that would fix it.

Detection tells the owner something is wrong. That is where most products
stop, and stopping there is what makes them reports. Archon writes the letter:
the supplier who never sent the receipt gets a document request, the broker who
underpaid gets a dispute quoting its own remittance back at it, the toll
company that billed twice gets a refund request.

Two rules govern this module, and both are load-bearing.

**Every figure is computed, never phrased.** The amounts and references come
from the `Finding` a deterministic detector produced. `narrator.py` may rewrite
the covering sentence into better English; it cannot introduce a number,
because the numbers are already in the string before it is ever shown a model.

**Nothing is sent.** A draft is filed into Archon's own state with
`status="filed"` and there is no send path in this repository. That is where
the chore stops and the one human gate begins. The close is unattended right up
to the edge of somebody else's inbox, and not one inch past it.
"""
from __future__ import annotations

from .models import Draft, DraftKind, ExceptionKind, Finding

#: Which finding kind produces which corrective document. A kind that is absent
#: here has no honest letter to write: an outlier is a question for the owner,
#: an unreadable scan needs a human to look at it, and an out-of-period date is
#: a bookkeeping decision, not a dispute.
DRAFT_FOR_FINDING = {
    ExceptionKind.PAYMENT_WITHOUT_DOCUMENT: DraftKind.DOCUMENT_REQUEST,
    ExceptionKind.SHORT_PAY: DraftKind.SHORT_PAY_DISPUTE,
    ExceptionKind.DUPLICATE_CHARGE: DraftKind.DUPLICATE_REFUND,
    ExceptionKind.LOAD_UNPAID: DraftKind.PAYMENT_REMINDER,
}


def _money(amount: float, currency: str = "USD") -> str:
    return f"{currency} {amount:,.2f}"


def _document_request(finding: Finding, company: str) -> Draft:
    return Draft(
        kind=DraftKind.DOCUMENT_REQUEST,
        recipient=finding.counterparty or "the supplier on this payment",
        subject=f"Missing documentation for payment {finding.reference}",
        body=(
            f"We show a payment of {_money(finding.amount)} leaving our account "
            f"referencing {finding.reference}, and we hold no invoice or receipt "
            f"against it.\n\n"
            f"Please send the supporting document so we can record the expense "
            f"correctly. If this payment was not raised by you, tell us and we "
            f"will trace it at our end.\n\n"
            f"{company}"
        ),
        amount=finding.amount,
        reference=finding.reference,
        finding_kind=finding.kind,
        source_file=finding.source_file,
    )


def _short_pay_dispute(finding: Finding, company: str) -> Draft:
    return Draft(
        kind=DraftKind.SHORT_PAY_DISPUTE,
        recipient=finding.counterparty or "the broker",
        subject=f"Short payment on load {finding.reference}",
        body=(
            f"Your remittance settled load {finding.reference} "
            f"{_money(finding.amount)} below the rate we confirmed.\n\n"
            f"{finding.message}\n\n"
            f"Please confirm whether a deduction was intended and on what basis, "
            f"or release the balance with your next remittance.\n\n"
            f"{company}"
        ),
        amount=finding.amount,
        reference=finding.reference,
        finding_kind=finding.kind,
        source_file=finding.source_file,
    )


def _duplicate_refund(finding: Finding, company: str) -> Draft:
    return Draft(
        kind=DraftKind.DUPLICATE_REFUND,
        recipient=finding.counterparty or "the supplier on this charge",
        subject=f"Duplicate charge of {_money(finding.amount)}",
        body=(
            f"{finding.message}\n\n"
            f"Please confirm the duplicate and credit {_money(finding.amount)} "
            f"back to the account. If the two charges are genuinely separate, "
            f"send the detail for each and we will clear our query.\n\n"
            f"{company}"
        ),
        amount=finding.amount,
        reference=finding.reference,
        finding_kind=finding.kind,
        source_file=finding.source_file,
    )


def _payment_reminder(finding: Finding, company: str) -> Draft:
    return Draft(
        kind=DraftKind.PAYMENT_REMINDER,
        recipient=finding.counterparty or "the broker",
        subject=f"Load {finding.reference} remains unpaid",
        body=(
            f"Load {finding.reference} was delivered and invoiced at "
            f"{_money(finding.amount)}, and no part of it appears on any "
            f"remittance we have received.\n\n"
            f"Please confirm the payment date, or tell us what you still need "
            f"from us to release it.\n\n"
            f"{company}"
        ),
        amount=finding.amount,
        reference=finding.reference,
        finding_kind=finding.kind,
        source_file=finding.source_file,
    )


_BUILDERS = {
    DraftKind.DOCUMENT_REQUEST: _document_request,
    DraftKind.SHORT_PAY_DISPUTE: _short_pay_dispute,
    DraftKind.DUPLICATE_REFUND: _duplicate_refund,
    DraftKind.PAYMENT_REMINDER: _payment_reminder,
}


def draft_for(finding: Finding, company: str = "Accounts") -> Draft | None:
    """The corrective document for one finding, or None if there is no honest one."""
    kind = DRAFT_FOR_FINDING.get(finding.kind)
    if kind is None:
        return None
    if finding.amount <= 0:
        # Nothing to ask for. A zero-amount dispute wastes the recipient's time
        # and burns the owner's credibility with a counterparty they depend on.
        return None
    return _BUILDERS[kind](finding, company)


def draft_all(findings: list[Finding], company: str = "Accounts") -> list[Draft]:
    """Draft a corrective document for every finding that warrants one."""
    drafts = [draft_for(f, company) for f in findings]
    return [d for d in drafts if d is not None]


def recoverable(drafts: list[Draft]) -> float:
    """Money the filed drafts are chasing.

    Only the kinds that ask for money back count. A document request recovers
    nothing; it makes the books complete, which is worth doing and is not worth
    overstating as a recovery.
    """
    chasing = (DraftKind.SHORT_PAY_DISPUTE, DraftKind.DUPLICATE_REFUND,
               DraftKind.PAYMENT_REMINDER)
    return round(sum(d.amount for d in drafts if d.kind in chasing), 2)


def draft_for_decisions(decisions, company: str = "Accounts") -> list[Draft]:
    """Write a letter for every finding that was decided to warrant one.

    The decisions come from `policy.apply_choices`, which has already overruled
    anything the books will not accept, so this can trust what it is handed and
    does not re-litigate it. A finding escalated to the owner produces no
    letter by design: escalation means a person looks, not that nothing happens.
    """
    from .policy import Disposition

    drafts = [
        draft_for(decision.finding, company)
        for decision in decisions
        if decision.applied is Disposition.DRAFT
    ]
    return [draft for draft in drafts if draft is not None]
