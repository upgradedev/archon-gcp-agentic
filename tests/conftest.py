"""Shared fixtures. Everything here is offline: no key, no network, no cloud."""
from __future__ import annotations

import pytest

from archon.journal import FixedClock
from archon.mailbox import read_period
from archon.models import (
    DocType,
    Document,
    FuelLine,
    RemittanceLine,
)
from archon.store import LocalStore

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
