"""The Cloud Run service: the judge's page, the API, and the unattended trigger.

Three surfaces, and the third is the one that matters for the claim.

`GET /` serves the single page a visitor opens with no account and no install,
presses one button, and watches the close run step by step.

`GET /api/*` serves the close as JSON, which is what that page renders and what
anyone can curl.

`POST /events` is the trigger. In the deployed shape a bookkeeper drops the
month's documents into a Cloud Storage bucket, the bucket's own finalize
notification publishes to a Pub/Sub topic, its push subscription calls this
route with an OIDC token, and the month closes. Nobody pressed anything.
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
import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .. import PERIOD, __version__, paths
from ..runtime.close import run_close
from ..runtime.mailbox import available_periods, read_period
from . import auth, gcs, headers, ratelimit
from . import delivery as delivery_mod
from .store import LocalStore, get_store

log = logging.getLogger("archon.service")

WEB_ROOT = paths.WEB_ROOT

#: Set ARCHON_USE_GEMINI=1 to have the ADK reporting pipeline phrase the
#: summary. Unset, the deterministic narrator writes it. The books are
#: identical either way, which is why the demo does not need a key.
USE_GEMINI = os.getenv("ARCHON_USE_GEMINI", "").lower() in ("1", "true", "yes")

#: Set ARCHON_AGENT_CLOSE=1 to have the ADK agent drive the close on this route
#: rather than the deterministic orchestrator calling the same tools directly.
#:
#: It is a separate switch from ARCHON_USE_GEMINI on purpose. The narrator is a
#: phrasing layer and a model failure there costs a sentence; the agent is the
#: control flow, and a model failure there costs the close. The public route is
#: also what the readiness gate probes as a veto, so this path falls back rather
#: than 500s, and `/api/health` reports which one actually ran.
USE_AGENT = os.getenv("ARCHON_AGENT_CLOSE", "").lower() in ("1", "true", "yes")

COMPANY = os.getenv("ARCHON_COMPANY", "Bell Ridge Haulage")

app = FastAPI(
    title="Archon",
    version=__version__,
    description="An agent that closes a small haulier's month unattended.",
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Every response carries the headers a public page should carry.

    Added because a DAST scan against the running container found five of them
    missing, and a finding a scanner reports is a finding whether or not anyone
    was going to exploit it. Fixed here rather than added to the scanner's
    ignore list.
    """
    response = await call_next(request)
    headers.apply(response.headers)
    return response


def _narrator():
    """The Gemini-backed narrator, when one is asked for and importable."""
    if not USE_GEMINI:
        return None
    try:
        from .agents import gemini_narrator

        return gemini_narrator()
    except Exception:  # pragma: no cover - ADK absent in a slim image
        return None


def _close(period: str, store=None, documents=None, raw=None, source=None,
           *, public: bool = False, allow_model: bool = True) -> dict:
    """Run one close and return it as the shape the page renders.

    `store` decides what the run is allowed to touch, and it was described here
    as "the whole of the least-privilege control". It was not, and the sentence
    was the reason nobody looked further: the store was handed in and the
    DELIVERER was not, so `run_close` resolved one from the environment. On a
    deployment configured for real mail, an anonymous visitor pressing a demo
    button put a message in the owner's inbox, and the page's own boot fetch
    did it without anyone pressing anything.

    `public=True` is what an anonymous caller gets. It hands in a deliverer
    that has no code path that sends, so this is structural rather than a flag
    that configuration can switch back on.
    """
    if documents is None:
        try:
            documents, raw = read_period(period)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if source is None:
            source = {"mailbox": "bundled-sample",
                      "release": os.getenv("ARCHON_RELEASE") or None,
                      "detail": f"corpus/{period}, the synthetic month shipped "
                                "with the repository"}

    target = store if store is not None else get_store()
    previous = _previous_statements(period)
    deliverer = delivery_mod.SandboxDelivery() if public else None
    # `allow_model=False` is a hard refusal, not a preference: no agent, no
    # narrator, no Gemini call of any kind. It is what a GET gets.
    narrator = _narrator() if allow_model else None

    # The agent first, when it is switched on. A model that refuses, times out
    # or is not reachable must not take the judge's button down with it, so the
    # failure is logged with its reason and the deterministic path runs. Which
    # one produced the payload is stamped on it rather than inferred.
    if USE_AGENT and allow_model:
        try:
            from .agents import run_agent_close

            result, _final = run_agent_close(
                period=period, company=COMPANY, store=target,
                previous=previous, narrator=narrator,
                documents=documents, raw=raw, source=source,
                deliverer=deliverer,
            )
            if result is not None:
                payload = result.to_dict()
                payload["driver"] = "adk-agent"
                return payload
            log.warning("agent close produced no result for %s; falling back", period)
        except Exception as exc:                       # noqa: BLE001 - see docstring
            log.warning("agent close failed for %s (%s: %s); falling back",
                        period, type(exc).__name__, exc)

    result = run_close(
        period=period, documents=documents, company=COMPANY,
        store=target, narrator=narrator, raw_texts=raw, previous=previous,
        source=source, deliverer=deliverer,
    )
    payload = result.to_dict()
    payload["driver"] = "deterministic"
    return payload


def _previous_statements(period: str):
    """The month before this one, if it has ever been closed.

    Read from the store rather than recomputed, so the comparison is against
    what was actually filed. A trend that recomputes its own history is a
    second source of truth for a number that already has one.
    """
    from ..domain.models import Statements

    periods = available_periods()
    try:
        index = periods.index(period)
    except ValueError:
        return None
    if index == 0:
        return None

    stored = get_store().load_close(COMPANY, periods[index - 1])
    if not stored or "statements" not in stored:
        # Never closed, so close it quietly to have something to compare
        # against. It is the same deterministic run and it costs milliseconds.
        try:
            documents, raw = read_period(periods[index - 1])
        except FileNotFoundError:
            return None
        from ..adapters.store import LocalStore

        # A REHEARSAL, and this is the second time the same mistake was found
        # in the same shape. The public route was given a sandbox deliverer and
        # this call was not, so an anonymous press on the July button sent the
        # owner a letter about JUNE: not the month anyone asked for, closed
        # only to source a trend comparison, and mailed because nobody had
        # thought of it as a close at all.
        #
        # `commit=False` is the whole answer rather than another deliverer to
        # remember: nothing is stored, nothing is delivered, no model is
        # called, and only the figures are kept. A month closed to be compared
        # against is not a month being closed.
        return run_close(period=periods[index - 1], documents=documents,
                         company=COMPANY, store=LocalStore(),
                         raw_texts=raw, commit=False).statements

    fields = {f: stored["statements"].get(f) for f in Statements.__dataclass_fields__}
    return Statements(**fields)


def _model_id() -> str:
    """The model this deployment would actually call.

    Read from the same place the agent reads it, so health cannot report one
    model while the close uses another.
    """
    from .agents import DEFAULT_MODEL

    return DEFAULT_MODEL


def _last_successful_close() -> dict:
    """What the last close that actually finished says about itself.

    `close_path` reports what this deployment is CONFIGURED to do, and that is
    a different fact from what it has DONE. A container with the agent switched
    on whose every model call is failing still answers "adk-agent", so a judge
    reading the health endpoint is told the sponsor claim holds while the
    deterministic fallback is quietly carrying every close.

    So the configured path and the observed one are now separate fields. This
    one is read from the durable store, which is the only place that knows.
    Failures are swallowed on purpose: health must answer even when the store
    cannot, and an unknown last run is reported as unknown rather than as an
    outage.
    """
    try:
        stored = get_store().load_close(COMPANY, PERIOD)
    except Exception:                                  # noqa: BLE001
        return {"driver": None, "release": None, "run_id": None, "outcome": None}
    if not stored:
        return {"driver": None, "release": None, "run_id": None, "outcome": None}
    return {
        "driver": stored.get("driver"),
        "release": (stored.get("source") or {}).get("release"),
        "run_id": stored.get("run_id"),
        "outcome": stored.get("outcome"),
        "period": stored.get("period"),
    }


@app.get("/api/health")
def health() -> dict:
    """Liveness, plus enough detail to tell which backend a deploy is using."""
    store = get_store()
    last = _last_successful_close()
    configured = "adk-agent" if USE_AGENT else "deterministic"
    return {
        "status": "ok",
        "version": __version__,
        "store": getattr(store, "backend", "unknown"),
        "gemini": USE_GEMINI,
        # Configured, observed, and whether they agree. The third is the one a
        # judge actually wants: "degraded" means this deployment is set up to
        # run the agent and the last close it finished did not.
        "close_path_configured": configured,
        "last_close": last,
        "degraded": bool(
            USE_AGENT and last.get("driver") and last.get("driver") != "adk-agent"
        ),
        # Kept under the old name because the page, the readiness gate and the
        # video's preflight all read it. It still means "configured".
        "close_path": configured,
        "model": _model_id() if (USE_AGENT or USE_GEMINI) else None,
        # Which build answered. K_REVISION is stamped by Cloud Run itself;
        # ARCHON_RELEASE is set at deploy time to the short commit, so a judge
        # can tie this JSON to a commit without trusting our README.
        "revision": os.getenv("K_REVISION"),
        "release": os.getenv("ARCHON_RELEASE"),
        "events_auth": auth.posture(),
        "periods": available_periods(),
    }


@app.get("/api/periods")
def periods() -> dict:
    """Which months have mail waiting."""
    return {"periods": available_periods(), "default": PERIOD}


#: The deterministic close a cold container serves until a real one is filed.
#: One per period per process, and never invalidated: a stored close is
#: returned before this is consulted, so the only thing this can go stale
#: against is itself.
_COLD_START_CLOSES: dict[str, dict] = {}


@app.post("/api/close/{period}")
def close_period(period: str, request: Request) -> dict:
    """Close a period now. This is what the button on the page calls.

    Anonymous, and deliberately unable to write: the run happens against an
    ephemeral in-memory store, so a stranger pressing the button cannot change
    anything the owner will read later. The books are byte-identical to the
    trusted path; only their lifetime differs.

    It is also unable to SEND, and bounded in what it may spend. Neither was
    true before: the store was handed in and the deliverer was not, and nothing
    limited how often a stranger could start a thinking-model conversation on a
    public URL.
    """
    caller = request.client.host if request.client else "unknown"
    allowed, why = ratelimit.PUBLIC_CLOSES.check(caller)
    if not allowed:
        raise HTTPException(status_code=429, detail=why)

    with ratelimit.PUBLIC_CLOSES.slot() as slot:
        if not slot.acquired:
            raise HTTPException(
                status_code=429,
                detail=(f"{ratelimit.PUBLIC_CLOSES.concurrent} fresh closes are already "
                        "running. The saved close is available now and is the same books."),
            )
        return _close(period, store=LocalStore(), public=True)


@app.get("/api/close/{period}")
def read_close(period: str) -> dict:
    """The last close of a period, from the store. A read, and only a read.

    This used to fall back to RUNNING a close when nothing was stored, which
    made a GET a side effect: the page fetches this on every load, so an
    anonymous visitor arriving at a cold container started an agent close and,
    before the deliverer was fixed, could put mail in the owner's inbox without
    pressing anything. A refresh loop was an unbounded bill from a route that
    reads.

    So the fallback is a DETERMINISTIC close: no agent, no model call, nothing
    delivered, nothing stored. It gives a judge the closed month they should
    see on arrival, and it is honest about what produced it -- the payload is
    stamped `driver: deterministic`, and the page says so.
    """
    stored = get_store().load_close(COMPANY, period)
    if stored:
        return stored

    # Computed once per process per period. The page fires this route on every
    # load, so on a cold container a refresh loop recomputed the whole month
    # every time: no model and no mail any more, but still a full close per
    # request, from a route nothing rate limits and nothing should, because
    # rate limiting a page load breaks the page.
    #
    # The answer is deterministic and the mail it reads is bundled with the
    # image, so the second caller can have the first caller's copy. It is not
    # invalidated because nothing it depends on can change inside one process:
    # a real close arriving in the durable store is returned above, before this
    # is ever reached.
    cached = _COLD_START_CLOSES.get(period)
    if cached is not None:
        return cached

    payload = _close(period, store=LocalStore(), public=True, allow_model=False)
    _COLD_START_CLOSES[period] = payload
    return payload


@app.post("/events")
async def events(request: Request) -> JSONResponse:
    """The unattended trigger: a Pub/Sub push from a bucket notification.

    There is no Eventarc trigger here, and calling it one would misname the
    architecture to the people who built the services. `infra/main.tf` creates
    a `google_storage_notification` on the bucket, a topic it publishes to, and
    a push subscription that calls this route.

    Accepts that envelope, works out which period the object belongs to, and closes
    it. Any malformed envelope is acknowledged rather than retried: Pub/Sub
    redelivers on a non-2xx, and a message that will never parse would be
    redelivered until it expired, at the cost of one close per attempt.
    """
    # Verified before anything is parsed or persisted. This is the only route
    # that can write to the durable store, so it is the only one that needs a
    # caller identity, and it fails closed when it cannot establish one.
    verdict = auth.verify_push_request(request.headers.get("authorization"))
    if not verdict.allowed:
        # Logged, because a refusal that only appears in a response body is
        # invisible to whoever is debugging it. A trigger silently 403ing is
        # the failure that looks exactly like a trigger nobody pulled, and the
        # access log alone cannot tell those apart.
        log.warning(
            "refused a trigger: %s (caller=%s, audience_configured=%s)",
            verdict.reason, verdict.caller, auth.required(),
        )
        return JSONResponse(
            {"status": "refused", "reason": verdict.reason}, status_code=403
        )

    try:
        envelope = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"status": "ignored", "reason": "not JSON"}, status_code=200)

    period = _period_from_envelope(envelope)
    if period is None:
        return JSONResponse(
            {"status": "ignored", "reason": "no period in the event"}, status_code=200
        )

    # One close per object generation. Pub/Sub is at-least-once, and without
    # this a redelivered message re-runs the close and re-writes the record,
    # which is wasteful at best and, with the agent on, costs a model run.
    # A month can arrive as one object or as twenty-seven, and the difference
    # matters: every OBJECT_FINALIZE starts a close, so a bookkeeper dragging a
    # folder into the bucket runs the month once per file. Each close is
    # correct and each supersedes the last, but twenty-six of them are wasted,
    # and with the agent on each one is a model conversation.
    #
    # The fix for that is a batch-complete signal, and it has to be explicit:
    # Cloud Storage does not say "that was the last one", and a settle window
    # needs a durable timer that a scale-to-zero container does not have.
    #
    # `ARCHON_BATCH_MARKER` names the object that means "the batch is
    # complete". Ordinary uploads are recorded and acknowledged; only the
    # marker closes the month, so twenty-seven uploads plus one marker is one
    # close, one run journal and one set of drafts over the whole month.
    #
    # It is DECLARED BY THE DEPLOYMENT rather than defaulted in here, and
    # `infra/main.tf` sets it to `_READY` so the live service has it on. That is
    # the right home for it: whether a month arrives as one object or as
    # twenty-seven is a fact about a customer's bookkeeping, not about this
    # library, and a deployment whose mail genuinely arrives one whole month at
    # a time should leave it unset.
    #
    # Baking the default in here was tried and reverted: it changed what every
    # test that drives this route is testing, because "an object landed" stopped
    # meaning "close the month" for all of them at once. The behaviour is worth
    # having and the coupling is not.
    batch_marker = os.getenv("ARCHON_BATCH_MARKER", "").strip()
    if batch_marker:
        obj = ((envelope.get("message") or {}).get("attributes") or {}).get("objectId", "")
        name = obj.rsplit("/", 1)[-1]
        # STARTSWITH, not equals. A marker cannot always be overwritten: on
        # this project's own bucket the owner holds bucket-level roles only and
        # `gcloud storage cp` over an existing object returns 403 on
        # `storage.objects.get`. Re-closing a month then needs a NEW object, so
        # `_READY2` and `_READY-2026-08-28` have to count. Found by doing it.
        if name and not name.lower().startswith(batch_marker.lower()):
            log.info("collecting %s for %s; waiting for %s", name, period, batch_marker)
            return JSONResponse(
                {"status": "collecting", "period": period, "object": name,
                 "reason": f"held until {batch_marker} says the batch is complete",
                 # Named so that whoever is watching the logs, or curling this
                 # route, is told what to do next rather than left wondering
                 # why nothing happened.
                 "next": f"upload an empty object named {batch_marker} under "
                         f"mail/{period}/ to close the month"},
                status_code=200)

    marker_key = gcs.dedupe_key(envelope, period)
    if marker_key:
        # ONE operation, not three. This used to read the marker, decide it was
        # absent, and then write it, with a gap in the middle that two Pub/Sub
        # deliveries of the same message both fitted through: both read absent,
        # both ran a close, and with the agent on that is two model
        # conversations and two owner digests for one event.
        #
        # `claim` creates the marker only if nothing holds it, atomically --
        # a lock in memory, `create()` in Firestore, which fails when the
        # document exists. Losing the race is now indistinguishable from
        # arriving second, which is what it always was.
        mine = get_store().claim(COMPANY, marker_key,
                                 {"period": period, "status": "processing"})
        if not mine:
            marker = get_store().load_close(COMPANY, marker_key) or {}
            if marker.get("status") == "closed":
                return JSONResponse(
                    {"status": "duplicate", "reason":
                     "this object generation already closed the period"},
                    status_code=200)
            # Someone else holds it and has not finished. A 200 here would ack
            # work that has not been done; a 503 makes Pub/Sub come back, by
            # which time the marker is either closed (duplicate) or still held
            # (come back again). If the holder died with its instance, the
            # retries land here until the message expires, each answered in
            # milliseconds.
            return JSONResponse(
                {"status": "in-progress", "reason":
                 "this object generation is being closed right now"},
                status_code=503)

    # The mail is the actual objects in the bucket the event names, not the
    # bundled corpus. The event used to pick only the *period* and the close
    # re-read the repository's own sample month, which meant the object a
    # bookkeeper uploaded was never opened. If the bucket cannot be read the
    # failure is acknowledged and named rather than retried forever: a push
    # handler that 500s on a permanent error is redelivered until it expires.
    bucket = _bucket_from_envelope(envelope)
    documents = raw = source = None
    if bucket:
        try:
            documents, raw, manifest = await run_in_threadpool(
                gcs.read_gcs_period, bucket, period)
            if documents:
                source = gcs.event_source(envelope, period, manifest)
            else:
                return JSONResponse(
                    {"status": "ignored",
                     "reason": f"no readable mail under gs://{bucket}/mail/{period}/"},
                    status_code=200)
        except Exception as exc:                       # noqa: BLE001 - push handler
            log.error("gcs mailbox read failed for %s (%s: %s)",
                      period, type(exc).__name__, exc)
            # Transient storage trouble is retryable and says so: a 503 makes
            # Pub/Sub redeliver once the blip passes. Anything else is acked
            # with the reason recorded, because a permanently malformed event
            # redelivered until expiry is a close per attempt for nothing.
            transient = type(exc).__name__ in {
                "ServiceUnavailable", "InternalServerError", "TooManyRequests",
                "DeadlineExceeded", "GatewayTimeout", "RetryError",
            }
            return JSONResponse(
                {"status": "error",
                 "reason": f"could not read gs://{bucket}/mail/{period}/: "
                           f"{type(exc).__name__}"},
                status_code=503 if transient else 200)
    elif period not in available_periods():
        return JSONResponse(
            {"status": "ignored", "reason": f"no mail for {period}"}, status_code=200
        )

    # In the threadpool, deliberately. This handler is `async def`, so a
    # synchronous close here runs ON the event loop, and with the agent on a
    # thinking model that is minutes of blocking: every request on the
    # instance, /api/health included, starves behind it, Cloud Run times them
    # all out at once, and Pub/Sub answers the 504s with redeliveries. That is
    # not a theory: it took the deployed service down for half an hour on
    # 2026-08-24 and the fix was this line.
    result = await run_in_threadpool(
        _close, period, documents=documents, raw=raw, source=source)
    if marker_key:
        get_store().save_close(COMPANY, marker_key,
                               {"period": period, "status": "closed",
                                "run_id": result["run_id"]})
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


def _bucket_from_envelope(envelope: dict) -> str | None:
    """The bucket the event names, from attributes or the payload."""
    message = envelope.get("message") or {}
    attributes = message.get("attributes") or {}
    if attributes.get("bucketId"):
        return str(attributes["bucketId"])
    if message.get("data"):
        try:
            payload = json.loads(base64.b64decode(message["data"]).decode("utf-8"))
            return payload.get("bucket") or None
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
    return None


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
