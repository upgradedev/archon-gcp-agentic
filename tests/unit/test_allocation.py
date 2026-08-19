"""The allocation engine: one payment split back across many loads."""
from __future__ import annotations

from archon.domain.allocation import (
    allocate_all,
    allocate_remittance,
    loads_by_ref,
    settled_load_refs,
    unsettled_loads,
)
from tests.conftest import load, remittance


def test_one_remittance_splits_across_every_load_line():
    docs = [load("L-1", 1000.0), load("L-2", 2000.0), load("L-3", 1500.0)]
    remit = remittance("R-1", [("L-1", 1000.0, 0.0, None),
                               ("L-2", 2000.0, 0.0, None),
                               ("L-3", 1500.0, 0.0, None)], fee=135.0)
    docs.append(remit)

    result = allocate_remittance(remit, docs)

    assert len(result.allocations) == 3
    assert result.allocated_gross == 4500.0
    assert all(a.matched and a.settled_in_full for a in result.allocations)


def test_the_identity_closes_when_the_fee_is_booked_once():
    """What landed equals what the lines pay, less the fee charged once."""
    docs = [load("L-1", 1000.0), load("L-2", 2000.0)]
    remit = remittance("R-1", [("L-1", 1000.0, 0.0, None), ("L-2", 2000.0, 0.0, None)],
                       fee=90.0)
    docs.append(remit)

    result = allocate_remittance(remit, docs)

    assert result.remittance_total == 2910.0     # 3000 gross less the 90 fee
    assert result.residual == 0.0
    assert result.reconciles


def test_a_residual_is_surfaced_not_absorbed():
    """A remittance whose own arithmetic does not close must say so."""
    docs = [load("L-1", 1000.0)]
    remit = remittance("R-1", [("L-1", 1000.0, 0.0, None)], fee=30.0, total=900.0)
    docs.append(remit)

    result = allocate_remittance(remit, docs)

    assert not result.reconciles
    assert result.residual == -70.0              # 900 landed, 970 expected


def test_a_load_paid_light_is_matched_but_not_settled():
    docs = [load("L-1", 2260.0, accessorial=200.0)]
    remit = remittance("R-1", [("L-1", 2260.0, 0.0, None)], fee=0.0)
    docs.append(remit)

    line = allocate_remittance(remit, docs).allocations[0]

    assert line.matched
    assert not line.settled_in_full
    assert line.invoiced == 2460.0


def test_a_stated_deduction_still_settles_the_load_in_full():
    """A deduction the broker declared is an argument, not an accounting gap."""
    docs = [load("L-1", 2000.0)]
    remit = remittance("R-1", [("L-1", 1850.0, 150.0, "Lumper fee")], fee=0.0)
    docs.append(remit)

    line = allocate_remittance(remit, docs).allocations[0]

    assert line.settled_in_full
    assert line.reason == "Lumper fee"


def test_a_line_for_an_unknown_load_is_allocated_but_flagged():
    """The money did arrive. The missing artifact is the finding."""
    docs = [load("L-1", 1000.0)]
    remit = remittance("R-1", [("L-1", 1000.0, 0.0, None), ("L-OLD", 500.0, 0.0, None)],
                       fee=0.0)
    docs.append(remit)

    result = allocate_remittance(remit, docs)

    assert result.unmatched_load_refs == ["L-OLD"]
    assert result.allocated_gross == 1500.0
    unknown = next(a for a in result.allocations if a.load_ref == "L-OLD")
    assert not unknown.matched
    assert unknown.invoiced is None


def test_rounding_inside_a_dollar_is_not_a_short_pay():
    docs = [load("L-1", 1000.0)]
    remit = remittance("R-1", [("L-1", 999.40, 0.0, None)], fee=0.0)
    docs.append(remit)

    assert allocate_remittance(remit, docs).allocations[0].settled_in_full


def test_unsettled_loads_are_the_ones_no_remittance_touched():
    docs = [load("L-1", 1000.0), load("L-2", 2000.0), load("L-3", 1500.0)]
    remit = remittance("R-1", [("L-1", 1000.0, 0.0, None)], fee=0.0)
    docs.append(remit)

    results = allocate_all(docs)

    assert settled_load_refs(results) == {"L-1"}
    assert [d.load_ref for d in unsettled_loads(docs, results)] == ["L-2", "L-3"]


def test_loads_are_indexed_by_reference():
    docs = [load("L-1", 1000.0), load("L-2", 2000.0)]
    assert sorted(loads_by_ref(docs)) == ["L-1", "L-2"]


def test_a_month_with_no_remittance_allocates_nothing():
    assert allocate_all([load("L-1", 1000.0)]) == []
