"""Read a month of mail off disk.

In production this is a Cloud Storage prefix the bucket's finalize
notification watches: the owner's bookkeeper forwards the month's documents,
the objects land, and the close fires. Locally and in the bundled demo it is
a directory. Both produce
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

import re
from pathlib import Path

from .. import paths
from ..domain.extract import extract_document
from ..domain.models import Document

#: Where the bundled month lives, relative to the repository root.
CORPUS_ROOT = paths.CORPUS_ROOT


#: A period is a month, and this is the only shape one has. The check is on the
#: format a period HAS rather than on the characters a traversal happens to use,
#: because enumerating the second is how you miss the encoding nobody thought
#: of: `%2e%2e` arrives here already decoded as `..`.
PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def read_period(period: str, root: Path | None = None) -> tuple[list[Document], dict[str, str]]:
    """Every artifact for one period, structured, plus the raw text of each.

    Returns the documents in filename order, which is deterministic across
    platforms and is what keeps a run id stable for the same month.

    The period is validated here, at the filesystem boundary, rather than only
    in the route that happens to be public today. `GET /api/close/%2e%2e`
    answered 200 and ran a close over `corpus/..`: the segment went from an
    anonymous URL to a directory join with nothing in between asserting it was
    a month. Every caller gets the check, not just the one that was attacked.
    """
    if not PERIOD.fullmatch(period or ""):
        raise FileNotFoundError(f"{period!r} is not a period")
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
