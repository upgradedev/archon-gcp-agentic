"""The Cloud Run service: the judge's page, the API, and the unattended trigger.

Three surfaces, and the third is the one that matters for the claim.

`GET /` serves the single page a visitor opens with no account and no install,
presses one button, and watches the close run step by step.

`GET /api/*` serves the close as JSON, which is what that page renders and what
anyone can curl.

`POST /events` is the trigger. In the deployed shape a bookkeeper drops the
month's documents into a Cloud Storage bucket, Eventarc turns the object-finalize
into a Pub/Sub push, and this route closes the month. Nobody pressed anything.
The button on the page exists so a judge can see the same thing happen on demand,
because "it fires when a file lands" is not watchable in a four-minute video.

The service holds no state of its own. Everything a run produces goes to
`store.py`, which is Firestore in the deployed shape and memory locally, so a
container that scales to zero between months loses nothing.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import PERIOD, __version__
from .close import run_close
from .mailbox import available_periods, read_period
from .store import get_store

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

#: Set ARCHON_USE_GEMINI=1 to have the ADK reporting pipeline phrase the
#: summary. Unset, the deterministic narrator writes it. The books are
#: identical either way, which is why the demo does not need a key.
USE_GEMINI = os.getenv("ARCHON_USE_GEMINI", "").lower() in ("1", "true", "yes")

COMPANY = os.getenv("ARCHON_COMPANY", "Bell Ridge Haulage")

app = FastAPI(
    title="Archon",
    version=__version__,
    description="An agent that closes a small haulier's month unattended.",
)


def _narrator():
    """The Gemini-backed narrator, when one is asked for and importable."""
    if not USE_GEMINI:
        return None
    try:
        from .agents import gemini_narrator

        return gemini_narrator()
    except Exception:  # pragma: no cover - ADK absent in a slim image
        return None


def _close(period: str) -> dict:
    """Run one close and return it as the shape the page renders."""
    try:
        documents, raw = read_period(period)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    result = run_close(
        period=period, documents=documents, company=COMPANY,
        store=get_store(), narrator=_narrator(), raw_texts=raw,
    )
    return result.to_dict()


@app.get("/api/health")
def health() -> dict:
    """Liveness, plus enough detail to tell which backend a deploy is using."""
    store = get_store()
    return {
        "status": "ok",
        "version": __version__,
        "store": getattr(store, "backend", "unknown"),
        "gemini": USE_GEMINI,
        "periods": available_periods(),
    }


@app.get("/api/periods")
def periods() -> dict:
    """Which months have mail waiting."""
    return {"periods": available_periods(), "default": PERIOD}


@app.post("/api/close/{period}")
def close_period(period: str) -> dict:
    """Close a period now. This is what the button on the page calls."""
    return _close(period)


@app.get("/api/close/{period}")
def read_close(period: str) -> dict:
    """The last close of a period, from the store.

    Falls back to running one when nothing is stored. A judge arriving at a
    cold container should see a closed month, not an empty state explaining
    that they need to press something first.
    """
    stored = get_store().load_close(COMPANY, period)
    return stored or _close(period)


@app.post("/events")
async def events(request: Request) -> JSONResponse:
    """The unattended trigger: a Pub/Sub push from Eventarc.

    Accepts the Pub/Sub envelope Eventarc sends for a Cloud Storage
    object-finalize, works out which period the object belongs to, and closes
    it. Any malformed envelope is acknowledged rather than retried: Pub/Sub
    redelivers on a non-2xx, and a message that will never parse would be
    redelivered until it expired, at the cost of one close per attempt.
    """
    try:
        envelope = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"status": "ignored", "reason": "not JSON"}, status_code=200)

    period = _period_from_envelope(envelope)
    if period is None:
        return JSONResponse(
            {"status": "ignored", "reason": "no period in the event"}, status_code=200
        )
    if period not in available_periods():
        return JSONResponse(
            {"status": "ignored", "reason": f"no mail for {period}"}, status_code=200
        )

    result = _close(period)
    return JSONResponse(
        {
            "status": "closed",
            "period": period,
            "run_id": result["run_id"],
            "outcome": result["outcome"],
            "exceptions": len(result["findings"]),
            "drafts": len(result["drafts"]),
        },
        status_code=200,
    )


def _period_from_envelope(envelope: dict) -> str | None:
    """Pull the period out of a Pub/Sub push envelope.

    Two shapes are accepted, because both turn up in practice: the object name
    inside the base64 `message.data` payload, and a period set directly in
    `message.attributes` by a Cloud Scheduler job. An object at
    `mail/2026-07/remittance.pdf` closes 2026-07.
    """
    message = envelope.get("message") or {}
    attributes = message.get("attributes") or {}

    if attributes.get("period"):
        return str(attributes["period"])

    name = attributes.get("objectId") or ""
    if not name and message.get("data"):
        try:
            payload = json.loads(base64.b64decode(message["data"]).decode("utf-8"))
            name = payload.get("name") or payload.get("objectId") or ""
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            name = ""

    for part in str(name).split("/"):
        if len(part) == 7 and part[4] == "-" and part[:4].isdigit() and part[5:].isdigit():
            return part
    return None


@app.get("/")
def index() -> FileResponse:
    """The page a judge opens."""
    return FileResponse(WEB_ROOT / "index.html")


if WEB_ROOT.is_dir():
    app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")
