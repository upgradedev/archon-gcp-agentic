"""Independent check of the run-identity claim, driven from the real trigger.

Written to *refute* the claim if it could be refuted. It could not. The three
failing tests below were built from scratch rather than adapted from the
reporting agent's file, and two of them assert on records the product itself
writes and reads through the published `Store` interface -- the `/events`
dedupe markers -- rather than on `run_id_for`'s return value or on a private
dict, so the store testifies to the collision in its own words.

What is confirmed:

* `run_id_for` (close.py:194-208) hashes only the period and the ordered
  (source_file, doc_type) pairs. No amount, date, load reference or raw byte
  reaches it, so a corrected document produces the id the uncorrected one
  produced. Its docstring's "Change one document and the id changes" is false
  for a change of content; it is true only for adding, losing or renaming one.
* `gcs.dedupe_key` keys on object+generation on purpose, so a corrected
  re-upload of the same object is a *new* event and closes the month again.
  Two distinct events therefore land on one `runs/{run_id}` document, and
  `save_run` / `save_drafts` are unconditional keyed writes (store.py:74, 83;
  Firestore `.set()` at 113 and `batch.set()` at 126), so the second close's
  journal replaces the first's rather than sitting beside it.
* `put_document` is keyed on a bare filename that neither mailbox qualifies
  (`mailbox.py:44` uses `path.name`; `gcs.py:68` strips `mail/<period>/`), so
  the corrected re-upload also overwrites the archived copy of the original
  text -- inside one period, with no cross-month contrivance needed.

What bounds it, and is asserted here as a passing test rather than argued:
nothing in `src/`, `service/` or `web/` ever calls `load_run` or `load_drafts`.
Those two collections are write-only in production. The record the UI actually
reads is `closes/{company}::{period}`, which is keyed by period and would be
replaced by any re-close whatever the run id were, and after a correction it
holds the *corrected* books, which are the right ones. So this is a real gap
against the product's stated "a trail you can walk back through", with no
wrong books, no money moved and no reader degraded.

Everything is offline: bundled corpus, injected fake storage client, in-memory
store, fixed clock.
"""
from __future__ import annotations

import base64
import copy
import json

import pytest

from archon.adapters import gcs
from archon.adapters.store import LocalStore
from archon.domain.extract import extract_document
from archon.runtime.close import run_close, run_id_for
from archon.runtime.journal import FixedClock
from archon.runtime.mailbox import read_period

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from archon.adapters import auth, service  # noqa: E402

PERIOD = "2026-07"
BUCKET = "bell-ridge-mail"
AMENDED = "load-L-7101.txt"
ORIGINAL_RATE = "2,450.00"
CORRECTED_RATE = "9,450.00"


# --------------------------------------------------------------------------
# offline doubles
# --------------------------------------------------------------------------

class WitnessStore(LocalStore):
    """The real store, keeping a note of every key the close wrote through.

    Only the six published `Store` methods are overridden. Nothing here reaches
    into `LocalStore`'s internals to manufacture a result; `archived()` reads
    back what `put_document` stored, which is the only readback the protocol
    has for that collection (it defines no getter for documents at all).
    """

    def __init__(self) -> None:
        super().__init__()
        self.run_writes: list[str] = []
        self.draft_writes: list[str] = []
        self.document_writes: list[str] = []

    def put_document(self, name: str, content: str) -> str:
        self.document_writes.append(name)
        return super().put_document(name, content)

    def save_run(self, run: dict) -> str:
        self.run_writes.append(run["run_id"])
        return super().save_run(run)

    def save_drafts(self, run_id: str, drafts: list) -> list[str]:
        self.draft_writes.append(run_id)
        return super().save_drafts(run_id, drafts)

    def archived(self, name: str) -> str | None:
        return self._documents.get(name)


class FakeBlob:
    def __init__(self, name: str, data: bytes, generation: int) -> None:
        self.name = name
        self._data = data
        self.size = len(data)
        self.generation = generation

    def download_as_bytes(self) -> bytes:
        return self._data


class FakeBucket:
    """One live generation per object name, the way `list_blobs` behaves.

    Real `list_blobs` without `versions=True` returns only live generations, so
    re-uploading a name supersedes rather than adds. Keeping that faithful
    matters: a bucket that returned both generations would hand the close 28
    documents instead of 27 and the run id would move for a reason that has
    nothing to do with the claim under test.
    """

    def __init__(self, name: str = BUCKET) -> None:
        self.name = name
        self._objects: dict[str, FakeBlob] = {}

    def upload(self, period: str, filename: str, data: bytes) -> FakeBlob:
        key = f"mail/{period}/{filename}"
        prior = self._objects.get(key)
        blob = FakeBlob(key, data, (prior.generation + 1) if prior else 1)
        self._objects[key] = blob
        return blob

    def list_blobs(self, bucket_name, prefix=""):
        assert bucket_name == self.name
        return [b for b in self._objects.values() if b.name.startswith(prefix)]


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


@pytest.fixture
def rig(monkeypatch):
    """The real FastAPI app, one durable store, an injected storage client."""
    monkeypatch.delenv(auth.AUDIENCE_ENV, raising=False)
    monkeypatch.delenv(auth.CALLER_ENV, raising=False)

    durable = WitnessStore()
    monkeypatch.setattr(service, "get_store", lambda: durable)

    bucket = FakeBucket()
    real_read = gcs.read_gcs_period
    monkeypatch.setattr(
        service.gcs, "read_gcs_period",
        lambda bucket_name, period, client=None: real_read(
            bucket_name, period, client=bucket),
    )
    return service.app, durable, bucket


def push(app, envelope) -> dict:
    return TestClient(app).post("/events", json=envelope).json()


def marker_for(store, envelope) -> dict | None:
    """The `/events` idempotency marker, read the way the route reads it."""
    return store.load_close(service.COMPANY, gcs.dedupe_key(envelope, PERIOD))


def corrected(text: str) -> str:
    assert ORIGINAL_RATE in text, "the corpus document changed under this test"
    return text.replace(ORIGINAL_RATE, CORRECTED_RATE)


# --------------------------------------------------------------------------
# 1. the derivation, with every other variable pinned
# --------------------------------------------------------------------------

def test_run_id_moves_when_a_document_s_figures_move():
    """One rate corrected, same file, same family: the id should move.

    Built through the real extractor so the two documents are the ones the
    product would have built, and the controls are asserted first so a
    differing id could not be blamed on the family changing underneath.
    """
    documents, raw = read_period(PERIOD)
    index = [d.source_file for d in documents].index(AMENDED)
    original = documents[index]
    replacement = extract_document(corrected(raw[AMENDED]),
                                   source_file=AMENDED, period=PERIOD)

    assert replacement.source_file == original.source_file == AMENDED
    assert replacement.doc_type is original.doc_type
    assert (original.net_amount, replacement.net_amount) == (2450.0, 9450.0)

    amended = list(documents)
    amended[index] = replacement
    assert len(amended) == len(documents)

    before, after = run_id_for(PERIOD, documents), run_id_for(PERIOD, amended)
    assert before != after, (
        f"a confirmation of {original.net_amount:,.2f} and a corrected one of "
        f"{replacement.net_amount:,.2f} are both run {before}: run_id_for "
        f"hashes period + source_file + doc_type and never reads the bytes")


def test_re_reading_the_identical_month_keeps_one_run_id():
    """The property the derivation was built for, and it holds. Passes.

    This is why the finding is a gap in the trail rather than a broken close:
    a redelivered trigger really is answered with the same id and the same
    books, which is what the design was after.
    """
    first, _ = read_period(PERIOD)
    second, _ = read_period(PERIOD)
    store = WitnessStore()

    a = run_close(period=PERIOD, documents=first, company=service.COMPANY,
                  store=store, clock=FixedClock())
    b = run_close(period=PERIOD, documents=second, company=service.COMPANY,
                  store=store, clock=FixedClock())

    assert a.run_id == b.run_id
    assert a.statements.revenue == b.statements.revenue
    assert store.run_writes == [a.run_id] * 4      # two closes, two writes each


# --------------------------------------------------------------------------
# 2. two real events, one journal -- asserted on the store's own markers
# --------------------------------------------------------------------------

def test_two_distinct_events_do_not_collapse_onto_one_run_journal(rig):
    """A bookkeeper corrects one rate and re-uploads it under the same name.

    That is generation 2 of the same object, so `dedupe_key` misses on purpose
    and the month closes again -- correctly, the books moved. The assertion is
    made on the two `/events` markers the route itself writes and reads back
    (`service.py:365`), so nothing here depends on a private attribute or on
    `run_id_for`: the store is asked which run each event produced.
    """
    app, durable, bucket = rig
    _, raw = read_period(PERIOD)
    landed = {name: bucket.upload(PERIOD, name, text.encode())
              for name, text in raw.items()}

    first_event = finalize(landed[AMENDED], "m-1")
    assert landed[AMENDED].generation == 1
    assert push(app, first_event)["status"] == "closed"

    amended_blob = bucket.upload(PERIOD, AMENDED, corrected(raw[AMENDED]).encode())
    assert amended_blob.generation == 2           # same object, new bytes
    second_event = finalize(amended_blob, "m-2")
    assert push(app, second_event)["status"] == "closed", (
        "a new generation is meant to close the month again")

    one, two = marker_for(durable, first_event), marker_for(durable, second_event)
    assert one and two and one["status"] == two["status"] == "closed", (
        "both events were accepted and both recorded a completed close")

    assert one["run_id"] != two["run_id"], (
        f"the store holds two distinct event markers, "
        f"{gcs.dedupe_key(first_event, PERIOD)} and "
        f"{gcs.dedupe_key(second_event, PERIOD)}, and both name run "
        f"{one['run_id']}. Only one runs/ document was ever written "
        f"({sorted(set(durable.run_writes))}) and one drafts/ key "
        f"({sorted(set(durable.draft_writes))}), so the journal and the "
        f"corrective drafts of the first close are gone: the second close "
        f"wrote over them under the identity it inherited")


def test_the_corrected_reupload_keeps_the_archived_original_artifact(rig):
    """Step 1 promises the raw artifact is "put beyond reach of anything that
    follows". Within one period, a corrected re-upload reaches it.

    `documents/{name}` carries no period, bucket, generation or company, so the
    second close's `put_document("load-L-7101.txt", ...)` lands on the first's.
    No cross-month filename coincidence is needed for this one; it is the same
    object in the same month.
    """
    app, durable, bucket = rig
    _, raw = read_period(PERIOD)
    for name, text in raw.items():
        bucket.upload(PERIOD, name, text.encode())

    original_text = raw[AMENDED]
    assert push(app, finalize(bucket.upload(PERIOD, AMENDED,
                                            original_text.encode()), "m-1")
                )["status"] == "closed"
    archived_first = copy.deepcopy(durable.archived(AMENDED))
    assert ORIGINAL_RATE in archived_first        # the premise

    assert push(app, finalize(bucket.upload(PERIOD, AMENDED,
                                            corrected(original_text).encode()),
                              "m-2"))["status"] == "closed"

    assert durable.archived(AMENDED) == archived_first, (
        f"documents/{AMENDED} now holds the corrected text "
        f"({CORRECTED_RATE}) and the {ORIGINAL_RATE} the first close actually "
        f"posted from is not under any other key; put_document was called "
        f"twice with the same bare name "
        f"({durable.document_writes.count(AMENDED)} writes to that key)")


# --------------------------------------------------------------------------
# 3. what bounds the blast radius. Passes, and is the severity argument.
# --------------------------------------------------------------------------

def test_the_lost_collections_are_never_read_by_the_product():
    """`runs/` and `drafts/` are write-only outside the test suite.

    Grepped rather than asserted-by-narrative: no module under `src/archon`,
    `service/` or `web/` calls `load_run` or `load_drafts`. The record the UI
    renders is `closes/{company}::{period}`, which is keyed by period and is
    replaced by any re-close regardless of run id -- and after a correction it
    holds the corrected books, which are the ones a reader wants. That is why
    this is a trail-integrity gap and not a wrong-books defect.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    callers = []
    for folder in ("src", "service", "web"):
        for path in (root / folder).rglob("*"):
            if path.suffix not in {".py", ".js", ".html"} or not path.is_file():
                continue
            if path.name == "store.py":          # where they are defined
                continue
            if re.search(r"load_run|load_drafts", path.read_text(encoding="utf-8")):
                callers.append(str(path.relative_to(root)))

    assert callers == [], f"something does read them back: {callers}"

    store = WitnessStore()
    documents, raw = read_period(PERIOD)
    result = run_close(period=PERIOD, documents=documents,
                       company=service.COMPANY, store=store,
                       clock=FixedClock(), raw_texts=raw)
    # The books a reader sees are keyed by period, not by run.
    assert store.load_close(service.COMPANY, PERIOD)["run_id"] == result.run_id
