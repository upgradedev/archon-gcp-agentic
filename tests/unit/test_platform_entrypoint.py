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


def test_nothing_in_this_repository_justifies_a_javascript_build():
    """The failure that started this, verbatim from the platform log:

        sh: line 1: vite: command not found
        Error: Command "vite build" exited with 127

    The repository was never the cause. The PROJECT carried a framework preset
    from its first import, so the platform installed the Python dependencies
    correctly and then ran a JavaScript bundler against a tree that has never
    contained one. A preset survives every push, which is why three commits
    changed nothing.

    I first answered that with `framework: null` and `buildCommand: ""` in
    vercel.json, which override the dashboard. That was right for the stale
    project and WRONG once it was deleted: `null` selects "Other", and this
    platform ships a FastAPI preset that a fresh import detects from
    `fastapi` in requirements.txt. Forcing "Other" risks suppressing the
    detection that is now working in our favour, so both overrides are gone
    and auto-detection is left alone.

    What remains asserted is the premise underneath all of it. If a
    package.json ever appears, whoever adds it should meet this test and
    reconsider, rather than silently contradicting the CSP that refuses
    inline script and style precisely because the page is hand-written.
    """
    assert not (ROOT / "package.json").exists()
    assert not (ROOT / "node_modules").exists()
    for name in ("vite.config.js", "vite.config.ts", "webpack.config.js",
                 "rollup.config.js", "next.config.js"):
        assert not (ROOT / name).exists(), f"{name} would justify a build step"


def test_the_runtime_files_the_app_reads_are_not_excluded_from_the_bundle():
    """`excludeFiles` trims the function, and trimming the wrong directory is
    a deploy that builds cleanly and 500s on the first request.

    `corpus/` is the bundled month the demo closes and `web/` is the page
    itself, both read off disk at request time. Tests, the video pipeline,
    terraform and CI scripts are not reachable at runtime and only cost bundle
    space against the size limit.
    """
    import json

    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    excluded = config["functions"]["service/main.py"]["excludeFiles"]

    for needed in ("corpus", "web", "src"):
        assert needed not in excluded, (
            f"{needed}/ is read at request time; excluding it deploys a 500"
        )
    for waste in ("tests/**", "video/**", "infra/**"):
        assert waste in excluded


def test_the_declared_python_version_is_one_the_platform_offers():
    """3.12, 3.13 and 3.14 are the available versions, 3.12 the default. An
    unsupported pin is silently ignored, which is worse than a failure: the
    build runs on a version nobody chose."""
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()

    assert pinned in ("3.12", "3.13", "3.14"), (
        f"{pinned} is not offered by the platform runtime"
    )


def test_a_failed_import_reports_why_instead_of_dying_silently():
    """The afternoon this cost.

    The function crashed on every path including a static file, and the only
    thing the platform said was FUNCTION_INVOCATION_FAILED. The app imported
    cleanly on the build machine and inside the container, so nothing local
    reproduced it and there was nothing to read but a request id.

    An import failure now serves its own traceback. Simulated here by pointing
    the shim at a package that cannot exist, because the real one imports
    fine and a test that cannot trigger the branch proves nothing about it.
    """
    import importlib.util

    source = (ROOT / "service" / "main.py").read_text(encoding="utf-8")
    broken = source.replace("from archon.adapters.service import app",
                            "from archon.adapters.no_such_module import app")
    assert broken != source, "the import line moved; this test no longer arms the branch"

    namespace: dict = {"__file__": str(ROOT / "service" / "main.py")}
    spec = importlib.util.spec_from_loader("broken_shim", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(ROOT / "service" / "main.py")
    exec(compile(broken, "service/main.py", "exec"), module.__dict__)  # noqa: S102

    assert hasattr(module, "app"), "a failed import must still export an app to serve"

    # Driven as a raw ASGI callable, deliberately, because that is what it is.
    # Using a framework test client here would hide the property that matters.
    import asyncio

    sent: list = []

    async def drive():
        await module.app(
            {"type": "http", "method": "GET", "path": "/api/health"},
            None, lambda message: _collect(message),
        )

    async def _collect(message):
        sent.append(message)

    asyncio.run(drive())

    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body").decode()

    assert start["status"] == 500
    assert "could not import" in body
    assert "no_such_module" in body, "the traceback is the whole point"
    assert "project root" in body and "root entries" in body
    assert namespace is not None


def test_the_failure_handler_imports_nothing_that_could_be_missing():
    """The first version of that handler built a FastAPI app to report the
    failure, and the failure it had to report was `No module named 'fastapi'`.
    It raised inside its own except block and the diagnosis never reached the
    page, for exactly the case it existed to cover.

    So the except branch is standard library only, asserted by reading it.
    """
    source = (ROOT / "service" / "main.py").read_text(encoding="utf-8")
    branch = source.split("except Exception:")[1]

    for forbidden in ("fastapi", "starlette", "pydantic", "uvicorn", "archon"):
        assert f"import {forbidden}" not in branch and f"from {forbidden}" not in branch, (
            f"the failure handler imports {forbidden}, which may be the thing "
            "that is missing when it runs"
        )


def test_the_two_dependency_lists_say_the_same_thing():
    """The defect that produced `ModuleNotFoundError: fastapi` in production.

    A `[project]` table makes pyproject.toml the dependency manifest. With no
    `dependencies` array it reads as "this package needs nothing", the
    platform installs nothing, requirements.txt is never consulted, and the
    build reports success because there was genuinely nothing to install. The
    function then dies on its first third-party import.

    Both lists now exist, so both can drift. This is the thing that stops it.
    """
    import tomllib

    declared = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["dependencies"]
    pinned = [ln.strip() for ln
              in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.lstrip().startswith("#")]

    assert declared, "an empty dependencies array installs nothing, silently"
    assert sorted(declared) == sorted(pinned), (
        "pyproject and requirements.txt disagree. "
        f"only in pyproject: {sorted(set(declared) - set(pinned))}; "
        f"only in requirements: {sorted(set(pinned) - set(declared))}"
    )
    assert any(d.startswith("fastapi") for d in declared)
