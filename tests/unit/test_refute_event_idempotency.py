"""An independent attempt to REFUTE the /events idempotency reproduction.

Everything here is driven through the real FastAPI application, the real
`auth` module in its unconfigured posture, the real `gcs.read_gcs_period`
(with the storage client injected, which is the seam that module documents),
and the real `run_close`.  Nothing internal to `service` is stubbed except
`get_store`, which is pinned to ONE instance because that is what the deployed
shape has -- `FirestoreStore` is a fresh object per call pointing at one
database -- whereas an unpinned `get_store()` hands out a brand new
`LocalStore` on every call and no marker could survive its own function call.

Deliberately NOT patched, unlike the reproduction under test:

* `service.run_close` is left alone.  The fan-out is counted from what the
  durable store actually ends up holding (run journals, drafts, markers) and
  from what the bucket was actually asked for (listings, downloads), which is
  what a GCS bill and a Firestore console would show.
* The prior-month amplifier is counted through `service.read_period`, the
  corpus mailbox reader.  On the event path the documents come from GCS, so
  `_close` never calls it; every call is `_previous_statements` re-closing the
  month before.
* The race test's store has NO barrier and no cross-thread coordination of any
  kind.  It has latency and nothing else.
"""
from __future__ import annotations

import base64
import json
import threading
import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from archon import paths  # noqa: E402
from archon.adapters import auth, gcs, service  # noqa: E402
from archon.adapters.store import LocalStore, close_key  # noqa: E402
from archon.runtime.close import run_id_for  # noqa: E402

PERIOD = "2026-07"
PREVIOUS = "2026-06"
BUCKET = "bell-ridge-mail"

#: Captured once, at import, so a second rig in the same test wraps the real
#: function and not the first rig's wrapper.  (Re-reading them inside the
#: helper silently made the second rig read the first rig's bucket.)
REAL_READ_GCS_PERIOD = gcs.read_gcs_period
REAL_READ_PERIOD = service.read_period


# ── a bucket that meters itself ──────────────────────────────────────────────

class FakeBlob:
    def __init__(self, name, data, generation, meter):
        self.name = name
        self._data = data
        self.size = len(data)
        self.generation = generation
        self._meter = meter

    def download_as_bytes(self):
        self._meter.append(self.name)
        return self._data


class FakeBucket:
    """The `list_blobs` / `download_as_bytes` surface `read_gcs_period` uses."""

    def __init__(self):
        self.blobs = []
        self.downloads = []
        self.listings = []

    def upload(self, period, name, data, generation=1):
        blob = FakeBlob(f"mail/{period}/{name}", data, generation, self.downloads)
        self.blobs.append(blob)
        return blob

    def list_blobs(self, bucket_name, prefix=""):
        assert bucket_name == BUCKET
        self.listings.append(prefix)
        return [b for b in self.blobs if b.name.startswith(prefix)]


def mail_for(period):
    directory = paths.CORPUS_ROOT / period
    return [(p.name, p.read_bytes()) for p in sorted(directory.glob("*.txt"))]


def finalize(blob, message_id):
    """Exactly what a `google_storage_notification` OBJECT_FINALIZE push is."""
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


class Rig:
    def __init__(self, store, bucket, prior_month_closes):
        self.store = store
        self.bucket = bucket
        self.prior_month_closes = prior_month_closes

    def deliver(self, blob, message_id):
        return TestClient(service.app).post("/events", json=finalize(blob, message_id))

    def run_ids(self):
        return set(self.store._runs)

    def markers(self):
        return [k for k in self.store._closes if "#event-" in k]


def build_rig(monkeypatch, store=None):
    """The real app, one durable store, one fake bucket, meters on both."""
    monkeypatch.delenv(auth.AUDIENCE_ENV, raising=False)
    monkeypatch.delenv(auth.CALLER_ENV, raising=False)
    monkeypatch.delenv("ARCHON_SMTP_HOST", raising=False)

    durable = store if store is not None else LocalStore()
    monkeypatch.setattr(service, "get_store", lambda: durable)

    bucket = FakeBucket()
    monkeypatch.setattr(
        service.gcs, "read_gcs_period",
        lambda bucket_name, period, client=None: REAL_READ_GCS_PERIOD(
            bucket_name, period, client=bucket))

    prior = []

    def counted_read_period(period):
        prior.append(period)
        return REAL_READ_PERIOD(period)

    monkeypatch.setattr(service, "read_period", counted_read_period)
    return Rig(durable, bucket, prior)


# ── 1. the fan-out, counted from the store and the bucket ────────────────────

def test_a_month_uploaded_one_file_at_a_time_is_closed_once_per_file(monkeypatch):
    """27 OBJECT_FINALIZE pushes, nothing else, measured on the outputs.

    No counter is wrapped around `run_close`.  The evidence is what the durable
    store holds afterwards and what the bucket was asked for, which is what a
    Firestore console and a GCS bill would show.
    """
    rig = build_rig(monkeypatch)
    mail = mail_for(PERIOD)

    for index, (name, data) in enumerate(mail, start=1):
        blob = rig.bucket.upload(PERIOD, name, data)
        response = rig.deliver(blob, f"m-{index}")
        assert response.status_code == 200
        assert response.json()["status"] == "closed"

    drafts = sum(len(rig.store.load_drafts(rid)) for rid in rig.run_ids())
    assert len(rig.run_ids()) == 1, (
        f"one month of mail, uploaded as {len(mail)} objects, was closed under "
        f"{len(rig.run_ids())} distinct run ids -- {len(rig.run_ids())} run "
        f"journals, {drafts} filed drafts, {len(rig.markers())} dedupe markers, "
        f"{len(rig.bucket.listings)} bucket listings and "
        f"{len(rig.bucket.downloads)} object downloads, and "
        f"{rig.prior_month_closes.count(PREVIOUS)} extra closes of {PREVIOUS} "
        f"on top")


# ── 2. severity: the books converge; only the cost fans out ──────────────────

def test_the_month_that_ends_up_filed_is_the_same_month_either_way(monkeypatch):
    """The correction the audit's framing needs.

    Whatever the fan-out costs, the record at `closes/{company}::{period}` after
    the 27th upload is byte-for-byte the record a single close over the finished
    bucket produces: same run id, same statements, same trial balance, same
    manifest.  Nothing is corrupted, double-posted or left half-written -- the
    waste is compute and journal litter, not wrong books.
    """
    incremental = build_rig(monkeypatch)
    for index, (name, data) in enumerate(mail_for(PERIOD), start=1):
        blob = incremental.bucket.upload(PERIOD, name, data)
        assert incremental.deliver(blob, f"m-{index}").status_code == 200
    after_the_drip = incremental.store.load_close(service.COMPANY, PERIOD)

    once = build_rig(monkeypatch)
    for name, data in mail_for(PERIOD):
        blob = once.bucket.upload(PERIOD, name, data)
    assert once.deliver(once.bucket.blobs[-1], "m-only").status_code == 200
    after_one_close = once.store.load_close(service.COMPANY, PERIOD)

    # Everything the books consist of.  `journal` and `digest` carry wall-clock
    # timestamps and `source` names which object triggered it, so those three
    # are the only keys excluded.
    booked = [k for k in after_one_close
              if k not in {"journal", "digest", "source", "receipt"}]
    assert booked                                   # the comparison is not empty
    for key in booked:
        assert after_the_drip[key] == after_one_close[key], (
            f"the dripped month and the single close disagree on {key!r}")
    assert after_the_drip["run_id"] == after_one_close["run_id"]
    assert after_the_drip["source"]["objects_read"] == 27
    assert after_one_close["source"]["objects_read"] == 27
    # And the one close cost one listing and one download per object.
    assert len(once.bucket.downloads) == 27
    assert len(once.bucket.listings) == 1


def test_nothing_is_emailed_by_any_of_those_closes_as_deployed(monkeypatch):
    """"Composes the owner a month-end digest" is true; "mails it" is not.

    `infra/main.tf` sets no `ARCHON_SMTP_HOST`, so `get_deliverer()` returns
    `FiledDelivery` and every one of those digests is composed and filed with
    nothing leaving the machine.  The repeated close is expensive, not noisy in
    the owner's inbox, unless SMTP is configured -- which the deployed
    terraform does not do.
    """
    rig = build_rig(monkeypatch)
    for name, data in mail_for(PERIOD):
        rig.bucket.upload(PERIOD, name, data)
    assert rig.deliver(rig.bucket.blobs[-1], "m-1").status_code == 200

    receipt = rig.store.load_close(service.COMPANY, PERIOD)["receipt"]
    assert receipt["channel"] == "filed"
    assert receipt["delivered"] is False
    assert "nothing left this machine" in receipt["detail"]


def test_the_prior_month_amplifier_stops_once_june_is_closed_durably(monkeypatch):
    """The "54 run_close calls" number is conditional, not structural.

    `_previous_statements` re-closes the prior month into a throwaway
    `LocalStore()` only while the prior month is missing from the durable store.
    Close June through the same trigger first -- which is what a bookkeeper
    working through the year actually does -- and every later July event finds
    it and re-closes nothing.  The doubling is an artefact of never having
    closed June, not a property of the handler.
    """
    rig = build_rig(monkeypatch)

    # June arrives and closes, through the same route.
    for index, (name, data) in enumerate(mail_for(PREVIOUS), start=1):
        blob = rig.bucket.upload(PREVIOUS, name, data)
        assert rig.deliver(blob, f"jun-{index}").status_code == 200
    assert rig.store.load_close(service.COMPANY, PREVIOUS) is not None

    before = rig.prior_month_closes.count(PREVIOUS)
    for index, (name, data) in enumerate(mail_for(PERIOD), start=1):
        blob = rig.bucket.upload(PERIOD, name, data)
        assert rig.deliver(blob, f"jul-{index}").status_code == 200

    assert rig.prior_month_closes.count(PREVIOUS) == before, (
        "with June in the durable store, no July event re-closed it")


# ── 3. the race, with latency and nothing else ───────────────────────────────

class LaggyStore(LocalStore):
    """A store whose round trips cost what a Firestore round trip costs.

    No barrier, no shared counter, no cross-thread coordination.  The value is
    read at the moment the call is made and handed back one network hop later,
    which is what a strongly-consistent Firestore read is.  30 ms is the
    conservative end of a same-region Firestore round trip.
    """

    def __init__(self, latency=0.030):
        super().__init__()
        self.latency = latency

    def load_close(self, company, period):
        value = super().load_close(company, period)
        time.sleep(self.latency)
        return value

    def save_close(self, company, period, payload):
        time.sleep(self.latency)
        return super().save_close(company, period, payload)


def test_two_simultaneous_deliveries_of_one_event_both_close_it(monkeypatch):
    """Two Cloud Run instances, one Firestore, one duplicated Pub/Sub message.

    The only thing the test coordinates is the *arrival* of the two deliveries,
    which is what "concurrent" means; the store itself knows nothing about
    threads.  `service.events` does `load_close(marker)` and then, on a miss,
    `save_close(marker)`, and `Store` has no create-if-absent and no
    compare-and-set, so both reads land in the gap and both write.
    """
    shared = LaggyStore()
    rig = build_rig(monkeypatch, store=shared)
    for name, data in mail_for(PERIOD):
        rig.bucket.upload(PERIOD, name, data)

    envelope = finalize(rig.bucket.blobs[0], "m-duplicated-delivery")
    gate = threading.Barrier(2)
    results = [None, None]

    def deliver(slot):
        with TestClient(service.app) as client:       # portal up before the gate
            gate.wait(timeout=30)
            results[slot] = client.post("/events", json=envelope)

    threads = [threading.Thread(target=deliver, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    statuses = [r.json()["status"] for r in results]
    marker = shared.load_close(service.COMPANY,
                               gcs.dedupe_key(envelope, PERIOD))
    assert statuses.count("closed") == 1, (
        f"one object generation was answered {statuses} by two simultaneous "
        f"deliveries, so both ran the whole close; the bucket was listed "
        f"{len(rig.bucket.listings)} times and {len(rig.bucket.downloads)} "
        f"objects were downloaded, and the marker ends up as "
        f"{marker.get('status') if marker else None}")


def test_the_two_racing_closes_agree_with_each_other(monkeypatch):
    """Severity again: the duplicate close is idempotent, not divergent.

    `run_id_for` derives the id from the period and the documents, both
    deliveries read the same bucket, so both produce the same run id and write
    the same record over the same key.  The race costs a second close (a Gemini
    agent run in the deployed configuration, where `agent_close` defaults to
    "1"); it does not fork the books.
    """
    shared = LaggyStore()
    rig = build_rig(monkeypatch, store=shared)
    for name, data in mail_for(PERIOD):
        rig.bucket.upload(PERIOD, name, data)

    envelope = finalize(rig.bucket.blobs[0], "m-duplicated-delivery")
    gate = threading.Barrier(2)
    results = [None, None]

    def deliver(slot):
        with TestClient(service.app) as client:
            gate.wait(timeout=30)
            results[slot] = client.post("/events", json=envelope)

    threads = [threading.Thread(target=deliver, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    payloads = [r.json() for r in results]
    documents, _raw, _manifest = REAL_READ_GCS_PERIOD(BUCKET, PERIOD,
                                                      client=rig.bucket)
    expected = run_id_for(PERIOD, documents)

    assert {p["run_id"] for p in payloads} == {expected}
    assert {p["outcome"] for p in payloads} == {payloads[0]["outcome"]}
    assert len(rig.run_ids()) == 1
    assert close_key(service.COMPANY, PERIOD) in shared._closes
