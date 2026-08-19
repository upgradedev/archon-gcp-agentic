"""Read a month of mail off disk.

In production this is a Cloud Storage prefix that Eventarc points at: the
owner's bookkeeper forwards the month's documents, the objects land, and the
close fires. Locally and in the bundled demo it is a directory. Both produce
the same list of `Document`, which is why the demo a judge clicks and the
deployed pipeline exercise the same code.

The bundled corpus under `corpus/2026-07/` is synthetic. Bell Ridge Haulage
does not exist, the brokers do not exist, and no figure in it came from any
real firm's books. It was written to contain one instance of every defect the
detectors in `exceptions.py` look for, so a judge watching the close can see
each detector fire on something real rather than on a fixture that agrees with
it.
"""
from __future__ import annotations

from pathlib import Path

from .extract import extract_document
from .models import Document

#: Where the bundled month lives, relative to the repository root.
CORPUS_ROOT = Path(__file__).resolve().parent.parent / "corpus"


def read_period(period: str, root: Path | None = None) -> tuple[list[Document], dict[str, str]]:
    """Every artifact for one period, structured, plus the raw text of each.

    Returns the documents in filename order, which is deterministic across
    platforms and is what keeps a run id stable for the same month.
    """
    directory = (root or CORPUS_ROOT) / period
    if not directory.is_dir():
        raise FileNotFoundError(f"no mail for period {period} at {directory}")

    documents: list[Document] = []
    raw: dict[str, str] = {}
    for path in sorted(directory.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        raw[path.name] = text
        documents.append(extract_document(text, source_file=path.name, period=period))
    return documents, raw


def available_periods(root: Path | None = None) -> list[str]:
    """Periods with mail waiting, oldest first."""
    base = root or CORPUS_ROOT
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())
