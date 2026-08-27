"""Independent attempt to REFUTE the section-5 intake-skipping claim. It failed.

NOTE ON THE FILENAME: this file is named `test_refute_*` because refuting was
the assignment, not because it refutes anything. It did not. Test 1 below began
as a REPRODUCTION and failed on the code as it stood then, the same way the
`test_repro_*` files in this directory do. Read the name as the question that
was asked, not as the answer that came back.

The defect it reproduced has since been fixed, and every test here now asserts
the fixed behaviour. The reasoning is kept, in the past tense, because the
reasoning is what stops the defect coming back.

WHAT WAS WRONG: `read_gcs_period` filtered an object out of the month before it
could become a `Document`, on a name that was not `.txt`, on a size over
`MAX_OBJECT_BYTES`, and on a `UnicodeDecodeError`. Everything downstream was
handed only the survivors, so no gate could see the object that never arrived
and a month closed green with a remittance sitting unread in the bucket. Now
every BLOCKING skip comes back as a `DocType.UNKNOWN` document carrying
`failure_reason="not read from the mailbox: ..."`, G6 refuses the month and
names the file, and the one skip that is not an unaccounted artifact -- bytes
identical to an object already read -- is marked non-blocking and lets the
month close.

The other agent's repro used three dropped objects at once, one of which was a
1 MB block of `x` bytes it built by hand. That invited the obvious objection:
the failure was manufactured out of an input no bookkeeper would ever upload.

So this file throws that objection at the claim as hard as it can. It removes
the PDF, removes the hand-built oversize filler, and keeps exactly ONE dropped
object -- a perfectly ordinary broker remittance, `.txt`, 400-odd bytes, well
under the cap, whose text the deterministic extractor parses without complaint.
The only thing wrong with it is that it was saved as UTF-16, which is what
Windows Notepad's "Unicode" option and Windows PowerShell 5.1's `Out-File`
produce by default.

The test is a differential: the SAME month, the SAME characters, the SAME real
`read_gcs_period`, the SAME real `POST /events` route, run twice, differing
only in the text encoding of one object. The encoding still moves the books,
because one run has a remittance in it and the other does not. What changed is
that the run missing it is now refused by name instead of being handed over as
a finished month with every gate green.

Fakes are the repository's own: `FakeBlob`/`FakeClient` here are the same shape
as `tests/integration/test_gcs_ingestion.py`, which is the project's
established stand-in for `google-cloud-storage` (the library is deliberately
not a test dependency, and `read_gcs_period` takes an injected client for
exactly this reason).
"""
from __future__ import annotations

import base64
import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from archon.adapters import auth, gcs, service  # noqa: E402
from archon.adapters.store import LocalStore  # noqa: E402
from archon.domain.models import DocType  # noqa: E402

PERIOD = "2026-07"
BUCKET = "bell-ridge-mail"

LOAD_OBJ = "load-L-9001.txt"
REMIT_OBJ = "remittance-MFX-RA-9001.txt"
BANK_OBJ = "bank-2026-07-24.txt"


# ── the two documents, in the corpus's own label-block format ────────────────

LOAD = """MIDWEST FREIGHT EXCHANGE
RATE CONFIRMATION

Document Type: Load Confirmation
Load Number: L-9001
Date: 2026-07-10
Broker: Midwest Freight Exchange
Carrier Unit: T-1
Miles: 500
Linehaul Rate: 1,000.00
Accessorial: 0.00
Total Payable: 1,000.00
"""

#: The broker paid it. 1,000.00 gross, 30.00 factoring fee, 970.00 credited.
REMITTANCE = """MIDWEST FREIGHT EXCHANGE
REMITTANCE ADVICE

Document Type: Broker Remittance
Remittance Number: MFX-RA-9001
Date: 2026-07-24
Broker: Midwest Freight Exchange
Loads Settled: 1
Factoring Fee: 30.00
Amount Credited: 970.00

LOAD LINES
Load L-9001  Gross 1,000.00  Deduction 0.00  Reason -
"""


# ── the repository's own fake bucket ─────────────────────────────────────────

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


def obj(name: str, data: bytes, generation: int = 1) -> FakeBlob:
    return FakeBlob(f"mail/{PERIOD}/{name}", data, generation)


def envelope(name: str, generation: str = "1", message_id: str = "m-1") -> dict:
    """The push envelope a `google_storage_notification` actually delivers."""
    path = f"mail/{PERIOD}/{name}"
    return {"message": {
        "messageId": message_id,
        "attributes": {"bucketId": BUCKET, "objectId": path,
                       "objectGeneration": generation,
                       "eventType": "OBJECT_FINALIZE"},
        "data": base64.b64encode(
            json.dumps({"bucket": BUCKET, "name": path}).encode()).decode(),
    }}


@pytest.fixture
def rig(monkeypatch):
    """The real app, with the injected storage client swapped for a fake.

    Nothing else is replaced. `read_gcs_period` is the real function; the
    wrapper only supplies the `client=` argument the function already takes.
    """
    monkeypatch.delenv(auth.AUDIENCE_ENV, raising=False)
    monkeypatch.delenv(auth.CALLER_ENV, raising=False)
    monkeypatch.setattr(service, "USE_AGENT", False)

    state: dict = {}

    def use(blobs):
        """Point intake at this bucket and hand back a fresh durable store."""
        durable = LocalStore()
        client = FakeClient(blobs)
        state["store"] = durable
        monkeypatch.setattr(service, "get_store", lambda: durable)
        real = gcs.read_gcs_period
        monkeypatch.setattr(
            service.gcs, "read_gcs_period",
            lambda bucket_name, period, client=None: real(
                bucket_name, period, client=client or FakeClient(blobs)))
        assert client is not None
        return durable

    return use


def close_a_month(use, blobs, trigger: str, generation: str = "1") -> dict:
    """One real POST /events, returning the record the durable store kept."""
    durable = use(blobs)
    response = TestClient(service.app).post(
        "/events", json=envelope(trigger, generation))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "closed", response.json()
    return durable.load_close(service.COMPANY, PERIOD)


def accounting_surfaces(payload: dict) -> str:
    """Everything a person reads as the RESULT of the month, as one string.

    `source` is excluded on purpose: it is the machine-readable provenance
    block, and whether it names the object is a separate question this file
    answers separately, below.
    """
    digest = payload.get("digest") or {}
    return "\n".join([
        json.dumps(payload.get("gates") or []),
        json.dumps(payload.get("findings") or []),
        json.dumps(payload.get("drafts") or []),
        payload.get("summary") or "",
        payload.get("outcome_reason") or "",
        (digest.get("subject") or "") + "\n" + (digest.get("body") or ""),
        json.dumps(payload.get("journal") or {}),
    ])


# ── 1. the refutation attempt: one ordinary object, encoding the only variable ─

def test_one_utf16_remittance_blocks_the_month_and_g6_names_it(rig):
    """Same month twice. Only the text encoding of the remittance differs.

    This is the claim with every arguable input removed. No PDF, no image, no
    hand-built megabyte. One `.txt` object of ~430 bytes carrying a remittance
    the extractor parses correctly -- proven below, because the UTF-8 run
    parses these exact characters -- and the only difference between the two
    runs is `str.encode("utf-16")` versus `str.encode("utf-8")`.

    What used to happen: the two runs produced different books and BOTH
    reported six green gates. Intake was deciding the month's figures and no
    gate could see it. Run A's books said the broker still owed 1,000.00 for
    L-9001 while the remittance settling it was sitting in the mailbox, and
    nothing an owner or a judge reads as the result of the month named the
    object, so there was nothing to go and fix.

    What happens now: the refused object comes back from intake as an UNKNOWN
    document carrying its reason, G6 fails on it by name, and run A is blocked
    instead of closed. The two runs still produce different books -- one of them
    really is missing a remittance, and no fix at intake can invent it -- but
    the month missing it is no longer offered as a finished close.
    """
    load = LOAD.encode("utf-8")

    # Run A: the bookkeeper's Windows tool saved the remittance as UTF-16.
    dropped = close_a_month(
        rig,
        [obj(LOAD_OBJ, load), obj(REMIT_OBJ, REMITTANCE.encode("utf-16"))],
        trigger=REMIT_OBJ)

    # Run B: byte-identical characters, saved as UTF-8 instead.
    read = close_a_month(
        rig,
        [obj(LOAD_OBJ, load), obj(REMIT_OBJ, REMITTANCE.encode("utf-8"))],
        trigger=REMIT_OBJ, generation="2")

    # The one object really was refused, and only that one. `blocking` is the
    # distinction the fix turns on: a refusal that leaves the month incomplete,
    # as against a duplicate of bytes already in the books.
    assert dropped["source"]["objects_read"] == 1
    assert dropped["source"]["objects_skipped"] == 1
    assert dropped["source"]["skipped"][0]["reason"] == "not utf-8"
    assert dropped["source"]["skipped"][0]["blocking"] is True
    assert read["source"]["objects_read"] == 2
    assert read["source"]["objects_skipped"] == 0

    # The encoding alone still moves the books. The broker's payment is in run
    # B and is nowhere in run A.
    assert dropped["allocations"] == []
    assert len(read["allocations"]) == 1
    assert read["allocations"][0]["remittance_total"] == 970.0
    assert dropped["statements"] != read["statements"]

    # So run A is refused, and run B, which read every object, closes clean on
    # all seven gates.
    assert dropped["outcome"] == "blocked"
    assert read["outcome"] == "closed"
    assert [g["passed"] for g in read["gates"]] == [True] * 7

    # G6 is the gate that refuses it, and it is the only one that fails: the
    # books the survivors produced still balance, which is exactly why nothing
    # else could have caught this.
    failed = [g["rule"] for g in dropped["gates"] if not g["passed"]]
    assert len(failed) == 1 and failed[0].startswith("G6"), failed

    # Somewhere an owner or a judge reads as the result of run A names the
    # object, so "which file do I re-save?" has an answer.
    named = REMIT_OBJ in accounting_surfaces(dropped)

    assert named, (
        f"REGRESSION of the section-5 intake defect, on the least arguable "
        f"input available. The same month, the same characters, closed twice "
        f"through the real POST /events. The only difference is that one "
        f"object was saved as UTF-16: {REMIT_OBJ} was refused at intake with "
        f"reason {dropped['source']['skipped'][0]['reason']!r}. Run A came "
        f"back {dropped['outcome']!r} with "
        f"{len(dropped['allocations'])} remittance(s) allocated against run "
        f"B's {len(read['allocations'])}, so its books say the broker still "
        f"owes 1,000.00 for L-9001 when the remittance settling it was sitting "
        f"in the mailbox -- and no gate, finding, draft, summary, outcome "
        f"reason, owner digest or journal step names the object a person has "
        f"to go and fix.")


# ── 2. the G3 sub-claim, on a realistic dropped object ───────────────────────

def bank_line(date: str, direction: str, amount: str, reference: str) -> str:
    """A bank statement line in the corpus's own format."""
    return ("FIRST PLAINS BANK\nACCOUNT ACTIVITY\n\n"
            "Document Type: Bank Transaction\n"
            f"Date: {date}\nDirection: {direction}\nAmount: {amount}\n"
            f"Reference: {reference}\nPaid To: Roadway Fuel Network\n")


def test_g3_still_cannot_see_intake_loss_and_g6_refuses_the_month_anyway(rig):
    """A correction to the audit's stated G3 mechanism, tested rather than argued.

    `validate` calls `g3_bank_movement_agrees(ledger, ledger.documents)`, so
    "the bank lines observed" is derived from the very documents the ledger was
    built from. It is self-referential, and that gives it exactly two
    behaviours in the face of an object intake refused, neither of which is
    detection:

      * drop the ONLY bank line and the gate has no input left, so it reports
        passed-and-skipped and never evaluates. This is what actually happened
        in the other agent's four-object run, whose sole survivor was a load
        confirmation -- so their wording, "a dropped bank line vanishes from
        BOTH sides of its comparison and G3 passes at zero drift", did not
        describe their own scenario.

      * leave a bank line surviving and the gate DOES evaluate, and then the
        wording is right: the dropped line is missing from `observed` and from
        `booked` alike, and the gate agrees with itself at zero drift.

    Both are still true, and both are asserted here, because the fix did not
    touch G3 and could not: a gate that compares the books against the
    documents the books were built from can never see a document that never
    arrived. What used to follow from that was a month that closed green with
    7,281.00 of bank movement nobody had read. What follows now is G6, which
    compares nothing -- intake hands it the refused object as an UNKNOWN
    document, and it refuses the month and names the file. That is the reason
    the fix belongs at intake rather than in the gate.
    """
    load = obj(LOAD_OBJ, LOAD.encode("utf-8"))
    kept = obj("bank-2026-07-20.txt",
               bank_line("2026-07-20", "out", "2,322.00", "INS-2026-07").encode("utf-8"))
    dropped = obj(BANK_OBJ,
                  bank_line("2026-07-24", "out", "7,281.00", "FCN-2026-07"
                            ).encode("utf-16"))

    # (a) the only bank line refused: the gate never runs.
    only = close_a_month(rig, [load, dropped], trigger=BANK_OBJ)
    assert only["source"]["objects_skipped"] == 1
    g3_only = next(g for g in only["gates"] if g["rule"].startswith("G3"))
    assert g3_only["passed"] is True
    assert g3_only["message"].startswith("Skipped:"), g3_only["message"]

    # (b) one bank line survives, so the gate evaluates -- and agrees anyway.
    both = close_a_month(rig, [load, kept, dropped],
                         trigger=BANK_OBJ, generation="2")
    assert both["source"]["objects_read"] == 2
    assert both["source"]["objects_skipped"] == 1
    g3_both = next(g for g in both["gates"] if g["rule"].startswith("G3"))
    assert g3_both["passed"] is True
    assert not g3_both["message"].startswith("Skipped:"), g3_both["message"]
    # Zero drift: the 7,281.00 that intake refused is absent from the books and
    # absent from "what the statements show", so the two agree about a month
    # neither of them saw in full.
    assert "-2,322.00" in g3_both["message"], g3_both["message"]
    assert "7,281" not in g3_both["message"], g3_both["message"]

    # Neither month is closeable, in both cases for the reason G3 could not
    # give: the object whose bytes intake refused, named.
    assert only["outcome"] == both["outcome"] == "blocked"
    for payload in (only, both):
        g6 = next(g for g in payload["gates"] if g["rule"].startswith("G6"))
        assert g6["passed"] is False, g6["message"]
        assert BANK_OBJ in g6["message"], g6["message"]


# ── 3. the half of the audit's wording that never held, and the half that did ─

def test_the_drop_is_recorded_in_provenance_and_carried_into_the_close(rig):
    """The audit's word 'silently' was always wrong. The rest of it was right.

    `event_source` has always persisted `objects_skipped` and a
    `{object, reason}` per dropped object, and `web/app.js` has always rendered
    a count in the origin panel and a `pill warn` row per object in the mailbox
    table. So the drop was recorded. The defect was that the provenance block
    was the ONLY place it appeared: nothing downstream consumed it, and the run
    journal an owner actually scrolls counted just the survivors -- "1
    artifacts" for a mailbox holding two -- with no note that anything had been
    set aside.

    Now the manifest entry also carries `blocking`, and every blocking entry is
    handed to the close as an UNKNOWN document, so one refusal shows up three
    times over: in the intake tally, in the exceptions an owner reads, and in
    the verify step that refuses to present the month.
    """
    payload = close_a_month(
        rig,
        [obj(LOAD_OBJ, LOAD.encode("utf-8")),
         obj(REMIT_OBJ, REMITTANCE.encode("utf-16"))],
        trigger=REMIT_OBJ)

    src = payload["source"]
    assert src["objects_skipped"] == 1
    assert src["skipped"] == [
        {"object": f"mail/{PERIOD}/{REMIT_OBJ}", "reason": "not utf-8",
         "blocking": True}]

    # ... and the intake journal step now counts the refused object as one of
    # the month's artifacts, under the family it was given for being refused.
    intake = next(s for s in payload["journal"]["steps"] if s["name"] == "intake")
    assert intake["status"] == "ok"
    assert intake["detail"].startswith("2 artifacts"), intake["detail"]
    assert "unknown x1" in intake["detail"], intake["detail"]

    # ... the exception list an owner reads names the file and records that
    # nothing was posted from it.
    unrecognised = [f for f in payload["findings"]
                    if f["kind"] == "unrecognised_document"]
    assert [f["source_file"] for f in unrecognised] == [REMIT_OBJ]

    # ... and the step that decides whether the month is presentable says no.
    verify = next(s for s in payload["journal"]["steps"] if s["name"] == "verify")
    assert verify["status"] == "blocked"
    assert "G6" in verify["detail"], verify["detail"]


# ── 4. the dropped object is genuinely parseable: not a malformed input ──────

def test_the_dropped_remittance_parses_perfectly_once_it_is_utf8():
    """Forecloses the 'you fed it garbage' objection at the extractor level.

    The bytes that intake refuses carry a remittance the deterministic parser
    reads completely: right family, right reference, right total, right load
    line. Intake never asks it.
    """
    utf16 = REMITTANCE.encode("utf-16")
    assert len(utf16) < gcs.MAX_OBJECT_BYTES
    with pytest.raises(UnicodeDecodeError):
        utf16.decode("utf-8")
    assert utf16.decode("utf-16") == REMITTANCE

    docs, _raw, manifest = gcs.read_gcs_period(
        BUCKET, PERIOD,
        client=FakeClient([obj(REMIT_OBJ, REMITTANCE.encode("utf-8"))]))

    assert manifest["skipped"] == []
    assert len(docs) == 1
    assert docs[0].doc_type is DocType.BROKER_REMITTANCE
    assert docs[0].document_number == "MFX-RA-9001"
    assert docs[0].remittance_total == 970.0
    assert docs[0].factoring_fee == 30.0
    assert [line.load_ref for line in docs[0].lines] == ["L-9001"]
