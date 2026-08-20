"""The agent's authority, and the boundary the ledger draws around it.

This file exists because of a fair criticism: the first design gave the agent
six tools that reported slices of a function which had already run, and a test
that locked in "the agent's decisions change nothing". On a submission judged
mostly on autonomous action, that is decoration.

So these assert both halves of the fix. The agent really decides, and the
ledger really overrules it, and the overrule is always recorded rather than
silent.
"""
from __future__ import annotations

import pytest

from archon.domain.models import ExceptionKind, Finding
from archon.domain.policy import (
    ALWAYS_ESCALATE,
    Disposition,
    apply_choices,
    decide_outcome,
    default_policy,
    summarise,
)
from archon.domain.validation import ValidationResult


def finding(kind: ExceptionKind, amount: float = 100.0, severity: str = "error") -> Finding:
    return Finding(kind=kind, severity=severity, reference="REF", amount=amount,
                   message="m", counterparty="Someone")


def gates(passed: bool = True):
    return [ValidationResult(rule="G1: x", passed=passed,
                             severity="info" if passed else "error", message="m")]


# ── the agent genuinely decides ──────────────────────────────────────────────

def test_the_agent_can_choose_not_to_chase_something_chaseable():
    """The decision a rule cannot make: a small dispute against a broker who
    sends steady work. The standing policy always chases. An agent may not."""
    findings = [finding(ExceptionKind.SHORT_PAY, 40.0)]

    standing = apply_choices(findings, None)
    agent = apply_choices(findings, {0: Disposition.NOTE})

    assert standing[0].applied is Disposition.DRAFT
    assert agent[0].applied is Disposition.ESCALATE   # error, so seen but not chased
    assert agent[0].chosen is Disposition.NOTE


def test_a_warning_the_agent_notes_is_left_alone_entirely():
    findings = [finding(ExceptionKind.LOAD_UNPAID, 40.0, severity="warning")]

    decisions = apply_choices(findings, {0: Disposition.NOTE})

    assert decisions[0].applied is Disposition.NOTE
    assert not decisions[0].clamped


def test_the_agent_can_escalate_instead_of_writing_a_letter():
    findings = [finding(ExceptionKind.LOAD_UNPAID, 2780.0, severity="warning")]

    decisions = apply_choices(findings, {0: Disposition.ESCALATE})

    assert decisions[0].applied is Disposition.ESCALATE
    assert not decisions[0].clamped


def test_two_different_deciders_produce_two_different_outcomes():
    """The property the old design could not have: the decision matters."""
    findings = [finding(ExceptionKind.SHORT_PAY, 200.0),
                finding(ExceptionKind.DUPLICATE_CHARGE, 412.85)]

    chasing = apply_choices(findings, {0: Disposition.DRAFT, 1: Disposition.DRAFT})
    cautious = apply_choices(findings, {0: Disposition.DRAFT, 1: Disposition.ESCALATE})

    assert sum(d.drafts_a_letter for d in chasing) == 2
    assert sum(d.drafts_a_letter for d in cautious) == 1


# ── and the ledger overrules it ──────────────────────────────────────────────

def test_a_kind_with_no_honest_letter_cannot_be_drafted():
    findings = [finding(ExceptionKind.AMOUNT_OUTLIER, 1890.0, severity="warning")]

    decision = apply_choices(findings, {0: Disposition.DRAFT})[0]

    assert decision.chosen is Disposition.DRAFT
    assert decision.applied is Disposition.ESCALATE
    assert decision.clamped
    assert "no honest letter" in decision.reason


def test_a_finding_worth_nothing_cannot_be_chased():
    findings = [finding(ExceptionKind.SHORT_PAY, 0.0)]

    decision = apply_choices(findings, {0: Disposition.DRAFT})[0]

    assert decision.applied is Disposition.ESCALATE
    assert "wastes a counterparty" in decision.reason


@pytest.mark.parametrize("kind", sorted(ALWAYS_ESCALATE, key=lambda k: k.value))
@pytest.mark.parametrize("chosen", [Disposition.DRAFT, Disposition.NOTE])
def test_some_kinds_always_reach_a_person_whatever_the_agent_says(kind, chosen):
    """An unreadable document noted away is how a month closes over a missing
    invoice. No decision may do that."""
    decision = apply_choices([finding(kind, 0.0, severity="warning")], {0: chosen})[0]

    assert decision.applied is Disposition.ESCALATE
    assert decision.clamped
    assert "always needs a person" in decision.reason


def test_an_error_may_be_left_unchased_but_not_left_unseen():
    decision = apply_choices([finding(ExceptionKind.SHORT_PAY, 200.0)],
                             {0: Disposition.NOTE})[0]

    assert decision.applied is Disposition.ESCALATE
    assert "not left unseen" in decision.reason


def test_a_missing_choice_defaults_to_noting_rather_than_acting():
    """An agent that says nothing about a finding must not cause a letter."""
    decision = apply_choices([finding(ExceptionKind.LOAD_UNPAID, 100.0,
                                      severity="warning")], {})[0]

    assert decision.applied is Disposition.NOTE


def test_every_overrule_carries_its_reason():
    findings = [finding(ExceptionKind.AMOUNT_OUTLIER, 10.0, severity="warning"),
                finding(ExceptionKind.UNREADABLE_DOCUMENT, 0.0, severity="warning")]

    for decision in apply_choices(findings, {0: Disposition.DRAFT, 1: Disposition.NOTE}):
        assert decision.clamped
        assert decision.reason and decision.reason != "as decided"


# ── the outcome, and the one direction the agent cannot argue ────────────────

def test_the_agent_may_withhold_a_close_every_gate_passed():
    """Every gate can pass over books that are quietly wrong. A bookkeeper who
    says 'this does not look right' is doing the job."""
    outcome, reason = decide_outcome(gates(True), [], agent_verdict="blocked")

    assert outcome == "blocked"
    assert "withheld" in reason


def test_the_agent_can_ask_for_more_documents():
    outcome, _ = decide_outcome(gates(True), [], agent_verdict="needs_documents")

    assert outcome == "needs_documents"


@pytest.mark.parametrize("verdict", ["closed", None, "anything at all"])
def test_no_verdict_can_close_a_month_whose_gates_failed(verdict):
    """The one direction that is arithmetic, not judgement."""
    outcome, reason = decide_outcome(gates(False), [], agent_verdict=verdict)

    assert outcome == "blocked"
    assert "a gate failed" in reason


def test_with_no_agent_a_clean_month_closes():
    outcome, reason = decide_outcome(gates(True), [], agent_verdict=None)

    assert outcome == "closed"
    assert "nothing was withheld" in reason


# ── the standing policy, for when there is no agent ──────────────────────────

def test_the_standing_policy_chases_everything_chaseable():
    findings = [finding(ExceptionKind.SHORT_PAY, 200.0),
                finding(ExceptionKind.AMOUNT_OUTLIER, 1890.0, severity="warning"),
                finding(ExceptionKind.UNREADABLE_DOCUMENT, 0.0, severity="warning")]

    choices = default_policy(findings)

    assert choices[0] is Disposition.DRAFT
    assert choices[1] is Disposition.NOTE
    assert choices[2] is Disposition.ESCALATE


def test_the_summary_names_what_was_overruled():
    findings = [finding(ExceptionKind.AMOUNT_OUTLIER, 10.0, severity="warning")]

    line = summarise(apply_choices(findings, {0: Disposition.DRAFT}))

    assert "1 escalate" in line
    assert "1 overruled by the ledger" in line
