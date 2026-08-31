"""What happens to a month when the close CRASHES, and who is allowed to retry.

The idempotency work established that one object generation closes a period
once. It established nothing about the other half of at-least-once delivery:
the attempt that starts and does not finish.

`/events` claims a marker with `status: "processing"` and writes `closed` after
`_close` returns. Between those two lines there is a container. If it dies --
Cloud Run ends the request at 600s and the instance may keep running, it is
close raises -- the marker stays `processing` and nothing ever changes it. Every
redelivery reads `processing` and gets a 503, forever, and Pub/Sub retries a
month that CANNOT close until the message expires days later. Then the event is
gone. Nobody is told, and the marker is poisoned permanently: even re-uploading
the same object generation is refused.

That is the failure mode this file drives. It also drives the asymmetry Phase 1
opened and did not finish: `gcs.is_marker` was narrowed so a document called
`_READYish.txt` is INGESTED, and this route still decides the same name is the
batch-complete signal with a bare `startswith`. One object, read two ways.
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta

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
    """`gcs.is_marker` says `_READYish.txt` is a DOCUMENT and ingests it. This
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
    working = service._close
    broken = [True]

    def flaky(period, **kwargs):
        if broken[0]:
            raise RuntimeError("transient")
        return working(period, **kwargs)

    monkeypatch.setattr(service, "_close", flaky)
    post(env)
    broken[0] = False

    second = post(env)

    assert body(second)["status"] == "closed", (
        "the month is permanently unclosable after one crash")


def test_a_holder_that_died_with_its_instance_does_not_block_the_month_forever(
        wired):
    """Nothing runs when a container is killed: no except clause, no finally.
    The marker is left `processing` by a worker that no longer exists, and the
    only thing that distinguishes it from a live holder is how old it is.

    Cloud Run ends the request at 600s (`infra/main.tf`), so a claim older
    than the lease cannot still be working."""
    store, closes = wired
    env = load_event("503")
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
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
    fresh = datetime.now(UTC).isoformat()
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


def test_a_close_that_finishes_after_its_lease_was_stolen_does_not_overwrite(
        wired, monkeypatch):
    """The other end of the take-over, and the reason the success write is a
    compare-and-set too.

    A holder whose lease expired can still be alive -- it is unlikely, because
    Cloud Run ends the request at 600s and the lease is 900s, but the instance
    is not "cannot". If it is, a second worker has already retaken the claim,
    run the month and written `closed`. The original then finishes and, writing
    unconditionally, would put its own run back on top of the winner's.

    So the last write checks it still holds what it took."""
    store, _ = wired
    env = load_event("506")
    key = gcs.dedupe_key(env, PERIOD)
    working = service._close

    def close_and_get_robbed(period, **kwargs):
        result = working(period, **kwargs)
        # While this attempt was working, somebody else's lease take-over won.
        store.save_close(service.COMPANY, key,
                         {"period": PERIOD, "status": "closed", "attempt": 2,
                          "run_id": "the-other-workers-run"})
        return result

    monkeypatch.setattr(service, "_close", close_and_get_robbed)

    response = post(env)

    assert store.load_close(service.COMPANY, key)["run_id"] == "the-other-workers-run", (
        "the superseded attempt wrote over the worker that actually holds the claim")
    assert body(response)["status"] == "superseded"
    assert response.status_code == 200, "the work was done, just not by the holder"


# -- the store the route actually gets ---------------------------------------

def test_the_memory_backend_remembers_between_two_calls(monkeypatch):
    """`get_store()` built a NEW `LocalStore` on every call, so on the memory
    backend nothing the route wrote was ever read back by anything.

    Every marker write went into a throwaway and every marker read came out of
    a different empty one, which means the claim never held, the duplicate
    branch was unreachable, and one Pub/Sub message delivered twice ran the
    month twice. It looked correct because `claim` on an empty store always
    says yes.

    Firestore hid it -- that store is durable by construction, and the live
    service sets `GOOGLE_CLOUD_PROJECT` -- so the defect was invisible exactly
    where the tests run and the demo runs."""
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    from archon.adapters import store as store_mod

    store_mod.reset_local_store()
    first = store_mod.get_store()
    first.save_close("acme", "2026-07", {"status": "closed"})

    assert store_mod.get_store().load_close("acme", "2026-07") is not None, (
        "the second caller got a different store and saw nothing")
    assert store_mod.get_store() is first


def test_a_container_killed_over_and_over_eventually_dead_letters(wired, monkeypatch):
    """The attempt cap was only checked on the `failed` branch, so the one
    scenario it exists for did not reach it.

    A container that is KILLED -- OOM, instance reclaim, the 600s deadline --
    runs no except clause, so the marker it leaves says `processing`, not
    `failed`. The next delivery finds the lease expired and retakes. Killed
    again: `processing` again. The counter climbs and nothing ever consults it,
    so a close that reliably kills its container retries until the Pub/Sub
    message expires days later. That is exactly the poison event the dead
    letter was built to end, and it was the one case that could not reach it.
    """
    store, _ = wired
    env = load_event("601")
    key = gcs.dedupe_key(env, PERIOD)
    stale = (datetime.now(UTC) - timedelta(seconds=1000)).isoformat()
    # Three attempts have already been killed mid-close. Nothing wrote `failed`,
    # because nothing got the chance to.
    store.save_close(service.COMPANY, key,
                     {"period": PERIOD, "status": "processing", "attempt": 3,
                      "claimed_at": stale})

    response = post(env)

    assert body(response)["status"] == "dead-letter", (
        "a killed container is retried forever; the cap never sees it")
    assert response.status_code == 200, "a dead letter must be acked, not retried"


def test_a_failing_close_does_not_write_over_the_worker_that_holds_the_claim(
        wired, monkeypatch):
    """The success write was made a compare-and-set and the FAILURE write was
    left a bare one, sixteen lines apart, under a comment explaining why the
    first needed it.

    The reasoning is identical and the consequence is worse. A holder whose
    lease expired can still be alive; by the time it raises, the worker that
    took the claim over may have CLOSED the month. Writing `failed`
    unconditionally puts that back on top of `closed`, and the next redelivery
    reads `failed`, retakes, and closes a month that is already closed. Double
    execution, produced by the mechanism that exists to prevent it -- and it
    resets the attempt counter on the way past.
    """
    store, _ = wired
    env = load_event("602")
    key = gcs.dedupe_key(env, PERIOD)

    def die_after_being_robbed(period, **kwargs):
        store.save_close(service.COMPANY, key,
                         {"period": PERIOD, "status": "closed", "attempt": 2,
                          "run_id": "the-holder's-run"})
        raise RuntimeError("this attempt lost its claim and then fell over")

    monkeypatch.setattr(service, "_close", die_after_being_robbed)

    response = post(env)

    held = store.load_close(service.COMPANY, key)
    assert held["status"] == "closed", (
        "a superseded failure overwrote the holder's closed month")
    assert held["run_id"] == "the-holder's-run"
    assert response.status_code == 200, "the holder owns it now; do not retry this one"


def test_the_dedupe_key_is_a_document_id_not_a_path():
    """The idempotency marker is the safety-critical record in this build, and
    its Firestore document id was built from the raw GCS object name.

    A Firestore document path alternates collection and document, so an id
    carrying slashes is not an id: `closes/{company}::{period}#event-mail/
    2026-07/_READY@gen` is really a document in a subcollection two levels
    down. That happens to be a legal path with an even number of slashes, so
    every claim so far has quietly worked while living somewhere other than
    where `store.py` says it does.

    Add one more slash -- an object in a subfolder, which is exactly what the
    Cloud Console's "create folder" button invites, and which this intake now
    tolerates rather than blocks -- and the same expression resolves to a
    COLLECTION. Firestore raises, the events route 500s, and the month cannot
    be claimed at all.
    """
    for obj in ("mail/2026-07/_READY", "mail/2026-07/scans/_READY", "_READY"):
        env = {"message": {"attributes": {"objectId": obj, "objectGeneration": "9"}}}

        key = gcs.dedupe_key(env, PERIOD)

        assert "/" not in key, f"{obj!r} produced a key Firestore reads as a path: {key!r}"


@pytest.mark.parametrize("period", ["2026-13", "2026-00", "2026-1", "2026-7", "not-a-month"])
def test_a_month_that_is_not_a_month_never_reaches_a_claim(wired, period):
    """`2026-13` is shaped like a period and is not one.

    The attribute was returned verbatim, so such an event claimed a marker, ran
    `_close`, failed on the filesystem, recorded an attempt and answered 503 --
    asking Pub/Sub to redeliver something that can never succeed. Three
    attempts and a dead letter, spent on a typo.

    It is ignored at the parser now, before anything is claimed.
    """
    store, closes = wired
    env = {"message": {"attributes": {"period": period}, "messageId": "m1"}}

    response = post(env)

    assert body(response)["status"] == "ignored"
    assert response.status_code == 200, "a permanently invalid event must not be retried"
    assert closes == [], "a close was attempted for a period that cannot exist"


def test_a_real_month_still_gets_through(wired):
    """The guard has to let the product work."""
    store, closes = wired

    response = post({"message": {"attributes": {"period": PERIOD}, "messageId": "m2"}})

    assert body(response)["status"] == "closed"
    assert closes == [PERIOD]


def test_a_stale_worker_writes_no_business_record_and_sends_nothing(wired, monkeypatch):
    """The fence, tested on what it is for: the RECORDS, not the marker.

    The lease and the compare-and-set settled who may do the work. They did not
    stop a worker that lost the claim mid-close from doing all of it -- the
    books, the run, the letters and the owner's digest were written before
    anything checked again, and the loser found out at the final marker write,
    when the damage was durable. Cloud Run's documentation says a container may
    keep running after its request times out, so this worker is not a
    hypothesis.

    Two attempts. The second takes the claim over while the first is working.
    The first must then write nothing and send nothing, and say so.
    """
    store, _ = wired
    env = load_event("901")
    key = gcs.dedupe_key(env, PERIOD)
    sent: list = []
    working = service._close

    class CountingDelivery:
        def send(self, message):
            sent.append(message)
            return {"status": "delivered"}

    monkeypatch.setattr(service.delivery_mod, "get_deliverer", lambda: CountingDelivery())

    def robbed_mid_close(period, **kwargs):
        # Somebody else's take-over lands while this attempt is inside the close.
        store.save_close(service.COMPANY, key,
                         {"period": PERIOD, "status": "processing", "attempt": 2,
                          "claimed_at": datetime.now(UTC).isoformat()})
        return working(period, **kwargs)

    monkeypatch.setattr(service, "_close", robbed_mid_close)

    response = post(env)

    assert body(response)["status"] == "superseded"
    assert response.status_code == 200, "the holder owns it; do not ask for a redelivery"
    assert sent == [], "a superseded worker delivered to the owner"
    assert store.load_close(service.COMPANY, PERIOD) is None, (
        "a superseded worker wrote the month's books")
    held = store.load_close(service.COMPANY, key)
    assert held["attempt"] == 2, "the loser overwrote the holder's marker"
    assert held["status"] == "processing", "the loser marked a month it does not own"


def test_the_fence_is_actually_consulted(wired):
    """A guard nobody can prove ran is a comment.

    This asserts the fence was asked at least once per business write on an
    ordinary close, so a refactor that quietly stops consulting it fails here
    rather than in production a month later.
    """
    from archon.adapters.store import FencedStore

    store, _ = wired
    store.claim(service.COMPANY, "k", {"period": PERIOD, "attempt": 1})
    fence = FencedStore(store, service.COMPANY, "k", 1)

    fence.put_document("a.txt", "x")
    fence.save_run({"run_id": "r"})
    fence.save_close(service.COMPANY, PERIOD, {"status": "closed"})

    assert fence.checks == 3


# -- the decision itself, without a store, a bucket or a route ---------------

def verdict(marker: dict, age_seconds: int = 0) -> str:
    now = datetime.now(UTC)
    if "claimed_at" not in marker and marker.get("status") == "processing":
        marker = dict(marker, claimed_at=(
            now - timedelta(seconds=age_seconds)).isoformat())
    return service.claim_verdict(marker, now, lease=900, max_attempts=3)


@pytest.mark.parametrize("marker, age, expected", [
    ({"status": "closed"}, 0, "duplicate"),
    ({"status": "dead-letter"}, 0, "dead-letter"),
    ({"status": "processing"}, 10, "wait"),
    ({"status": "processing"}, 899, "wait"),
    ({"status": "processing"}, 901, "retake"),
    ({"status": "failed", "attempt": 1}, 0, "retake"),
    ({"status": "failed", "attempt": 2}, 0, "retake"),
    ({"status": "failed", "attempt": 3}, 0, "dead-letter"),
    ({"status": "failed", "attempt": 9}, 0, "dead-letter"),
    # Written before the lease existed. Expired, not immortal.
    ({"status": "processing", "claimed_at": None}, 0, "retake"),
    ({"status": "processing", "claimed_at": "not a date"}, 0, "retake"),
    # Nothing recognisable is not a reason to refuse a month forever.
    ({}, 0, "retake"),
    ({"status": "something new"}, 0, "retake"),
])
def test_the_claim_decision_table(marker, age, expected):
    """The rule that decides whether a month can ever close again, checked
    directly. Every route test above exercises one row of this; the table is
    here so the boundary cases are stated rather than implied -- in particular
    that the lease boundary is 900s and that attempt 3 of 3 is the last one."""
    assert verdict(marker, age) == expected
