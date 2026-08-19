"""The chore: close a haulier's month, unattended, end to end.

This is the whole product in one function. A month of mail goes in. Ten steps
later the books are posted, the remittance is split across the loads it
settles, the exceptions are triaged worst-first, the corrective documents are
written and filed, the close has checked its own work against five gates, the
period is marked closed with a trail you can walk back through, and the owner
has a letter about it wherever they read their mail.

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
from collections.abc import Callable
from dataclasses import dataclass, field

from ..adapters import delivery as delivery_mod
from ..adapters.delivery import Deliverer, Receipt
from ..adapters.store import Store, get_store
from ..domain import allocation as allocation_mod
from ..domain import digest as digest_mod
from ..domain import drafts as drafts_mod
from ..domain import exceptions as exceptions_mod
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
from .journal import Clock, RunJournal

#: A narrator takes the fact sheet and returns English. The default is the
#: deterministic one; `agents.py` supplies a Gemini-backed one. It can never
#: introduce a figure, because it is handed text, not documents.
Narrator = Callable[[str], str]


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
    digest: Digest | None = None
    receipt: Receipt | None = None
    stored: dict = field(default_factory=dict)

    @property
    def closed(self) -> bool:
        return self.outcome == "closed"

    @property
    def recoverable(self) -> float:
        """Money the filed drafts are chasing back."""
        return drafts_mod.recoverable(self.drafts)

    def to_dict(self) -> dict:
        """The shape the API serves and the browser renders."""
        from ..adapters.store import _plain

        return {
            "run_id": self.run_id,
            "period": self.period,
            "company": self.company,
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
            "journal": self.journal.to_dict(),
            "recoverable": self.recoverable,
            "digest": self.digest.to_dict() if self.digest else None,
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "facts": self.facts,
        }


def run_id_for(period: str, documents: list[Document]) -> str:
    """A run id derived from the period and the exact documents in it.

    Deriving it rather than randomising it makes a close idempotent in the way
    that matters: re-running the same month over the same mail produces the same
    run id, overwrites the same record, and does not litter the trail with
    near-identical runs. Change one document and the id changes, which is
    correct, because it is a different month.
    """
    digest = hashlib.sha256()
    digest.update(period.encode())
    for doc in documents:
        digest.update((doc.source_file or "").encode())
        digest.update(str(doc.doc_type.value).encode())
    return f"{period}-{digest.hexdigest()[:10]}"


def run_close(period: str,
              documents: list[Document],
              company: str | None = None,
              store: Store | None = None,
              clock: Clock | None = None,
              narrator: Narrator | None = None,
              raw_texts: dict[str, str] | None = None,
              deliverer: Deliverer | None = None,
              owner_email: str | None = None) -> CloseResult:
    """Close one period. Returns even when it fails; read `outcome`.

    Raising on a bad month would be the wrong shape. A close that hits a
    problem has still done eight useful things, and the owner needs to see them
    and the problem. So every path returns a `CloseResult`, and `outcome` says
    whether the books can be trusted.
    """
    store = store or get_store()
    deliverer = deliverer or delivery_mod.get_deliverer()
    run = RunJournal(run_id=run_id_for(period, documents), period=period, clock=clock)
    ledger = Ledger(period=period, company=company)
    stored: dict = {}

    # 1. Intake. Nothing is interpreted yet; the raw artifacts are put beyond
    #    reach of anything that follows, so the trail starts before the books do.
    with run.step("intake", "Take in the month's mail") as step:
        by_type: dict[str, int] = {}
        for doc in documents:
            by_type[doc.doc_type.value] = by_type.get(doc.doc_type.value, 0) + 1
        for name, text in (raw_texts or {}).items():
            store.put_document(name, text)
        step.note(
            f"{len(documents)} artifacts: "
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
            f"{len(results)} remittance(s) split across {lines} loads; "
            + (f"{len(off)} left a residual" if off else "every one reconciles"),
            remittances=len(results), load_lines=lines, unreconciled=len(off),
        )

    # 4. Reconcile. Which loads got paid, which are still outstanding.
    with run.step("reconcile", "Reconcile loads against what the brokers paid") as step:
        settled = allocation_mod.settled_load_refs(results)
        outstanding = allocation_mod.unsettled_loads(documents, results)
        loads = len(allocation_mod.loads_by_ref(documents))
        step.note(
            f"{len(settled)} of {loads} loads settled, {len(outstanding)} still outstanding",
            loads=loads, settled=len(settled), outstanding=len(outstanding),
        )

    # 5. Triage. Nine detectors, ranked worst-first by severity then money.
    with run.step("triage", "Find what is missing or does not add up") as step:
        findings = exceptions_mod.find_all(documents, results, period)
        errors = [f for f in findings if f.severity == "error"]
        step.note(
            f"{len(findings)} exception(s), {len(errors)} of them errors, "
            f"{exceptions_mod.exposure(errors):,.2f} at stake",
            exceptions=len(findings), errors=len(errors),
            at_stake=exceptions_mod.exposure(errors),
        )

    # 6. Draft. The step that separates an agent from a report.
    with run.step("draft", "Write the corrective documents") as step:
        filed = drafts_mod.draft_all(findings, company or "Accounts")
        step.note(
            f"{len(filed)} document(s) drafted and filed unsent, chasing "
            f"{drafts_mod.recoverable(filed):,.2f}",
            drafts=len(filed), recoverable=drafts_mod.recoverable(filed),
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
        facts = facts_sheet(statements, findings, gates, filed)
        deterministic = narrate(statements, findings, gates, filed)
        summary = deterministic
        source = "deterministic"
        if narrator is not None:
            try:
                phrased = narrator(facts)
                if phrased and phrased.strip():
                    summary, source = phrased.strip(), "gemini"
            except Exception as exc:  # a model failure must not fail a close
                step.note(f"narrator unavailable ({type(exc).__name__}), used the "
                          f"deterministic summary")
        step.note(
            f"summary written from a {len(facts.splitlines())}-line fact sheet "
            f"({source}); no figure was phrased by a model",
            fact_lines=len(facts.splitlines()), source=source,
        )

    # 9. File. The period is recorded closed, or recorded blocked and why.
    outcome = "closed" if validation_mod.all_passed(gates) else "blocked"
    with run.step("file", "File the close and mark the period") as step:
        run.finish(outcome)
        result = CloseResult(
            run_id=run.run_id, period=period, company=company, outcome=outcome,
            statements=statements, allocations=results, findings=findings,
            gates=gates, drafts=filed, summary=summary, facts=facts,
            journal=run, ledger=ledger,
        )
        stored["close"] = store.save_close(company, period, result.to_dict())
        stored["drafts"] = store.save_drafts(run.run_id, filed)
        stored["run"] = store.save_run(run.to_dict())
        step.note(
            f"period {period} marked {outcome}; books, {len(filed)} draft(s) and a "
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

    result.stored = stored
    # Both trailing steps land after `run.finish`, so the trail records its own
    # filing and its own notification. Re-stamping the outcome keeps
    # `journal.outcome` and `result.outcome` from ever disagreeing.
    run.finish(outcome)
    return result


def documents_summary(documents: list[Document]) -> dict[str, int]:
    """Count the month's mail by family. Used by the API's intake preview."""
    counts: dict[str, int] = {}
    for doc in documents:
        counts[doc.doc_type.value] = counts.get(doc.doc_type.value, 0) + 1
    for doc_type in DocType:
        counts.setdefault(doc_type.value, 0)
    return counts
