"""A start command any platform can run from the repository root.

The problem this solves, stated plainly. The package lives under `src/`, which
is the layout that stops a stray `import archon` from picking up the working
tree instead of the installed copy. The cost is that `archon` is NOT importable
from the repository root, so `uvicorn archon.adapters.service:app` fails there
with ModuleNotFoundError.

Every environment that runs this already solves it, each in its own way and
none of them portable:

  Dockerfile   ENV PYTHONPATH=/app/src
  run.py       sys.path.insert(..., "src")
  pytest       pyproject's [tool.pytest.ini_options]
  editable     pip install -e .

A platform that clones the repository and looks for something to start reads
none of those. It reads a `Procfile`, and the command in it has to import
cleanly from the root. So this module does the same two lines the others do,
then re-exports the application unchanged.

**There is no second application here.** `app` below IS
`archon.adapters.service.app`, the same object the container serves, so nothing
about the deployed behaviour can drift from what a platform preview shows. A
test asserts the identity rather than trusting this sentence.

Runtime notes, so a preview is not mistaken for the production deployment:

- With no `GOOGLE_CLOUD_PROJECT` the store falls back to memory. The books are
  identical; they simply do not survive the process. `/api/health` says which.
- With `ARCHON_AGENT_CLOSE` unset the close is deterministic and takes a few
  seconds. With it on, a close runs a thinking model for a minute or more,
  which is longer than most platforms allow a single request to live. Leave it
  off outside Cloud Run.
- `/events` verifies an OIDC token minted by this project's Pub/Sub push
  subscription. Anywhere else it correctly refuses every caller.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from archon.adapters.service import app  # noqa: E402  (the path comes first)

__all__ = ["app"]
