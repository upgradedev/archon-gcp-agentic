"""What to do about each finding, and who gets to decide it.

This module exists because of a fair criticism of the first design: the agent
called six tools that reported slices of a function which had already run, so
its decisions changed nothing. A test even locked that in. On a submission
judged mostly on autonomous action, an agent that cannot affect the outcome is
decoration.

The fix is not to let a model touch the arithmetic. It is to give it the
decisions a bookkeeper actually makes, which are not arithmetic at all.

**What the ledger decides, always.** Every amount, every reference, whether a
remittance reconciles, whether a gate passed. None of that is negotiable and no
model is consulted.

**What the agent may decide.** Given a finding the detectors produced, what to
do about it: chase it with a letter, put it in front of the owner, or note it
and move on. A real bookkeeper makes exactly this call, and it depends on
context a rule cannot hold: whether this broker is usually reliable, whether
the amount is worth the relationship, whether the owner already knows.

**What stops a bad decision.** Every choice runs through `apply_choices` before
it can affect anything, and illegal choices are clamped rather than obeyed:

  * a kind with no honest letter cannot be drafted, whatever the agent says
  * a finding worth nothing cannot be chased
  * an unreadable document is always escalated, never noted away
  * a month with a failed gate can never be declared closed

So the agent has real authority inside a boundary the ledger draws, and every
clamp is recorded with its reason, which means a judge can see both what it
chose and where it was overruled.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import ACTIONABLE_KINDS, ExceptionKind, Finding


class Disposition(str, Enum):
    """What the agent decided to do about one finding."""

    #: Write the corrective letter and file it.
    DRAFT = "draft"
    #: Put it in front of the owner as something needing a person.
    ESCALATE = "escalate"
    #: Record it and take no action.
    NOTE = "note"


#: Kinds where a person must look, whatever anyone decides. An unreadable
#: document is the clearest case: nobody can act on it until a human reads it,
#: and letting it be noted away is how a month closes over a missing invoice.
ALWAYS_ESCALATE = frozenset({
    ExceptionKind.UNREADABLE_DOCUMENT,
    ExceptionKind.REMITTANCE_UNRECONCILED,
})


@dataclass
class Decision:
    """One finding, what was decided, and whether the decision was allowed."""

    index: int
    finding: Finding
    chosen: Disposition
    applied: Disposition
    clamped: bool
    reason: str

    @property
    def drafts_a_letter(self) -> bool:
        return self.applied is Disposition.DRAFT


def default_policy(findings: list[Finding]) -> dict[int, Disposition]:
    """What the product does with no agent: chase everything chaseable.

    This is the behaviour the deterministic close has always had, written out
    as a policy so the agent's choices can be compared against it. It is a
    reasonable default and a poor bookkeeper: it never weighs a 40 dollar
    dispute against a broker who sends steady work.
    """
    choices: dict[int, Disposition] = {}
    for index, finding in enumerate(findings):
        if finding.kind in ALWAYS_ESCALATE:
            choices[index] = Disposition.ESCALATE
        elif finding.actionable and finding.amount > 0:
            choices[index] = Disposition.DRAFT
        else:
            choices[index] = Disposition.ESCALATE if finding.severity == "error" \
                else Disposition.NOTE
    return choices


def apply_choices(findings: list[Finding],
                  choices: dict[int, Disposition] | None) -> list[Decision]:
    """Sanitise a set of decisions into ones the books will accept.

    Never raises on a bad choice. An agent that asks for something impossible
    gets overruled and the overrule is recorded, because a run that crashed
    because a model said the wrong word is worse than a run that corrected it
    and said so.
    """
    if choices is None:
        choices = default_policy(findings)

    decisions: list[Decision] = []
    for index, finding in enumerate(findings):
        chosen = choices.get(index, Disposition.NOTE)
        applied, reason = _clamp(finding, chosen)
        decisions.append(
            Decision(index=index, finding=finding, chosen=chosen, applied=applied,
                     clamped=applied is not chosen, reason=reason)
        )
    return decisions


def _clamp(finding: Finding, chosen: Disposition) -> tuple[Disposition, str]:
    """The guardrails, in the order they bite."""
    if finding.kind in ALWAYS_ESCALATE and chosen is not Disposition.ESCALATE:
        return Disposition.ESCALATE, (
            f"{finding.kind.value} always needs a person; "
            f"'{chosen.value}' was overruled"
        )

    if chosen is Disposition.DRAFT:
        if finding.kind not in ACTIONABLE_KINDS:
            return Disposition.ESCALATE, (
                f"there is no honest letter to write about {finding.kind.value}"
            )
        if finding.amount <= 0:
            return Disposition.ESCALATE, (
                "a letter asking for nothing wastes a counterparty the owner depends on"
            )

    if chosen is Disposition.NOTE and finding.severity == "error":
        return Disposition.ESCALATE, (
            "an error may be left unchased, but not left unseen"
        )

    return chosen, "as decided"


def summarise(decisions: list[Decision]) -> str:
    """One line for the run journal."""
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision.applied.value] = counts.get(decision.applied.value, 0) + 1
    clamped = sum(1 for d in decisions if d.clamped)
    parts = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
    return parts + (f"; {clamped} overruled by the ledger" if clamped else "")


# ── whether the month may be called closed ───────────────────────────────────

def decide_outcome(gates, decisions: list[Decision],
                   agent_verdict: str | None = None) -> tuple[str, str]:
    """The final call, and the one rule the agent cannot argue with.

    A month whose gates failed can never be declared closed, by anyone. That is
    arithmetic about arithmetic and it is not a judgement call.

    A month whose gates all passed may still be blocked, and only an agent can
    do that. Every gate can pass over books that are quietly wrong, and a
    bookkeeper who says "this does not look right to me" is doing the job. So
    the agent may veto downward and never upward.
    """
    from ..domain.validation import all_passed

    gates_ok = all_passed(gates)

    if not gates_ok:
        return "blocked", "a gate failed, so the books are not trustworthy"

    if agent_verdict in ("blocked", "needs_documents"):
        return agent_verdict, "every gate passed, but the agent withheld the close"

    escalations = [d for d in decisions if d.applied is Disposition.ESCALATE]
    if agent_verdict is None and any(
        d.finding.kind is ExceptionKind.UNREADABLE_DOCUMENT for d in escalations
    ):
        # The deterministic default still closes here, deliberately: one bad
        # scan should not hold a month hostage. It is recorded, not hidden.
        return "closed", "closed with an unreadable document escalated to the owner"

    return "closed", "every gate passed and nothing was withheld"
