"""The allocation arithmetic is not about trucking, and here is the proof.

The README used to claim the close engine "has no trucking in it" and that the
vocabulary "lives at the edges". That was false twice over: `allocate_all`'s
identifiers are haulage throughout (`load_ref`, `broker`, `factoring_fee`,
`settled_load_refs`), and `drafts.py` writes "Short payment on load L-7105" to
"the broker". A claim a reviewer can disprove by reading one function is worse
than no claim.

What IS true is narrower and worth asserting rather than asserting nothing:
**the arithmetic generalises even though the names do not.** One settlement
divided across the obligations it covers, less a fee charged once on the batch,
with the residual identity `landed == lines - fee`, is the same computation
whether the obligations are eight freight loads or three freelance invoices.

So this file runs a freelancer-shaped settlement through the SAME
`allocate_all` and asserts the identity closes to the cent. It deliberately
uses the existing trucking-named types, because pretending otherwise would be
the same overclaim in a new place. The names are haulage; the mathematics is
not; and the README now says exactly that, pointing here.

What this does NOT prove, stated so nobody reads more into it than it carries:
no second domain pack ships in this entry. `DocType`, the chart of accounts and
every `Statements` field that reaches the JSON are trucking, `ledger.py`
dispatches on `"_post_" + doc_type.value`, and `extract.py`'s classifier keys
and model prompt name a haulier. A real freelancer pack is a port, not a flag.
"""
from __future__ import annotations

from archon.domain.allocation import allocate_all
from tests.conftest import load, remittance

#: A month a design agency would recognise, expressed in the types this
#: repository actually has. Three client engagements invoiced separately; one
#: platform payout settling all three at once, minus a processor fee charged
#: once on the batch. The shape is the freelancer case; the field names are the
#: haulage ones, and that mismatch is the honest state of the codebase.
ENGAGEMENTS = [
    ("brand-sprint", 4_800.00),
    ("site-rebuild", 12_500.00),
    ("retainer-aug", 2_200.00),
]
PROCESSOR_FEE = 293.25          # 1.5% of the batch, charged once


def freelance_month():
    documents = [load(ref, fee) for ref, fee in ENGAGEMENTS]
    documents.append(remittance(
        "PAYOUT-2026-08",
        [(ref, fee, 0.0, None) for ref, fee in ENGAGEMENTS],
        fee=PROCESSOR_FEE,
    ))
    return documents


def test_the_allocation_arithmetic_is_not_about_trucking():
    """The claim the README makes, asserted rather than asserted about.

    Three invoices, one payout, one processor fee. Nothing here is a load, a
    mile or a broker, and the identity still closes to the cent.
    """
    result = allocate_all(freelance_month())

    assert len(result) == 1
    batch = result[0]

    invoiced = sum(fee for _ref, fee in ENGAGEMENTS)
    assert batch.allocated_gross == invoiced
    assert batch.factoring_fee == PROCESSOR_FEE
    assert batch.remittance_total == round(invoiced - PROCESSOR_FEE, 2)

    # The residual identity, which is the whole differentiator: what landed
    # equals what the lines pay, less the fee taken once on the batch.
    assert batch.reconciles
    assert batch.residual == 0.0


def test_each_engagement_is_settled_at_what_it_was_actually_paid():
    """Allocation, not matching. Every line is settled individually even though
    a single credit covers all three and equals none of them."""
    batch = allocate_all(freelance_month())[0]

    settled = {line.load_ref: line.paid for line in batch.allocations}

    assert settled == dict(ENGAGEMENTS)
    assert all(line.settled_in_full for line in batch.allocations)
    # And the payout matches no single invoice, which is why matching fails.
    assert batch.remittance_total not in settled.values()


def test_an_underpaid_engagement_is_caught_the_same_way_a_load_is():
    """One invoice paid short by 400. The engine reports it as unsettled
    without knowing what an invoice is."""
    short = [("brand-sprint", 4_800.00, 4_400.00),
             ("site-rebuild", 12_500.00, 12_500.00),
             ("retainer-aug", 2_200.00, 2_200.00)]
    documents = [load(ref, invoiced) for ref, invoiced, _paid in short]
    documents.append(remittance(
        "PAYOUT-2026-08",
        [(ref, paid, 0.0, None) for ref, _invoiced, paid in short],
        fee=PROCESSOR_FEE,
    ))

    batch = allocate_all(documents)[0]
    by_ref = {line.load_ref: line for line in batch.allocations}

    assert by_ref["brand-sprint"].matched
    assert not by_ref["brand-sprint"].settled_in_full
    assert by_ref["site-rebuild"].settled_in_full
    # The batch still reconciles: the money that arrived is accounted for. The
    # shortfall is an exception, not a broken identity, and conflating those
    # two is how a suspense account gets invented.
    assert batch.reconciles


def test_the_names_in_the_engine_are_trucking_and_the_readme_must_say_so():
    """A guard on the correction, not on the code.

    If someone renames these to neutral terms, this test goes red and whoever
    did it has to update the README paragraph that currently admits the names
    are haulage. The failure mode being prevented is the README drifting back
    to claiming neutrality it does not have.
    """
    import pathlib

    from archon.domain import allocation

    source = pathlib.Path(allocation.__file__).read_text(encoding="utf-8")
    for haulage_name in ("load_ref", "factoring_fee", "broker", "remittance"):
        assert haulage_name in source, (
            f"{haulage_name} is gone from allocation.py. If the engine was "
            "renamed to neutral terms, update the README's breadth paragraph: "
            "it currently states plainly that the names are trucking."
        )

    readme = (pathlib.Path(allocation.__file__).parents[3] / "README.md"
              ).read_text(encoding="utf-8")
    admission = " ".join(readme.split())
    assert "The engine's *names* are trucking throughout" in admission, (
        "the README no longer admits that the engine's names are haulage; "
        "either restore the admission or rename the engine for real"
    )
