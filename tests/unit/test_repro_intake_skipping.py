"""Reproduction of the audit's section-5 claim about GCS intake skipping.

The claim: `read_gcs_period` silently drops PDFs, images, non-UTF8 bytes and
anything over `MAX_OBJECT_BYTES`, and the close can still report success with
those documents ignored.

The mechanism, read out of the source rather than guessed at:

`adapters/gcs.py` filters an object out of the month *before* it can become a
`Document` -- three `continue`s, on `not name.endswith(".txt")`, on
`blob.size > MAX_OBJECT_BYTES`, and on `UnicodeDecodeError`. Everything
downstream of intake is handed only the survivors:

    read_gcs_period -> documents -> run_close(documents=...) -> Ledger.add_all
                                 -> exceptions.find_all(documents, ...)
                                 -> validation.validate(ledger, results)

`g6_every_document_was_accounted_for` reads `ledger.documents_of(UNKNOWN)`.
An object that was dropped at intake never became a `Document`, is not in the
`Ledger`, and therefore cannot be `UNKNOWN`. G6 covers the document the
extractor did not RECOGNISE; it structurally cannot cover the object the
adapter never HANDED IT. Its own docstring describes exactly this failure one
layer further down the pipe -- a month that "closed at zero revenue and
reported every gate passed" -- which is why the gap matters.

What is NOT claimed here, because it is false: the skip is not unrecorded.
`event_source` puts `objects_skipped` and a per-object `{object, reason}` list
into the persisted `source` block, and `web/app.js` renders both (a count in
the origin panel, a `pill warn` row per object in the mailbox table). So the
precise finding is *non-blocking*, not *invisible*: the provenance panel names
the dropped objects while the accounting surfaces -- outcome, six gates,
findings, summary, owner digest, run journal -- all behave as though the
bucket contained only the survivors.

Everything here is offline. `google-cloud-storage` is injected as a fake, the
way the rest of the suite does it, and the close is driven through the real
`POST /events` route so the assertions are about what the deployed service
answers, not about what a helper function returns.
"""
from __future__ import annotations

import base64
import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from archon.adapters import auth, gcs, service  # noqa: E402
from archon.adapters.store import LocalStore  # noqa: E402
from archon.runtime.close import run_id_for  # noqa: E402

PERIOD = "2026-07"
BUCKET = "bell-ridge-mail"


# ── the four objects the bookkeeper put in the bucket ────────────────────────

def load_text(ref: str, amount: float) -> bytes:
    """The real label-block format `domain/extract.py` parses."""
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


#: A month's mail as a bookkeeper actually produces it: one document typed into
#: a text file, one scanned to PDF, one exported far too large, one saved by a
#: Windows tool in UTF-16. Only the first survives intake.
GOOD_TXT = "load-1.txt"
THE_PDF = "invoice-scan.pdf"          # dropped: "not text"
THE_OVERSIZE = "statement-export.txt"  # dropped: over MAX_OBJECT_BYTES
THE_NON_UTF8 = "remittance-utf16.txt"  # dropped: "not utf-8"
SKIPPED_NAMES = (THE_PDF, THE_OVERSIZE, THE_NON_UTF8)


class FakeBlob:
    """One object in the fake bucket, with the surface `read_gcs_period` uses."""

    def __init__(self, name: str, data: bytes, generation: int = 1):
        self.name = name
        self._data = data
        self.size = len(data)
        self.generation = generation

    def download_as_bytes(self) -> bytes:
        return self._data


class FakeBucket:
    def __init__(self, blobs: list[FakeBlob] | None = None):
        self.blobs: list[FakeBlob] = list(blobs or [])

    def upload(self, name: str, data: bytes, generation: int = 1) -> FakeBlob:
        blob = FakeBlob(f"mail/{PERIOD}/{name}", data, generation)
        self.blobs.append(blob)
        return blob

    def list_blobs(self, bucket_name, prefix=""):
        assert bucket_name == BUCKET
        return [b for b in self.blobs if b.name.startswith(prefix)]


def a_months_mail(bucket: FakeBucket) -> FakeBlob:
    """Fill the bucket with the four objects. Returns the trigger object."""
    trigger = bucket.upload(GOOD_TXT, load_text("L-9001", 1000.0))
    # A PDF, by magic bytes and extension. The extractor never sees it.
    bucket.upload(THE_PDF, b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n")
    # 1 byte over the cap, and it is a perfectly ordinary text document.
    bucket.upload(THE_OVERSIZE, b"Document Type: Bank Transaction\n"
                  + b"x" * gcs.MAX_OBJECT_BYTES)
    # A real remittance, saved as UTF-16 with a BOM. `.txt`, under the cap,
    # and it still never becomes a Document.
    bucket.upload(THE_NON_UTF8, load_text("L-9002", 2500.0).decode().encode("utf-16"))
    return trigger


def finalize(blob: FakeBlob, message_id: str = "m-1") -> dict:
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
    """The real app and one durable store, with a fake bucket behind intake.

    Only two things are replaced: the storage client (injected by design, so
    the suite runs without `google-cloud-storage`) and `get_store`, pinned to
    one instance so "what is in the durable store" can be asked at all.
    `USE_AGENT` is forced off so the deterministic close is what answers,
    regardless of the developer's environment.
    """
    monkeypatch.delenv(auth.AUDIENCE_ENV, raising=False)   # the open posture
    monkeypatch.delenv(auth.CALLER_ENV, raising=False)
    monkeypatch.setattr(service, "USE_AGENT", False)

    durable = LocalStore()
    monkeypatch.setattr(service, "get_store", lambda: durable)

    bucket = FakeBucket()
    real_read = gcs.read_gcs_period

    def read_through_the_fake(bucket_name, period, client=None):
        return real_read(bucket_name, period, client=bucket)

    monkeypatch.setattr(service.gcs, "read_gcs_period", read_through_the_fake)

    return service.app, durable, bucket


def post(app, envelope) -> object:
    """One Pub/Sub push, through the real route, middleware included."""
    return TestClient(app).post("/events", json=envelope)


def _text_of(payload: dict) -> dict[str, str]:
    """Every human-readable accounting surface the close produced, as text.

    Deliberately excludes `source`, which is the machine-readable provenance
    block. The question this answers is whether anything an owner or a judge
    reads as *the result of the month* mentions the dropped objects.
    """
    digest = payload.get("digest") or {}
    return {
        "the six gates": json.dumps(payload.get("gates") or []),
        "the exceptions": json.dumps(payload.get("findings") or []),
        "the drafts": json.dumps(payload.get("drafts") or []),
        "the month-end summary": payload.get("summary") or "",
        "the owner's digest": (digest.get("subject") or "") + "\n"
                              + (digest.get("body") or ""),
        "the run journal": json.dumps(payload.get("journal") or {}),
        "the outcome reason": payload.get("outcome_reason") or "",
    }


# ── 1. the claim, driven end to end ──────────────────────────────────────────

def test_three_dropped_objects_do_not_stop_the_month_closing_green(rig):
    """Four objects go into the bucket, one becomes a document, six gates pass.

    This is the audit's claim in its strongest and most falsifiable form. It is
    not "the system never knew" -- it knew, by name and by reason, and the two
    assertions above the invariant record that. It is that knowing changed
    nothing: `policy.decide_outcome` reads gates, decisions and the agent
    verdict, and none of those three can see an object that intake dropped.

    The invariant asserted is the weakest one a correct system could hold: an
    artifact in the month's mailbox that never reached the books either blocks
    the close or is named on some surface a human reads. Not "must fail G6" --
    a warning finding, a line in the owner's letter or a note on the trail
    would all satisfy it.
    """
    app, durable, bucket = rig
    trigger = a_months_mail(bucket)
    assert len(bucket.blobs) == 4

    response = post(app, finalize(trigger))

    assert response.status_code == 200
    assert response.json()["status"] == "closed"

    payload = durable.load_close(service.COMPANY, PERIOD)
    src = payload["source"]

    # It knew. Three of the four objects were seen, named and dropped.
    assert src["objects_read"] == 1
    assert src["objects_skipped"] == 3
    assert {s["object"].rsplit("/", 1)[-1] for s in src["skipped"]} == set(SKIPPED_NAMES)

    # And it closed anyway, on all six gates, with the one surviving document.
    assert payload["outcome"] == "closed"
    assert [g["passed"] for g in payload["gates"]] == [True] * 6
    g6 = next(g for g in payload["gates"] if g["rule"].startswith("G6"))
    assert g6["message"] == "Skipped: every document matched a known family", (
        "G6 reports passed-and-skipped: the three dropped objects never became "
        "Documents, so `ledger.documents_of(UNKNOWN)` is empty and the gate has "
        "nothing to look at")

    surfaces = _text_of(payload)
    named_on = sorted(name for name, text in surfaces.items()
                      if any(obj in text for obj in SKIPPED_NAMES))
    blocked = payload["outcome"] != "closed" or any(
        not g["passed"] for g in payload["gates"])

    assert blocked or named_on, (
        f"3 of the 4 objects under gs://{BUCKET}/mail/{PERIOD}/ never became "
        f"documents ({', '.join(SKIPPED_NAMES)}), and the month closed anyway: "
        f"outcome={payload['outcome']!r}, 6/6 gates passed, "
        f"{len(payload['findings'])} exception(s) raised and not one of them "
        f"names a dropped object. Nothing that reads as the result of the month "
        f"-- gates, exceptions, drafts, summary, the owner's digest or the run "
        f"trail -- mentions them; only the machine-readable `source` block does. "
        f"The books say revenue "
        f"{payload['statements']['revenue']:,.2f} from one load confirmation, "
        f"and the UTF-16 remittance for L-9002 that would have settled it was "
        f"dropped at intake.")


# ── 2. what the trail a judge replays actually says ──────────────────────────

def test_the_run_trail_reports_one_artifact_when_four_were_uploaded(rig):
    """Step 1 of the journal is the close's own account of what it took in.

    It counts `len(documents)` -- the survivors -- so the trail an owner
    scrolls through on Monday, and a judge replays, states a smaller month than
    the one that was uploaded, with no note that anything was set aside.
    Passing, because it documents current behaviour precisely.
    """
    app, durable, bucket = rig
    trigger = a_months_mail(bucket)

    post(app, finalize(trigger))
    payload = durable.load_close(service.COMPANY, PERIOD)

    intake = next(s for s in payload["journal"]["steps"] if s["name"] == "intake")
    assert intake["detail"].startswith("1 artifacts"), intake["detail"]
    assert "skip" not in intake["detail"].lower()
    assert intake["status"] == "ok"


# ── 3. the provenance block is the one place that does say so ────────────────

def test_the_skip_is_recorded_in_provenance_even_though_it_blocks_nothing(rig):
    """The half of the audit's wording that does not hold: not *silent*.

    `event_source` persists a reason per dropped object and `web/app.js`
    renders them as `pill warn` rows in the mailbox table. The defect is that
    this is the only place it appears, and nothing consumes it.
    """
    app, durable, bucket = rig
    trigger = a_months_mail(bucket)

    post(app, finalize(trigger))
    src = durable.load_close(service.COMPANY, PERIOD)["source"]

    reasons = {s["object"].rsplit("/", 1)[-1]: s["reason"] for s in src["skipped"]}
    assert reasons[THE_PDF] == "not text"
    assert "cap" in reasons[THE_OVERSIZE]
    assert reasons[THE_NON_UTF8] == "not utf-8"


# ── 4. a bucket of nothing but unreadable objects leaves no trace at all ──────

def test_a_mailbox_of_only_pdfs_is_acknowledged_and_never_closed(rig):
    """The second silence, kept separate from the claim under test.

    When every object is dropped, `documents` is empty, `/events` answers 200
    `ignored`, Pub/Sub acks, and no close record is written. A bookkeeper who
    scans their whole month to PDF gets no books, no letter, no blocked run and
    no error -- the trigger fired and the system agreed there was nothing to do.
    """
    app, durable, bucket = rig
    pdf = bucket.upload(THE_PDF, b"%PDF-1.7\n")
    bucket.upload("photo.jpg", b"\xff\xd8\xff\xe0")

    response = post(app, finalize(pdf))

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": f"no readable mail under gs://{BUCKET}/mail/{PERIOD}/",
    }
    assert durable.load_close(service.COMPANY, PERIOD) is None


# ── 5. the run id cannot tell the two months apart ───────────────────────────

def test_adding_an_unreadable_document_produces_the_same_run_id():
    """`run_id_for` hashes the surviving documents, so a month that gained a
    scanned invoice is byte-identical to the month without it: same id, same
    record overwritten, no way to tell from the trail that the mail changed.

    Deliberately does not take `rig`: that fixture patches the shared `gcs`
    module attribute, and this test needs the real `read_gcs_period` with its
    own injected clients.
    """
    clean = FakeBucket([FakeBlob(f"mail/{PERIOD}/{GOOD_TXT}",
                                 load_text("L-9001", 1000.0))])
    with_pdf = FakeBucket(list(clean.blobs))
    with_pdf.upload(THE_PDF, b"%PDF-1.7\n")

    docs_clean, _r, _m = gcs.read_gcs_period(BUCKET, PERIOD, client=clean)
    docs_pdf, _r2, manifest = gcs.read_gcs_period(BUCKET, PERIOD, client=with_pdf)

    assert len(docs_clean) == len(docs_pdf) == 1
    assert len(manifest["skipped"]) == 1
    assert run_id_for(PERIOD, docs_clean) == run_id_for(PERIOD, docs_pdf)
