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
from datetime import UTC

import pytest

from archon.adapters import gcs, service
from archon.adapters.store import LocalStore
from archon.domain.models import DocType
from archon.domain.validation import validate
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

    reasons = {s["object"].rsplit("/", 1)[-1]: s["reason"] for s in manifest["skipped"]}
    assert "cap" in reasons["huge.txt"]
    assert reasons["photo.jpg"] == "not text"
    assert reasons["binary.txt"] == "not utf-8"

    # One document was READ. The other three still arrive, as UNKNOWN documents
    # carrying why they could not be, because an object skipped before it became
    # a document was invisible to every gate and a month could close green with
    # a remittance sitting unread in the bucket.
    readable = [d for d in documents if d.doc_type is not DocType.UNKNOWN]
    refused = [d for d in documents if d.doc_type is DocType.UNKNOWN]

    assert len(readable) == 1
    assert {d.source_file for d in refused} == {"huge.txt", "photo.jpg", "binary.txt"}
    assert all(d.failure_reason.startswith("not read from the mailbox:") for d in refused)

    # And G6 refuses the month rather than closing over them.
    gates = validate(_ledger_of(documents), [])
    g6 = next(g for g in gates if g.rule.startswith("G6"))
    assert g6.passed is False
    assert "huge.txt" in g6.message or "3 document(s)" in g6.message


def _ledger_of(documents):
    """A ledger holding exactly these documents, for asserting on the gates."""
    from archon.domain.ledger import Ledger

    ledger = Ledger(period=PERIOD, company="Bell Ridge Haulage")
    ledger.add_all(documents)
    return ledger


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
    closes. A 503 makes Pub/Sub retry until the marker says closed.

    "Running" is now a claim inside its lease, which is why the marker carries
    when it was taken. It has to: a claim with no timestamp is one whose holder
    cannot be shown to be alive, and answering 503 to those forever is how a
    container that died mid-close used to block its month until the message
    expired. `test_a_holder_that_died_with_its_instance_does_not_block_the_month_forever`
    is the other side of this assertion."""
    from datetime import datetime

    store, _runs = wired
    env = envelope(generation="777")
    key = gcs.dedupe_key(env, PERIOD)
    store.save_close(service.COMPANY, key,
                     {"period": PERIOD, "status": "processing", "attempt": 1,
                      "claimed_at": datetime.now(UTC).isoformat()})

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


def test_a_transient_storage_failure_is_retryable_and_a_permanent_one_is_not(
        wired, monkeypatch):
    """A 200 on a transient blip acks the event permanently and the month
    never closes; a non-2xx on a malformed event redelivers it until expiry,
    one close attempt per redelivery. Each failure gets the right one."""
    class ServiceUnavailable(Exception):
        pass

    def blip(bucket, period, client=None):
        raise ServiceUnavailable("storage had a moment")

    monkeypatch.setattr(service.gcs, "read_gcs_period", blip)
    response = _post(envelope(generation="880"))
    assert response.status_code == 503, "a transient failure must be redelivered"

    def broken(bucket, period, client=None):
        raise ValueError("this event will never parse better")

    monkeypatch.setattr(service.gcs, "read_gcs_period", broken)
    response = _post(envelope(generation="881"))
    assert response.status_code == 200, "a permanent failure must not redeliver forever"
    assert json.loads(response.body)["status"] == "error"


def test_the_batch_marker_is_not_read_as_a_document(monkeypatch):
    """The interaction of two changes that are each correct on their own.

    `ARCHON_BATCH_MARKER` names a control object that means "the batch is
    complete". It has no extension and no content, so the fail-closed intake
    read it as an artifact that could not be read, turned it into an UNKNOWN
    document, and G6 refused the month. Every batched close came back
    `blocked`, over a month whose books were perfectly fine.

    Found on the live service after deploying, not here, which is the argument
    for closing a real month before calling the work done.
    """
    monkeypatch.setenv("ARCHON_BATCH_MARKER", "_READY")
    client = FakeClient([
        blob("load-1.txt", load_text("L-9001", 1000.0)),
        blob("_READY", b""),
        # A SUFFIXED marker, because a marker cannot always be overwritten: on
        # this project's own bucket the owner holds bucket-level roles only, so
        # re-closing a month needs a new object name. It must not come back as
        # an unreadable document either.
        blob("_READY2", b""),
        # NOT a marker, and the distinction is the whole point. An underscore
        # is a convention, not a guarantee: for one commit every name starting
        # with one was waved through as a control object, which would have
        # dropped an export named `_invoice.txt` silently and closed the month
        # green over it. Only the configured marker and a suffix that could not
        # be a filename anybody means are controls.
        blob("_notes", b"scratch"),
    ])

    documents, _raw, manifest = gcs.read_gcs_period(BUCKET, PERIOD, client=client)

    # `_notes` is not a marker, so it is mail the intake could not read, and it
    # arrives as an UNKNOWN document that G6 will refuse the month over. That
    # is the correct outcome: a control object is waved through and everything
    # else that cannot be read stops the close.
    assert [d.doc_type for d in documents] == [
        DocType.LOAD_CONFIRMATION, DocType.UNKNOWN]
    controls = [s for s in manifest["skipped"] if not s["blocking"]]
    assert {c["object"].rsplit("/", 1)[-1] for c in controls} == {"_READY", "_READY2"}
    assert all("not a document" in c["reason"] for c in controls)

    blocked = [s for s in manifest["skipped"] if s["blocking"]]
    assert {b["object"].rsplit("/", 1)[-1] for b in blocked} == {"_notes"}

    # The marker passes through and everything else that could not be read
    # stops the close. Both halves matter: a control object must not block a
    # month, and nothing else may be waved through with it.
    g6 = next(g for g in validate(_ledger_of(documents), []) if g.rule.startswith("G6"))
    assert g6.passed is False
    assert "_notes" in g6.message


def test_the_marker_alone_does_not_block_the_month():
    """The half this gate exists for: a batch marker is not an unread document."""
    import os

    os.environ["ARCHON_BATCH_MARKER"] = "_READY"
    try:
        client = FakeClient([
            blob("load-1.txt", load_text("L-9001", 1000.0)),
            blob("_READY", b""),
        ])
        documents, _raw, _manifest = gcs.read_gcs_period(BUCKET, PERIOD, client=client)
    finally:
        os.environ.pop("ARCHON_BATCH_MARKER", None)

    assert [d.doc_type for d in documents] == [DocType.LOAD_CONFIRMATION]
    g6 = next(g for g in validate(_ledger_of(documents), []) if g.rule.startswith("G6"))
    assert g6.passed is True


def test_a_renamed_copy_never_displaces_the_document_it_was_copied_from():
    """Which name the trail points at, when two objects hold the same bytes.

    Content-hash dedupe keeps the first object read and folds the rest away.
    Plain alphabetical order decided that, and `-` sorts before `.`, so
    `remittance-TFX-RA-4417-redelivery-3.txt` beat
    `remittance-TFX-RA-4417.txt`. The live manifest then reported the month's
    actual remittance as a duplicate of an experimental copy of itself, which
    reads to anyone inspecting provenance as though the real document was
    ignored.

    A copy is named after its original and is therefore longer. Shortest name
    wins, and the rule needs no knowledge of which object the event named.
    """
    canonical = load_text("L-7105", 2460.0)
    client = FakeClient([
        blob("load-L-7105-redelivery-3.txt", canonical),
        blob("load-L-7105.txt", canonical),
        blob("zz-another-copy.txt", canonical),
    ])

    documents, _raw, manifest = gcs.read_gcs_period(BUCKET, PERIOD, client=client)

    assert [d.source_file for d in documents] == ["load-L-7105.txt"]
    folded = {s["object"].rsplit("/", 1)[-1] for s in manifest["skipped"]}
    assert folded == {"load-L-7105-redelivery-3.txt", "zz-another-copy.txt"}
    assert all("load-L-7105.txt" in s["reason"] for s in manifest["skipped"])


def test_an_uppercase_extension_is_still_a_text_file():
    """`INVOICE.TXT` off a Windows scanner was refused as "not text".

    Blocking, so it was never silently dropped -- the month came back refused
    rather than wrong, which is the right direction to fail. But the extension
    is the bookkeeper's typing, not a protocol, and a close refused over a
    capital letter is a close refused.
    """
    client = FakeClient([blob("LOAD-1.TXT", load_text("L-9001", 1234.0))])

    documents, _raw, manifest = gcs.read_gcs_period(BUCKET, PERIOD, client=client)

    assert [s for s in manifest.get("skipped", []) if "not text" in s["reason"]] == []
    assert len(documents) == 1, "an uppercase extension is still text"


def test_a_folder_placeholder_does_not_block_the_month():
    """Clicking "create folder" in the Cloud Console writes a zero-byte object
    whose name ends in a slash. The extension test called it "not text" and
    blocked the period -- permanently, because this bucket's owner holds
    `legacyBucketOwner`, which carries no object permissions, so they cannot
    delete it either. An ordinary click in Google's own UI, and the month
    stops closing with no way back.

    A file the product genuinely cannot read must still block. That is G6's
    whole job, and the two cases are told apart by zero bytes and a trailing
    slash, which no real document has.
    """
    client = FakeClient([
        blob("load-1.txt", load_text("L-9001", 1234.0)),
        blob("scans/", b""),
        blob("receipt.pdf", b"%PDF-1.7 not text"),
    ])

    documents, _raw, manifest = gcs.read_gcs_period(BUCKET, PERIOD, client=client)

    blocking = [s for s in manifest["skipped"] if s["blocking"]]
    assert [s["object"] for s in blocking] == [f"mail/{PERIOD}/receipt.pdf"], (
        "the folder placeholder blocked the month, or the pdf stopped blocking it")
    assert any("folder placeholder" in s["reason"] for s in manifest["skipped"])
