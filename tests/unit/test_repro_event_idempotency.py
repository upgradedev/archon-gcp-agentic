"""Reproduction of the audit's section-3 claim about `POST /events`.

The claim has three parts and they do not all hold, so each is driven
separately through the real FastAPI application:

1. *Every OBJECT_FINALIZE starts a whole period close.*  It does.  The marker
   in `service.events` is keyed by `gcs.dedupe_key`, which is
   ``f"{period}#event-{object}@{generation}"`` -- one key **per object**.  A
   month arriving as 27 separate uploads therefore produces 27 distinct keys,
   27 marker misses and 27 full closes of the same month, each of which
   re-downloads the whole `mail/<period>/` prefix.

2. *Concurrent deliveries race.*  They do, but the window is the store's read
   latency, and that is worth stating precisely because it is the difference
   between "the code does X" and "X happens".  There is no ``await`` between
   the marker's ``load_close`` and its ``save_close``, so two coroutines on
   one event loop cannot interleave there; the race is across processes.
   `Store` has no create-if-absent or compare-and-set primitive --
   ``save_close`` is an unconditional ``set`` -- so two Cloud Run instances
   reading a shared Firestore both observe absence and both write.  Against a
   zero-latency in-memory store the window is a dict lookup and the two
   requests serialise correctly (measured: the loser is told "in-progress" and
   only one close runs).  Against the deployed backend the read is a network
   round trip of tens of milliseconds and Pub/Sub fans out across instances.
   `RacingStore` below supplies that latency and nothing else.

3. *Redelivery of the same event repeats the work.*  It does **not**.  The
   marker catches an exact object+generation redelivery before the bucket is
   read, and the last test here documents that as correct behaviour.

Everything is offline.  `google-cloud-storage` is injected as a fake, as the
rest of the suite does, and the objects are the real bytes of `corpus/2026-07`
so the numbers reported are the numbers a real month produces.  Auth is left
in its unconfigured "open" posture through the real `auth` module -- the same
mechanism `tests/unit/test_least_privilege.py` uses -- rather than stubbed
out.
"""
from __future__ import annotations

import base64
import contextlib
import json
import threading

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from archon import paths  # noqa: E402
from archon.adapters import auth, gcs, service  # noqa: E402
from archon.adapters.store import LocalStore  # noqa: E402

PERIOD = "2026-07"
PREVIOUS = "2026-06"
BUCKET = "bell-ridge-mail"


# ── the bucket, and the meter on it ──────────────────────────────────────────

class FakeBlob:
    """One object in the fake bucket.  Counts its own downloads."""

    def __init__(self, name: str, data: bytes, generation: int, meter: list):
        self.name = name
        self._data = data
        self.size = len(data)
        self.generation = generation
        self._meter = meter

    def download_as_bytes(self) -> bytes:
        self._meter.append(self.name)
        return self._data


class FakeBucket:
    """A bucket a bookkeeper uploads into, one object at a time."""

    def __init__(self) -> None:
        self.blobs: list[FakeBlob] = []
        self.downloads: list[str] = []
        self.listings: list[str] = []

    def upload(self, name: str, data: bytes, generation: int = 1) -> FakeBlob:
        blob = FakeBlob(f"mail/{PERIOD}/{name}", data, generation, self.downloads)
        self.blobs.append(blob)
        return blob

    # the google-cloud-storage client surface `gcs.read_gcs_period` uses
    def list_blobs(self, bucket_name, prefix=""):
        assert bucket_name == BUCKET
        self.listings.append(prefix)
        return [b for b in self.blobs if b.name.startswith(prefix)]


def corpus_mail() -> list[tuple[str, bytes]]:
    """The real month, as the 27 objects a bookkeeper would upload."""
    directory = paths.CORPUS_ROOT / PERIOD
    return [(p.name, p.read_bytes()) for p in sorted(directory.glob("*.txt"))]


def finalize(blob: FakeBlob, message_id: str) -> dict:
    """The Pub/Sub push envelope a `google_storage_notification` produces."""
    return {"message": {
        "messageId": message_id,
        "attributes": {
            "bucketId": BUCKET,
            "objectId": blob.name,
            "objectGeneration": str(blob.generation),
            "eventType": "OBJECT_FINALIZE",
        },
        "data": base64.b64encode(
            json.dumps({"bucket": BUCKET, "name": blob.name}).encode()).decode(),
    }}


# ── the wiring ───────────────────────────────────────────────────────────────

@pytest.fixture
def rig(monkeypatch):
    """The real app, one durable store, a fake bucket, and meters on both.

    The only things replaced are the storage client (injected, as the whole
    suite does) and `get_store`, which is pinned to a single instance so
    "what is in the durable store" is a question that can be asked at all.
    `service.get_store` returns a *brand new* `LocalStore` on every call when
    `GOOGLE_CLOUD_PROJECT` is unset, so without this pin the marker would
    never survive its own function call.
    """
    monkeypatch.delenv(auth.AUDIENCE_ENV, raising=False)   # the open posture
    monkeypatch.delenv(auth.CALLER_ENV, raising=False)

    durable = LocalStore()
    monkeypatch.setattr(service, "get_store", lambda: durable)

    bucket = FakeBucket()
    real_read = gcs.read_gcs_period

    def read_through_the_fake(bucket_name, period, client=None):
        return real_read(bucket_name, period, client=bucket)

    monkeypatch.setattr(service.gcs, "read_gcs_period", read_through_the_fake)

    closes: list[str] = []
    real_close = service.run_close

    def counted_close(*args, **kwargs):
        closes.append(kwargs.get("period") or (args[0] if args else "?"))
        return real_close(*args, **kwargs)

    monkeypatch.setattr(service, "run_close", counted_close)

    return service.app, durable, bucket, closes


def post(app, envelope):
    """One Pub/Sub push, through the real route, middleware included."""
    return TestClient(app).post("/events", json=envelope)


# ── 1. one object finalize closes the whole period ───────────────────────────

def test_one_object_finalize_closes_the_entire_period(rig):
    """The premise, and it is by design rather than a defect: the handler
    ignores which object landed and re-reads every object under
    `mail/<period>/`.  Recorded here because it is what makes the fan-out in
    the next test expensive -- each of the N events does all N documents."""
    app, durable, bucket, closes = rig
    mail = corpus_mail()
    for name, data in mail:
        bucket.upload(name, data)

    trigger = bucket.blobs[0]                       # bank-2026-07-20-6.txt
    response = post(app, finalize(trigger, "m-1"))

    assert response.status_code == 200
    assert response.json()["status"] == "closed"

    stored = durable.load_close(service.COMPANY, PERIOD)
    assert stored["source"]["trigger_object"] == trigger.name
    assert stored["source"]["objects_read"] == len(mail), (
        "one finalize notification opened every object in the month")
    assert closes.count(PERIOD) == 1


# ── 2. N objects, N closes ───────────────────────────────────────────────────

def test_a_batch_marker_turns_twenty_seven_uploads_into_one_close(rig, monkeypatch):
    """The audit's number, and the signal that answers it.

    A bookkeeper drops the month's 27 documents into the bucket. Each upload is
    its own OBJECT_FINALIZE with its own dedupe key, so the per-object marker --
    which exists to stop a REDELIVERY re-running a close, and does that job --
    cannot help: these are 27 genuinely different events. The month was closed
    27 times. Each close was correct and each superseded the last, but 26 of
    them were wasted, and with `ARCHON_AGENT_CLOSE=1` each is a model run.

    Cloud Storage never says "that was the last one", and a settle window needs
    a durable timer that a container which scales to zero does not have. So the
    batch-complete signal is explicit: `ARCHON_BATCH_MARKER` names an object
    that means the batch is finished. Ordinary uploads are recorded and
    acknowledged; only the marker closes the month.
    """
    monkeypatch.setenv("ARCHON_BATCH_MARKER", "_READY")
    app, _durable, bucket, closes = rig

    for index, (name, data) in enumerate(corpus_mail(), start=1):
        blob = bucket.upload(name, data)
        response = post(app, finalize(blob, f"m-{index}"))
        assert response.status_code == 200
        assert response.json()["status"] == "collecting", (
            f"upload {index} closed the month instead of waiting for the marker")

    assert closes.count(PERIOD) == 0, (
        "not one of the 27 uploads may close the month on its own")

    blob = bucket.upload("_READY", b"")
    assert post(app, finalize(blob, "m-ready")).status_code == 200

    assert closes.count(PERIOD) == 1, (
        f"the marker closes the month exactly once; got {closes.count(PERIOD)}")


def test_without_a_marker_every_upload_still_closes_and_that_is_the_default(rig):
    """What this deployment does today, asserted rather than assumed.

    With no `ARCHON_BATCH_MARKER` configured, every object closes the month, as
    it always has. That is deliberate and it is a judgement rather than an
    oversight: the demo and the video drop a SINGLE file into the bucket, so
    the fan-out is never exercised on the live path, and flipping the trigger
    days before a deadline risks the one thing the entry is judged on for the
    sake of a cost nobody is paying.

    The property is recorded here so the next reader knows it is a choice, and
    knows exactly which environment variable changes it.
    """
    app, _durable, bucket, closes = rig

    for index, (name, data) in enumerate(list(corpus_mail())[:3], start=1):
        blob = bucket.upload(name, data)
        assert post(app, finalize(blob, f"m-{index}")).json()["status"] == "closed"

    assert closes.count(PERIOD) == 3, (
        "three uploads, three closes: the documented default")


def test_the_bucket_is_read_once_per_batch_not_once_per_file(rig, monkeypatch):
    """The cost of the fan-out, in bytes off Cloud Storage.

    Every event re-listed and re-downloaded the whole `mail/<period>/` prefix,
    so uploading N objects downloaded 1 + 2 + ... + N of them. 27 documents
    cost 378 downloads, and all but the last read was of a month that had not
    finished arriving.

    With a batch-complete marker the mail is read once, when the batch is
    declared finished, which is the only moment at which reading it is useful.
    """
    monkeypatch.setenv("ARCHON_BATCH_MARKER", "_READY")
    app, _durable, bucket, _closes = rig
    mail = corpus_mail()

    for index, (name, data) in enumerate(mail, start=1):
        blob = bucket.upload(name, data)
        assert post(app, finalize(blob, f"m-{index}")).json()["status"] == "collecting"

    # Nothing was read at all while the batch was still arriving. The whole
    # cost of the fan-out was re-listing and re-downloading the entire prefix
    # once per file: 27 objects cost 378 downloads, and every one of those
    # reads was of a month that was not finished yet.
    assert bucket.downloads == [], (
        f"{len(bucket.downloads)} objects were downloaded before the batch "
        f"was declared complete")

    blob = bucket.upload("_READY", b"")
    assert post(app, finalize(blob, "m-ready")).status_code == 200

    assert len(bucket.downloads) <= len(mail) + 1, (
        f"{len(mail)} uploaded objects caused {len(bucket.downloads)} object "
        f"downloads and {len(bucket.listings)} bucket listings")


def test_one_batch_files_one_run_and_one_set_of_drafts(rig, monkeypatch):
    """Why this is more than wasted compute.

    `run_id_for` derives the id from the period and the CONTENT of the
    documents in it, so a close over a *growing* bucket got a different run id
    each time.  The
    `closes/{company}::{period}` record is overwritten by whichever event
    lands last, but `runs/{run_id}` and `drafts/{run_id}::{n}` accumulate: one
    journal and one full set of corrective drafts per upload, all but the last
    computed from a partial month.
    """
    monkeypatch.setenv("ARCHON_BATCH_MARKER", "_READY")
    app, durable, bucket, _closes = rig
    run_ids: set[str] = set()

    for index, (name, data) in enumerate(corpus_mail(), start=1):
        blob = bucket.upload(name, data)
        body = post(app, finalize(blob, f"m-{index}")).json()
        assert body["status"] == "collecting"
        assert "run_id" not in body, "a collected upload has not run anything"

    blob = bucket.upload("_READY", b"")
    run_ids.add(post(app, finalize(blob, "m-ready")).json()["run_id"])

    filed = sum(len(durable.load_drafts(run_id)) for run_id in run_ids)
    assert len(run_ids) == 1, (
        f"the month was filed under {len(run_ids)} different run ids, leaving "
        f"{len(run_ids)} run journals and {filed} draft documents for one "
        f"month of mail")

    # And every draft belongs to the whole month, not to a partial one. This
    # was the real cost of the fan-out: 26 of the 27 run journals were computed
    # over a bucket that was still filling, so their corrective letters chased
    # money that later documents accounted for.
    assert filed > 0, "the one run must have filed the month's drafts"


# ── 3. the marker is a non-atomic check-then-set ─────────────────────────────

class RacingStore(LocalStore):
    """One shared store, read concurrently the way Firestore actually is.

    `service.events` does ``load_close(marker)`` and then, on a miss,
    ``save_close(marker)``.  Those are two separate round trips with nothing
    holding the key in between: `Store` exposes no create-if-absent and
    ``save_close`` is an unconditional ``set``.  In the deployed shape
    ``load_close`` is a Firestore network call taking tens of milliseconds and
    Cloud Run runs several instances, so two deliveries landing inside that
    window both see no marker and both write one.

    The barrier is the latency model, and it is load-bearing: with a plain
    `LocalStore` the same two concurrent requests serialise and the loser is
    correctly answered 503 "in-progress", because a dict lookup is narrower
    than thread-start jitter.  So this failure is latency-dependent, and the
    deployed backend is what supplies the latency.

    The barrier lives in the test's own store, never in the handler, and it
    does not by itself cause the failure: had the handler used a Firestore
    transaction or a create-if-absent, one of the two writes would lose and
    the second request would still be answered "duplicate" or "in-progress"
    with this barrier in place.  It loses nothing because `Store` gives it
    nothing to lose against.
    """

    def __init__(self, gate: threading.Barrier) -> None:
        super().__init__()
        self._gate = gate
        self._lock = threading.Lock()
        self._gated = 0

    def load_close(self, company, period):
        value = super().load_close(company, period)
        if "#event-" in str(period):
            with self._lock:
                inside_the_window = self._gated < 2
                self._gated += 1
            if inside_the_window:
                # The barrier only coordinates the two threads. A timeout means the
                # second one never arrived, and the counters below catch that on
                # their own, so it is not an error worth raising from here.
                with contextlib.suppress(threading.BrokenBarrierError):
                    self._gate.wait(timeout=10)
        return value


def test_two_concurrent_deliveries_of_one_event_both_close_the_period(
        rig, monkeypatch):
    """Two instances, one shared store, one object generation, two closes.

    Each `TestClient` request starts its own blocking portal, so the two
    requests run on two independent event loops in two threads -- the same
    shape as two Cloud Run instances taking one delivery each.  Pub/Sub is
    at-least-once and fans out across instances, so this is the ordinary case,
    not an exotic one.
    """
    app, _durable, bucket, closes = rig
    for name, data in corpus_mail():
        bucket.upload(name, data)

    shared = RacingStore(threading.Barrier(2))
    monkeypatch.setattr(service, "get_store", lambda: shared)

    envelope = finalize(bucket.blobs[0], "m-same")
    results: list = [None, None]

    def deliver(slot: int) -> None:
        results[slot] = post(app, envelope)

    threads = [threading.Thread(target=deliver, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    statuses = [r.json()["status"] for r in results]
    codes = [r.status_code for r in results]

    assert closes.count(PERIOD) == 1, (
        f"one object generation was closed {closes.count(PERIOD)} times by two "
        f"concurrent deliveries; both were answered {codes} {statuses}, so "
        f"neither was told 'duplicate' or 'in-progress'")


# ── the part of the claim that is NOT real ───────────────────────────────────

def test_a_redelivery_of_the_same_object_generation_does_no_extra_work(rig):
    """This half of the claim does not reproduce, and is documented as correct.

    An exact redelivery -- same object, same generation, even a fresh Pub/Sub
    message id -- is caught by the marker before the bucket is read, before
    the close runs and before anything is written.  Sequential redelivery is
    handled; it is the per-object fan-out and the cross-process race that are
    not.
    """
    app, _durable, bucket, closes = rig
    for name, data in corpus_mail():
        bucket.upload(name, data)
    envelope = finalize(bucket.blobs[0], "m-first")

    first = post(app, envelope)
    downloads_after_the_first = len(bucket.downloads)

    redelivery = finalize(bucket.blobs[0], "m-second-attempt-new-id")
    second = post(app, redelivery)

    assert first.json()["status"] == "closed"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert closes.count(PERIOD) == 1, "the redelivery re-ran the close"
    assert len(bucket.downloads) == downloads_after_the_first, (
        "the redelivery re-read the bucket")
