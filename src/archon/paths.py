"""Where the repository root is, found rather than counted.

Both the bundled corpus and the judge's page live outside the package, so two
modules needed to know where the repository root is. Both did it by counting
`.parent` hops, and both broke silently the moment the package moved under
`src/`: the corpus lookup started pointing at a directory that has never
existed, and only a test caught it.

Counting parents encodes the current directory depth into every file that needs
a path. Walking up to a marker encodes nothing, so the next move costs nothing.
"""
from __future__ import annotations

from pathlib import Path

#: Files that only ever exist at the top of this repository.
_MARKERS = ("pyproject.toml", "LICENSE")


def repo_root(start: Path | None = None) -> Path:
    """The repository root, by walking up until a marker file appears.

    Falls back to the package's own grandparent rather than raising: a missing
    marker should degrade to a wrong-but-obvious path that a `FileNotFoundError`
    names, not stop the process from importing.
    """
    here = (start or Path(__file__)).resolve()
    for candidate in here.parents:
        if all((candidate / marker).exists() for marker in _MARKERS):
            return candidate
    return here.parents[2]


ROOT = repo_root()

#: The bundled month of mail, and the page a judge opens.
CORPUS_ROOT = ROOT / "corpus"
WEB_ROOT = ROOT / "web"
