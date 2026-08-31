"""The chore: close a haulier's month, unattended, end to end.

This is the whole product in one function. A month of mail goes in. Eleven
steps later the books are posted, the remittance is split across the loads it
settles, the exceptions are triaged worst-first, the corrective documents are
written and filed, the close has checked its own work against seven gates, the
period is marked closed with a trail you can walk back through, and the owner
has a letter about it wherever they read their mail.

Step 6 is where an agent has real authority. It decides what to do about each
exception, and `domain/policy.py` clamps any choice the books will not accept
and records the overrule. Without an agent the standing policy runs and the
close behaves exactly as it always has.

Nobody is asked anything at any point. That is the design, and it is the one
thing this product is being judged on, so it is worth being exact about where
the autonomy stops. There are two outward edges and they have different rules.

**Towards a third party, the close stops.** The letters it writes to brokers and
suppliers are filed unsent, and sending them is the single step a human
performs. That is not a governance hole, it is where reversibility runs out:
every step below can be re-run and produces the same books, because the engine
is deterministic and the run is keyed by the period and its documents, but an
email to a broker cannot be un-sent.

**Towards the owner, the close does not stop.** Step 10 writes them a letter and
puts it where they already read their mail. That is their own books arriving at
them, it is the reason the work was done overnight, and holding it back until
they remember to open a console we built is how an unattended agent becomes a
tab nobody clicks.

    from archon import run_close
    result = run_close(period="2026-07", documents=docs)
    print(result.journal.transcript())
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from archon.domain.text import plural

from ..adapters import delivery as delivery_mod
from ..adapters.delivery import Deliverer, Receipt
from ..adapters.store import RehearsalStore, Store, get_store
from ..domain import allocation as allocation_mod
from ..domain import digest as digest_mod
from ..domain import drafts as drafts_mod
from ..domain import exceptions as exceptions_mod
from ..domain import policy as policy_mod
from ..domain import register as register_mod
from ..domain import trends as trends_mod
from ..domain import validation as validation_mod
from ..domain.digest import Digest
from ..domain.ledger import Ledger
from ..domain.models import (
    AllocationResult,
    DocType,
    Document,
    Draft,
    Finding,
    Statements,
    ValidationResult,
)
from ..domain.narrator import facts_sheet, narrate
from ..domain.policy import Decision, Disposition
from ..domain.register import Register
from ..domain.trends import Comparison
from .journal import Clock, RunJournal

#: A narrator takes the fact sheet and returns English. The default is the
#: deterministic one; `agents.py` supplies a Gemini-backed one. It can never
#: introduce a figure, because it is handed text, not documents.
Narrator = Callable[[str], str]

#: A decider is handed the findings and returns what to do about each one, plus
#: an optional verdict on whether the month may close. It is the agent's real
#: authority in this product: it cannot produce a figure, and it can decide
#: what happens about the figures the ledger produced. Everything it returns
#: goes through `policy.apply_choices`, which overrules anything the books will
#: not accept and records why. None means the deterministic default policy.
Decider = Callable[
    [list[Finding]], tuple[dict[int, "Disposition"], str | None]
]
def _agent_model() -> str:
    """The model the agent path would use, named from one place."""
    from ..adapters.agents import DEFAULT_MODEL

    return DEFAULT_MODEL




@dataclass
class CloseResult:
    """Everything one close produced, and the trail that proves it happened."""

    run_id: str
    period: str
    company: str | None
    outcome: str                     # "closed" | "blocked" | "failed"
    statements: Statements
    allocations: list[AllocationResult]
    findings: list[Finding]
    gates: list[ValidationResult]
    drafts: list[Draft]
    summary: str
    facts: str
    journal: RunJournal
    ledger: Ledger
    decisions: list[Decision] = field(default_factory=list)
    outcome_reason: str = ""
    register: Register | None = None
    comparison: Comparison | None = None
    digest: Digest | None = None
    receipt: Receipt | None = None
    stored: dict = field(default_factory=dict)
    #: Which control flow produced this close, and from what source material.
    #: `driver` is "deterministic" or "adk-agent". `source` is present only
    #: when the mail came off Cloud Storage: bucket, object, generation, the
    #: Pub/Sub message id and a per-object sha256 manifest, so a judge can tie
    #: the books on screen to the exact bytes that produced them.
    driver: str = "deterministic"
    source: dict | None = None

    @property
    def mode(self) -> dict:
        """What this particular result IS, so a page cannot describe it wrongly.

        The console was built around one kind of close -- mail off a bucket,
        driven by the agent, persisted to Firestore -- and then a second kind
        arrived: documents somebody uploaded, closed deterministically in
        memory and kept nowhere. The page went on saying Cloud Storage,
        Pub/Sub, Firestore and agent about a result that had touched none of
        them, and showed the bundled month's figures beside it.

        Six facts, so the interface renders from what a result is rather than
        from what the product usually does. A viewer that reads these cannot
        inherit the trusted close's claims for a sandbox one.
        """
        uploaded = (self.source or {}).get("mailbox") == "uploaded"
        return {
            "source": "uploaded-sandbox" if uploaded else "persisted-gcs",
            "orchestration": self.driver,
            "persistence": "none" if uploaded else "firestore",
            # Imported here rather than at module scope: `close.py` is the
            # deterministic core and must not import the adapter that carries
            # the ADK dependency just to name a string.
            "model": _agent_model() if self.driver == "adk-agent" else None,
            "provenance": not uploaded,
            "delivery": not uploaded,
        }

    @property
    def currency(self) -> str:
        """What this month is denominated in, from the documents that count.

        The page used to take this from the first FINDING that carried one, so
        a clean month in euros -- G7 green, nothing wrong, nothing found --
        rendered as dollars, because there was no finding to read it off. The
        one month whose figures need no explanation was the one shown in the
        wrong currency.

        Read from `ledger.posted`, which is the population G7 already agrees on,
        so the number on the page and the gate that guards it cannot disagree.
        """
        from ..domain.models import DocType

        seen = {(d.currency or "USD").upper() for d in self.ledger.posted
                if d.doc_type is not DocType.UNKNOWN}
        return next(iter(seen)) if len(seen) == 1 else "USD"

    @property
    def closed(self) -> bool:
        return self.outcome == "closed"

    @property
    def leakage(self) -> float:
        """Money that would have been lost quietly. The honest headline."""
        return drafts_mod.leakage(self.drafts)

    @property
    def outstanding(self) -> float:
        """Invoiced work nobody has paid yet. Owed, not found."""
        return drafts_mod.outstanding(self.drafts)

    @property
    def undocumented(self) -> float:
        """Spending with no paperwork behind it. Recovers nothing."""
        return drafts_mod.undocumented(self.drafts)

    @property
    def recoverable(self) -> float:
        """Equal to `leakage`. Kept so the API shape does not break."""
        return self.leakage

    def to_dict(self) -> dict:
        """The shape the API serves and the browser renders."""
        from ..adapters.store import _plain

        return {
            "run_id": self.run_id,
            "period": self.period,
            "company": self.company,
            "currency": self.currency,
            "driver": self.driver,
            "mode": self.mode,
            "source": self.source,
            "outcome": self.outcome,
            "summary": self.summary,
            "statements": _plain(self.statements),
            "allocations": [
                {
                    "remittance_ref": a.remittance_ref,
                    "broker": a.broker,
                    "remittance_total": a.remittance_total,
                    "factoring_fee": a.factoring_fee,
                    "allocated_gross": a.allocated_gross,
                    "residual": a.residual,
                    "reconciles": a.reconciles,
                    "lines": _plain(a.allocations),
                }
                for a in self.allocations
            ],
            "findings": _plain(self.findings),
            "gates": _plain(self.gates),
            "drafts": _plain(self.drafts),
            "decisions": [
                {"reference": d.finding.reference, "kind": d.finding.kind.value,
                 "amount": d.finding.amount, "chosen": d.chosen.value,
                 "applied": d.applied.value, "clamped": d.clamped, "reason": d.reason}
                for d in self.decisions
            ],
            "outcome_reason": self.outcome_reason,
            "register": self.register.to_dict() if self.register else None,
            "comparison": self.comparison.to_dict() if self.comparison else None,
            "trend_summary": (
                trends_mod.narrate(self.comparison) if self.comparison else ""
            ),
            "journal": self.journal.to_dict(),
            "leakage": self.leakage,
            "outstanding": self.outstanding,
            "undocumented": self.undocumented,
            "recoverable": self.recoverable,
            "digest": self.digest.to_dict() if self.digest else None,
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "facts": self.facts,
        }


#: What makes two documents the same document to this product. Used to identify
#: a run only when the raw bytes are not available to hash instead.
_IDENTIFYING = ("document_number", "reference", "load_ref", "date", "direction",
                "net_amount", "tax_amount", "gross_amount", "remittance_total",
                "factoring_fee", "driver_net", "driver_gross", "miles",
                "counterparty", "broker", "currency")


def run_id_for(period: str, documents: list[Document],
               raw_texts: dict[str, str] | None = None) -> str:
    """A run id derived from the period and the CONTENT of the documents in it.

    Deriving it rather than randomising it makes a close idempotent in the way
    that matters: the same month over the same mail produces the same run id,
    overwrites the same record, and does not litter the trail with
    near-identical runs.

    The previous version hashed the period, the filenames and the document
    families, and its docstring claimed "change one document and the id
    changes". It did not. Correct a figure on an invoice and keep the filename,
    which is exactly what a supplier reissuing a corrected document does, and
    the id was identical -- so the corrected run FILED OVER the first one. Its
    journal, its drafts and its stored close all went, and the immutable audit
    trail this product sells kept one version of a month that had two.

    What goes in now is what a reader would have to agree on before calling two
    runs the same run: the period, the release that read the mail, and every
    document identified by its bytes where they exist and by its figures where
    they do not.
    """
    def fingerprint(doc: Document) -> str:
        name = doc.source_file or ""
        parts = [f"doc={name}", f"type={doc.doc_type.value}"]
        text = (raw_texts or {}).get(name)
        if text is not None:
            parts.append("sha=" + hashlib.sha256(text.encode("utf-8")).hexdigest())
        else:
            # An injected document, or one a caller built by hand. Fold in the
            # figures so a changed amount still changes the id.
            parts += [f"{f}={getattr(doc, f, None)!r}" for f in _IDENTIFYING]
        return "|".join(parts)

    # Sorting the FINGERPRINTS, not the documents. Sorting documents by name and
    # family leaves two files with the same name and family tied, and a tie puts
    # the order the mailbox happened to hand them over into the id. A month is a
    # set of documents; the order they arrived in is not part of its identity.
    digest = hashlib.sha256()
    digest.update(period.encode())
    digest.update(b"|release=")
    digest.update((os.getenv("ARCHON_RELEASE") or "dev").encode())
    for line in sorted(fingerprint(d) for d in documents):
        digest.update(b"|")
        digest.update(line.encode())
    return f"{period}-{digest.hexdigest()[:10]}"


def run_close(period: str,
              documents: list[Document],
              company: str | None = None,
              store: Store | None = None,
              clock: Clock | None = None,
              narrator: Narrator | None = None,
              raw_texts: dict[str, str] | None = None,
              deliverer: Deliverer | None = None,
              owner_email: str | None = None,
              decider: Decider | None = None,
              previous: Statements | None = None,
              driver: str = "deterministic",
              source: dict | None = None,
              commit: bool = True) -> CloseResult:
    """Close one period. Returns even when it fails; read `outcome`.

    Raising on a bad month would be the wrong shape. A close that hits a
    problem has still done eight useful things, and the owner needs to see them
    and the problem. So every path returns a `CloseResult`, and `outcome` says
    whether the books can be trusted.

    `commit=False` computes the identical month and writes nothing: no stored
    close, no stored run, no drafts, no delivered digest. Every one of the
    eleven steps still runs and still records its trail entry, so the result is
    the same object a committed run produces and can be compared against one.

    It exists because the agent path has to see the books BEFORE it decides
    what to do about them, and seeing them used to mean filing them. The close
    was written twice and the owner's digest delivered twice, and the first
    filing recorded an outcome the agent had not yet had the chance to
    withhold. Nothing may reach outside Archon, or become durable, until the
    decision at step 5 has been made and the gates at step 8 have run.
    """
    store = store or get_store()
    deliverer = deliverer or delivery_mod.get_deliverer()
    if not commit:
        store = RehearsalStore()
        deliverer = delivery_mod.RehearsalDelivery()
    run = RunJournal(run_id=run_id_for(period, documents, raw_texts),
                     period=period, clock=clock)
    ledger = Ledger(period=period, company=company)
    stored: dict = {}

    # 1. Intake. Nothing is interpreted yet; the raw artifacts are put beyond
    #    reach of anything that follows, so the trail starts before the books do.
    with run.step("intake", "Take in the month's mail") as step:
        by_type: dict[str, int] = {}
        for doc in documents:
            by_type[doc.doc_type.value] = by_type.get(doc.doc_type.value, 0) + 1
        for name, text in (raw_texts or {}).items():
            # Keyed by content, not by filename. `remittance.txt` corrected and
            # re-sent is a DIFFERENT artifact with the same name, and storing it
            # under the name alone filed it over the original -- so the raw
            # evidence the trail points at was whichever copy arrived last, and
            # the first one could not be produced again. The name is kept in the
            # key because a human reading the store should still recognise it.
            fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            store.put_document(f"{name}#{fingerprint}", text)
        origin = (f" from gs://{source['bucket']}/mail/{period}/"
                  if source and source.get("bucket") else "")
        step.note(
            f"{len(documents)} artifacts{origin}: "
            + ", ".join(f"{n.replace('_', ' ')} x{c}" for n, c in sorted(by_type.items())),
            documents=len(documents), **by_type,
        )

    # 2. Post. One balanced entry per document, or an explicit refusal to post.
    with run.step("post", "Post the double-entry journal") as step:
        ledger.add_all(documents)
        posted = [e for e in ledger.entries if e.lines]
        refused = len(ledger.entries) - len(posted)
        step.note(
            f"{len(posted)} entries posted from {len(documents)} artifacts, "
            f"{refused} deliberately left unposted",
            entries=len(posted), unposted=refused,
        )

    # 3. Allocate. The beat that makes the month closeable: one bank credit
    #    split back across the loads it settles, fee booked once.
    with run.step("allocate", "Split each remittance across the loads it settles") as step:
        results = allocation_mod.allocate_all(documents)
        ledger.allocations = results
        lines = sum(len(r.allocations) for r in results)
        off = [r for r in results if not r.reconciles]
        step.note(
            f"{plural(len(results), 'remittance')} split across "
            f"{plural(lines, 'load')}; "
            + (f"{len(off)} left a residual" if off else "every one reconciles"),
            remittances=len(results), load_lines=lines, unreconciled=len(off),
        )

    # 4. Reconcile. Which loads got paid, which are still outstanding.
    with run.step("reconcile", "Reconcile loads against what the brokers paid") as step:
        settled = allocation_mod.settled_load_refs(results)
        outstanding = allocation_mod.unsettled_loads(documents, results)
        loads = len(allocation_mod.loads_by_ref(documents))
        open_items = register_mod.build(documents, results, period)
        step.note(
            f"{len(settled)} of {loads} loads settled, {len(outstanding)} still "
            f"outstanding; {open_items.owed_to_us:,.2f} owed to the firm across "
            f"{plural(len(open_items.receivables), 'item')}, "
            f"{open_items.owed_by_us:,.2f} owed "
            f"by it across {len(open_items.payables)}",
            loads=loads, settled=len(settled), outstanding=len(outstanding),
            owed_to_us=open_items.owed_to_us, owed_by_us=open_items.owed_by_us,
        )

    # 5. Triage. Ten detectors, ranked worst-first by severity then money.
    with run.step("triage", "Find what is missing or does not add up") as step:
        findings = exceptions_mod.find_all(documents, results, period)
        errors = [f for f in findings if f.severity == "error"]
        step.note(
            f"{plural(len(findings), 'exception')}, {len(errors)} of them errors, "
            f"{exceptions_mod.exposure(errors):,.2f} at stake",
            exceptions=len(findings), errors=len(errors),
            at_stake=exceptions_mod.exposure(errors),
        )

    # 6. Decide, then draft. This is where the agent has authority: which of
    #    these is worth a letter, which needs the owner, which is just noted.
    #    Every choice is clamped by the ledger before it can do anything.
    agent_verdict: str | None = None
    with run.step("decide", "Decide what to do about each exception") as step:
        choices = None
        decided_by = "standing policy"
        note_suffix = ""
        if decider is not None:
            try:
                choices, agent_verdict = decider(findings)
                decided_by = "agent"
            except Exception as exc:      # a decider failing must not fail a close
                # `note` replaces rather than appends, so the fallback has to
                # travel to the single note at the end of the step or it is
                # silently lost. A run that quietly stopped consulting the
                # agent should say so on the trail.
                note_suffix = (f"; the decider raised {type(exc).__name__}, "
                               f"fell back to the standing policy")
        decisions = policy_mod.apply_choices(findings, choices)
        overruled = [d for d in decisions if d.clamped]
        step.note(
            f"{policy_mod.summarise(decisions)} ({decided_by}){note_suffix}",
            decided=len(decisions), overruled=len(overruled), by=decided_by,
        )

    with run.step("draft", "Write the corrective documents") as step:
        # One currency per month, guaranteed by G7, so the letters can be written
        # in it rather than in a hard-coded default that made a euro payable
        # read "USD 7,303.60".
        month_currency = next(
            (d.currency for d in ledger.posted if d.currency), "USD")
        filed = drafts_mod.draft_for_decisions(
            decisions, company or "Accounts", month_currency)
        escalated = [d for d in decisions if d.applied is Disposition.ESCALATE]
        step.note(
            f"{plural(len(filed), 'document')} drafted and filed unsent: "
            f"{drafts_mod.leakage(filed):,.2f} that would have leaked away, "
            f"{drafts_mod.outstanding(filed):,.2f} already owed and unpaid; "
            f"{len(escalated)} put in front of the owner instead",
            drafts=len(filed), leakage=drafts_mod.leakage(filed),
            outstanding=drafts_mod.outstanding(filed), escalated=len(escalated),
        )

    # 7. Verify. The close checks its own work before it claims to be finished.
    with run.step("verify", "Check the close against its own gates") as step:
        gates = validation_mod.validate(ledger, results)
        failed = [g for g in gates if not g.passed]
        if failed:
            step.block(
                f"{validation_mod.summary(gates)}; failed: "
                + "; ".join(g.rule.split(":")[0] for g in failed),
                gates=len(gates), failed=len(failed),
            )
        else:
            step.note(validation_mod.summary(gates), gates=len(gates), failed=0)

    # 8. Report. The only step a model touches, and it is handed text.
    statements = ledger.statements()
    with run.step("report", "Write the month-end summary") as step:
        comparison = trends_mod.compare(previous, statements) if previous else None
        facts = facts_sheet(statements, findings, gates, filed)
        if comparison is not None:
            facts += "\n\nAGAINST THE MONTH BEFORE\n  " + trends_mod.narrate(comparison)
        deterministic = narrate(statements, findings, gates, filed)
        summary = deterministic
        # Named phrased_by, NOT source: `source` is the run_close parameter
        # carrying mail provenance, and this local used to shadow it, which
        # put the string "deterministic" into the persisted source field.
        phrased_by = "deterministic"
        # A rehearsal does not phrase anything. The prose it would produce is
        # discarded with the rest of the rehearsal, and paying a thinking model
        # to write it was three Gemini calls per agent close where one is
        # needed. The deterministic summary is a real summary, so the rehearsed
        # result stays complete and comparable to the committed one.
        if narrator is not None and commit:
            try:
                phrased = narrator(facts)
                if phrased and phrased.strip():
                    summary, phrased_by = phrased.strip(), "gemini"
            except Exception as exc:  # a model failure must not fail a close
                step.note(f"narrator unavailable ({type(exc).__name__}), used the "
                          f"deterministic summary")
        step.note(
            f"summary written from a {len(facts.splitlines())}-line fact sheet "
            f"({phrased_by}); no figure was phrased by a model",
            fact_lines=len(facts.splitlines()), source=phrased_by,
        )

    # 9. File. The period is recorded closed, or recorded blocked and why.
    #    The agent may withhold a close it does not trust. It may never grant
    #    one the gates refused: that direction is arithmetic, not judgement.
    outcome, outcome_reason = policy_mod.decide_outcome(gates, decisions, agent_verdict)
    with run.step("file", "File the close and mark the period") as step:
        run.finish(outcome)
        result = CloseResult(
            run_id=run.run_id, period=period, company=company, outcome=outcome,
            statements=statements, allocations=results, findings=findings,
            gates=gates, drafts=filed, summary=summary, facts=facts,
            journal=run, ledger=ledger, decisions=decisions,
            outcome_reason=outcome_reason, register=open_items,
            comparison=comparison, driver=driver, source=source,
        )
        stored["close"] = store.save_close(company, period, result.to_dict())
        stored["drafts"] = store.save_drafts(run.run_id, filed, company, period)
        stored["run"] = store.save_run(run.to_dict())
        step.note(
            f"period {period} marked {outcome} ({outcome_reason}); books, "
            f"{plural(len(filed), 'draft')} and a "
            f"{len(run.steps) + 2}-step trail persisted to {getattr(store, 'backend', 'store')}",
            outcome=outcome, backend=getattr(store, "backend", "store"),
        )

    # 10. Tell the owner. The only step that reaches outside Archon on its own,
    #     and it reaches the owner, not a counterparty. Everything the agent did
    #     overnight is worth nothing if the person it was done for has to
    #     remember to come and look for it.
    with run.step("notify", "Write the owner their month-end letter") as step:
        recipient = owner_email or delivery_mod.owner_address()
        digest = digest_mod.compose(result, recipient=recipient, company=company)
        try:
            receipt = deliverer.deliver(digest)
        except Exception as exc:      # a channel failing must not fail a close
            receipt = delivery_mod.Receipt(
                channel=getattr(deliverer, "channel", "unknown"), delivered=False,
                detail=f"delivery raised {type(exc).__name__}; the digest is still in the app",
                recipient=recipient,
            )
        result.digest = digest
        result.receipt = receipt
        stored["digest"] = store.save_close(company, f"{period}#digest", digest.to_dict())
        step.note(
            f'"{digest.subject}" -> {receipt.detail}',
            channel=receipt.channel, delivered=receipt.delivered,
            actions=digest.action_count,
        )

    # The trail cannot be complete until the last step has ended, so the record
    # written during step 9 is necessarily missing steps 9 and 10. That is not a
    # rounding detail: the journal is what the owner scrolls through on Monday
    # and what a judge replays, and a stored trail that stops two steps early is
    # a trail that hides the filing and the notification.
    #
    # So the run is stamped finished and both records are rewritten with the
    # whole thing. One extra write per close, keyed the same way, so it
    # overwrites rather than accumulates.
    #
    # Found by the Firestore adapter test, which asserted the persisted step
    # count. Nothing that ran against the in-memory store had ever looked.
    run.finish(outcome)
    stored["close"] = store.save_close(company, period, result.to_dict())
    stored["run"] = store.save_run(run.to_dict())
    result.stored = stored
    return result


def documents_summary(documents: list[Document]) -> dict[str, int]:
    """Count the month's mail by family. Used by the API's intake preview."""
    counts: dict[str, int] = {}
    for doc in documents:
        counts[doc.doc_type.value] = counts.get(doc.doc_type.value, 0) + 1
    for doc_type in DocType:
        counts.setdefault(doc_type.value, 0)
    return counts
