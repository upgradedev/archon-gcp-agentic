"""Which month a document belongs to, and whether it belongs to this one.

Extracted because two callers need the same answer and were not getting it.
`exceptions.find_out_of_period` computed the bounds and raised a warning;
`Ledger._post` computed nothing and posted the figures anyway. The detector's
own docstring already said what should happen -- "what it must not do is post
it silently into the wrong month, which is how a period gets reopened three
weeks later" -- so the rule existed in prose and in one of the two places that
had to obey it.

**`Document.period` is not the document's month.** It is the month being
closed, stamped by `extract_document(text, period=...)` on every document it
produces, so a June invoice read during a July close carries `period="2026-07"`
and comparing it to the ledger's period can never be false. The month a
document belongs to lives in its DATE, which is why everything here works from
`doc.date` and why a check written the obvious way silently passed.
"""
from __future__ import annotations

from datetime import date, datetime

#: Deliberately no bare `%d/%m/%y`. A two-digit year is a coin flip and
#: `extract._iso_date` already refuses ambiguous slashed dates upstream.
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y")


def parse_date(value: str | None) -> date | None:
    """Best-effort date parse. Returns None rather than guessing."""
    if not value:
        return None
    text = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def period_bounds(period: str) -> tuple[date, date] | None:
    """First day of the period and first day of the next, or None if unparseable."""
    try:
        year, month = int(period[:4]), int(period[5:7])
    except (ValueError, IndexError):
        return None
    first = date(year, month, 1)
    last = date(year + (month == 12), (month % 12) + 1, 1)
    return first, last


def belongs_to(period: str, when: str | None) -> bool:
    """Does a document dated `when` belong in `period`?

    An UNDATED document belongs, and that is deliberate. Refusing to post
    everything whose date could not be read would empty the books over a
    formatting change, and an unreadable date is already its own finding. The
    boundary is half-open so the first and last day of the month are inside it
    and the first of the next month is not.
    """
    bounds = period_bounds(period)
    parsed = parse_date(when)
    if bounds is None:
        # The PERIOD is unparseable, which is a programming error rather than a
        # document problem, and refusing every document over it would turn a
        # bad argument into an empty month. Nothing to place documents against,
        # so place them.
        return True
    if parsed is None:
        # The DOCUMENT has no date this product can read. It returned True
        # here, which is fail-open on the one question that decides whether
        # money lands in this month or another: an invoice dated "sometime in
        # July" was posted into whatever month happened to be closing.
        #
        # Refusing is the honest answer and it is not the end of the story.
        # `find_out_of_period` raises an error finding naming the file, and G6
        # refuses the month, so an undated document stops the close rather than
        # quietly joining it.
        return False
    first, next_first = bounds
    return first <= parsed < next_first
