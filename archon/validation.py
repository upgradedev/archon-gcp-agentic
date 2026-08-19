"""The gates a closed month must pass before Archon will call it closed.

Findings say what is wrong with the *firm's* month. These gates say whether
Archon's own work can be trusted. They are the difference between an agent that
finished and an agent that produced something, and they are why the close can
run unattended: a run that fails a gate closes as `blocked` and says which gate,
rather than filing a confident report nobody checked.

Five rules, deliberately few enough to hold in your head:

    G1  every journal entry balances
    G2  every remittance's own arithmetic closes
    G3  the bank movement the books predict matches the bank lines observed
    G4  no document was posted twice
    G5  nothing unreadable was quietly assigned a figure

A gate whose inputs are absent reports passed-and-skipped, so a thin month is
never failed for being thin. A gate that fails is never widened to make a run
go green; the run stays blocked and the defect gets fixed.
"""
from __future__ import annotations

from collections import Counter

from .models import (
    Account,
    AllocationResult,
    DocType,
    Document,
    ValidationResult,
)


def _skipped(rule: str, why: str) -> ValidationResult:
    return ValidationResult(rule=rule, passed=True, severity="info",
                            message=f"Skipped: {why}")


def g1_entries_balance(ledger) -> ValidationResult:
    """Every posted entry has equal debits and credits."""
    rule = "G1: every journal entry balances"
    if not ledger.entries:
        return _skipped(rule, "nothing was posted")
    unbalanced = [e for e in ledger.entries if not e.is_balanced]
    if unbalanced:
        return ValidationResult(
            rule, False, "error",
            f"{len(unbalanced)} of {len(ledger.entries)} entries do not balance: "
            + "; ".join(e.memo for e in unbalanced[:3]),
        )
    return ValidationResult(rule, True, "info",
                            f"{len(ledger.entries)} entries, all balanced")


def g2_remittances_reconcile(results: list[AllocationResult]) -> ValidationResult:
    """Each remittance's credit equals its lines less the fee charged once."""
    rule = "G2: every remittance reconciles to its load lines"
    if not results:
        return _skipped(rule, "no remittance in the period")
    broken = [r for r in results if not r.reconciles]
    if broken:
        return ValidationResult(
            rule, False, "error",
            f"{len(broken)} of {len(results)} remittances leave a residual: "
            + "; ".join(f"{r.remittance_ref} off by {r.residual:,.2f}" for r in broken),
        )
    return ValidationResult(
        rule, True, "info",
        f"{len(results)} remittance(s) reconcile to "
        f"{sum(len(r.allocations) for r in results)} load lines",
    )


def g3_bank_movement_agrees(ledger, documents: list[Document]) -> ValidationResult:
    """The bank movement the books imply equals the bank lines actually seen.

    This is the gate that catches a whole family of silent errors at once: a
    remittance posted at gross instead of net, a payment posted twice, a fuel
    statement booked to the wrong side. Any of them move the ledger's bank
    account away from what the bank itself reported.
    """
    rule = "G3: ledger bank movement equals observed bank lines"
    bank_lines = [d for d in documents if d.doc_type == DocType.BANK_TRANSACTION]
    remittances = [d for d in documents if d.doc_type == DocType.BROKER_REMITTANCE]
    if not bank_lines and not remittances:
        return _skipped(rule, "no bank line or remittance in the period")

    observed = round(
        sum((d.net_amount or 0.0) if d.direction == "in" else -(d.net_amount or 0.0)
            for d in bank_lines)
        + sum(d.remittance_total or 0.0 for d in remittances),
        2,
    )
    debits, credits = ledger.balances().get(Account.BANK, (0.0, 0.0))
    booked = round(debits - credits, 2)
    drift = round(booked - observed, 2)
    if abs(drift) >= 0.01:
        return ValidationResult(
            rule, False, "error",
            f"books move the bank by {booked:,.2f}; the statements show "
            f"{observed:,.2f}, a {drift:,.2f} difference",
        )
    return ValidationResult(rule, True, "info",
                            f"bank movement agrees at {booked:,.2f}")


def g4_no_double_posting(ledger) -> ValidationResult:
    """No source artifact produced more than one entry."""
    rule = "G4: no document posted twice"
    sources = [e.source_doc for e in ledger.entries if e.source_doc]
    if not sources:
        return _skipped(rule, "no entry carries a source document")
    repeats = [name for name, count in Counter(sources).items() if count > 1]
    if repeats:
        return ValidationResult(
            rule, False, "error",
            f"{len(repeats)} document(s) posted more than once: " + ", ".join(repeats[:3]),
        )
    return ValidationResult(rule, True, "info",
                            f"{len(set(sources))} documents, each posted once")


def g5_unreadable_left_unposted(ledger) -> ValidationResult:
    """Nothing that could not be read was given a figure anyway.

    The gate exists because this is the failure that would not announce
    itself. An estimated invoice balances, rolls up and reads perfectly; it is
    simply not true. So the rule is structural: an unreadable document must
    post zero lines.
    """
    rule = "G5: unreadable documents carry no figures"
    unreadable = ledger.documents_of(DocType.UNREADABLE)
    if not unreadable:
        return _skipped(rule, "every document was readable")
    names = {d.source_file for d in unreadable}
    offenders = [e.memo for e in ledger.entries if e.source_doc in names and e.lines]
    if offenders:
        return ValidationResult(
            rule, False, "error",
            f"{len(offenders)} unreadable document(s) were posted with figures: "
            + "; ".join(offenders[:3]),
        )
    return ValidationResult(
        rule, True, "info",
        f"{len(unreadable)} unreadable document(s), none posted",
    )


def validate(ledger, results: list[AllocationResult]) -> list[ValidationResult]:
    """Run every gate over a closed month. Pure, deterministic, no model call."""
    return [
        g1_entries_balance(ledger),
        g2_remittances_reconcile(results),
        g3_bank_movement_agrees(ledger, ledger.documents),
        g4_no_double_posting(ledger),
        g5_unreadable_left_unposted(ledger),
    ]


def all_passed(gates: list[ValidationResult]) -> bool:
    return all(g.passed for g in gates)


def summary(gates: list[ValidationResult]) -> str:
    passed = sum(1 for g in gates if g.passed)
    return f"{passed}/{len(gates)} gates passed"
