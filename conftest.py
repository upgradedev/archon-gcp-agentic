"""Put `src/` on the path so the suite and the CLI run from a clean checkout.

A src layout normally needs an install. This product's whole spin-up claim is
that `python -m archon.cli` works on a fresh clone with nothing installed, so
the path is arranged here instead of asking a judge to `pip install -e .`.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
