"""The fact sheet, and the prose that may only ever phrase it.

This module is the boundary where a language model is finally allowed near the
month, and the shape of that boundary is the product's central claim. The model
is handed a fact sheet of already-computed figures and asked for English. It is
never handed the documents, never asked for a total, and never in a position to
introduce a number, because every number it could reach is already a string by
the time it sees it.

`facts_sheet` is what goes in. `narrate` is the deterministic English that comes
out when there is no key, no network, or no wish to spend: it is not a
degraded mode but the reference text, and the Gemini path in `agents.py` is
judged against it. That ordering matters. If the offline sentence and the model
sentence disagree about a figure, the offline sentence is right.
"""
from __future__ import annotations

from .models import Draft, Finding, Statements, ValidationResult

#: How many findings the owner-facing summary names before it stops listing.
TOP_FINDINGS = 5


def facts_sheet(statements: Statements, findings: list[Finding],
                gates: list[ValidationResult], drafts: list[Draft]) -> str:
    """Every figure the narrative is allowed to use, as plain text.

    Deliberately readable. When a judge asks what grounds the summary, the
    answer is this block, and it should be legible without a debugger.
    """
    lines = [
        f"PERIOD {statements.period}",
        "",
        "PROFIT AND LOSS",
        f"  revenue (linehaul)      {statements.revenue_linehaul:>14,.2f}",
        f"  revenue (accessorial)   {statements.revenue_accessorial:>14,.2f}",
        f"  revenue (total)         {statements.revenue:>14,.2f}",
        f"  fuel                    {statements.fuel:>14,.2f}",
        f"  tolls                   {statements.tolls:>14,.2f}",
        f"  maintenance             {statements.maintenance:>14,.2f}",
        f"  insurance               {statements.insurance:>14,.2f}",
        f"  driver pay              {statements.driver_pay:>14,.2f}",
        f"  factoring fees          {statements.factoring_fees:>14,.2f}",
        f"  operating expenses      {statements.operating_expenses:>14,.2f}",
        f"  net profit              {statements.net_profit:>14,.2f}",
        "",
        "CASH",
        f"  cash in                 {statements.cash_in:>14,.2f}",
        f"  cash out                {statements.cash_out:>14,.2f}",
        f"  net cash                {statements.net_cash:>14,.2f}",
        f"  receivables             {statements.accounts_receivable:>14,.2f}",
        f"  payables                {statements.accounts_payable:>14,.2f}",
        "",
        "OPERATIONS",
        f"  miles run               {statements.total_miles:>14,.2f}",
        f"  revenue per mile        {_or_dash(statements.revenue_per_mile)}",
        f"  cost per mile           {_or_dash(statements.cost_per_mile)}",
    ]

    if statements.per_truck:
        lines.append("")
        lines.append("PER TRUCK")
        for truck, row in sorted(statements.per_truck.items()):
            lines.append(
                f"  {truck:<10} miles {row['miles']:>9,.0f}  revenue {row['revenue']:>11,.2f}"
                f"  direct cost {row['direct_cost']:>10,.2f}"
                f"  cost/mile {_or_dash(row['cost_per_mile'])}"
            )

    lines += ["", f"EXCEPTIONS ({len(findings)})"]
    for finding in findings:
        lines.append(
            f"  [{finding.severity}] {finding.kind.value} {finding.reference} "
            f"{finding.amount:,.2f} - {finding.message}"
        )

    lines += ["", f"GATES ({sum(1 for g in gates if g.passed)}/{len(gates)} passed)"]
    for gate in gates:
        lines.append(f"  {'PASS' if gate.passed else 'FAIL'} {gate.rule}: {gate.message}")

    lines += ["", f"DRAFTS FILED ({len(drafts)}, none sent)"]
    for draft in drafts:
        lines.append(
            f"  {draft.kind.value} to {draft.recipient} re {draft.reference} "
            f"{draft.amount:,.2f}"
        )

    for note in statements.notes:
        lines += ["", f"NOTE {note}"]

    return "\n".join(lines)


def _or_dash(value: float | None) -> str:
    return f"{value:>14,.3f}" if value is not None else f"{'not computable':>14}"


def narrate(statements: Statements, findings: list[Finding],
            gates: list[ValidationResult], drafts: list[Draft]) -> str:
    """The deterministic month-end summary. No key, no network, no model.

    Written the way the owner would want to hear it: the result, then the money
    still on the table, then what was done about it, then what a person still
    has to do.
    """
    errors = [f for f in findings if f.severity == "error"]
    at_stake = round(sum(f.amount for f in errors), 2)

    profit_word = "made" if statements.net_profit >= 0 else "lost"
    sentences = [
        f"{statements.period} closed on {statements.total_miles:,.0f} miles, "
        f"{statements.revenue:,.2f} billed and {statements.operating_expenses:,.2f} spent, "
        f"so the firm {profit_word} {abs(statements.net_profit):,.2f}."
    ]

    if statements.cost_per_mile is not None and statements.revenue_per_mile is not None:
        margin = round(statements.revenue_per_mile - statements.cost_per_mile, 3)
        sentences.append(
            f"That is {statements.revenue_per_mile:,.3f} a mile earned against "
            f"{statements.cost_per_mile:,.3f} a mile spent, leaving {margin:,.3f} a mile."
        )

    if statements.factoring_fees:
        sentences.append(
            f"Factoring took {statements.factoring_fees:,.2f} before the money reached "
            f"the bank, which is why cash in of {statements.cash_in:,.2f} is below "
            f"what was billed."
        )

    if errors:
        sentences.append(
            f"{len(errors)} problem(s) worth {at_stake:,.2f} need attention, worst first: "
            + "; ".join(f"{f.reference} ({f.amount:,.2f})" for f in errors[:TOP_FINDINGS])
            + "."
        )
    else:
        sentences.append("No errors were found in the month's documents.")

    if drafts:
        sentences.append(
            f"{len(drafts)} corrective document(s) were drafted and filed, none sent; "
            f"they are waiting for you to approve and send them."
        )

    failed = [g for g in gates if not g.passed]
    if failed:
        sentences.append(
            f"The close did not pass {len(failed)} of its own gates ("
            + "; ".join(g.rule.split(":")[0] for g in failed)
            + "), so these books are not trustworthy until that is fixed."
        )

    return " ".join(sentences)


NARRATOR_INSTRUCTION = """\
You are the reporting stage of a bookkeeping agent that has just closed a month
for a small trucking firm. You are given a fact sheet of figures that have
already been computed and verified.

Write two to four sentences for the owner. Lead with the result, then the money
still recoverable, then what the agent already did about it.

Report only figures that appear in the fact sheet. Do not add, total, average or
infer any number. If a figure you want is not in the sheet, leave it out.
"""
