"""The mail is the bytes in the bucket, and the record proves which bytes.

The defect this suite exists to stop recurring: `/events` took only the
*period* out of the notification and re-read the bundled corpus. The trigger
was real, the ingestion was not, and nothing on any surface said so. A judge
who uploaded their own remittance got a close of our sample month back.

Everything here drives fakes. `google-cloud-storage` is injected precisely so
none of this needs a network, a bucket or the library installed.
"""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from archon.adapters import gcs, service
from archon.adapters.store import LocalStore
from tests.conftest import PERIOD

BUCKET = "test-mail-bucket"


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeBlob:
    def __init__(self, name: str, data: bytes, generation: int = 1):
        self.name = name
        self._data = data
        self.size = len(data)
        self.generation = generation

    def download_as_bytes(self) -> bytes:
        return self._data


class FakeClient:
    def __init__(self, blobs):
        self._blobs = list(blobs)

    def list_blobs(self, bucket_name, prefix=""):
        assert bucket_name == BUCKET
        return [b for b in self._blobs if b.name.startswith(prefix)]


def load_text(ref: str, amount: float) -> bytes:
    """The real label-block format the extractor parses, from the corpus."""
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


def blob(name: str, data: bytes, generation: int = 1) -> FakeBlob:
    return FakeBlob(f"mail/{PERIOD}/{name}", data, generation)


def envelope(obj: str = f"mail/{PERIOD}/load-1.txt", generation: str = "111",
             message_id: str = "m-1") -> dict:
    return {"message": {
        "messageId": message_id,
        "attributes": {"bucketId": BUCKET, "objectId": obj,
                       "objectGeneration": generation, "eventType": "OBJECT_FINALIZE"},
        "data": base64.b64encode(json.dumps(
            {"bucket": BUCKET, "name": obj}).encode()).decode(),
    }}


# ── reading the bucket ───────────────────────────────────────────────────────

def test_the_objects_bytes_are_what_gets_parsed():
    client = FakeClient([blob("load-1.txt", load_text("L-9001", 1234.0))])

    documents, raw, manifest = gcs.read_gcs_period(BUCKET, PERIOD, client=client)

    assert [d.load_ref for d in documents] == ["L-9001"]
    assert "Load Number: L-9001" in raw["load-1.txt"]
    assert manifest["read"][0]["sha256"] == hashlib.sha256(
        load_text("L-9001", 1234.0)).hexdigest()


def test_a_different_object_produces_a_different_close():
    """The audit's acceptance line, verbatim: same month, different object,
    different result. This is what 'the ingestion is real' means."""
    a = FakeClient([blob("load-1.txt", load_text("L-9001", 1000.0))])
    b = FakeClient([blob("load-1.txt", load_text("L-9001", 2500.0))])

    close_a = service._close(PERIOD, store=LocalStore(), **_mail(a))
    close_b = service._close(PERIOD, store=LocalStore(), **_mail(b))

    assert close_a["statements"]["revenue"] == 1000.0
    assert close_b["statements"]["revenue"] == 2500.0
    assert close_a["source"]["manifest"][0]["sha256"] !=         close_b["source"]["manifest"][0]["sha256"]


def _mail(client):
    documents, raw, manifest = gcs.read_gcs_period(BUCKET, PERIOD, client=client)
    return {"documents": documents, "raw": raw,
            "source": gcs.event_source(envelope(), PERIOD, manifest)}


def test_identical_bytes_under_two_names_are_one_artifact():
    """A copy of the remittance under a second name must not become a second
    remittance: it would double the allocation and invent a duplicate-charge
    finding on clean books."""
    data = load_text("L-9001", 1000.0)
    client = FakeClient([blob("load-1.txt", data), blob("load-1-copy.txt", data)])

    documents, _raw, manifest = gcs.read_gcs_period(BUCKET, PERIOD, client=client)

    assert len(documents) == 1
    assert len(manifest["read"]) == 1
    assert manifest["skipped"][0]["reason"].startswith("identical bytes")


def test_oversize_and_non_text_objects_are_skipped_and_named():
    client = FakeClient([
        blob("load-1.txt", load_text("L-9001", 1000.0)),
        blob("huge.txt", b"x" * (gcs.MAX_OBJECT_BYTES + 1)),
        blob("photo.jpg", b"\xff\xd8\xff"),
        blob("binary.txt", b"\xff\xfe\x00\x01"),
    ])

    documents, _raw, manifest = gcs.read_gcs_period(BUCKET, PERIOD, client=client)

    assert len(documents) == 1
    reasons = {s["object"].rsplit("/", 1)[-1]: s["reason"] for s in manifest["skipped"]}
    assert "cap" in reasons["huge.txt"]
    assert reasons["photo.jpg"] == "not text"
    assert reasons["binary.txt"] == "not utf-8"


# ── provenance ───────────────────────────────────────────────────────────────

def test_the_persisted_record_carries_the_source_and_its_hashes():
    client = FakeClient([blob("load-1.txt", load_text("L-9001", 1000.0))])
    store = LocalStore()

    payload = service._close(PERIOD, store=store, **_mail(client))

    src = payload["source"]
    assert src["mailbox"] == "gcs"
    assert src["bucket"] == BUCKET
    assert src["trigger_object"] == f"mail/{PERIOD}/load-1.txt"
    assert src["message_id"] == "m-1"
    assert src["manifest"][0]["sha256"]
    # And it is in the STORE, not only the response: the page's cold load
    # reads the store, and provenance that evaporates on reload proves nothing.
    assert store.load_close(service.COMPANY, PERIOD)["source"]["bucket"] == BUCKET


def test_a_bundled_run_says_it_is_the_bundled_sample():
    payload = service._close(PERIOD, store=LocalStore())

    assert payload["source"]["mailbox"] == "bundled-sample"
    assert payload["driver"] == "deterministic"


# ── the envelope helpers ─────────────────────────────────────────────────────

def test_the_bucket_and_dedupe_key_come_out_of_the_envelope():
    env = envelope(obj=f"mail/{PERIOD}/r.txt", generation="42")

    assert service._bucket_from_envelope(env) == BUCKET
    assert gcs.dedupe_key(env, PERIOD) == f"{PERIOD}#event-mail/{PERIOD}/r.txt@42"


def test_a_scheduler_event_with_no_object_dedupes_on_the_message_id():
    env = {"message": {"messageId": "sched-7", "attributes": {"period": PERIOD}}}

    assert gcs.dedupe_key(env, PERIOD) == f"{PERIOD}#event-msg-sched-7"


def test_an_envelope_with_neither_object_nor_message_id_has_no_key():
    assert gcs.dedupe_key({"message": {}}, PERIOD) is None


# ── idempotency at the route ────────────────────────────────────────────────

@pytest.fixture
def wired(monkeypatch):
    """The route's collaborators, faked at the seams the route actually uses."""
    store = LocalStore()
    runs: list[str] = []
    monkeypatch.setattr(service, "get_store", lambda: store)
    monkeypatch.setattr(service.auth, "verify_push_request",
                        lambda _h: type("V", (), {"allowed": True, "caller": "t",
                                                  "reason": "test"})())
    fake = FakeClient([blob("load-1.txt", load_text("L-9001", 1000.0), 111)])
    real_read = gcs.read_gcs_period

    def counted_read(bucket, period, client=None):
        runs.append(period)
        return real_read(bucket, period, client=fake)

    monkeypatch.setattr(service.gcs, "read_gcs_period", counted_read)
    return store, runs


def _post(env: dict):
    """Drive the async route without a server."""
    import asyncio

    class FakeRequest:
        headers = {"authorization": "Bearer t"}

        async def json(self):
            return env

    return asyncio.run(service.events(FakeRequest()))


def test_the_same_object_generation_closes_the_period_exactly_once(wired):
    store, runs = wired

    first = json.loads(_post(envelope(generation="111")).body)
    second = json.loads(_post(envelope(generation="111")).body)

    assert first["status"] == "closed"
    assert second["status"] == "duplicate"
    assert runs.count(PERIOD) == 1, "the duplicate delivery re-ran the close"


def test_a_new_generation_of_the_same_object_closes_again(wired):
    _store, runs = wired

    assert json.loads(_post(envelope(generation="111")).body)["status"] == "closed"
    assert json.loads(_post(envelope(generation="222")).body)["status"] == "closed"
    assert runs.count(PERIOD) == 2


def test_an_unreadable_bucket_is_acknowledged_and_named_not_retried_forever(
        wired, monkeypatch):
    def explode(bucket, period, client=None):
        raise RuntimeError("bucket gone")

    monkeypatch.setattr(service.gcs, "read_gcs_period", explode)

    response = _post(envelope(generation="333"))
    body = json.loads(response.body)

    assert response.status_code == 200, "a 500 here means redelivery until expiry"
    assert body["status"] == "error"
    assert "RuntimeError" in body["reason"]


def test_a_redelivery_during_a_running_close_is_told_to_come_back(wired):
    """503, not 200. A 200 acks work that has not finished; if the first
    attempt then dies with its instance, the event is gone and the month never
    closes. A 503 makes Pub/Sub retry until the marker says closed."""
    store, _runs = wired
    env = envelope(generation="777")
    key = gcs.dedupe_key(env, PERIOD)
    store.save_close(service.COMPANY, key, {"period": PERIOD, "status": "processing"})

    response = _post(env)

    assert response.status_code == 503
    assert json.loads(response.body)["status"] == "in-progress"


def test_the_blocking_close_runs_off_the_event_loop():
    """The outage of 2026-08-24, asserted structurally. `/events` is an async
    handler; a synchronous close inside it runs ON the loop, and a minutes-long
    agent close starves every request on the instance, health included. Cloud
    Run 504s them all and Pub/Sub answers with redeliveries. The handler must
    hand both blocking calls to the threadpool."""
    import inspect

    body = inspect.getsource(service.events)

    assert "run_in_threadpool" in body, "the close is back on the event loop"
    # A synchronous call reads `gcs.read_gcs_period(...)` or `_close(period`;
    # the threadpool form passes the callable as an argument instead. The
    # pattern is deliberately narrow so `load_close(`/`save_close(` (fast
    # store lookups) do not trip it.
    for call in ("gcs.read_gcs_period(", "_close(period"):
        direct = [line.strip() for line in body.splitlines()
                  if call in line and "run_in_threadpool" not in line]
        assert direct == [], f"called synchronously on the loop: {direct}"
