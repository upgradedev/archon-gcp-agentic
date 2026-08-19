"""The month-end digest: the one thing that arrives without being asked for.

Everything else in this product happens on a surface Archon built. A haulier
does not open a bookkeeping console on the first of the month, because a
haulier is driving. If the only place the answer exists is a page we made, we
have shipped another tab nobody opens, and the work the agent did overnight
sits there unread.

So the close ends by writing the owner a short letter and putting it where they
already are. The subject line carries the number that decides whether they read
further, and the body is four blocks: what the month came to, what is
recoverable and from whom, what Archon already did about it, and the one thing
left for a person to do.

**This is not the outbound edge.** The broker's inbox is a third party and stays
behind the human gate in `drafts.py`. The owner's own inbox is the owner's own
books arriving at the owner, which is the whole point of doing the work
unattended. Those are two different boundaries and the product treats them
differently on purpose: the letters to counterparties are composed and filed
unsent, and this one is composed and delivered.

Every figure here comes from the close that produced it. The digest is a view,
never a second computation, so it cannot disagree with the books it summarises.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .drafts import recoverable
from .models import Draft

#: How many actions the digest names before it stops listing. A digest that
#: lists everything is a report, and a report is the thing this replaces.
TOP_ACTIONS = 3


@dataclass
class Digest:
    """One month-end letter to the owner, ready to be delivered."""

    period: str
    company: str
    recipient: str
    subject: str
    body: str
    outcome: str
    run_id: str
    net_profit: float
    recoverable: float
    action_count: int
    attachments: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "period": self.period, "company": self.company, "recipient": self.recipient,
            "subject": self.subject, "body": self.body, "outcome": self.outcome,
            "run_id": self.run_id, "net_profit": self.net_profit,
            "recoverable": self.recoverable, "action_count": self.action_count,
            "attachments": list(self.attachments),
        }


def _money(amount: float) -> str:
    return f"{amount:,.2f}"


def subject_line(period: str, statements, drafts: list[Draft], outcome: str) -> str:
    """The subject decides whether the rest is read, so it carries the number.

    Money still recoverable beats profit, because profit is a fact the owner
    can do nothing about today and the recoverable figure is a list of phone
    calls they can make this morning.
    """
    if outcome != "closed":
        return f"{period}: the close did not pass its own checks"
    chasing = recoverable(drafts)
    if chasing:
        return f"{period} closed. {_money(chasing)} is recoverable, {len(drafts)} letters ready"
    return f"{period} closed. {_money(statements.net_profit)} profit, nothing outstanding"


def compose(result, recipient: str, company: str | None = None) -> Digest:
    """Build the owner's month-end letter from a finished close.

    Takes the whole `CloseResult` rather than loose figures on purpose: there
    is then no way for this to hold a number the close did not produce.
    """
    statements = result.statements
    company = company or result.company or "your firm"
    errors = [f for f in result.findings if f.severity == "error"]

    lines = [
        result.summary,
        "",
        "THE MONTH",
        f"  revenue            {_money(statements.revenue):>14}",
        f"  operating costs    {_money(statements.operating_expenses):>14}",
        f"  net profit         {_money(statements.net_profit):>14}",
    ]
    if statements.cost_per_mile is not None:
        lines += [
            f"  miles run          {statements.total_miles:>14,.0f}",
            f"  earned per mile    {statements.revenue_per_mile:>14,.3f}",
            f"  spent per mile     {statements.cost_per_mile:>14,.3f}",
        ]

    if result.drafts:
        lines += ["", f"WHAT I ALREADY DID ({len(result.drafts)} letters written and filed)"]
        for draft in result.drafts[:TOP_ACTIONS]:
            lines.append(
                f"  {_money(draft.amount):>12}  {draft.recipient}: {draft.subject}"
            )
        if len(result.drafts) > TOP_ACTIONS:
            lines.append(f"  and {len(result.drafts) - TOP_ACTIONS} more, all in the app.")
        chasing = recoverable(result.drafts)
        lines += ["", f"  Money these can actually recover: {_money(chasing)}"]
        no_recovery = round(sum(d.amount for d in result.drafts) - chasing, 2)
        if no_recovery:
            lines.append(
                f"  A further {_money(no_recovery)} is documentation I asked for rather "
                f"than money I can win back."
            )

    watch = [f for f in result.findings if f.severity != "error" and not f.actionable]
    if watch:
        lines += ["", "WORTH A LOOK, NOTHING WRITTEN"]
        for finding in watch[:TOP_ACTIONS]:
            lines.append(f"  {finding.reference}: {finding.message}")

    lines += ["", "WHAT I NEED FROM YOU"]
    if result.outcome != "closed":
        failed = [g.rule.split(":")[0] for g in result.gates if not g.passed]
        lines.append(
            f"  These books did not pass my own checks ({', '.join(failed)}), so do not "
            f"rely on them until that is fixed."
        )
    elif result.drafts:
        lines.append(
            f"  Read the {len(result.drafts)} letters and press send on the ones you agree "
            f"with. I have not sent any of them, and I will not."
        )
    else:
        lines.append("  Nothing. The month is closed and there is nothing outstanding.")

    lines += ["", f"  {len(errors)} error(s), {len(result.findings)} exception(s) in total.",
              f"  Run {result.run_id}, {len(result.journal.steps) + 1} steps, "
              f"every one on the record.", "", f"  {company}"]

    return Digest(
        period=result.period,
        company=company,
        recipient=recipient,
        subject=subject_line(result.period, statements, result.drafts, result.outcome),
        body="\n".join(lines),
        outcome=result.outcome,
        run_id=result.run_id,
        net_profit=statements.net_profit,
        recoverable=recoverable(result.drafts),
        action_count=len(result.drafts),
        attachments=[f"{d.kind.value}-{d.reference}.txt" for d in result.drafts],
    )
