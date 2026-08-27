"""The audit's section-5 claim about GCS intake skipping, and the fix for it.

The claim, as filed: `read_gcs_period` silently dropped PDFs, images, non-UTF8
bytes and anything over `MAX_OBJECT_BYTES`, and the close could still report
success with those documents ignored.

The mechanism, read out of the source rather than guessed at. `adapters/gcs.py`
filtered an object out of the month *before* it could become a `Document` --
three `continue`s, on `not name.endswith(".txt")`, on
`blob.size > MAX_OBJECT_BYTES`, and on `UnicodeDecodeError`. Everything
downstream of intake was handed only the survivors:

    read_gcs_period -> documents -> run_close(documents=...) -> Ledger.add_all
                                 -> exceptions.find_all(documents, ...)
                                 -> validation.validate(ledger, results)

`g6_every_document_was_accounted_for` reads `ledger.documents_of(UNKNOWN)`. An
object dropped at intake never became a `Document`, was not in the `Ledger`,
and therefore could not be `UNKNOWN`. G6 covers the document the extractor did
not RECOGNISE; it could not cover the object the adapter never HANDED IT. G6's
own docstring describes that failure one layer further down the pipe -- a month
that "closed at zero revenue and reported every gate passed" -- which is why
the gap mattered.

What was NOT claimed, because it was false even then: the skip was never
unrecorded. `event_source` puts `objects_skipped` and a per-object
`{object, reason}` list into the persisted `source` block, and `web/app.js`
renders both. So the precise finding was *non-blocking*, not *invisible*: the
provenance panel named the dropped objects while the accounting surfaces --
outcome, gates, findings, summary, owner digest, run journal -- all behaved as
though the bucket had contained only the survivors.

That is fixed, and these tests now hold intake to the fix. Every BLOCKING skip
comes back from `read_gcs_period` as a `DocType.UNKNOWN` Document carrying
`failure_reason="not read from the mailbox: ..."`. It posts nothing, G6 refuses
the month, and the file is named on every surface an owner reads. The one skip
that is deliberately non-blocking is the content-hash duplicate: bytes already
read under another name are in the books already, so refusing a month over a
file someone forwarded twice would be the wrong answer.

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
#: Windows tool in UTF-16. Only the first is readable; the other three reach the
#: books as UNKNOWN documents carrying the reason they could not be read.
GOOD_TXT = "load-1.txt"
THE_PDF = "invoice-scan.pdf"          # unreadable: "not text"
THE_OVERSIZE = "statement-export.txt"  # unreadable: over MAX_OBJECT_BYTES
THE_NON_UTF8 = "remittance-utf16.txt"  # unreadable: "not utf-8"
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
    # A PDF, by magic bytes and extension. The extractor still never sees it;
    # intake now hands the books an UNKNOWN document in its place.
    bucket.upload(THE_PDF, b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n")
    # 1 byte over the cap, and it is a perfectly ordinary text document.
    bucket.upload(THE_OVERSIZE, b"Document Type: Bank Transaction\n"
                  + b"x" * gcs.MAX_OBJECT_BYTES)
    # A real remittance, saved as UTF-16 with a BOM. `.txt`, under the cap, and
    # still undecodable, so it arrives carrying its reason rather than missing.
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
    reads as *the result of the month* mentions the objects intake could not
    read. Before the fix the answer was nothing at all.
    """
    digest = payload.get("digest") or {}
    return {
        "the gates": json.dumps(payload.get("gates") or []),
        "the exceptions": json.dumps(payload.get("findings") or []),
        "the drafts": json.dumps(payload.get("drafts") or []),
        "the month-end summary": payload.get("summary") or "",
        "the owner's digest": (digest.get("subject") or "") + "\n"
                              + (digest.get("body") or ""),
        "the run journal": json.dumps(payload.get("journal") or {}),
        "the outcome reason": payload.get("outcome_reason") or "",
    }


# ── 1. the claim, driven end to end ──────────────────────────────────────────

def test_three_unreadable_objects_block_the_month_and_are_named(rig):
    """Four objects go into the bucket, three cannot be read, the month blocks.

    This is the audit's claim in its strongest and most falsifiable form, now
    inverted into the behaviour that answers it. The old defect was not that
    the system never knew -- it knew, by name and by reason, and the provenance
    assertions below still record that. It was that knowing changed nothing:
    `policy.decide_outcome` reads gates, decisions and the agent verdict, and
    none of those three could see an object that intake had dropped before it
    became a `Document`.

    The invariant is the one a correct system has to hold: an artifact in the
    month's mailbox that never reached the books either blocks the close or is
    named on some surface a human reads. This close does both. G6 refuses the
    month, and all three filenames appear in the gates, the exceptions, the
    month-end summary and the owner's digest, so the reader who only ever sees
    the letter still learns which three files are missing from the books.
    """
    app, durable, bucket = rig
    trigger = a_months_mail(bucket)
    assert len(bucket.blobs) == 4

    response = post(app, finalize(trigger))

    # `status` is the route's answer to Pub/Sub -- a close ran, do not redeliver.
    # `outcome`, further down, is the close's verdict on the books themselves.
    assert response.status_code == 200
    assert response.json()["status"] == "closed"

    payload = durable.load_close(service.COMPANY, PERIOD)
    src = payload["source"]

    # It knew, and it still says so: one object read, three named and skipped.
    assert src["objects_read"] == 1
    assert src["objects_skipped"] == 3
    assert {s["object"].rsplit("/", 1)[-1] for s in src["skipped"]} == set(SKIPPED_NAMES)

    # And now it refuses the month rather than closing on the survivor alone.
    assert payload["outcome"] == "blocked"
    g6 = next(g for g in payload["gates"] if g["rule"].startswith("G6"))
    failed = [g["rule"] for g in payload["gates"] if not g["passed"]]
    assert failed == [g6["rule"]], (
        "G6 is the gate that must refuse this month, and it must be the only "
        f"one failing on mail that is otherwise sound: {failed}")
    assert g6["severity"] == "error"
    for name in SKIPPED_NAMES:
        assert name in g6["message"], g6["message"]

    # The failure has to reach the surfaces a person actually reads, not just
    # the gate list a machine parses.
    surfaces = _text_of(payload)
    for surface in ("the gates", "the exceptions", "the month-end summary",
                    "the owner's digest"):
        unnamed = [obj for obj in SKIPPED_NAMES if obj not in surfaces[surface]]
        assert not unnamed, (
            f"{surface} does not name {', '.join(unnamed)}. Three of the four "
            f"objects under gs://{BUCKET}/mail/{PERIOD}/ never became readable "
            f"documents, and the UTF-16 remittance for L-9002 that would have "
            f"settled the one load confirmation is among them.")


# ── 2. what the trail a judge replays actually says ──────────────────────────

def test_the_run_trail_reports_all_four_objects_that_were_uploaded(rig):
    """Step 1 of the journal is the close's own account of what it took in.

    It counts `len(documents)`, and that used to mean the survivors only -- so
    the trail an owner scrolled through on Monday, and a judge replayed, stated
    a smaller month than the one that was uploaded, with no note that anything
    had been set aside. Now the unreadable objects are documents too, so the
    count matches the bucket and the family breakdown says how many of them
    nothing could be posted from.
    """
    app, durable, bucket = rig
    trigger = a_months_mail(bucket)

    post(app, finalize(trigger))
    payload = durable.load_close(service.COMPANY, PERIOD)

    intake = next(s for s in payload["journal"]["steps"] if s["name"] == "intake")
    assert intake["detail"].startswith("4 artifacts"), intake["detail"]
    assert "unknown x3" in intake["detail"], intake["detail"]
    assert intake["counts"]["documents"] == 4
    assert intake["counts"]["unknown"] == 3
    assert intake["status"] == "ok"


# ── 3. the provenance block still says which object and why ──────────────────

def test_the_skip_is_recorded_in_provenance_with_a_reason_per_object(rig):
    """The half of the audit's wording that never held: not *silent*.

    `event_source` persists a reason per skipped object and `web/app.js`
    renders them as `pill warn` rows in the mailbox table. The defect was that
    this was the only place it appeared and nothing consumed it. It is still
    the only place the exact reason is written down, which is why it matters
    that it is written down accurately -- and `blocking` is now the flag that
    decides whether the object also becomes an UNKNOWN document and refuses the
    month, so a wrong reason here is a wrong verdict downstream.
    """
    app, durable, bucket = rig
    trigger = a_months_mail(bucket)

    post(app, finalize(trigger))
    src = durable.load_close(service.COMPANY, PERIOD)["source"]

    reasons = {s["object"].rsplit("/", 1)[-1]: s["reason"] for s in src["skipped"]}
    assert reasons[THE_PDF] == "not text"
    assert "cap" in reasons[THE_OVERSIZE]
    assert reasons[THE_NON_UTF8] == "not utf-8"
    assert all(s["blocking"] for s in src["skipped"])


# ── 4. a bucket of nothing but unreadable objects is refused, not ignored ─────

def test_a_mailbox_of_only_pdfs_is_closed_blocked_rather_than_ignored(rig):
    """The second silence, kept separate from the claim under test.

    When every object was dropped, `documents` came back empty, `/events`
    answered 200 `ignored`, Pub/Sub acked, and no close record was written at
    all. A bookkeeper who scanned their whole month to PDF got no books, no
    letter, no blocked run and no error: the trigger fired and the system
    agreed there was nothing to do. That is the worst version of the defect,
    because zero documents and zero revenue are indistinguishable from a quiet
    month.

    Now the unreadable objects are the documents, so there IS something to
    close, and closing it produces a blocked month naming both files.
    """
    app, durable, bucket = rig
    pdf = bucket.upload(THE_PDF, b"%PDF-1.7\n")
    bucket.upload("photo.jpg", b"\xff\xd8\xff\xe0")

    response = post(app, finalize(pdf))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "closed"
    assert body["outcome"] == "blocked"
    assert body["exceptions"] == 2

    payload = durable.load_close(service.COMPANY, PERIOD)
    assert payload is not None, "a month of unreadable mail must leave a record"
    assert payload["outcome"] == "blocked"

    g6 = next(g for g in payload["gates"] if g["rule"].startswith("G6"))
    assert not g6["passed"]
    assert THE_PDF in g6["message"] and "photo.jpg" in g6["message"]

    # Zero revenue is exactly what a silently-emptied month looked like, so the
    # figure alone proves nothing. What proves it is the letter that goes with
    # it: the owner is told the books did not pass, and which files are why.
    assert payload["statements"]["revenue"] == 0.0
    digest = payload["digest"]
    assert "did not pass" in digest["subject"]
    assert THE_PDF in digest["body"] and "photo.jpg" in digest["body"]


# ── 5. the run id tells the two months apart ─────────────────────────────────

def test_adding_an_unreadable_document_produces_a_different_run_id():
    """`run_id_for` hashes the documents, and the scanned invoice is now one of
    them, so a month that gained a PDF is no longer byte-identical to the month
    without it. It used to be: same id, same record overwritten, no way to tell
    from the trail that the mail had changed.

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

    assert len(docs_clean) == 1
    assert len(docs_pdf) == 2
    assert len(manifest["skipped"]) == 1

    # The PDF is carried into the books as the artifact nobody could read, and
    # it says so in its own words. That reason is what G6 and the digest quote.
    unreadable = next(d for d in docs_pdf if d.source_file == THE_PDF)
    assert unreadable.failure_reason.startswith("not read from the mailbox:")
    assert "not text" in unreadable.failure_reason

    assert run_id_for(PERIOD, docs_clean) != run_id_for(PERIOD, docs_pdf)
