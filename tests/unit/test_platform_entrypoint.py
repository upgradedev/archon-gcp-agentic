"""The start command a platform reads has to work from the repository root.

The defect this closes was found the plain way: the owner connected a sibling
project to a hosting platform in one click and it worked, connected this one
and it failed. The cause was not architecture, which is what I guessed first
and got wrong. Both projects are ADK agents on Firestore with the identical
`src/` layout. The sibling simply carries a two-line path shim in a top-level
module plus a Procfile naming it; this one carried the same shim four times
over, in the Dockerfile, in run.py, in pytest config and in an editable
install, and not once in a place a platform reads.

These assert the bridge, and that it is a bridge rather than a second
application.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_the_app_is_importable_from_the_repository_root():
    """A subprocess with the root as its only path, which is the state a
    platform's start command actually runs in. Importing it in-process would
    pass on this suite's own sys.path and prove nothing."""
    result = subprocess.run(
        [sys.executable, "-c", "import service.main; print(service.main.app)"],
        cwd=ROOT, capture_output=True, text=True,
    )

    assert result.returncode == 0, (
        f"the platform start command cannot import from the root:\n{result.stderr}"
    )
    assert "FastAPI" in result.stdout


def test_it_re_exports_the_same_application_rather_than_building_a_second_one():
    """Two apps would be two behaviours, and the preview would stop predicting
    the deployment. Asserted by identity, not by reading the file."""
    import service.main
    from archon.adapters import service as deployed

    assert service.main.app is deployed.app


def test_vercel_is_pointed_at_the_shim_because_it_cannot_find_it_alone():
    """Vercel auto-detects a FastAPI `app` only at the root, in `src/` or in
    `app/`, under six filenames. `src/archon/adapters/service.py` matches none
    of them, and `service/main.py` is not one of the searched locations either,
    so the entrypoint must be declared or the deploy finds nothing to run."""
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entrypoint = config["tool"]["vercel"]["entrypoint"]

    module, _, attribute = entrypoint.partition(":")
    assert attribute == "app"
    assert (ROOT / (module.replace(".", "/") + ".py")).is_file(), (
        f"the declared entrypoint {entrypoint} does not exist on disk"
    )


def test_the_web_directory_stays_in_the_bundle():
    """The half-broken deploy this prevents.

    `app.mount("/static", StaticFiles(directory=WEB_ROOT))` makes Vercel
    promote `web/` to the CDN and, by default, drop it from the function. But
    `GET /` is not a mount: it returns `FileResponse(WEB_ROOT / "index.html")`,
    read off disk at request time. With the default, `/static/*` would serve
    happily from the CDN while the page itself returned 500, which is worse
    than a clean failure because it looks like it deployed.
    """
    import tomllib

    from archon.adapters import service

    source = pathlib.Path(service.__file__).read_text(encoding="utf-8")
    assert 'FileResponse(WEB_ROOT / "index.html")' in source, (
        "the index route no longer reads from disk; re-check whether the "
        "static exclusion below is still needed"
    )

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["vercel"]["fastapi"]["static"]["exclude"] is False


def test_the_procfile_names_a_command_that_matches_the_container():
    """A Procfile that starts something the Dockerfile does not is a second
    deployment shape nobody checks. Both must name the same ASGI app."""
    lines = [ln for ln in (ROOT / "Procfile").read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert len(lines) == 1, f"a Procfile with two process types is two deployments: {lines}"
    web = lines[0]
    assert web.startswith("web: uvicorn service.main:app")
    assert "--port $PORT" in web

    # The container reaches the same object by a different route: it puts src
    # on PYTHONPATH and names the package path directly.
    assert "PYTHONPATH=/app/src" in dockerfile
    assert "uvicorn archon.adapters.service:app" in dockerfile


def test_the_python_pin_is_a_version_the_project_supports():
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert pinned.count(".") == 1, f"pin a minor version, got {pinned!r}"
    major, minor = (int(part) for part in pinned.split("."))
    assert (major, minor) >= (3, 11), "pyproject requires >= 3.11"
    assert 'requires-python = ">=3.11"' in pyproject
