"""Read a month of mail off Cloud Storage, and prove which bytes were read.

The gap this closes: `/events` used to take only the *period* out of the
notification and re-read the bundled corpus, so the object a bookkeeper
actually uploaded was never opened. The trigger was real; the ingestion was
not. Now the close that a notification starts is built from the exact objects
under `mail/<period>/` in the bucket the event names, and the persisted record
carries a manifest of what was read: name, generation, size and sha256 per
object. A judge can hash the object and match the books to the bytes.

Design constraints, each earned:

- **Content-hash dedupe.** Pub/Sub is at-least-once and people re-upload the
  same document under a new name. Two objects with identical bytes are one
  artifact, and counting them twice would invent a duplicate remittance and
  change the books. First name in sorted order wins; the manifest records what
  was folded away rather than hiding it.
- **A size cap, refused loudly.** A 200 MB object is not a month-end document,
  and downloading it inside a push handler is how the trigger gets taken down.
  Oversize objects are skipped and named in the manifest.
- **Text only.** The extractor reads text. An object that does not decode, or
  is not `.txt`, is recorded as skipped rather than guessed at, the same
  honesty rule the unreadable-document detector applies.
- **The client is injected.** `google-cloud-storage` is imported inside the
  function, so the suite runs on machines that do not have it installed and
  every test drives a fake. The container has the real one.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re

from ..domain.extract import extract_document
from ..domain.models import DocType, Document

log = logging.getLogger("archon.gcs")

#: Nothing in a month's mail is this large. A fuel card statement is ~2 KB.
MAX_OBJECT_BYTES = 1_000_000


#: What a marker may be called, beyond the configured name itself. A marker
#: cannot always be overwritten -- on this project's own bucket the owner holds
#: bucket-level roles only, so `cp` over an existing object returns 403 and
#: re-closing a month needs a NEW object -- so a suffix has to be allowed. What
#: is allowed is a suffix of digits, dashes, underscores or dots: `_READY2`,
#: `_READY-2026-08-28`, `_READY.3`.
_MARKER_SUFFIX = re.compile(r"^[0-9._-]*$")


def is_marker(name: str, marker: str) -> bool:
    """Is this object the batch-complete signal rather than mail?

    Deliberately NOT "starts with an underscore". That was the rule for one
    commit and it is a convention, not a guarantee: an export that names files
    `_invoice.txt` would have had every one of them silently dropped as a
    control object, non-blocking, so the month closed green over a purchase
    invoice nobody opened. Fail-closed intake exists to stop exactly that, and
    the widened rule walked straight through it.

    So: the configured marker, or the configured marker followed by a suffix
    that could not be a filename anybody means. `_READY2` counts. `_READY.txt`
    does not, because a `.txt` is mail.
    """
    if not marker:
        return False
    lowered, wanted = name.lower(), marker.lower()
    if lowered == wanted:
        return True
    if not lowered.startswith(wanted):
        return False
    return bool(_MARKER_SUFFIX.match(name[len(marker):]))


def read_gcs_period(bucket_name: str, period: str, client=None,
                    ) -> tuple[list[Document], dict[str, str], dict]:
    """Every artifact under mail/<period>/ in the bucket, plus its manifest.

    Returns (documents, raw_texts, manifest). Documents come back in object
    name order, the same rule the local mailbox uses, so a run id computed
    from the same content is the same run id whichever mailbox served it.

    Raises whatever the storage client raises: the caller decides whether a
    listing failure is retried, acknowledged or reported, because only the
    caller knows it is inside a push handler.
    """
    if client is None:
        from google.cloud import storage

        client = storage.Client()

    prefix = f"mail/{period}/"
    # Read here rather than passed in, so the signature stays the two arguments
    # every test double in this suite already wraps.
    marker = os.getenv("ARCHON_BATCH_MARKER", "").strip()
    read: list[dict] = []
    skipped: list[dict] = []
    seen_hashes: dict[str, str] = {}
    documents: list[Document] = []
    raw: dict[str, str] = {}

    # Shortest name first, then alphabetical. When two objects hold identical
    # bytes the first one read wins and the rest are folded away, so this rule
    # decides which name the trail points at.
    #
    # Plain alphabetical picked whichever sorted first, and `-` sorts before
    # `.`, so `remittance-MFX-RA-4417-redelivery-3.txt` beat
    # `remittance-MFX-RA-4417.txt` and the manifest reported the month's actual
    # remittance as a duplicate of an experimental copy of itself. A copy is
    # named after its original and is therefore longer: keeping the shorter
    # name keeps the canonical one, and it needs no knowledge of which object
    # the event named.
    for blob in sorted(client.list_blobs(bucket_name, prefix=prefix),
                       key=lambda b: (len(b.name), b.name)):
        name = blob.name[len(prefix):]
        if not name:                                   # the prefix placeholder
            continue
        if is_marker(name, marker):
            # The batch-complete signal is a CONTROL object, not a document.
            # It has no extension and no content, so the fail-closed intake
            # below read it as an unreadable artifact, G6 refused the month and
            # every batched close came back "blocked". Found on the live
            # service, because it is the interaction of two changes that are
            # each correct on their own.
            skipped.append({"object": blob.name, "blocking": False,
                            "reason": "batch-complete marker, not a document"})
            continue
        # Case-folded, because the extension is the bookkeeper's typing and not
        # a protocol. `INVOICE.TXT` off a Windows scanner is a text file; the
        # exact-case test called it "not text" and blocked the month over it.
        # Blocking rather than dropping meant it was never silently lost, which
        # is why this reads as a nuisance rather than a hole -- but a month
        # refused over a capital letter is still a month refused.
        if not name.lower().endswith(".txt"):
            skipped.append({"object": blob.name, "reason": "not text",
                            "blocking": True})
            continue
        if (blob.size or 0) > MAX_OBJECT_BYTES:
            skipped.append({"object": blob.name, "reason":
                            f"{blob.size} bytes is over the {MAX_OBJECT_BYTES} cap",
                            "blocking": True})
            continue

        data = blob.download_as_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen_hashes:
            # The one skip that is NOT an unaccounted artifact: these exact
            # bytes are already in the books under another name, so the month
            # is complete without it and blocking here would refuse a month
            # over a file someone forwarded twice.
            skipped.append({"object": blob.name, "reason":
                            f"identical bytes already read as {seen_hashes[digest]}",
                            "blocking": False})
            continue
        seen_hashes[digest] = name

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append({"object": blob.name, "reason": "not utf-8",
                            "blocking": True})
            continue

        read.append({"object": blob.name, "generation": str(blob.generation or ""),
                     "bytes": len(data), "sha256": digest})
        raw[name] = text
        documents.append(extract_document(text, source_file=name, period=period))

    # An object that was skipped before it could become a Document is invisible
    # to every gate: G6 counts documents that matched no family, and something
    # that never reached the extractor never became a document at all. So a
    # month could report "every gate passed" with a remittance in the bucket
    # that nobody read, which is the exact silent corruption this product
    # exists to prevent.
    #
    # Each blocking skip becomes an UNKNOWN document carrying its reason. It
    # posts nothing -- `Ledger._post_unknown` sees to that -- and G6 refuses the
    # month and names the file. The owner is told which object and why.
    for entry in skipped:
        if not entry.get("blocking"):
            continue
        documents.append(Document(
            doc_type=DocType.UNKNOWN, period=period,
            source_file=entry["object"][len(prefix):] or entry["object"],
            failure_reason=f"not read from the mailbox: {entry['reason']}",
        ))

    manifest = {"bucket": bucket_name, "prefix": prefix,
                "read": read, "skipped": skipped}
    log.info("gcs mailbox %s%s: %d read, %d skipped",
             bucket_name, prefix, len(read), len(skipped))
    return documents, raw, manifest


def event_source(envelope: dict, period: str, manifest: dict) -> dict:
    """The provenance block persisted with a close the event started.

    Everything a judge needs to tie the books to the trigger: which object
    landed (with its generation, so an overwrite is distinguishable), which
    Pub/Sub message delivered it, and the hash manifest of every object the
    close actually read.
    """
    message = envelope.get("message") or {}
    attributes = message.get("attributes") or {}
    return {
        "mailbox": "gcs",
        # Which BUILD read these bytes. `/api/health` reports the release it is
        # running and separately the release that produced the last close, and
        # the second was reading this field, which nothing wrote: it answered
        # null on the deployed service, which is the same shape as the defect
        # that pair of fields exists to close. A parser fix changes what the
        # same bytes mean, so the build is part of the provenance.
        "release": os.getenv("ARCHON_RELEASE") or None,
        "bucket": manifest["bucket"],
        "period": period,
        "trigger_object": attributes.get("objectId") or "",
        "trigger_generation": attributes.get("objectGeneration") or "",
        "message_id": message.get("messageId") or message.get("message_id") or "",
        "objects_read": len(manifest["read"]),
        "objects_skipped": len(manifest["skipped"]),
        "manifest": manifest["read"],
        "skipped": manifest["skipped"],
    }


def dedupe_key(envelope: dict, period: str) -> str | None:
    """One close per object generation, however many times Pub/Sub delivers.

    Keyed on object + generation rather than message id, because Pub/Sub may
    mint a new message id for the same delivery attempt, and because an
    overwrite of the same object (a new generation) is a genuinely new event
    that should close the month again.
    """
    message = envelope.get("message") or {}
    attributes = message.get("attributes") or {}
    obj = attributes.get("objectId") or ""
    generation = attributes.get("objectGeneration") or ""
    if not obj:
        # A scheduler-style event with only a period set: fall back to the
        # message id so a duplicate push of that exact message is still caught.
        mid = message.get("messageId") or message.get("message_id") or ""
        return f"{period}#event-msg-{mid}" if mid else None
    return f"{period}#event-{obj}@{generation}"
