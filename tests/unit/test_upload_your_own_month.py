"""The route that answers "does it work on MY month", and its refusals.

The demo answered one question well and refused the only one an owner actually
has. `_close` had always taken documents directly, so what was missing was a
door -- and a door onto a public URL is only as good as what it will not let
through.
"""
from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from archon.adapters import ratelimit  # noqa: E402
from archon.adapters.service import (  # noqa: E402
    UPLOAD_MAX_BYTES_EACH,
    UPLOAD_MAX_DOCUMENTS,
    app,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]


def corpus(name: str) -> dict:
    return {"name": name,
            "text": (ROOT / "corpus" / "2026-07" / name).read_text(encoding="utf-8")}


@pytest.fixture
def client():
    ratelimit.PUBLIC_CLOSES.reset()
    return TestClient(app)


def test_a_visitor_closes_their_own_month(client):
    """Two documents somebody brought, and the short payment inside them."""
    body = client.post("/api/close/upload", json={
        "period": "2026-07",
        "documents": [corpus("load-L-7105.txt"), corpus("remittance-TFX-RA-4417.txt")],
    }).json()

    assert body["outcome"] == "closed"
    assert body["recoverable"] == 200.0, "the accessorial the advice dropped"
    assert body["source"]["mailbox"] == "uploaded"


def test_no_model_is_ever_reached_on_this_route(client):
    """The text is a stranger's. A route that forwards it to a language model
    is a prompt-injection surface, and the deterministic close is the half that
    finds the money anyway."""
    body = client.post("/api/close/upload", json={
        "period": "2026-07",
        "documents": [corpus("load-L-7105.txt")],
    }).json()

    assert body["driver"] == "deterministic"


def test_nothing_a_visitor_sends_is_kept(client):
    """An ephemeral store, so the upload cannot appear in the owner's books.

    The close a judge reads at /api/close/2026-07 must be untouched by anything
    a stranger pushed at this route.
    """
    before = client.get("/api/close/2026-07").json()

    client.post("/api/close/upload", json={
        "period": "2026-07",
        "documents": [corpus("load-L-7101.txt")],
    })

    after = client.get("/api/close/2026-07").json()
    assert after["run_id"] == before["run_id"], "an upload overwrote the stored close"


@pytest.mark.parametrize("payload, expected", [
    ({"period": "2026-07", "documents": []}, 400),
    ({"period": "2026-07", "documents": "not a list"}, 400),
    ({"period": "not-a-month", "documents": [{"name": "a.txt", "text": "x"}]}, 400),
    ({"period": "2026-07", "documents": [{"name": "a.pdf", "text": "x"}]}, 400),
    ({"period": "2026-07", "documents": [{"name": "a.txt"}]}, 400),
    ({"period": "2026-07", "documents": [{"name": "a.txt", "text": 5}]}, 400),
])
def test_what_the_route_refuses(client, payload, expected):
    assert client.post("/api/close/upload", json=payload).status_code == expected


def test_too_many_documents_is_refused_with_the_local_way_out(client):
    """A cap is a refusal, so it has to say what to do instead."""
    many = [{"name": f"d{i}.txt", "text": "x"} for i in range(UPLOAD_MAX_DOCUMENTS + 1)]

    response = client.post("/api/close/upload",
                           json={"period": "2026-07", "documents": many})

    assert response.status_code == 413
    assert "--mail" in response.json()["detail"], "the cap must name the local route"


def test_one_oversized_document_is_refused(client):
    big = {"name": "big.txt", "text": "x" * (UPLOAD_MAX_BYTES_EACH + 1)}

    response = client.post("/api/close/upload",
                           json={"period": "2026-07", "documents": [big]})

    assert response.status_code == 413


def test_a_path_in_a_filename_cannot_escape(client):
    """The name is used as a source_file and reaches a manifest, so a path in
    it is a path traversal waiting for somewhere to land."""
    body = client.post("/api/close/upload", json={
        "period": "2026-07",
        "documents": [{"name": "../../etc/passwd.txt", "text": "Document Type: Unknown"}],
    }).json()

    files = [d.get("source_file") for d in body.get("documents", [])] or \
            [f.get("source_file") for f in body.get("findings", [])]
    assert not any("/" in str(f) or ".." in str(f) for f in files if f), files
