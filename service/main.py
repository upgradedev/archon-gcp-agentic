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
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_ROOT / "src"))

try:
    from archon.adapters.service import app
except Exception:                                    # noqa: BLE001 - see below
    # An import that fails here takes the whole function down before a single
    # route is registered, and the platform reports that as one opaque line:
    # FUNCTION_INVOCATION_FAILED, on every path including a static file. That
    # is the least debuggable failure a deployment can have, and it cost an
    # afternoon: the app imported cleanly on this machine and in the
    # container, so nothing local could reproduce it.
    #
    # So a failed import produces a diagnosis instead of a corpse. Every value
    # below is already public in this repository (module names, paths, the
    # contents of the bundle) and the real application never reaches this
    # branch, so nothing is exposed that a reader of the source could not
    # already see. The container does NOT use this module at all: the
    # Dockerfile starts `archon.adapters.service:app` directly, so this
    # fallback cannot mask a problem in production.
    #
    # **Standard library only, and that is the whole lesson.** The first
    # version of this handler built a FastAPI app to report the failure, and
    # the failure it needed to report was `ModuleNotFoundError: fastapi`. It
    # raised inside its own except block, the platform logged both tracebacks
    # and served neither, and the diagnosis was invisible for exactly the case
    # it was written for. A diagnostic that depends on the thing that broke is
    # not a diagnostic. This is a bare ASGI callable: no framework, no import
    # that can fail, nothing to install.
    _FAILURE = traceback.format_exc()
    _CONTEXT = "\n".join([
        f"entrypoint   {_HERE}",
        f"project root {_ROOT}",
        f"src present  {(_ROOT / 'src').is_dir()}",
        f"corpus       {(_ROOT / 'corpus').is_dir()}",
        f"web          {(_ROOT / 'web').is_dir()}",
        f"python       {sys.version.split()[0]}",
        f"cwd          {Path.cwd()}",
        "root entries " + ", ".join(sorted(p.name for p in _ROOT.iterdir())[:30]),
    ])

    async def app(scope, receive, send):  # noqa: ANN001, ANN201  (raw ASGI)
        """Serve the traceback, in about twenty lines of standard library."""
        if scope["type"] != "http":
            return
        body = ("Archon could not import on this platform.\n\n"
                f"{_FAILURE}\n{_CONTEXT}\n").encode()
        await send({"type": "http.response.start", "status": 500,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

__all__ = ["app"]
