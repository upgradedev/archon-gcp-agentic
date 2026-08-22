"""The open items: what is owed to the firm, and what the firm owes.

Every accounting package has this screen and it is the one an owner actually
opens, because it answers the two questions that decide whether payroll clears:
who has not paid me, and who have I not paid.

Archon had neither. It reported a single accounts-receivable total rolled up
from the journal, which tells you the size of the problem and nothing about its
shape. A total cannot be chased. A named counterparty with an amount and an age
can.

Two deliberate choices.

**A short pay leaves a partial receivable, not a closed one.** If a load was
invoiced at 2,460 and the broker paid 2,260 with no stated reason, 200 is still
open. Treating the load as settled because money arrived against it is how the
200 disappears, and disappearing quietly is exactly what this product exists to
stop.

**Age is measured to the end of the period being closed, not to today.** A
close is a statement about a month. Re-running last July in December must not
silently age everything by five months, because then the same run produces two
different answers and the books stop being reproducible.

Pure and deterministic. No model, no clock, no network.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .allocation import loads_by_ref, settled_load_refs
from .exceptions import _period_bounds, parse_date
from .models import AllocationResult, DocType, Document

#: Buckets an owner thinks in. A receivable at 90 days is a different
#: conversation from one at 20, and the boundaries are the ones a haulier's
#: broker terms actually use rather than a generic 30/60/90.
AGE_BUCKETS = ((0, 14, "current"), (15, 30, "15-30 days"),
               (31, 60, "31-60 days"), (61, 10_000, "over 60 days"))


@dataclass
class OpenItem:
    """One thing still owed, in either direction."""

    direction: str            # "receivable" | "payable"
    counterparty: str
    reference: str
    invoiced: float
    paid: float
    open_amount: float
    document_date: str | None
    age_days: int | None
    bucket: str
    source_file: str | None = None
    note: str = ""

    @property
    def partially_paid(self) -> bool:
        return self.paid > 0 and self.open_amount > 0


@dataclass
class Register:
    """Both sides, with the totals an owner reads first."""

    period: str
    receivables: list[OpenItem]
    payables: list[OpenItem]

    @property
    def owed_to_us(self) -> float:
        return round(sum(item.open_amount for item in self.receivables), 2)

    @property
    def owed_by_us(self) -> float:
        return round(sum(item.open_amount for item in self.payables), 2)

    @property
    def net_position(self) -> float:
        """Positive means more is coming in than going out."""
        return round(self.owed_to_us - self.owed_by_us, 2)

    def by_counterparty(self, direction: str) -> dict[str, float]:
        """Open amount per counterparty, which is how it gets chased."""
        items = self.receivables if direction == "receivable" else self.payables
        totals: dict[str, float] = {}
        for item in items:
            totals[item.counterparty] = round(
                totals.get(item.counterparty, 0.0) + item.open_amount, 2
            )
        return dict(sorted(totals.items(), key=lambda kv: -kv[1]))

    def aged(self, direction: str) -> dict[str, float]:
        items = self.receivables if direction == "receivable" else self.payables
        totals = {label: 0.0 for _, _, label in AGE_BUCKETS}
        totals["undated"] = 0.0
        for item in items:
            totals[item.bucket] = round(totals[item.bucket] + item.open_amount, 2)
        return {label: total for label, total in totals.items() if total}

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return {
            "period": self.period,
            "owed_to_us": self.owed_to_us,
            "owed_by_us": self.owed_by_us,
            "net_position": self.net_position,
            "receivables": [asdict(i) for i in self.receivables],
            "payables": [asdict(i) for i in self.payables],
            "receivables_by_counterparty": self.by_counterparty("receivable"),
            "payables_by_counterparty": self.by_counterparty("payable"),
            "receivables_aged": self.aged("receivable"),
            "payables_aged": self.aged("payable"),
        }


def _age(document_date: str | None, period_end: date | None) -> tuple[int | None, str]:
    when = parse_date(document_date)
    if when is None or period_end is None:
        return None, "undated"
    days = max(0, (period_end - when).days)
    for low, high, label in AGE_BUCKETS:
        if low <= days <= high:
            return days, label
    return days, "over 60 days"


def _period_end(period: str) -> date | None:
    bounds = _period_bounds(period)
    if bounds is None:
        return None
    from datetime import timedelta

    return bounds[1] - timedelta(days=1)


#: Expense families that create a payable when they are posted.
_PAYABLE_KINDS = (DocType.TOLL_INVOICE, DocType.MAINTENANCE_INVOICE,
                  DocType.INSURANCE_INVOICE, DocType.FUEL_CARD_STATEMENT)


def _paid_references(documents: list[Document]) -> set[str]:
    """Every reference an outgoing bank line settles."""
    return {
        (d.reference or "").strip()
        for d in documents
        if d.doc_type is DocType.BANK_TRANSACTION and d.direction == "out"
    } - {""}


def build(documents: list[Document], allocations: list[AllocationResult],
         period: str) -> Register:
    """The open items on both sides, as at the end of the period."""
    period_end = _period_end(period)

    receivables = _receivables(documents, allocations, period_end)
    payables = _payables(documents, period_end)
    return Register(period=period, receivables=receivables, payables=payables)


def _receivables(documents: list[Document], allocations: list[AllocationResult],
                 period_end: date | None) -> list[OpenItem]:
    """Loads delivered and invoiced that are not fully paid.

    Covers both the load nobody has paid at all and the load a broker paid
    light, which is the one a total would hide.
    """
    paid_by_ref: dict[str, float] = {}
    for result in allocations:
        for line in result.allocations:
            paid_by_ref[line.load_ref] = round(
                paid_by_ref.get(line.load_ref, 0.0) + line.paid + line.deduction, 2
            )
    settled = settled_load_refs(allocations)

    items: list[OpenItem] = []
    for ref, load in sorted(loads_by_ref(documents).items()):
        invoiced = round((load.net_amount or 0.0) + (load.accessorial or 0.0), 2)
        paid = paid_by_ref.get(ref, 0.0)
        open_amount = round(invoiced - paid, 2)
        if open_amount < 0.01:
            continue
        age_days, bucket = _age(load.date, period_end)
        items.append(OpenItem(
            direction="receivable",
            counterparty=load.broker or load.counterparty or "unknown broker",
            reference=ref,
            invoiced=invoiced,
            paid=round(paid, 2),
            open_amount=open_amount,
            document_date=load.date,
            age_days=age_days,
            bucket=bucket,
            source_file=load.source_file,
            note=("paid short" if ref in settled else "no payment received"),
        ))
    return sorted(items, key=lambda i: -i.open_amount)


def _payables(documents: list[Document], period_end: date | None) -> list[OpenItem]:
    """Bills posted that no outgoing bank line has settled.

    A bill is treated as paid when a bank line references its document number.
    That is the same evidence the orphan-payment detector uses from the other
    direction, so the two can never disagree about whether something was paid.
    """
    paid = _paid_references(documents)
    items: list[OpenItem] = []

    for doc in documents:
        if doc.doc_type not in _PAYABLE_KINDS:
            continue
        reference = (doc.document_number or doc.source_file or "").strip()
        gross = round(doc.gross_amount or ((doc.net_amount or 0.0) + (doc.tax_amount or 0.0)), 2)
        if not reference or gross < 0.01 or reference in paid:
            continue
        age_days, bucket = _age(doc.date, period_end)
        items.append(OpenItem(
            direction="payable",
            counterparty=doc.counterparty or "unknown supplier",
            reference=reference,
            invoiced=gross,
            paid=0.0,
            open_amount=gross,
            document_date=doc.date,
            age_days=age_days,
            bucket=bucket,
            source_file=doc.source_file,
            note="no payment recorded against it",
        ))

    # Driver settlements are a payable until the net leaves the bank. The bank
    # line names the settlement, so the same reference rule applies.
    for doc in documents:
        if doc.doc_type is not DocType.DRIVER_SETTLEMENT:
            continue
        reference = (doc.document_number or "").strip()
        net = round(doc.driver_net or 0.0, 2)
        if not reference or net < 0.01:
            continue
        if any(reference in settled_ref for settled_ref in paid):
            continue
        age_days, bucket = _age(doc.date, period_end)
        items.append(OpenItem(
            direction="payable",
            counterparty=doc.driver or "driver",
            reference=reference,
            invoiced=net,
            paid=0.0,
            open_amount=net,
            document_date=doc.date,
            age_days=age_days,
            bucket=bucket,
            source_file=doc.source_file,
            note="driver settlement not yet paid",
        ))

    return sorted(items, key=lambda i: -i.open_amount)
