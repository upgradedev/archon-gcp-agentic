"""Reproduction of the audit's section-4 claim about run identity, now fixed.

The claim was that `run_id_for()` was derived from the period, the filename and
the document type only, so the same filename carrying *different bytes*
produced the same run id, and a prior audit trail could be overwritten.

It reproduced, and the whole of the evidence was nine lines of
`src/archon/runtime/close.py`::

    digest = hashlib.sha256()
    digest.update(period.encode())
    for doc in documents:
        digest.update((doc.source_file or "").encode())
        digest.update(str(doc.doc_type.value).encode())
    return f"{period}-{digest.hexdigest()[:10]}"

The id tracked the *set of (filename, document family)* in a period. Nothing
else about a document reached the hash: not an amount, not a date, not a load
reference, not a broker. Its own docstring claimed otherwise -- "Change one
document and the id changes, which is correct, because it is a different
month" -- and that was the sentence these tests contradicted.

`run_id_for` is content-addressed now. What goes into the hash is what a reader
would have to agree on before calling two runs the same run: the period, the
release that read the mail, and every document identified by the sha256 of its
bytes where they exist and by its figures where they do not. The fingerprints
are sorted, so the order the mailbox happened to hand the month over is not
part of its identity, and two files that share a name and a family no longer
tie. Step 1 archives raw text under `documents/{name}#{sha[:12]}`, so the
archived copy of what a close actually posted from cannot be replaced either.

Why it mattered beyond a naming smell, and each link is driven here rather
than argued:

1. A bookkeeper re-uploading a corrected document is a *new generation* of the
   same object, and `gcs.dedupe_key` deliberately keys on object+generation so
   that "an overwrite of the same object (a new generation) is a genuinely new
   event that should close the month again". The second close really runs.
2. The corrected month had the same filenames and the same families, so
   `run_id_for` handed it the id the first close had already used.
3. `store.save_run` is an unconditional write -- a dict assignment in
   `LocalStore`, a Firestore `.set()` in `FirestoreStore` -- so `runs/{run_id}`
   and `drafts/{run_id}::{n}` were the first close's trail until the moment the
   second close landed on top of them.

Two collisions are kept apart on purpose. `closes/{company}::{period}` being
overwritten was *not* this defect: that record is keyed by period and would be
replaced by a re-close whatever the run id were. The loss that belonged to run
identity was `runs/{run_id}` and `drafts/{run_id}::{n}`, which are the only
record of what the earlier close saw and what it filed.

Everything is offline: the bundled corpus, an injected fake storage client,
the in-memory store, and a `FixedClock` wherever two records are compared, so
no assertion here can be answered with "of course it changed, time passed".
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

PERIOD = "2026-07"
NEXT_PERIOD = "2026-08"
BUCKET = "bell-ridge-mail"
COMPANY = "Bell Ridge Haulage"

#: One real document out of the bundled month, and one figure inside it. A
#: rate confirmation whose linehaul was typed as 2,450.00 and is corrected to
#: 9,450.00 is the most ordinary month-end correction there is.
AMENDED = "load-L-7101.txt"
ORIGINAL_RATE = "2,450.00"
CORRECTED_RATE = "9,450.00"


def corrected_text(raw: dict[str, str]) -> str:
    """The same document, one figure corrected. Same name, different bytes."""
    text = raw[AMENDED]
    assert ORIGINAL_RATE in text, "the corpus document changed under this test"
    return text.replace(ORIGINAL_RATE, CORRECTED_RATE)


def amend(documents: list, raw: dict[str, str]) -> tuple[list, dict[str, str]]:
    """The month with that one document corrected, structured and raw.

    The replacement is re-extracted through the real extractor, and the test
    that uses it asserts the controls: same `source_file`, same `doc_type`, a
    figure that genuinely moved. Without those controls a differing run id
    could be an artefact of the extractor picking a different family rather
    than evidence about content.
    """
    index = [d.source_file for d in documents].index(AMENDED)
    replacement = extract_document(corrected_text(raw), source_file=AMENDED,
                                   period=PERIOD)
    amended = list(documents)
    amended[index] = replacement
    raw_amended = dict(raw)
    raw_amended[AMENDED] = corrected_text(raw)
    return amended, raw_amended


class RecordingStore(LocalStore):
    """The real store, with a note of every key the close asked it to use.

    Subclassed rather than reaching into `_documents`, so what is asserted is
    the key the close *chose* through the published interface.
    """

    def __init__(self) -> None:
        super().__init__()
        self.document_keys: list[str] = []

    def put_document(self, name: str, content: str) -> str:
        self.document_keys.append(name)
        return super().put_document(name, content)


def counts_for(run_record: dict, step_name: str) -> dict:
    """The machine-readable counts one step of a persisted trail recorded."""
    for step in run_record["steps"]:
        if step["name"] == step_name:
            return step["counts"]
    raise AssertionError(f"no {step_name} step in the persisted trail")


# -- 1. what the id is computed from ------------------------------------------

def test_the_run_id_moves_with_the_bytes_of_a_document():
    """Same filename, same family, a different figure inside: a different id.

    This is the claim at its narrowest, with every other variable pinned. The
    only thing that differs between the two lists is the content of one
    document, and the id is expected to move with it. It did not: the old
    derivation saw a filename and a family and stopped there.

    Both branches of the derivation are driven, because a caller can reach
    either. Without `raw_texts` a document is identified by its figures, which
    is what an injected or hand-built document gets; with them it is identified
    by the sha256 of the text the mailbox actually read. The figures branch is
    the weaker of the two and the one a fix could quietly leave behind, so it
    is asserted first and on its own.
    """
    documents, raw = read_period(PERIOD)
    index = [d.source_file for d in documents].index(AMENDED)
    original = documents[index]
    amended, raw_amended = amend(documents, raw)
    replacement = amended[index]

    # The controls. Only the bytes moved.
    assert replacement.source_file == original.source_file == AMENDED
    assert replacement.doc_type is original.doc_type
    assert (original.net_amount, replacement.net_amount) == (2450.0, 9450.0), (
        "the correction has to move a real figure or this proves nothing")
    assert len(amended) == len(documents)

    before, after = run_id_for(PERIOD, documents), run_id_for(PERIOD, amended)
    assert after != before, (
        f"a rate confirmation of {original.net_amount:,.2f} and a corrected one "
        f"of {replacement.net_amount:,.2f} were both filed as run {before}; "
        f"run_id_for is back to hashing period + source_file + doc_type and "
        f"never reading the document")

    # And the branch a real mailbox takes, where the bytes are on hand and the
    # figures never have to stand in for them.
    with_bytes = run_id_for(PERIOD, documents, raw_texts=raw)
    corrected = run_id_for(PERIOD, amended, raw_texts=raw_amended)
    assert corrected != with_bytes, (
        "the sha of the raw text is part of a document's fingerprint, so a "
        "corrected file arriving under the same name cannot inherit the first "
        "run's id")


def test_two_unrelated_documents_under_one_filename_get_different_ids():
    """The collision reduced to its smallest form, with no corpus involved.

    Two entirely unrelated load confirmations -- different broker, different
    load reference, different money, different date -- used to collide for no
    better reason than having arrived under the same filename. "attachment.txt"
    is not a contrivance; it is what a mail gateway calls the one file a broker
    attached, so the collision was reachable from ordinary mail.
    """
    from tests.conftest import load

    one = load("L-1000", 1000.0, broker="Broker One", date="2026-07-02")
    other = load("L-9999", 88_000.0, broker="Broker Two", date="2026-07-29")
    one.source_file = other.source_file = "attachment.txt"

    assert run_id_for(PERIOD, [one]) != run_id_for(PERIOD, [other]), (
        "two different loads filed under one attachment name share a run id")


# -- 2. the property the derivation was built for, which held throughout ------

def test_the_same_month_closed_twice_keeps_one_run_id():
    """The intended behaviour, which passed before the fix and passes after it.

    Re-reading the identical mail and closing it again produces the same id,
    which is what keeps a redelivered trigger from littering the trail with
    near-identical runs. This half of the design always worked; it was the
    content blindness underneath it that did not.

    Pinned here because the cheap fix for that blindness is a random or a
    clock-stamped id, and that would buy uniqueness by throwing away
    idempotence. A derived id has to keep both, so both are asserted. The
    release is folded into the hash as well, so this is idempotence within a
    release: a deploy is allowed to re-close a month under a new id, because
    the code that read the mail is part of what produced the answer.
    """
    first, _ = read_period(PERIOD)
    second, _ = read_period(PERIOD)

    assert run_id_for(PERIOD, first) == run_id_for(PERIOD, second)
    assert run_id_for(PERIOD, first) != run_id_for(NEXT_PERIOD, first), (
        "the period is in the hash, so two months never share an id")
    assert run_id_for(PERIOD, first) != run_id_for(PERIOD, first[:-1]), (
        "adding or losing a document does change the id")


# -- 3. the trail the second close no longer lands on top of ------------------

def test_a_corrected_document_files_a_second_journal_beside_the_first():
    """Driven through `run_close`, both runs against one store, one clock.

    The clock is fixed for both, so the only thing that can differ between the
    persisted trails is what the close actually saw. That control is what made
    the original failure unarguable: the two closes hashed to one id, and
    `save_run` is an unconditional keyed write, so the second trail landed on
    top of the first. The first close's journal was not under another key;
    there was no other key.
    """
    store = RecordingStore()
    documents, raw = read_period(PERIOD)

    first = run_close(period=PERIOD, documents=documents, company=COMPANY,
                      store=store, clock=FixedClock(), raw_texts=raw)
    filed = copy.deepcopy(store.load_run(first.run_id))
    assert filed is not None and counts_for(filed, "intake")["documents"] == 27

    amended, raw_amended = amend(documents, raw)
    second = run_close(period=PERIOD, documents=amended, company=COMPANY,
                       store=store, clock=FixedClock(), raw_texts=raw_amended)

    # The premise, stated so a reader can see the two closes were genuinely
    # different pieces of work and are entitled to two identities.
    assert second.statements.revenue != first.statements.revenue
    assert second.run_id != first.run_id, (
        f"both closes filed under run {first.run_id} while revenue moved "
        f"{first.statements.revenue:,.2f} -> {second.statements.revenue:,.2f}")
    assert len(store._runs) == 2

    surviving = store.load_run(first.run_id)
    assert surviving == filed, (
        f"run {first.run_id} was filed twice. The trail now stored under it is "
        f"the second close's: the triage step went from "
        f"{counts_for(filed, 'triage')} to "
        f"{counts_for(surviving, 'triage')}. A month that was closed twice has "
        f"to leave two readable journals, or the earlier one is a thing the "
        f"product says it kept and did not")


def test_a_corrected_document_leaves_the_first_runs_corrective_drafts_alone():
    """The same overwrite, in the record that carries money.

    `save_drafts(run_id, drafts)` keys on the run id alone, so while two closes
    shared an id the corrective documents the first one wrote and filed unsent
    were replaced by the second's. What a human would have to act on was
    decided by whichever close ran last. The key is unchanged; it is the id
    that is now honest, which is why this is asserted through `save_drafts`
    rather than through `run_id_for`.
    """
    store = RecordingStore()
    documents, raw = read_period(PERIOD)

    first = run_close(period=PERIOD, documents=documents, company=COMPANY,
                      store=store, clock=FixedClock(), raw_texts=raw)
    filed = copy.deepcopy(store.load_drafts(first.run_id))
    assert filed, "the first close filed corrective drafts"

    amended, raw_amended = amend(documents, raw)
    second = run_close(period=PERIOD, documents=amended, company=COMPANY,
                       store=store, clock=FixedClock(), raw_texts=raw_amended)

    assert store.load_drafts(first.run_id) == filed, (
        f"the {len(filed)} draft(s) filed under {first.run_id} by the first "
        f"close were replaced by the {len(second.drafts)} the second close "
        f"wrote, under the same key")


# -- 4. the raw artifacts, no longer keyed by filename alone ------------------

def test_two_different_source_objects_are_stored_under_two_document_keys():
    """`documents/{name}` used to carry no period, bucket, company or content.

    Step 1 of the close does ``store.put_document(key, text)`` from the keys of
    `raw_texts` exactly as the mailbox produced them, and both mailboxes strip
    the location: `mailbox.read_period` uses `path.name`, and
    `gcs.read_gcs_period` uses `blob.name[len(prefix):]` with the prefix
    ``mail/<period>/``. Two objects at different Cloud Storage paths, with
    different bytes, therefore landed on the same document, and the archived
    copy of the text the first close posted from became the second's.

    The key is now ``{name}#{sha256(text)[:12]}``, so what an artifact is filed
    under is decided by what is in it. The name is still the front of the key
    because a human reading the store has to recognise the file, and that is
    asserted here too: a content hash nobody can match to a document is a
    different kind of unreadable trail.

    Driven through the real GCS reader so the keys are the ones production
    computes. The bundled corpus happens to embed dates in its filenames,
    which is why the two shipped months never collided even before the fix --
    an accident of a naming convention, not a property of the store.
    """
    _, raw = read_period(PERIOD)
    bucket = NamedBucket()
    july = bucket.upload(PERIOD, AMENDED, raw[AMENDED].encode())
    august = bucket.upload(NEXT_PERIOD, AMENDED, corrected_text(raw).encode())
    assert july.name != august.name                     # different objects
    assert july.download_as_bytes() != august.download_as_bytes()

    july_docs, july_raw, _ = gcs.read_gcs_period(BUCKET, PERIOD, client=bucket)
    aug_docs, aug_raw, _ = gcs.read_gcs_period(BUCKET, NEXT_PERIOD, client=bucket)
    assert list(july_raw) == list(aug_raw) == [AMENDED]  # the premise

    store = RecordingStore()
    run_close(period=PERIOD, documents=july_docs, company=COMPANY, store=store,
              clock=FixedClock(), raw_texts=july_raw)
    run_close(period=NEXT_PERIOD, documents=aug_docs, company=COMPANY,
              store=store, clock=FixedClock(), raw_texts=aug_raw)

    assert len(set(store.document_keys)) == 2, (
        f"two objects at gs://{BUCKET}/{july.name} and gs://{BUCKET}/"
        f"{august.name} were both filed as documents/{store.document_keys[0]}; "
        f"the archived copy of the first is the text of the second")
    assert all(k.startswith(f"{AMENDED}#") for k in store.document_keys), (
        f"the artifact keys {store.document_keys} no longer begin with the "
        f"filename the mailbox read, so a human looking for {AMENDED} in the "
        f"store has nothing to look for")


# -- 5. end to end, through the real trigger ----------------------------------

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from archon.adapters import auth, service  # noqa: E402


class FakeBlob:
    """One object in the fake bucket."""

    def __init__(self, name: str, data: bytes, generation: int):
        self.name = name
        self._data = data
        self.size = len(data)
        self.generation = generation

    def download_as_bytes(self) -> bytes:
        return self._data


class NamedBucket:
    """A bucket keyed by object name, the way Cloud Storage behaves.

    One live generation per name. Re-uploading a name supersedes the previous
    object rather than adding a second one beside it -- which matters, because
    a bucket that returned both generations from `list_blobs` would make
    `read_gcs_period` build *two* documents where production builds one, the
    document count would change, and the run id would then differ for a reason
    that has nothing to do with the claim under test.
    """

    def __init__(self, name: str = BUCKET) -> None:
        self.name = name
        self._objects: dict[str, FakeBlob] = {}

    def upload(self, period: str, name: str, data: bytes) -> FakeBlob:
        key = f"mail/{period}/{name}"
        prior = self._objects.get(key)
        blob = FakeBlob(key, data, (prior.generation + 1) if prior else 1)
        self._objects[key] = blob
        return blob

    def object(self, period: str, name: str) -> FakeBlob:
        return self._objects[f"mail/{period}/{name}"]

    # the google-cloud-storage surface `gcs.read_gcs_period` uses
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
    """The real app and one durable store, with the storage client injected."""
    monkeypatch.delenv(auth.AUDIENCE_ENV, raising=False)      # the open posture
    monkeypatch.delenv(auth.CALLER_ENV, raising=False)

    durable = LocalStore()
    monkeypatch.setattr(service, "get_store", lambda: durable)

    bucket = NamedBucket()
    real_read = gcs.read_gcs_period
    monkeypatch.setattr(
        service.gcs, "read_gcs_period",
        lambda bucket_name, period, client=None: real_read(
            bucket_name, period, client=bucket),
    )
    return service.app, durable, bucket


def post(app, envelope):
    """One Pub/Sub push, through the real route, middleware included."""
    return TestClient(app).post("/events", json=envelope)


def test_reuploading_a_corrected_document_files_beside_the_first_runs_trail(rig):
    """The whole chain, from a bookkeeper's second upload to two kept trails.

    A month lands in the bucket and closes. The bookkeeper then notices the
    rate on one confirmation is wrong, corrects it and re-uploads it under the
    same name. That is generation 2 of the same object, so the dedupe marker
    (`{period}#event-{object}@{generation}`) misses by design and the month is
    closed again -- correctly, the books have changed. What was not correct was
    that the second close was handed the first close's identity, and this is
    the test that walks there without help: no `run_id_for` call, no injected
    document list, just two Pub/Sub pushes at the real route.
    """
    app, durable, bucket = rig
    _, raw = read_period(PERIOD)
    for name, text in raw.items():
        bucket.upload(PERIOD, name, text.encode())

    trigger = bucket.object(PERIOD, AMENDED)
    assert trigger.generation == 1
    first = post(app, finalize(trigger, "m-1")).json()
    assert first["status"] == "closed", first

    first_id = first["run_id"]
    first_trail = copy.deepcopy(durable.load_run(first_id))
    first_drafts = copy.deepcopy(durable.load_drafts(first_id))
    first_books = copy.deepcopy(durable.load_close(COMPANY, PERIOD))
    assert first_drafts, "the first close filed corrective drafts"
    assert counts_for(first_trail, "intake")["documents"] == len(raw)

    amended_object = bucket.upload(PERIOD, AMENDED, corrected_text(raw).encode())
    assert amended_object.generation == 2       # the same object, new bytes
    second = post(app, finalize(amended_object, "m-2")).json()
    assert second["status"] == "closed", (
        "a new generation of an object is meant to close the month again")

    # The books really did move, so the two closes are not the same work.
    second_books = durable.load_close(COMPANY, PERIOD)
    assert second_books["statements"]["revenue"] != first_books["statements"]["revenue"]

    assert second["run_id"] != first_id, (
        f"both closes filed under run {first_id}. The stored journal for that "
        f"id now reports "
        f"{counts_for(durable.load_run(first_id), 'draft')} where the first "
        f"close reported {counts_for(first_trail, 'draft')}, and the "
        f"{len(durable.load_drafts(first_id))} draft(s) under that id are the "
        f"second close's. Nothing that was filed the first time is still "
        f"readable: revenue went "
        f"{first_books['statements']['revenue']:,.2f} -> "
        f"{second_books['statements']['revenue']:,.2f} and there is one run "
        f"record, not two")

    # The point of two identities, said in the terms the product sells: the
    # earlier close is still there to walk back through, byte for byte.
    assert durable.load_run(first_id) == first_trail, (
        f"run {first_id} is still the key the first close filed under, but "
        f"what is stored there is no longer what it filed")
    assert durable.load_drafts(first_id) == first_drafts, (
        f"the {len(first_drafts)} corrective draft(s) filed under {first_id} "
        f"by the first close are not the ones now under that key, so what a "
        f"human was asked to act on is whatever ran last")
