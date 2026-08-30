"""Shared fixtures. Everything here is offline: no key, no network, no cloud."""
from __future__ import annotations

import pytest

from archon.adapters.store import LocalStore
from archon.domain.models import (
    DocType,
    Document,
    FuelLine,
    RemittanceLine,
)
from archon.runtime.journal import FixedClock
from archon.runtime.mailbox import read_period

PERIOD = "2026-07"


@pytest.fixture
def corpus():
    """The bundled month, structured, plus the raw text of each artifact."""
    return read_period(PERIOD)


@pytest.fixture
def documents(corpus):
    return corpus[0]


@pytest.fixture
def store():
    return LocalStore()


@pytest.fixture
def fixed_clock():
    """A clock that does not move, so a whole run is byte-stable."""
    return FixedClock()


# ── small hand-built documents, for detectors that need a controlled input ────

def load(ref: str, rate: float, *, accessorial: float = 0.0, miles: float = 1000,
         truck: str = "T-1", date: str = "2026-07-10",
         broker: str = "Test Broker") -> Document:
    return Document(
        doc_type=DocType.LOAD_CONFIRMATION, period=PERIOD, source_file=f"load-{ref}.txt",
        date=date, load_ref=ref, broker=broker, truck=truck, miles=miles,
        net_amount=rate, accessorial=accessorial, gross_amount=rate + accessorial,
    )


def remittance(ref: str, lines, *, fee: float = 0.0, total: float | None = None,
               date: str = "2026-07-24", broker: str = "Test Broker") -> Document:
    """`lines` is a list of (load_ref, gross, deduction, reason)."""
    built = [RemittanceLine(load_ref=r, gross=g, deduction=d, reason=n) for r, g, d, n in lines]
    gross = round(sum(line.gross for line in built), 2)
    return Document(
        doc_type=DocType.BROKER_REMITTANCE, period=PERIOD, source_file=f"remit-{ref}.txt",
        date=date, document_number=ref, broker=broker, factoring_fee=fee,
        remittance_total=round(gross - fee, 2) if total is None else total, lines=built,
    )


def bank(amount: float, reference: str, *, direction: str = "out",
         date: str = "2026-07-20", counterparty: str | None = None,
         driver: str | None = None) -> Document:
    return Document(
        doc_type=DocType.BANK_TRANSACTION, period=PERIOD,
        source_file=f"bank-{reference}.txt", date=date, direction=direction,
        net_amount=amount, reference=reference, counterparty=counterparty, driver=driver,
    )


def expense(doc_type: DocType, number: str, net: float, tax: float, *,
            supplier: str = "Test Supplier", date: str = "2026-07-15",
            truck: str | None = None) -> Document:
    return Document(
        doc_type=doc_type, period=PERIOD, source_file=f"exp-{number}.txt", date=date,
        document_number=number, counterparty=supplier, net_amount=net, tax_amount=tax,
        gross_amount=round(net + tax, 2), truck=truck,
    )


def fuel(number: str, fills, *, supplier: str = "Test Fuel",
         date: str = "2026-07-31") -> Document:
    """`fills` is a list of (date, truck, location, gallons, gross, tax)."""
    lines = [FuelLine(date=d, truck=t, location=loc, gallons=g, gross=gr, tax=tx)
             for d, t, loc, g, gr, tx in fills]
    gross = round(sum(line.gross for line in lines), 2)
    tax = round(sum(line.tax for line in lines), 2)
    return Document(
        doc_type=DocType.FUEL_CARD_STATEMENT, period=PERIOD,
        source_file=f"fuel-{number}.txt", date=date, document_number=number,
        counterparty=supplier, fuel_lines=lines, gross_amount=gross, tax_amount=tax,
        net_amount=round(gross - tax, 2),
    )


def settlement(number: str, gross: float, withheld: float, other: float, net: float,
               *, driver: str = "Driver", truck: str = "T-1",
               date: str = "2026-07-31") -> Document:
    return Document(
        doc_type=DocType.DRIVER_SETTLEMENT, period=PERIOD,
        source_file=f"set-{number}.txt", date=date, document_number=number,
        driver=driver, truck=truck, driver_gross=gross, tax_withheld=withheld,
        driver_deductions=other, driver_net=net,
    )


def unreadable(name: str = "scan.pdf", reason: str = "no text layer") -> Document:
    return Document(
        doc_type=DocType.UNREADABLE, period=PERIOD, source_file=name,
        date="2026-07-19", failure_reason=reason,
    )


@pytest.fixture(autouse=True)
def _forget_process_wide_state():
    """Two things outlive a test because they are process-wide, so clear both.

    The public close limiter: three presses per address per ten minutes is the
    production bound, so in a suite the fourth test to press the anonymous
    button would get a 429 from the presses of the three before it, and which
    three depends on collection order.

    The in-memory store: it is now one instance per process, deliberately --
    `get_store()` used to hand every caller a NEW `LocalStore`, so on the memory
    backend nothing written was ever read back and the events route claimed a
    marker in one throwaway and looked for it in another. Making it durable is
    the fix, and it means a close one test files is visible to the next unless
    it is cleared here.

    The cold-start cache: `service._COLD_START_CLOSES` memoises the anonymous
    GET's deterministic close for the life of the process, which is right in
    production -- one container, one bundled corpus, an answer that cannot go
    stale -- and wrong in a suite, where it means the second test to ask about
    a period gets the first test's payload instead of exercising the path. A
    guard asserting what a GET does on a cold container is inert once anything
    has warmed it.
    """
    from archon.adapters import ratelimit, service, store

    def clear():
        ratelimit.PUBLIC_CLOSES.reset()
        store.reset_local_store()
        service._COLD_START_CLOSES.clear()

    clear()
    yield
    clear()
