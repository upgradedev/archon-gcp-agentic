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
    """A name reaches a manifest and a document id, so it must not be able to
    climb out of anything.

    This asserted "no slash at all" until folders had to be kept: two
    documents in different folders are two documents, and flattening to the
    basename lost one of them silently. So the property is not the absence of
    a separator, it is that nothing can go UP -- no `..`, no leading slash, no
    drive letter -- and the store percent-encodes what is left before it ever
    becomes a document id.
    """
    body = client.post("/api/close/upload", json={
        "period": "2026-07",
        "documents": [
            {"name": "../../etc/passwd.txt", "text": "Document Type: Unknown"},
            {"name": "/absolute/x.txt", "text": "Document Type: Unknown"},
        ],
    }).json()

    names = ([d.get("source_file") for d in (body.get("documents") or [])]
             or [f.get("source_file") for f in (body.get("findings") or [])])
    for name in [n for n in names if n]:
        assert ".." not in str(name), name
        assert not str(name).startswith("/"), name
        assert ":" not in str(name), name


def test_two_documents_in_different_folders_are_two_documents(client):
    """The data loss. `raw` was keyed by BASENAME, so `scans/invoice.txt` and
    `email/invoice.txt` collided and the second silently replaced the first:
    two documents in, one closed, revenue short by the whole of the missing
    one, and no error anywhere.

    A bookkeeping product that quietly drops a document is the one thing this
    product exists not to be.
    """
    def load(name, ref, amount):
        return {"name": name, "text":
                f"Document Type: Load Confirmation\nLoad Number: {ref}\n"
                f"Date: 2026-07-01\nMiles: 10\nLinehaul Rate: {amount}.00\n"
                f"Total Payable: {amount}.00\n"}

    body = client.post("/api/close/upload", json={
        "period": "2026-07",
        "documents": [load("scans/invoice.txt", "A-1", 111),
                      load("email/invoice.txt", "B-2", 222)],
    }).json()

    assert body["statements"]["revenue"] == 333.0, "a document was dropped"


def test_a_real_duplicate_is_refused_by_name(client):
    """Two documents that genuinely share a logical name cannot both be kept,
    so the answer is a refusal that names them, never a silent overwrite."""
    doc = {"name": "invoice.txt", "text": "Document Type: Unknown\n"}

    response = client.post("/api/close/upload",
                           json={"period": "2026-07", "documents": [doc, dict(doc)]})

    assert response.status_code == 400
    assert "invoice.txt" in response.json()["detail"]
    assert "Nothing was closed" in response.json()["detail"]


def test_every_unsupported_file_is_named_at_once(client):
    """Raising on the first one told somebody who dragged in a folder about one
    file and left them to guess at the rest."""
    response = client.post("/api/close/upload", json={
        "period": "2026-07",
        "documents": [{"name": "a.pdf", "text": "x"},
                      {"name": "b.docx", "text": "y"},
                      {"name": "c.png", "text": "z"}],
    })

    detail = response.json()["detail"]
    assert response.status_code == 400
    assert all(name in detail for name in ("a.pdf", "b.docx", "c.png"))


def test_uppercase_extensions_are_accepted_here_too(client):
    """`.TXT` off a scanner is a text file on every other path in this product."""
    body = client.post("/api/close/upload", json={
        "period": "2026-07",
        "documents": [{"name": "LOAD.TXT", "text":
                       "Document Type: Load Confirmation\nLoad Number: U-1\n"
                       "Date: 2026-07-02\nMiles: 5\nLinehaul Rate: 50.00\n"
                       "Total Payable: 50.00\n"}],
    }).json()

    assert body["statements"]["revenue"] == 50.0
