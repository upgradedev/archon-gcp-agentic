"""What the month got wrong, found deterministically.

Everyone extracts what you have. The useful question is what you do not have,
and what you have that contradicts itself. Ten detectors run over the posted
month, and each one is pure: same documents in, same findings out, no model
call anywhere in this file.

The three thresholds below are the only tuned numbers in the product, and they
are learned from the firm's own books rather than imported from a jurisdiction
or a rate card. A fuel fill is an outlier because it is far above what *this*
firm normally spends at *that* supplier, not because it crossed a figure
somebody wrote down. That matters commercially: it means Archon works on the
first month, for a firm nobody has configured it for.

Detectors deliberately do not overlap. A load that was short-paid is reported
once, as a short-pay, and never also as unpaid; a duplicate charge is reported
once, on the second occurrence, never on both. Double-reporting is how an
exception list becomes noise, and a noisy list is one the owner stops reading.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime

from .allocation import unsettled_loads
from .models import (
    AllocationResult,
    DocType,
    Document,
    ExceptionKind,
    Finding,
)

#: Two charges to the same counterparty for the same amount inside this window
#: are a probable double-billing rather than a coincidence.
DUPLICATE_WINDOW_DAYS = 10

#: A charge this many times the counterparty's own median is an outlier.
OUTLIER_MULTIPLE = 4.0

#: Below this many prior charges there is no norm to be an outlier against.
OUTLIER_MIN_HISTORY = 3

#: Invoices needed before the firm's own prevailing tax rate can be inferred.
TAX_MIN_INVOICES = 3

#: Tolerances on the inferred tax figure: absolute, then relative to net.
TAX_ABS_TOLERANCE = 0.50
TAX_REL_TOLERANCE = 0.01

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y")


def parse_date(value: str | None) -> date | None:
    """Best-effort date parse. Returns None rather than guessing."""
    if not value:
        return None
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _period_bounds(period: str) -> tuple[date, date] | None:
    try:
        year, month = int(period[:4]), int(period[5:7])
    except (ValueError, IndexError):
        return None
    first = date(year, month, 1)
    last = date(year + (month == 12), (month % 12) + 1, 1)
    return first, last


# ── detectors ────────────────────────────────────────────────────────────────

def find_payments_without_documents(documents: list[Document]) -> list[Finding]:
    """Money left the bank with nothing behind it.

    The classic haulage version: a fuel card charge at a truck stop the office
    never got a receipt for, or a payment out referencing a supplier invoice
    that is not in the month's mail. The money is real and already gone; what
    is missing is the artifact that says what it bought.
    """
    known_refs = {
        d.document_number for d in documents if d.document_number
    } | {
        d.load_ref for d in documents if d.load_ref
    }
    known_refs.discard(None)

    findings: list[Finding] = []
    for doc in documents:
        if doc.doc_type != DocType.BANK_TRANSACTION or doc.direction != "out":
            continue
        reference = doc.reference or ""
        if reference in known_refs:
            continue
        if doc.driver or "settlement" in reference.lower():
            continue  # the driver settlement sheet is the document
        findings.append(
            Finding(
                kind=ExceptionKind.PAYMENT_WITHOUT_DOCUMENT,
                severity="error",
                reference=reference or (doc.source_file or "unreferenced"),
                amount=round(doc.net_amount or 0.0, 2),
                message=(
                    f"{doc.currency} {doc.net_amount or 0.0:,.2f} left the account "
                    f"referencing '{reference}'"
                    + (f" to {doc.counterparty}" if doc.counterparty else "")
                    + " with no invoice or receipt on file."
                ),
                counterparty=doc.counterparty,
                source_file=doc.source_file,
            )
        )
    return findings


def find_short_pays(results: list[AllocationResult]) -> list[Finding]:
    """Loads a broker paid less for than it agreed, without saying why.

    A stated deduction is not a short-pay: the broker held back a lumper fee
    and said so, and the owner can argue that on its merits. What this catches
    is the silent kind, where the remittance line is simply light and no reason
    is given, which is the version that otherwise disappears into a lump credit
    and is never recovered.
    """
    findings: list[Finding] = []
    for result in results:
        for allocation in result.allocations:
            if not allocation.matched or allocation.settled_in_full:
                continue
            shortfall = round(
                (allocation.invoiced or 0.0) - allocation.paid - allocation.deduction, 2
            )
            if shortfall <= 0:
                continue
            stated = f" The broker stated: '{allocation.reason}'." if allocation.reason else (
                " No reason was given on the remittance."
            )
            findings.append(
                Finding(
                    kind=ExceptionKind.SHORT_PAY,
                    severity="error",
                    reference=allocation.load_ref,
                    amount=shortfall,
                    message=(
                        f"Load {allocation.load_ref} was invoiced at "
                        f"{allocation.invoiced:,.2f} but remittance "
                        f"{allocation.remittance_ref} paid {allocation.paid:,.2f}, "
                        f"leaving {shortfall:,.2f} unpaid.{stated}"
                    ),
                    counterparty=result.broker,
                    source_file=allocation.remittance_ref,
                )
            )
    return findings


def find_unreconciled_remittances(results: list[AllocationResult]) -> list[Finding]:
    """A remittance whose own arithmetic does not close.

    What landed in the bank should equal the lines' gross less the fee charged
    once. When it does not, either a line is missing from the advice or the fee
    is not what the factor said it was, and both are worth a phone call.
    """
    findings: list[Finding] = []
    for result in results:
        if not result.reconciles:
            findings.append(
                Finding(
                    kind=ExceptionKind.REMITTANCE_UNRECONCILED,
                    severity="error",
                    reference=result.remittance_ref,
                    amount=abs(result.residual),
                    message=(
                        f"Remittance {result.remittance_ref} credited "
                        f"{result.remittance_total:,.2f}, but its {len(result.allocations)} "
                        f"load line(s) total {result.allocated_gross:,.2f} less a "
                        f"{result.factoring_fee:,.2f} factoring fee, leaving "
                        f"{result.residual:,.2f} unaccounted for."
                    ),
                    counterparty=result.broker,
                    source_file=result.remittance_ref,
                )
            )
        # An unmatched load line is reported whether or not the arithmetic
        # closes. A remittance can balance perfectly and still be paying for a
        # load nobody here has a confirmation for, which is its own problem.
        for load_ref in result.unmatched_load_refs:
            findings.append(
                Finding(
                    kind=ExceptionKind.REMITTANCE_UNRECONCILED,
                    severity="warning",
                    reference=load_ref,
                    amount=0.0,
                    message=(
                        f"Remittance {result.remittance_ref} pays load {load_ref}, "
                        f"which has no load confirmation on file."
                    ),
                    counterparty=result.broker,
                    source_file=result.remittance_ref,
                )
            )
    return findings


def find_unpaid_loads(documents: list[Document],
                      results: list[AllocationResult]) -> list[Finding]:
    """Loads run and invoiced that no remittance has settled at all."""
    findings = []
    for load in unsettled_loads(documents, results):
        amount = round((load.net_amount or 0.0) + (load.accessorial or 0.0), 2)
        findings.append(
            Finding(
                kind=ExceptionKind.LOAD_UNPAID,
                severity="warning",
                reference=load.load_ref or (load.source_file or "load"),
                amount=amount,
                message=(
                    f"Load {load.load_ref} for {load.broker or load.counterparty} was "
                    f"invoiced at {amount:,.2f} and no remittance has paid any part of it."
                ),
                counterparty=load.broker or load.counterparty,
                source_file=load.source_file,
            )
        )
    return findings


@dataclass
class _Charge:
    """One outgoing charge, flattened out of whatever document carried it.

    `reference` points at the individual charge, not at the document that
    carried it. A fuel card statement is one document but forty charges, and
    two detectors firing on two different fills would otherwise both report the
    statement number: the owner would see one reference listed as handled and
    as outstanding at the same time, with no way to tell which fill either
    meant. That is the kind of thing that makes an exception list untrustworthy.
    """

    counterparty: str          # who was paid, which is what a norm is grouped by
    place: str                 # where, so a duplicate is same place, same amount
    amount: float
    when: date | None
    reference: str             # what goes in front of the owner
    document: Document


def _charge_events(documents: list[Document]) -> list[_Charge]:
    """Flatten every outgoing charge, one entry per charge, not per document."""
    events: list[_Charge] = []
    for doc in documents:
        if doc.doc_type == DocType.FUEL_CARD_STATEMENT:
            supplier = doc.counterparty or "fuel card"
            statement = doc.document_number or doc.source_file or "statement"
            for line in doc.fuel_lines:
                where = line.location or ""
                stamp = line.date or "undated"
                events.append(_Charge(
                    counterparty=supplier,
                    place=where,
                    amount=round(line.gross, 2),
                    when=parse_date(line.date),
                    reference=f"{statement} / {stamp} {where}".strip(),
                    document=doc,
                ))
        elif doc.doc_type in (DocType.TOLL_INVOICE, DocType.MAINTENANCE_INVOICE,
                              DocType.INSURANCE_INVOICE):
            events.append(_Charge(
                counterparty=doc.counterparty or doc.doc_type.value,
                place="",
                amount=round(doc.gross_amount or doc.net_amount or 0.0, 2),
                when=parse_date(doc.date),
                reference=doc.document_number or doc.source_file or "invoice",
                document=doc,
            ))
    return events


def find_duplicate_charges(documents: list[Document]) -> list[Finding]:
    """The same amount charged twice by the same counterparty in a short window.

    Reported on the second occurrence only. Reporting both halves of a pair
    doubles the list and tells the owner nothing extra.
    """
    findings: list[Finding] = []
    seen: dict[tuple[str, str, float], list[date | None]] = {}
    for charge in _charge_events(documents):
        if charge.amount <= 0:
            continue
        key = (charge.counterparty, charge.place, charge.amount)
        priors = seen.setdefault(key, [])
        for prior in priors:
            close_in_time = (
                charge.when is None or prior is None
                or abs((charge.when - prior).days) <= DUPLICATE_WINDOW_DAYS
            )
            if close_in_time:
                findings.append(
                    Finding(
                        kind=ExceptionKind.DUPLICATE_CHARGE,
                        severity="error",
                        reference=charge.reference,
                        amount=charge.amount,
                        message=(
                            f"{charge.counterparty} charged {charge.amount:,.2f} twice "
                            f"within {DUPLICATE_WINDOW_DAYS} days"
                            + (f" at {charge.place}" if charge.place else "")
                            + "; the second charge is probably a duplicate."
                        ),
                        counterparty=charge.document.counterparty,
                        source_file=charge.document.source_file,
                    )
                )
                break
        priors.append(charge.when)
    return findings


def find_amount_outliers(documents: list[Document]) -> list[Finding]:
    """A charge far above what this firm normally pays that counterparty.

    The norm is the median of the firm's own other charges to the same
    counterparty, so a firm that habitually spends more is not nagged for it.
    Below `OUTLIER_MIN_HISTORY` prior charges there is no norm and nothing is
    reported, which is the honest answer rather than a guess with a threshold.
    """
    grouped: dict[str, list[_Charge]] = {}
    for charge in _charge_events(documents):
        if charge.amount > 0:
            grouped.setdefault(charge.counterparty, []).append(charge)

    findings: list[Finding] = []
    for counterparty, charges in grouped.items():
        if len(charges) <= OUTLIER_MIN_HISTORY:
            continue
        amounts = [c.amount for c in charges]
        for charge in charges:
            others = list(amounts)
            others.remove(charge.amount)
            median = statistics.median(others)
            if median <= 0 or charge.amount < median * OUTLIER_MULTIPLE:
                continue
            findings.append(
                Finding(
                    kind=ExceptionKind.AMOUNT_OUTLIER,
                    severity="warning",
                    reference=charge.reference,
                    amount=charge.amount,
                    message=(
                        f"A {charge.amount:,.2f} charge from {counterparty} is "
                        f"{charge.amount / median:.1f}x this firm's own median of "
                        f"{median:,.2f} for that counterparty."
                    ),
                    counterparty=charge.document.counterparty,
                    source_file=charge.document.source_file,
                )
            )
    return findings


def find_tax_inconsistencies(documents: list[Document]) -> list[Finding]:
    """An invoice whose tax figure contradicts the firm's own prevailing rate.

    The rate is inferred from the firm's own invoices, never assumed. Usually
    this catches a keying slip: a transposed figure, or a rate applied to the
    gross instead of the net.
    """
    taxed = [
        d for d in documents
        if d.doc_type in (DocType.TOLL_INVOICE, DocType.MAINTENANCE_INVOICE,
                          DocType.INSURANCE_INVOICE)
        and d.net_amount and d.tax_amount is not None
    ]
    if len(taxed) < TAX_MIN_INVOICES:
        return []

    rates = [round(d.tax_amount / d.net_amount, 4) for d in taxed if d.net_amount]
    prevailing = statistics.median(rates)
    if prevailing <= 0:
        return []

    findings: list[Finding] = []
    for doc in taxed:
        expected = round(doc.net_amount * prevailing, 2)
        drift = abs(expected - doc.tax_amount)
        tolerance = TAX_ABS_TOLERANCE + doc.net_amount * TAX_REL_TOLERANCE
        if drift <= tolerance:
            continue
        findings.append(
            Finding(
                kind=ExceptionKind.TAX_INCONSISTENCY,
                severity="warning",
                reference=doc.document_number or doc.source_file or "invoice",
                amount=round(drift, 2),
                message=(
                    f"Invoice {doc.document_number} shows {doc.tax_amount:,.2f} tax on "
                    f"{doc.net_amount:,.2f} net; this firm's own prevailing rate of "
                    f"{prevailing * 100:.1f}% implies {expected:,.2f}, a "
                    f"{drift:,.2f} difference."
                ),
                source_file=doc.source_file,
            )
        )
    return findings


def find_out_of_period(documents: list[Document], period: str) -> list[Finding]:
    """Artifacts dated outside the month being closed.

    Not necessarily wrong. A June invoice arriving in July mail is ordinary,
    and the close says so rather than refusing it. What it must not do is post
    it silently into the wrong month, which is how a period gets reopened three
    weeks later.
    """
    bounds = _period_bounds(period)
    if bounds is None:
        return []
    first, next_first = bounds

    findings: list[Finding] = []
    for doc in documents:
        when = parse_date(doc.date)
        if when is None or first <= when < next_first:
            continue
        amount = round(
            doc.gross_amount or doc.net_amount or doc.remittance_total or 0.0, 2
        )
        findings.append(
            Finding(
                kind=ExceptionKind.OUT_OF_PERIOD,
                severity="warning",
                reference=doc.document_number or doc.load_ref or (doc.source_file or "document"),
                amount=amount,
                message=(
                    f"{doc.doc_type.value.replace('_', ' ')} dated {doc.date} falls outside "
                    f"the period {period} being closed."
                ),
                source_file=doc.source_file,
            )
        )
    return findings


def find_unreadable(documents: list[Document]) -> list[Finding]:
    """Artifacts that arrived and could not be read.

    Reported with an amount of zero, because there is no amount. Archon will
    not estimate what an unreadable document said, and the refusal is the
    feature: this is the one place where a plausible guess would corrupt the
    books invisibly.
    """
    return [
        Finding(
            kind=ExceptionKind.UNREADABLE_DOCUMENT,
            severity="warning",
            reference=doc.source_file or "unreadable",
            amount=0.0,
            message=(
                f"{doc.source_file} could not be read ({doc.failure_reason or 'no text layer'}) "
                f"and was left unposted rather than estimated."
            ),
            source_file=doc.source_file,
        )
        for doc in documents
        if doc.doc_type == DocType.UNREADABLE
    ]


def find_unrecognised(documents: list[Document]) -> list[Finding]:
    """Artifacts that were read perfectly and matched no document family.

    This is the quieter twin of `find_unreadable`, and it was missing. An
    UNREADABLE document already produced a finding; an UNKNOWN one produced
    nothing at all. `Ledger._post_unknown` posts no entry, so the artifact
    left no trace anywhere: not in the books, not in the exceptions, not in
    the owner's letter. Three invoices in a format the extractor has never
    seen would close a month at zero revenue and report "every gate passed".

    That is the exact failure this product exists to prevent, so it is an
    ERROR rather than a warning: a month is not closeable while documents the
    owner sent are unaccounted for. It is deliberately NOT actionable, because
    there is no letter to write; the fix is a parser, not a dispute.
    """
    return [
        Finding(
            kind=ExceptionKind.UNRECOGNISED_DOCUMENT,
            severity="error",
            reference=doc.source_file or "unrecognised",
            amount=0.0,
            message=(
                f"{doc.source_file} was read but matched no document family, so "
                f"nothing was posted from it. Archon will not guess what kind of "
                f"document it is."
            ),
            source_file=doc.source_file,
        )
        for doc in documents
        if doc.doc_type == DocType.UNKNOWN
    ]


#: Severity order used to rank the list the owner actually reads.
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


def find_all(documents: list[Document], results: list[AllocationResult],
             period: str) -> list[Finding]:
    """Run every detector and return the findings worst-first.

    Ranking is severity, then amount descending. An owner with ten minutes
    reads the top of this list, so the top of the list has to be the money.
    """
    findings = (
        find_payments_without_documents(documents)
        + find_short_pays(results)
        + find_unreconciled_remittances(results)
        + find_unpaid_loads(documents, results)
        + find_duplicate_charges(documents)
        + find_amount_outliers(documents)
        + find_tax_inconsistencies(documents)
        + find_out_of_period(documents, period)
        + find_unreadable(documents)
        + find_unrecognised(documents)
    )
    findings.sort(key=lambda f: (_SEVERITY_RANK.get(f.severity, 9), -f.amount))
    return findings


def exposure(findings: list[Finding]) -> float:
    """Total money sitting in findings the owner could still recover or must document."""
    return round(sum(f.amount for f in findings), 2)
