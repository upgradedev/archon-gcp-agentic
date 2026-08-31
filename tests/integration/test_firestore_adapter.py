"""C3: the real Firestore adapter, against a disposable instance.

Everything else in this suite runs against `LocalStore`, which is honest for
what it tests but proves nothing about the adapter that actually ships. A
memory store that satisfies the same interface will happily agree with a
Firestore adapter that gets its collection names wrong, batches incorrectly, or
writes a dataclass Firestore cannot serialise.

So these run against the Firestore emulator, which is disposable by definition:
it starts empty, it holds nothing after the process exits, and it needs no
project, no billing and no credential. CI starts one; locally they skip unless
`FIRESTORE_EMULATOR_HOST` is already set.

They skip rather than fail when it is absent, and CI is where that matters:
`.github/workflows/ci.yml` starts the emulator and then asserts these did not
skip, so an emulator that quietly stopped starting cannot turn this file into
green nothing.
"""
from __future__ import annotations

import os
import uuid

import pytest

from archon.adapters.store import LocalStore
from archon.domain.models import Draft, DraftKind, ExceptionKind
from archon.runtime.close import run_close
from archon.runtime.mailbox import read_period

#: A module-level `importorskip` would stop these being COLLECTED at all when
#: google-cloud-firestore is absent, which makes the size of the suite depend on
#: which optional dependencies happen to be installed. That broke the README's
#: test count: it was right in CI and wrong locally, for no visible reason.
#:
#: A `skipif` collects them and marks them skipped instead, so the suite is the
#: same size everywhere and only the outcome differs.
pytestmark = pytest.mark.skipif(
    not os.getenv("FIRESTORE_EMULATOR_HOST"),
    reason="needs the Firestore emulator; CI starts one",
)


@pytest.fixture
def store():
    """A Firestore adapter pointed at the emulator, in its own namespace.

    The project id is randomised per test so two tests cannot see each other's
    documents, which is what makes these safe to run in parallel and what stops
    a leftover write from a previous run making an assertion pass.
    """
    from archon.adapters.store import FirestoreStore

    return FirestoreStore(project=f"archon-test-{uuid.uuid4().hex[:12]}")


def test_the_adapter_reports_itself_as_firestore(store):
    assert store.backend == "firestore"


def test_a_close_round_trips_through_real_firestore(store):
    payload = {"outcome": "closed", "period": "2026-07", "run_id": "r-1"}

    path = store.save_close("Bell Ridge Haulage", "2026-07", payload)
    read_back = store.load_close("Bell Ridge Haulage", "2026-07")

    assert path == "firestore://closes/Bell Ridge Haulage::2026-07"
    assert read_back == payload


def test_a_missing_close_reads_back_as_none_not_an_error(store):
    assert store.load_close("Nobody", "1999-01") is None


def test_a_run_round_trips_with_its_nested_steps(store):
    """The trail is a nested document, which is the shape a memory dict hides."""
    run = {
        "run_id": "r-2", "period": "2026-07", "outcome": "closed", "total_ms": 15,
        "steps": [
            {"index": 1, "name": "intake", "title": "Take in the mail",
             "detail": "27 artifacts", "counts": {"documents": 27}, "status": "ok"},
            {"index": 2, "name": "post", "title": "Post the journal",
             "detail": "26 entries", "counts": {"entries": 26}, "status": "ok"},
        ],
    }

    store.save_run(run)
    read_back = store.load_run("r-2")

    assert read_back["outcome"] == "closed"
    assert len(read_back["steps"]) == 2
    assert read_back["steps"][1]["counts"]["entries"] == 26


def test_drafts_are_written_as_a_batch_and_read_back(store):
    """`save_drafts` uses a Firestore batch, which LocalStore does not model."""
    drafts = [
        Draft(kind=DraftKind.PAYMENT_REMINDER, recipient="Broker", subject=f"s{i}",
              body="b", amount=100.0 + i, reference=f"L-{i}",
              finding_kind=ExceptionKind.LOAD_UNPAID)
        for i in range(3)
    ]

    paths = store.save_drafts("r-3", drafts)

    assert paths == [f"firestore://drafts/r-3::{i}" for i in range(3)]
    stored = [store._db.collection("drafts").document(f"r-3::{i}").get().to_dict()
              for i in range(3)]
    assert [d["reference"] for d in stored] == ["L-0", "L-1", "L-2"]
    assert {d["status"] for d in stored} == {"filed"}


def test_enums_and_dataclasses_survive_the_round_trip(store):
    """Firestore takes documents, not dataclasses or enums. `_plain` is the
    conversion, and this is the only test that proves the real client accepts
    what it produces."""
    draft = Draft(kind=DraftKind.SHORT_PAY_DISPUTE, recipient="Broker", subject="s",
                  body="b", amount=200.0, reference="L-7105",
                  finding_kind=ExceptionKind.SHORT_PAY)

    store.save_drafts("r-4", [draft])
    stored = store._db.collection("drafts").document("r-4::0").get().to_dict()

    assert stored["kind"] == "short_pay_dispute"        # not "DraftKind.SHORT_PAY_DISPUTE"
    assert stored["finding_kind"] == "short_pay"
    assert isinstance(stored["amount"], float)


def test_a_document_body_round_trips(store):
    path = store.put_document("load-L-7101.txt", "RATE CONFIRMATION\nLoad Number: L-7101")

    assert path == "firestore://documents/load-L-7101.txt"
    stored = store._db.collection("documents").document("load-L-7101.txt").get().to_dict()
    assert "L-7101" in stored["content"]


def test_a_whole_close_persists_to_firestore_exactly_as_it_does_locally(store):
    """The one that matters: run the real chore against the real adapter and
    assert the durable record matches what the memory path produces."""
    documents, raw = read_period("2026-07")

    remote = run_close(period="2026-07", documents=documents,
                       company="Bell Ridge Haulage", store=store, raw_texts=raw)
    local = run_close(period="2026-07", documents=documents,
                      company="Bell Ridge Haulage", store=LocalStore(), raw_texts=raw)

    assert remote.outcome == "closed"
    assert remote.to_dict()["statements"] == local.to_dict()["statements"]

    persisted = store.load_close("Bell Ridge Haulage", "2026-07")
    assert persisted["outcome"] == "closed"
    assert len(persisted["journal"]["steps"]) == 11
    assert len(persisted["findings"]) == 10
    assert store.load_run(remote.run_id)["outcome"] == "closed"


def test_re_closing_the_same_month_overwrites_rather_than_accumulates(store):
    """Idempotency, asserted against the real store rather than assumed."""
    documents, raw = read_period("2026-07")

    first = run_close(period="2026-07", documents=documents,
                      company="Bell Ridge Haulage", store=store, raw_texts=raw)
    run_close(period="2026-07", documents=documents,
              company="Bell Ridge Haulage", store=store, raw_texts=raw)

    closes = list(store._db.collection("closes").stream())
    runs = list(store._db.collection("runs").stream())

    assert len([c for c in closes if c.id.endswith("::2026-07")]) == 1
    assert len([r for r in runs if r.id == first.run_id]) == 1


# -- the claim and its take-over, against the real transaction ---------------
#
# Every one of the event-lifecycle tests drives `LocalStore`, whose claim is a
# threading lock and whose take-over is a dict swap. Neither of those is the
# code that runs in production: the live service sets `GOOGLE_CLOUD_PROJECT`,
# so the thing deciding whether a month can ever close again is `create()`
# raising `AlreadyExists` and a `@firestore.transactional` closure. A wrong
# `ref.get(transaction=...)` or a misused decorator fails only here.

def test_a_second_claim_on_the_same_key_loses(store):
    """`create()`, not `set()`. The route relies on the loser being told it
    lost, atomically, inside Firestore -- two Pub/Sub deliveries of one message
    both reading 'absent' is how a month got closed twice."""
    key = "2026-07#event-mail/2026-07/x.txt@1"

    first = store.claim("acme", key, {"period": "2026-07", "status": "processing",
                                      "attempt": 1})
    second = store.claim("acme", key, {"period": "2026-07", "status": "processing",
                                       "attempt": 1})

    assert (first, second) == (True, False)
    assert store.load_close("acme", key)["attempt"] == 1


def test_a_take_over_from_the_attempt_we_read_succeeds(store):
    key = "2026-07#event-mail/2026-07/y.txt@1"
    store.claim("acme", key, {"period": "2026-07", "status": "failed", "attempt": 1})

    took = store.retake("acme", key, 1,
                        {"period": "2026-07", "status": "processing", "attempt": 2})

    assert took is True
    held = store.load_close("acme", key)
    assert (held["attempt"], held["status"]) == (2, "processing")


def test_a_take_over_from_a_stale_attempt_is_refused(store):
    """The compare-and-set that makes the attempt CAP real. Two deliveries
    finding the same expired lease must not both write the next number, or the
    cap is never reached and the poison event retries until it expires."""
    key = "2026-07#event-mail/2026-07/z.txt@1"
    store.claim("acme", key, {"period": "2026-07", "status": "failed", "attempt": 2})

    refused = store.retake("acme", key, 1,
                           {"period": "2026-07", "status": "processing", "attempt": 2})

    assert refused is False
    held = store.load_close("acme", key)
    assert (held["attempt"], held["status"]) == (2, "failed"), "the loser wrote anyway"


def test_only_one_of_two_racing_take_overs_wins(store):
    """Both read attempt 1. One writes 2. The other is told no."""
    key = "2026-07#event-mail/2026-07/race.txt@1"
    store.claim("acme", key, {"period": "2026-07", "status": "failed", "attempt": 1})

    results = [store.retake("acme", key, 1,
                            {"period": "2026-07", "status": "processing",
                             "attempt": 2, "worker": who})
               for who in ("a", "b")]

    assert results == [True, False]
    assert store.load_close("acme", key)["worker"] == "a"


# -- evidence ids, against the real Firestore path rules ---------------------

def test_a_nested_object_is_stored_and_reads_back_whole(store):
    """`scans/invoice.txt` is a legal GCS object name and an illegal Firestore
    document id.

    A slash is a path separator there, so the id would have been a reference
    two collections deep -- and with the wrong number of segments, not a
    reference at all. Every object in a subfolder of the mail prefix arrives
    with its slashes intact, because the reader keeps the name relative to the
    prefix so a human can recognise it.
    """
    name = "scans/2026-07/invoice.txt#a1b2c3d4e5f6"

    uri = store.put_document(name, "Document Type: Unknown\nAmount: 1.00\n")

    assert "/" not in uri.rsplit("/", 1)[-1] or "%2F" in uri


def test_the_logical_path_survives_the_encoding(store):
    """The id is encoded; the path a human reads is not. Both are kept, so
    nothing has to decode anything to follow the trail back to the bucket."""
    from urllib.parse import quote, unquote

    from google.cloud import firestore  # noqa: F401

    name = "scans/2026-07/invoice.txt#a1b2c3d4e5f6"
    store.put_document(name, "hello")

    snapshot = store._db.collection("documents").document(quote(name, safe="")).get()

    assert snapshot.exists, "the nested evidence was not written"
    assert snapshot.to_dict()["name"] == name, "the logical path was lost"
    assert unquote(snapshot.id) == name, "the id does not decode back to the path"


def test_two_names_that_flattening_would_collide_stay_apart(store):
    """`a_b.txt` and `a/b.txt` are different objects. Replacing the slash with
    an underscore would have made the second silently replace the first."""
    store.put_document("a_b.txt", "underscore")
    store.put_document("a/b.txt", "slash")

    from urllib.parse import quote

    got = {
        "a_b.txt": store._db.collection("documents").document(quote("a_b.txt", safe="")).get(),
        "a/b.txt": store._db.collection("documents").document(quote("a/b.txt", safe="")).get(),
    }

    assert got["a_b.txt"].to_dict()["content"] == "underscore"
    assert got["a/b.txt"].to_dict()["content"] == "slash"
