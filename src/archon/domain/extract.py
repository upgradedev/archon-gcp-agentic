"""Turn a raw artifact into a structured `Document`.

Two paths, and the ordering between them is the point.

**The deterministic path** parses the label blocks that OCR leaves behind and
is the default. It needs no key, no network and no credential, which is what
lets the whole close run in CI, in the bundled demo, and on a judge's laptop
with nothing installed but Python.

**The Gemini path** in `agents.py` sends the artifact to a vision-capable model
and asks for the same structure back. It is what handles the real world, where
a rate confirmation is a photograph of a fax.

The deterministic path is the reference implementation, not the fallback. When
the two disagree about a figure on an artifact the deterministic parser can
read, the deterministic parser is right, and that is the direction the arrow
has to point in a product whose whole claim is that its books are true.

Both paths converge on `Document`, so nothing downstream knows or cares which
one ran.
"""
from __future__ import annotations

import re

from .models import DocType, Document, FuelLine, RemittanceLine

#: The label that decides which family an artifact belongs to. An artifact
#: without it is UNKNOWN and posts nothing, rather than being guessed at.
_TYPE_LABEL = "document type"

_TYPE_NAMES = {
    "load confirmation": DocType.LOAD_CONFIRMATION,
    "broker remittance": DocType.BROKER_REMITTANCE,
    "fuel card statement": DocType.FUEL_CARD_STATEMENT,
    "toll invoice": DocType.TOLL_INVOICE,
    "maintenance invoice": DocType.MAINTENANCE_INVOICE,
    "insurance invoice": DocType.INSURANCE_INVOICE,
    "driver settlement": DocType.DRIVER_SETTLEMENT,
    "bank transaction": DocType.BANK_TRANSACTION,
    "unreadable": DocType.UNREADABLE,
}

_MONEY = r"-?[\d,]+\.\d{2}"
_LOAD_LINE = re.compile(
    r"Load\s+(?P<ref>[\w-]+)\s+Gross\s+(?P<gross>" + _MONEY + r")"
    r"\s+Deduction\s+(?P<deduction>" + _MONEY + r")"
    r"(?:\s+Reason\s+(?P<reason>.+))?$",
    re.IGNORECASE,
)
_FUEL_LINE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<truck>[\w-]+)\s+(?P<location>.+?)\s+"
    r"Gallons\s+(?P<gallons>[\d.]+)\s+Gross\s+(?P<gross>" + _MONEY + r")"
    r"\s+Tax\s+(?P<tax>" + _MONEY + r")",
    re.IGNORECASE,
)


def _labels(text: str) -> dict[str, str]:
    """Every `Label: value` pair in the artifact, lowercased keys.

    First occurrence wins. A statement footer that repeats a total must not
    silently overwrite the header the figure was read from.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key and value and key not in found:
            found[key] = value
    return found


def _money(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = value.replace(",", "").replace("$", "").strip()
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def _pick(labels: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        if key in labels:
            return labels[key]
    return None


def _amount(labels: dict[str, str], *keys: str) -> float | None:
    return _money(_pick(labels, *keys))


def classify(text: str) -> DocType:
    """Which family an artifact belongs to, or UNKNOWN."""
    declared = _labels(text).get(_TYPE_LABEL, "").lower()
    return _TYPE_NAMES.get(declared, DocType.UNKNOWN)


def extract_document(text: str, source_file: str = "document.txt",
                     period: str = "2026-07") -> Document:
    """Parse one artifact into a `Document`. Never raises on bad input.

    An artifact this cannot read comes back as UNREADABLE with the reason
    attached, which is a finding the close reports, not an error that stops it.
    One bad scan in forty must not cost the owner their month-end.
    """
    labels = _labels(text)
    doc_type = classify(text)
    doc = Document(
        doc_type=doc_type,
        period=period,
        source_file=source_file,
        date=_pick(labels, "date", "statement date", "value date"),
        counterparty=_pick(labels, "supplier", "broker", "counterparty", "vendor"),
        document_number=_pick(labels, "invoice number", "statement number",
                              "remittance number", "settlement number", "load number"),
        currency=_pick(labels, "currency") or "USD",
    )

    handler = _HANDLERS.get(doc_type)
    if handler is not None:
        handler(text, labels, doc)
    return doc


def _load_confirmation(text: str, labels: dict[str, str], doc: Document) -> None:
    doc.load_ref = _pick(labels, "load number", "load")
    doc.broker = _pick(labels, "broker")
    doc.truck = _pick(labels, "carrier unit", "unit", "truck")
    doc.miles = _money(_pick(labels, "miles")) or _int(_pick(labels, "miles"))
    doc.net_amount = _amount(labels, "linehaul rate", "linehaul")
    doc.accessorial = _amount(labels, "accessorial") or 0.0
    doc.gross_amount = _amount(labels, "total payable", "total")


def _broker_remittance(text: str, labels: dict[str, str], doc: Document) -> None:
    doc.broker = _pick(labels, "broker")
    doc.factoring_fee = _amount(labels, "factoring fee") or 0.0
    doc.remittance_total = _amount(labels, "amount credited", "amount", "total")
    doc.lines = [
        RemittanceLine(
            load_ref=match.group("ref"),
            gross=_money(match.group("gross")) or 0.0,
            deduction=_money(match.group("deduction")) or 0.0,
            reason=_clean_reason(match.group("reason")),
        )
        for match in (_LOAD_LINE.search(line) for line in text.splitlines())
        if match
    ]


def _fuel_card_statement(text: str, labels: dict[str, str], doc: Document) -> None:
    doc.fuel_lines = [
        FuelLine(
            date=match.group("date"),
            truck=match.group("truck"),
            location=match.group("location").strip(),
            gallons=float(match.group("gallons")),
            gross=_money(match.group("gross")) or 0.0,
            tax=_money(match.group("tax")) or 0.0,
        )
        for match in (_FUEL_LINE.search(line) for line in text.splitlines())
        if match
    ]
    doc.gross_amount = round(sum(line.gross for line in doc.fuel_lines), 2)
    doc.tax_amount = round(sum(line.tax for line in doc.fuel_lines), 2)
    doc.net_amount = round(doc.gross_amount - doc.tax_amount, 2)


def _expense_invoice(text: str, labels: dict[str, str], doc: Document) -> None:
    doc.net_amount = _amount(labels, "net amount", "net")
    doc.tax_amount = _amount(labels, "tax")
    doc.gross_amount = _amount(labels, "total", "total due")
    doc.truck = _pick(labels, "unit", "truck")


def _driver_settlement(text: str, labels: dict[str, str], doc: Document) -> None:
    doc.driver = _pick(labels, "driver")
    doc.driver_gross = _amount(labels, "gross pay", "gross")
    doc.tax_withheld = _amount(labels, "tax withheld")
    doc.driver_deductions = _amount(labels, "other deductions")
    doc.driver_net = _amount(labels, "net pay", "net")
    doc.truck = _pick(labels, "unit", "truck")


def _bank_transaction(text: str, labels: dict[str, str], doc: Document) -> None:
    direction = (_pick(labels, "direction") or "").lower()
    doc.direction = "in" if direction.startswith("in") else "out"
    doc.net_amount = _amount(labels, "amount")
    doc.reference = _pick(labels, "reference")
    doc.driver = _pick(labels, "driver")
    doc.counterparty = _pick(labels, "paid to", "description", "counterparty")


def _unreadable(text: str, labels: dict[str, str], doc: Document) -> None:
    doc.failure_reason = _pick(labels, "failure", "reason")
    doc.source_file = _pick(labels, "source") or doc.source_file


_HANDLERS = {
    DocType.LOAD_CONFIRMATION: _load_confirmation,
    DocType.BROKER_REMITTANCE: _broker_remittance,
    DocType.FUEL_CARD_STATEMENT: _fuel_card_statement,
    DocType.TOLL_INVOICE: _expense_invoice,
    DocType.MAINTENANCE_INVOICE: _expense_invoice,
    DocType.INSURANCE_INVOICE: _expense_invoice,
    DocType.DRIVER_SETTLEMENT: _driver_settlement,
    DocType.BANK_TRANSACTION: _bank_transaction,
    DocType.UNREADABLE: _unreadable,
}


def _int(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.search(r"\d+", value)
    return float(match.group(0)) if match else None


def _clean_reason(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return None if text in ("", "-", "--") else text


#: The schema the Gemini path asks for, kept beside the deterministic parser so
#: the two cannot drift apart unnoticed. Field names match `Document` exactly.
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": [t.value for t in DocType]},
        "date": {"type": "string"},
        "counterparty": {"type": "string"},
        "document_number": {"type": "string"},
        "load_ref": {"type": "string"},
        "truck": {"type": "string"},
        "miles": {"type": "number"},
        "net_amount": {"type": "number"},
        "tax_amount": {"type": "number"},
        "gross_amount": {"type": "number"},
        "accessorial": {"type": "number"},
        "remittance_total": {"type": "number"},
        "factoring_fee": {"type": "number"},
        "direction": {"type": "string", "enum": ["in", "out"]},
        "reference": {"type": "string"},
    },
    "required": ["doc_type"],
}

EXTRACTION_INSTRUCTION = """\
You are reading one business document belonging to a small trucking firm.

Return the structured fields exactly as they appear on the document. Copy
figures character for character. Do not total, convert, round or infer any
amount: if a field is not printed on the document, leave it out.

If you cannot read the document, return doc_type "unreadable" and nothing else.
Returning "unreadable" is always better than returning a plausible guess.
"""
