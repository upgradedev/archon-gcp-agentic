"""One remittance can be the arrival of ONE bank credit, not of every credit
that resembles it.

`matching_remittance` is asked per bank line, independently, so two distinct
credits carrying the same reference each matched the same advice and each was
suppressed. One real movement of money vanished, and G3 agreed because it
reconciles through the same reading -- so the books were short and all seven
gates were green, which is the worst shape a defect can take here.
"""
from __future__ import annotations

import pytest

from archon.adapters.store import LocalStore
from archon.domain.models import DocType, Document
from archon.runtime.close import run_close

PERIOD = "2026-07"


def remittance(ref: str, total: float, broker: str = "Thackery Freight Exchange"):
    return Document(
        doc_type=DocType.BROKER_REMITTANCE, period=PERIOD, source_file=f"{ref}.txt",
        date="2026-07-10", counterparty=broker, broker=broker,
        document_number=ref, remittance_total=total, factoring_fee=0.0,
        currency="USD", lines=[],
    )


def credit(name: str, amount: float, ref: str | None = None,
           broker: str = "Thackery Freight Exchange"):
    return Document(
        doc_type=DocType.BANK_TRANSACTION, period=PERIOD, source_file=name,
        date="2026-07-11", counterparty=broker, direction="in",
        net_amount=amount, reference=ref, currency="USD",
    )


def close(documents):
    return run_close(period=PERIOD, documents=documents, company="X",
                     store=LocalStore(), commit=False)


def cash_in(result) -> float:
    return round(result.statements.cash_in or 0.0, 2)


def test_one_remittance_and_its_own_credit_post_once():
    """The behaviour that must not regress: the advice and the credit it
    produced are one movement, so cash is 100 and not 200."""
    result = close([remittance("RA-1", 100.0), credit("bank-1.txt", 100.0, "RA-1")])

    assert cash_in(result) == 100.0


def test_a_second_genuine_credit_is_not_swallowed_by_the_same_advice():
    """The defect. Two distinct credits, one advice: the first is the arrival
    of that advice and the second is money that actually moved."""
    result = close([remittance("RA-1", 100.0),
                    credit("bank-1.txt", 100.0, "RA-1"),
                    credit("bank-2.txt", 100.0, "RA-1")])

    assert cash_in(result) == 200.0, "a real bank credit disappeared"


def test_the_gates_do_not_pass_over_understated_cash():
    """G3 reconciles through the same matching, so it agreed with the error.
    Whatever the books do, they must not be short AND green."""
    result = close([remittance("RA-1", 100.0),
                    credit("bank-1.txt", 100.0, "RA-1"),
                    credit("bank-2.txt", 100.0, "RA-1")])

    g3 = next(g for g in result.gates if g.rule.startswith("G3"))
    assert cash_in(result) == 200.0 or not g3.passed, (
        "cash is understated and G3 is green")


def test_two_advices_of_the_same_amount_keep_their_own_credits():
    """Two brokers paying the same round figure in one month is ordinary."""
    result = close([remittance("RA-1", 100.0, "Alpha Freight"),
                    remittance("RA-2", 100.0, "Beta Freight"),
                    credit("bank-1.txt", 100.0, "RA-1", "Alpha Freight"),
                    credit("bank-2.txt", 100.0, "RA-2", "Beta Freight")])

    assert cash_in(result) == 200.0


def test_reference_free_matching_is_still_one_to_one():
    """Where a statement carries no reference the counterparty and amount are
    accepted, and that route has to be one-to-one too."""
    result = close([remittance("RA-1", 100.0),
                    credit("bank-1.txt", 100.0, None),
                    credit("bank-2.txt", 100.0, None)])

    assert cash_in(result) == 200.0


@pytest.mark.parametrize("amount, ref", [(150.0, "RA-1"), (100.0, "RA-9")])
def test_a_credit_that_does_not_match_posts_normally(amount, ref):
    """A different amount or a reference naming no advice is a separate
    payment, and guessing past it would swallow a real receipt."""
    result = close([remittance("RA-1", 100.0), credit("bank-1.txt", amount, ref)])

    assert cash_in(result) == round(100.0 + amount, 2)
