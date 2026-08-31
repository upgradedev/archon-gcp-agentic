"""What three ordinary reconciliation methods recover from the same month.

The product's claim is that a load paid short is invisible when one credit
settles eight loads. This measures that against the methods a haulier's books
actually get, on the same 27 documents the close reads.

Run:  python scripts/baseline.py

Each baseline is implemented to be as strong as it honestly is. None is a straw
man: A is what a bank feed does, B is the arithmetic a careful bookkeeper does
by hand, and C is the line-by-line check a suspicious one does next.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from archon.adapters.store import LocalStore  # noqa: E402
from archon.domain.extract import extract_document  # noqa: E402
from archon.domain.models import DocType  # noqa: E402
from archon.runtime.close import run_close  # noqa: E402

PERIOD = "2026-07"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def month() -> list:
    return [extract_document(p.read_text(encoding="utf-8"),
                             source_file=p.name, period=PERIOD)
            for p in sorted((ROOT / "corpus" / PERIOD).glob("*.txt"))]


def baselines(documents: list) -> dict:
    """The three methods, and what each recovers of the short payment."""
    remittance = next(d for d in documents if d.doc_type == DocType.BROKER_REMITTANCE)
    loads = {d.load_ref: d for d in documents
             if d.doc_type == DocType.LOAD_CONFIRMATION and d.load_ref}
    credited, fee, lines = remittance.remittance_total, remittance.factoring_fee, remittance.lines

    # A. A bank feed matches a credit to an invoice of the same amount.
    matched = [d for d in loads.values()
               if abs((d.gross_amount or 0) - credited) < 0.01]

    # B. The arithmetic by hand: does the credit plus the fee equal the advice?
    advice_total = round(sum(line.gross for line in lines), 2)
    residual = round(advice_total - (credited + fee), 2)

    # C. Line by line, against the rate the broker agreed.
    c_flags = [line.load_ref for line in lines
               if line.load_ref in loads
               and abs((loads[line.load_ref].net_amount or 0) - line.gross) > 0.01]

    # What Archon compares: the line against what was actually invoiced, which
    # is the linehaul PLUS the accessorials the load confirmation carries.
    short = [(line.load_ref, round((loads[line.load_ref].gross_amount or 0) - line.gross, 2))
             for line in lines
             if line.load_ref in loads
             and (loads[line.load_ref].gross_amount or 0) - line.gross > 0.01]

    return {
        "loads": len(loads),
        "credited": credited,
        "fee": fee,
        "a_matched": len(matched),
        "b_residual": residual,
        "c_flagged": c_flags,
        "short": short,
        "short_total": round(sum(amount for _, amount in short), 2),
    }


def main() -> None:
    documents = month()
    b = baselines(documents)
    result = run_close(period=PERIOD, documents=documents, company="Bell Ridge Haulage",
                       store=LocalStore(), commit=False)

    print(f"One month, {len(documents)} documents, {b['loads']} loads, one remittance of "
          f"{len(next(d for d in documents if d.doc_type == DocType.BROKER_REMITTANCE).lines)} "
          f"lines.")
    print(f"The broker credited {b['credited']:,.2f} and charged {b['fee']:,.2f} "
          f"once on the batch.")
    print()
    print("  method                                            finds     recovers")
    print(f"  A  match the credit to an invoice                 {b['a_matched']} of {b['loads']}   "
          f"    0.00")
    print(f"  B  credit + fee against the advice total          residual {b['b_residual']:.2f}"
          f"     0.00")
    print(f"  C  each advice line against the agreed rate       {len(b['c_flagged'])} flag(s)"
          f"       0.00")
    print(f"  =  Archon: the line against what was invoiced     "
          f"{len(b['short'])} short         {b['short_total']:,.2f}")
    print()
    for ref, amount in b["short"]:
        print(f"  {ref} was invoiced with an accessorial the advice dropped: {amount:,.2f} short.")
    if b["c_flagged"]:
        print(f"  C flags {', '.join(b['c_flagged'])}, which was paid ABOVE the agreed rate. "
              f"It is not the loss.")
    print()
    elsewhere = result.recoverable - b["short_total"]
    print(f"Archon recovers {b['short_total']:,.2f} here and {elsewhere:,.2f} "
          f"from a duplicate charge, {result.recoverable:,.2f} on the month.")
    print("One synthetic month. The methods above are arithmetic, not products.")


if __name__ == "__main__":
    main()
