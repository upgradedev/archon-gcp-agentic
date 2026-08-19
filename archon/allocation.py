"""Split one broker remittance back across the loads it settles.

This is the step that makes a haulier's month closeable, and it is the reason
this product is not a chatbot. A broker does not pay per load. It pays once a
fortnight, in a single bank credit, covering however many loads it feels like,
minus a factoring fee charged on the whole batch, minus whatever it decided to
hold back on individual loads. The bank shows one number. The books need nine.

Matching cannot do this. Matching asks "which document is this payment?" and
the answer here is "nine of them, at amounts none of which equal the payment."
Allocation asks a harder question and then proves its own answer:

    what landed in the bank  ==  what the lines pay  −  the fee charged once

That identity is `AllocationResult.residual`, and the close raises a residual
as an exception rather than absorbing it. A suspense account is where a
bookkeeper hides the thing they could not explain, and hiding it is precisely
what the owner is paying Archon not to do.

Pure and deterministic. Given the same documents this returns the same split
every time, which is what lets the whole thing be tested offline and what lets
a judge re-run the close and get the same books.
"""
from __future__ import annotations

from .models import (
    Allocation,
    AllocationResult,
    DocType,
    Document,
)

#: A load is settled in full when paid + stated deduction lands within this many
#: dollars of what it was invoiced at. Below a dollar is rounding between the
#: broker's system and ours; above it is a short-pay somebody should chase.
SETTLEMENT_TOLERANCE = 1.00


def _invoiced_amount(load: Document) -> float:
    """What a load confirmation says the broker owes, all in."""
    return round((load.net_amount or 0.0) + (load.accessorial or 0.0), 2)


def loads_by_ref(documents: list[Document]) -> dict[str, Document]:
    """Index the period's load confirmations by their reference."""
    return {
        d.load_ref: d
        for d in documents
        if d.doc_type == DocType.LOAD_CONFIRMATION and d.load_ref
    }


def allocate_remittance(remittance: Document, documents: list[Document]) -> AllocationResult:
    """Split one remittance across its load lines and check the identity holds.

    Every line becomes an `Allocation`, matched or not. A line naming a load
    Archon has never seen is still allocated (the money did arrive) but flagged
    unmatched, because the missing artifact is the finding, not the money.
    """
    loads = loads_by_ref(documents)
    ref = remittance.document_number or remittance.source_file or "remittance"

    allocations: list[Allocation] = []
    unmatched: list[str] = []

    for line in remittance.lines:
        load = loads.get(line.load_ref)
        invoiced = _invoiced_amount(load) if load is not None else None
        if load is None:
            unmatched.append(line.load_ref)
            settled = False
        else:
            settled = abs((line.gross + line.deduction) - invoiced) <= SETTLEMENT_TOLERANCE
        allocations.append(
            Allocation(
                remittance_ref=ref,
                load_ref=line.load_ref,
                invoiced=invoiced,
                paid=round(line.gross, 2),
                deduction=round(line.deduction, 2),
                reason=line.reason,
                matched=load is not None,
                settled_in_full=settled,
            )
        )

    return AllocationResult(
        remittance_ref=ref,
        broker=remittance.broker or remittance.counterparty,
        remittance_total=round(remittance.remittance_total or 0.0, 2),
        factoring_fee=round(remittance.factoring_fee or 0.0, 2),
        allocations=allocations,
        unmatched_load_refs=unmatched,
    )


def allocate_all(documents: list[Document]) -> list[AllocationResult]:
    """Allocate every remittance in the period, in the order they arrived."""
    return [
        allocate_remittance(d, documents)
        for d in documents
        if d.doc_type == DocType.BROKER_REMITTANCE
    ]


def settled_load_refs(results: list[AllocationResult]) -> set[str]:
    """Every load reference any remittance paid something towards.

    A load in this set has been paid, whether or not it was paid correctly.
    Whether it was paid *enough* is a separate question, asked by the short-pay
    detector, and keeping the two apart stops a short-paid load being reported
    twice under two different names.
    """
    return {a.load_ref for result in results for a in result.allocations}


def unsettled_loads(documents: list[Document],
                    results: list[AllocationResult]) -> list[Document]:
    """Loads that were run and invoiced but which no remittance has touched."""
    paid = settled_load_refs(results)
    return [
        load
        for ref, load in sorted(loads_by_ref(documents).items())
        if ref not in paid
    ]
