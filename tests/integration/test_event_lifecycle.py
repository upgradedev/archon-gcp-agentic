"""What happens to a month when the close CRASHES, and who is allowed to retry.

The idempotency work established that one object generation closes a period
once. It established nothing about the other half of at-least-once delivery:
the attempt that starts and does not finish.

`/events` claims a marker with `status: "processing"` and writes `closed` after
`_close` returns. Between those two lines there is a container. If it dies --
Cloud Run kills the request at 600s, the instance is scaled away mid-close, the
close raises -- the marker stays `processing` and nothing ever changes it. Every
redelivery reads `processing` and gets a 503, forever, and Pub/Sub retries a
month that CANNOT close until the message expires days later. Then the event is
gone. Nobody is told, and the marker is poisoned permanently: even re-uploading
the same object generation is refused.

That is the failure mode this file drives. It also drives the asymmetry Phase 1
opened and did not finish: `gcs._is_marker` was narrowed so a document called
`_READYish.txt` is INGESTED, and this route still decides the same name is the
batch-complete signal with a bare `startswith`. One object, read two ways.
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from archon.adapters import gcs, service
from archon.adapters.store import LocalStore
from tests.conftest import PERIOD

BUCKET = "lifecycle-bucket"


class FakeBlob:
    def __init__(self, name: str, data: bytes, generation: int = 1):
        self.name, self._data, self.size = name, data, len(data)
        self.generation = generation

    def download_as_bytes(self) -> bytes:
        return self._data


class FakeClient:
    def __init__(self, blobs):
        self._blobs = list(blobs)

    def list_blobs(self, bucket_name, prefix=""):
        return [b for b in self._blobs if b.name.startswith(prefix)]


def load_text(ref: str, amount: float) -> bytes:
    return f"""TEST BROKER
RATE CONFIRMATION

Document Type: Load Confirmation
Load Number: {ref}
Date: {PERIOD}-10
Broker: Test Broker
Carrier Unit: T-1
Miles: 500
Linehaul Rate: {amount:,.2f}
Accessorial: 0.00
Total Payable: {amount:,.2f}
""".encode()


def envelope(obj: str, generation: str = "111", message_id: str = "m-1") -> dict:
    return {"message": {
        "messageId": message_id,
        "attributes": {"bucketId": BUCKET, "objectId": obj,
                       "objectGeneration": generation, "eventType": "OBJECT_FINALIZE"},
        "data": base64.b64encode(json.dumps(
            {"bucket": BUCKET, "name": obj}).encode()).decode(),
    }}


def load_event(generation: str = "111") -> dict:
    return envelope(f"mail/{PERIOD}/load-1.txt", generation)


@pytest.fixture
def wired(monkeypatch):
    store = LocalStore()
    closes: list[str] = []
    monkeypatch.setattr(service, "get_store", lambda: store)
    monkeypatch.setattr(service.auth, "verify_push_request",
                        lambda _h: type("V", (), {"allowed": True, "caller": "t",
                                                  "reason": "test"})())
    fake = FakeClient([FakeBlob(f"mail/{PERIOD}/load-1.txt",
                                load_text("L-9001", 1000.0), 111)])
    real_read = gcs.read_gcs_period
    monkeypatch.setattr(service.gcs, "read_gcs_period",
                        lambda bucket, period, client=None:
                            real_read(bucket, period, client=fake))
    real_close = service._close

    def counted_close(period, **kwargs):
        closes.append(period)
        return real_close(period, **kwargs)

    monkeypatch.setattr(service, "_close", counted_close)
    return store, closes


def post(env: dict):
    class FakeRequest:
        headers = {"authorization": "Bearer t"}

        async def json(self):
            return env

    return asyncio.run(service.events(FakeRequest()))


def body(response) -> dict:
    return json.loads(response.body)


def marker_of(store, env) -> dict:
    return store.load_close(service.COMPANY, gcs.dedupe_key(env, PERIOD)) or {}


# -- the trigger and the mail must agree on what a marker is -----------------

def test_a_document_that_merely_starts_like_the_marker_does_not_trigger_the_close(
        wired, monkeypatch):
    """`gcs._is_marker` says `_READYish.txt` is a DOCUMENT and ingests it. This
    route said `startswith('_ready')` and closed the month on it. One object,
    read two ways: the trigger for the batch is simultaneously a line in the
    books, and a bookkeeper mid-upload gets an early close."""
    monkeypatch.setenv("ARCHON_BATCH_MARKER", "_READY")

    response = post(envelope(f"mail/{PERIOD}/_READYish.txt", "901"))

    assert body(response)["status"] == "collecting", (
        "the ingestion treats this name as mail; the trigger must agree")


def test_the_marker_itself_and_a_renamed_marker_still_close_the_month(
        wired, monkeypatch):
    """The narrowing must not undo why the prefix rule existed: this bucket's
    owner cannot overwrite an object, so re-closing needs `_READY2`."""
    monkeypatch.setenv("ARCHON_BATCH_MARKER", "_READY")

    assert body(post(envelope(f"mail/{PERIOD}/_READY", "902")))["status"] == "closed"
    assert body(post(envelope(f"mail/{PERIOD}/_READY2", "903")))["status"] == "closed"


# -- the close that starts and does not finish ------------------------------

def test_a_close_that_raises_records_the_failure_instead_of_holding_the_marker(
        wired, monkeypatch):
    """The claim is taken before `_close` runs and released nowhere. A raise
    leaves `processing` written and no code path that ever changes it."""
    store, _ = wired

    def explode(period, **kwargs):
        raise RuntimeError("model refused")

    monkeypatch.setattr(service, "_close", explode)
    env = load_event("501")

    response = post(env)

    assert response.status_code == 503, "a failed close must be retried"
    assert marker_of(store, env).get("status") == "failed", (
        "the marker still says processing, so no delivery can ever retry it")


def test_a_month_whose_close_crashed_can_be_closed_by_the_next_delivery(
        wired, monkeypatch):
    """The point of recording the failure. Pub/Sub redelivers; the second
    attempt must actually run, not read `processing` and 503 forever."""
    _store, _closes = wired
    env = load_event("502")

    def explode(period, **kwargs):
        raise RuntimeError("transient")

    monkeypatch.setattr(service, "_close", explode)
    post(env)
    monkeypatch.undo()

    second = post(env)

    assert body(second)["status"] == "closed", (
        "the month is permanently unclosable after one crash")


def test_a_holder_that_died_with_its_instance_does_not_block_the_month_forever(
        wired):
    """Nothing runs when a container is killed: no except clause, no finally.
    The marker is left `processing` by a worker that no longer exists, and the
    only thing that distinguishes it from a live holder is how old it is.

    Cloud Run kills the request at 600s (`infra/main.tf`), so a claim older
    than the lease cannot still be working."""
    store, closes = wired
    env = load_event("503")
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store.save_close(service.COMPANY, gcs.dedupe_key(env, PERIOD),
                     {"period": PERIOD, "status": "processing", "claimed_at": stale})

    response = post(env)

    assert body(response)["status"] == "closed", (
        "a dead holder's claim blocks the period until the message expires")
    assert closes.count(PERIOD) == 1


def test_a_live_holder_is_still_told_to_come_back(wired):
    """The guard on the fix. A claim taken seconds ago IS someone working."""
    store, closes = wired
    env = load_event("504")
    fresh = datetime.now(timezone.utc).isoformat()
    store.save_close(service.COMPANY, gcs.dedupe_key(env, PERIOD),
                     {"period": PERIOD, "status": "processing", "claimed_at": fresh})

    response = post(env)

    assert response.status_code == 503
    assert body(response)["status"] == "in-progress"
    assert closes == [], "a live close was run a second time"


def test_a_permanently_failing_event_is_recorded_and_acked_not_retried_to_expiry(
        wired, monkeypatch):
    """A poison event that 503s on every delivery is redelivered for as long as
    the subscription allows, and each attempt is a close attempt. After a
    bounded number of tries the failure is durable and the message is acked, so
    the record says which month failed and why instead of a week of retries."""
    store, _ = wired
    env = load_event("505")

    def explode(period, **kwargs):
        raise ValueError("bad")

    monkeypatch.setattr(service, "_close", explode)

    codes = [post(env).status_code for _ in range(4)]

    assert codes[-1] == 200, f"still asking Pub/Sub to retry after {len(codes)}: {codes}"
    assert marker_of(store, env).get("status") == "dead-letter"
    assert "ValueError" in str(marker_of(store, env).get("reason", ""))
