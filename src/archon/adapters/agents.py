"""The Google ADK layer: the agent that actually performs the close.

Everything in the modules beside this one is deterministic machinery. This is
where a Google Agent Framework picks that machinery up and runs it, and it is
the piece the sponsor requirement is about, so it is worth being precise about
what would break without it.

**Remove ADK and there is no agent.** The seven tools below are the close, one
step each, and it is the ADK `Agent` that decides to call them, in what order,
and when the month is finished. Delete it and what is left is a function
somebody has to remember to call, on a schedule somebody has to maintain, with
a summary nobody wrote. The unattended part of "closes the month unattended"
lives in this file.

**Remove Firestore and the run has no memory.** `store.py` holds the trail, the
books and the filed drafts between a Cloud Run container that exits and an
owner who looks on Monday.

Three deliberate design decisions:

**The agent decides something that matters.** An earlier version of this file
handed the agent seven tools that reported slices of a close which had already
run, and a test asserted that calling them out of order changed nothing. That
was a fair criticism: an agent whose decisions cannot affect the outcome is
decoration, and on a submission judged mostly on autonomous action it is the
worst thing to be.

So `decide_actions` is real authority. The agent weighs each exception and
chooses to chase, escalate or note it, and may withhold a close it does not
trust. `domain/policy.py` overrules anything the books will not accept and
records the reason, so the authority is bounded rather than absolute. What it
still cannot do is produce a figure: the arithmetic ran before it was asked,
and a test asserts the numbers are identical whoever decided.

**Models are injectable everywhere.** Every constructor takes a model object,
so the whole ADK surface, including genuine function calling and genuine
sequential state hand-off, runs offline in CI against scripted fakes with no
key and no spend.

**The agent cannot reach a figure.** Its tools return counts and summaries; the
arithmetic happened before it was asked. `gemini_narrator` is handed a fact
sheet as text. There is no path from this file to a number in the books.
"""
from __future__ import annotations

import json
import logging
import os

from ..domain.models import Document
from ..domain.narrator import NARRATOR_INSTRUCTION
from ..domain.policy import Disposition
from ..runtime.close import CloseResult, run_close
from ..runtime.journal import Clock
from ..runtime.mailbox import read_period

#: Gemini model used unless one is injected. Overridable so a deployment can
#: move without a code change.
#: Probed 2026-08-24 from this project: gemini-3.7-flash answers on the
#: GLOBAL Vertex endpoint (HTTP 200, modelVersion gemini-3.7-flash) and 404s
#: on us-central1, which is why the deployment sets GOOGLE_CLOUD_LOCATION=
#: global. gemini-3-flash and gemini-3-pro-preview 404 even globally. Anyone
#: changing this pin probes first; a model id nobody verified is an outage
#: waiting for a judge.
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")

CLOSE_INSTRUCTION = """\
You are Archon, the bookkeeper for a small trucking firm. You have been asked to
close a month, and nobody is watching. Work through it and finish.

Call the tools in this order:

  1. take_in_mail        - read the period's documents
  2. post_journal        - post the double-entry books
  3. allocate_remittances - split each broker remittance across the loads it settles
  4. triage_exceptions   - find what is missing or does not add up
  5. decide_actions      - YOUR JUDGEMENT. See below.
  6. verify_and_file     - check the close against its gates and file it

Step 5 is the one that is yours. `triage_exceptions` hands you what the books
found; you decide what should happen about each one, exactly as a bookkeeper
would:

  "draft"    - chase it with a letter to the counterparty
  "escalate" - put it in front of the owner, because it needs a person
  "note"     - record it and take no action

Weigh it. A large silent short pay is worth a letter. A forty dollar
discrepancy from a broker who sends steady work may be worth noting and moving
on, because the relationship is worth more than the forty dollars. Anything you
cannot read, or that suggests the books themselves are wrong, goes to the owner.

You may also withhold the close. If something about the month looks wrong to
you even though every check passed, say so, and it will not be filed as closed.
You cannot do the reverse: a month whose checks failed is never closed, whatever
you think.

Your choices are checked against the books before they take effect. If you ask
for something the books will not allow, you will be overruled and the reason
recorded. That is expected, not a failure.

Do not ask the user anything. Do not stop part way to report progress. Do not
skip a step because an earlier one found problems: a month with problems is
exactly the month that needs closing.

When verify_and_file returns, tell the owner in two or three sentences what the
month came to, what still needs their attention, and what you already did about
it. Use only figures the tools returned to you. Never calculate, total or
estimate a number yourself.
"""


log = logging.getLogger("archon.agents")


class CloseSession:
    """Holds one close while an agent walks through it, step by step.

    The state an ADK tool call mutates. It exists because ADK tools are plain
    functions: they need somewhere to put the ledger between the call that
    posts it and the call that files it.

    The session runs the same code `run_close` runs, and a test asserts the two
    produce identical books. That equivalence is what lets the deterministic
    path be the reference: if the agent ever drifts, the test that compares
    them goes red rather than the drift shipping.
    """

    def __init__(self, period: str, company: str | None = None,
                 store=None, clock: Clock | None = None,
                 previous=None, narrator=None,
                 documents: list[Document] | None = None,
                 raw: dict[str, str] | None = None,
                 source: dict | None = None, deliverer=None):
        self.period = period
        self.company = company
        self.store = store
        self.clock = clock
        #: Handed in by the public routes so an anonymous close cannot reach a
        #: real mail channel. None means "resolve from the environment", which
        #: is correct for the trusted path and was the defect on the public one.
        self.deliverer = deliverer
        #: Carried through so the agent path produces the SAME payload as the
        #: reference path. Without them the agent closes a month with no
        #: comparison against the one before, and the console's trends panel
        #: renders empty on the live route while it fills on the local one.
        self.previous = previous
        self.narrator = narrator
        #: Whether a mailbox was handed in is remembered as a fact of its
        #: own, tested with `is not None` rather than truthiness. `documents
        #: or []` cannot tell "the trigger injected an empty mailbox" from
        #: "nothing was injected", and the difference is the difference
        #: between an honest empty close and silently substituting the sample.
        self._mail_injected = documents is not None
        self.documents: list[Document] = list(documents) if documents is not None else []
        self.raw: dict[str, str] = dict(raw) if raw is not None else {}
        self.source = source
        self.result: CloseResult | None = None
        #: What the agent decided at step 5, and its verdict on the close.
        #: Empty until `decide_actions` is called, which is what makes the
        #: agent's judgement load-bearing rather than reported.
        self.choices: dict[int, Disposition] | None = None
        self.verdict: str | None = None
        self._findings: list = []
        #: Set by `_commit`, so the one run with side effects cannot happen twice.
        self._committed = False

    @property
    def committed(self) -> bool:
        """Whether the one step with side effects actually ran.

        Read by `run_agent_close` to tell a filed close from a rehearsal, which
        are otherwise the same object.
        """
        return self._committed

    # ── the seven tools, in the order the agent is told to call them ────────

    def take_in_mail(self) -> dict:
        """Read every document waiting for the period being closed.

        When the trigger already handed a mailbox in (the events path injects
        the exact objects it read off the bucket), this counts THAT mail and
        must not touch the bundled corpus. It used to read `read_period`
        unconditionally, which overwrote the injected documents with the
        sample month: the persisted record then carried genuine GCS hashes
        over books computed from bundled files, invisible only because the two
        happen to hold identical bytes. Found by audit, 2026-08-24.

        Returns:
            A count of the artifacts found, broken down by document family.
        """
        if not self._mail_injected:
            self.documents, self.raw = read_period(self.period)
        counts: dict[str, int] = {}
        for doc in self.documents:
            counts[doc.doc_type.value] = counts.get(doc.doc_type.value, 0) + 1
        return {"period": self.period, "artifacts": len(self.documents), "by_type": counts}

    def post_journal(self) -> dict:
        """Post every document to the double-entry journal.

        Returns:
            How many entries were posted and how many artifacts were
            deliberately left unposted because they could not be read.
        """
        self._ensure_run()
        assert self.result is not None
        posted = [e for e in self.result.ledger.entries if e.lines]
        return {
            "entries_posted": len(posted),
            "left_unposted": len(self.result.ledger.entries) - len(posted),
            "all_balanced": self.result.ledger.all_entries_balanced(),
        }

    def allocate_remittances(self) -> dict:
        """Split each broker remittance across the loads it settles.

        Returns:
            The remittances split, the load lines they cover, and whether each
            remittance's own arithmetic closed.
        """
        self._ensure_run()
        assert self.result is not None
        return {
            "remittances": len(self.result.allocations),
            "load_lines": sum(len(a.allocations) for a in self.result.allocations),
            "all_reconcile": all(a.reconciles for a in self.result.allocations),
            "unmatched_loads": [
                ref for a in self.result.allocations for ref in a.unmatched_load_refs
            ],
        }

    def triage_exceptions(self) -> dict:
        """Find everything missing or inconsistent in the month, worst first.

        Returns:
            Each exception's kind, reference, amount and severity.
        """
        self._ensure_run()
        assert self.result is not None
        self._findings = list(self.result.findings)
        return {
            "exceptions": [
                {"index": index, "kind": f.kind.value, "severity": f.severity,
                 "reference": f.reference, "amount": f.amount, "message": f.message}
                for index, f in enumerate(self._findings)
            ],
            "next": ("call decide_actions with what should happen about each index"),
        }

    def decide_actions(self, actions: dict[str, str], withhold_close: bool = False) -> dict:
        """Decide what happens about each exception. This is your judgement.

        Args:
            actions: what to do about each exception, keyed by its index as a
                string, valued "draft", "escalate" or "note".
            withhold_close: set true to refuse the close even though the
                checks passed, because something about the month looks wrong.

        Returns:
            What was applied, and every place the books overruled you, with the
            reason. Being overruled is expected and is not a failure.
        """
        chosen: dict[int, Disposition] = {}
        rejected: list[str] = []
        for key, value in (actions or {}).items():
            try:
                chosen[int(key)] = Disposition(str(value).strip().lower())
            except (ValueError, TypeError):
                rejected.append(f"{key}={value!r}")

        self.choices = chosen
        self.verdict = "blocked" if withhold_close else None

        # Re-running is what makes the decision take effect: the close is
        # recomputed with the decider in place. The figures are identical
        # either way, which a test asserts; only what is DONE changes.
        self.result = None
        self._ensure_run()
        assert self.result is not None

        return {
            "applied": [
                {"reference": d.finding.reference, "you_chose": d.chosen.value,
                 "applied": d.applied.value, "overruled": d.clamped, "why": d.reason}
                for d in self.result.decisions
            ],
            "letters_to_be_written": sum(1 for d in self.result.decisions
                                         if d.drafts_a_letter),
            "unreadable_values_ignored": rejected,
        }

    def draft_corrections(self) -> dict:
        """Write and file the corrective document for every actionable exception.

        Returns:
            Each draft's kind, recipient, amount and status. Status is always
            "filed": there is no send path.
        """
        self._ensure_run()
        assert self.result is not None
        return {
            "drafts": [
                {"kind": d.kind.value, "recipient": d.recipient, "reference": d.reference,
                 "amount": d.amount, "subject": d.subject, "status": d.status}
                for d in self.result.drafts
            ],
            "recoverable": self.result.recoverable,
        }

    def verify_and_file(self) -> dict:
        """Check the close against its gates, then file it and mark the period.

        Returns:
            The gate results, the period result, and whether the books can be
            trusted. An outcome of "blocked" means at least one gate failed.
        """
        self._commit()
        assert self.result is not None
        statements = self.result.statements
        return {
            "outcome": self.result.outcome,
            "gates": [{"rule": g.rule, "passed": g.passed, "message": g.message}
                      for g in self.result.gates],
            "revenue": statements.revenue,
            "operating_expenses": statements.operating_expenses,
            "net_profit": statements.net_profit,
            "miles": statements.total_miles,
            "cost_per_mile": statements.cost_per_mile,
            "revenue_per_mile": statements.revenue_per_mile,
            "run_id": self.result.run_id,
        }

    def _commit(self) -> None:
        """The one run that is allowed to write, and it happens once.

        Everything before this point was a rehearsal: the books were computed,
        the exceptions were triaged, the agent chose what to do about each of
        them and could have withheld the month, and none of it touched the
        store or the owner's inbox.

        This is the last of the eleven steps and the only one with side
        effects. Called twice, it does nothing the second time -- an agent that
        calls its own last tool again must not file a second copy of the month
        or send the owner a second digest.
        """
        if self._committed:
            return
        self._committed = True
        self.result = None
        self._ensure_run(commit=True)

    def _ensure_run(self, commit: bool = False) -> None:
        """Run the deterministic close once, on first tool call that needs it.

        The alternative, letting each tool mutate a half-built ledger, would
        mean the books depended on the agent calling the tools in the right
        order. They must not. So the engine runs once, deterministically, and
        the tools report slices of its result. The agent sequences the chore;
        it does not get to change the arithmetic by sequencing it differently.
        """
        if self.result is not None:
            return
        if not self.documents:
            self.take_in_mail()
        choices, verdict = self.choices, self.verdict

        def decider(_findings):
            return choices, verdict

        self.result = run_close(
            period=self.period, documents=self.documents, company=self.company,
            store=self.store, clock=self.clock, raw_texts=self.raw,
            previous=self.previous, narrator=self.narrator,
            driver="adk-agent", source=self.source, deliverer=self.deliverer,
            decider=decider if choices is not None or verdict else None,
            # False for every call but the last. Nothing is durable and
            # nothing is delivered until `_commit` runs, because the agent has
            # not decided yet and may still withhold the month.
            commit=commit,
        )


def build_close_agent(session: CloseSession, model=None, name: str = "archon_close"):
    """The ADK `Agent` that performs the close.

    `model` may be a Gemini model name or an injected model object. An injected
    object needs no key, which is how the agent is exercised for real in CI.
    """
    from google.adk.agents import Agent

    resolved = model or DEFAULT_MODEL
    if isinstance(resolved, str) and not (
        os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI")
    ):
        raise RuntimeError(
            "Set GOOGLE_API_KEY, or configure Vertex AI, or inject a model. "
            "The deterministic close needs none of these: python -m archon.cli"
        )

    return Agent(
        name=name,
        model=resolved,
        instruction=CLOSE_INSTRUCTION,
        tools=[
            session.take_in_mail,
            session.post_journal,
            session.allocate_remittances,
            session.triage_exceptions,
            session.decide_actions,
            session.draft_corrections,
            session.verify_and_file,
        ],
    )


def run_agent_close(period: str, company: str | None = None, model=None,
                    store=None, clock: Clock | None = None,
                    app_name: str = "archon", previous=None,
                    narrator=None, documents=None, raw=None,
                    source=None, deliverer=None) -> tuple[CloseResult | None, str]:
    """Let the ADK agent drive the close. Returns the result and its final word.

    This is the path the demo shows and the video records: one instruction in,
    seven tool calls in the order the instruction prescribes, the agent deciding
    what to do about each exception along the way, a closed month out.
    """
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    session = CloseSession(period=period, company=company, store=store, clock=clock,
                           previous=previous, narrator=narrator,
                           documents=documents, raw=raw, source=source,
                           deliverer=deliverer)
    agent = build_close_agent(session, model=model)
    runner = InMemoryRunner(agent=agent, app_name=app_name)
    user, session_id = "owner", f"close-{period}"
    _create_session(runner, app_name, user, session_id)

    message = types.Content(
        role="user",
        parts=[types.Part(text=f"Close the books for {period}. Do not ask me anything.")],
    )
    final = ""
    try:
        for event in runner.run(user_id=user, session_id=session_id,
                                new_message=message):
            if event.is_final_response() and event.content and event.content.parts:
                text = event.content.parts[0].text
                if text:
                    final = text
    except Exception as exc:                       # noqa: BLE001 - see below
        # A model can die at any point, including on the call AFTER
        # `verify_and_file` -- the stream drops, the quota runs out, the final
        # text errors. Letting that propagate meant the service's fallback ran
        # the WHOLE deterministic close again: a second copy of the month filed
        # over the agent's with standing policy in place of its decisions, and
        # a second digest to an owner who already has the first.
        #
        # A failure after the last step is not a reason to redo the last step.
        # Before it, nothing is durable and nothing was sent, so falling back
        # is exactly right and `None` asks for it.
        if not session.committed:
            log.warning("agent for %s failed before filing (%s: %s); falling back",
                        period, type(exc).__name__, exc)
            return None, final
        log.warning("agent for %s failed AFTER filing (%s: %s); keeping the "
                    "close it already filed", period, type(exc).__name__, exc)
        return session.result, final
    # A rehearsal is not a close, and from the outside the two are identical
    # objects. Every tool before `verify_and_file` runs the month against a
    # throwaway store and a deliverer that cannot send, precisely so the agent
    # can still withhold it; `_commit` is the only step that files anything.
    # Returning `session.result` unconditionally meant a model that stopped
    # early -- hit a limit, decided it was done, fell out of the loop -- handed
    # back a rehearsal that the service published with `driver: adk-agent` as
    # the filed close. Nothing was in the store, no digest was composed, no
    # trail was persisted, and on the event path the marker was then written
    # `closed` against that run id, so the month was considered done and would
    # never be run again. A month that silently never closed, reported closed
    # with seven of seven gates passed.
    #
    # `None` is the honest answer, and the caller already knows what to do with
    # it: log that the agent produced no result and run the deterministic path.
    if not session.committed:
        log.warning("agent for %s stopped before filing; discarding its rehearsal",
                    period)
        return None, final
    return session.result, final


def _create_session(runner, app_name: str, user: str, session_id: str) -> None:
    """ADK has shipped this both sync and async. Support whichever is present."""
    service = runner.session_service
    try:
        service.create_session_sync(app_name=app_name, user_id=user, session_id=session_id)
    except AttributeError:  # pragma: no cover - version shim
        import asyncio

        asyncio.new_event_loop().run_until_complete(
            service.create_session(app_name=app_name, user_id=user, session_id=session_id)
        )


# ── the reporting pipeline: an ADK SequentialAgent over the fact sheet ────────

_RECONCILER_INSTRUCTION = """\
You are the reconciliation stage. You are given a fact sheet of figures that
have already been computed. In one sentence, state whether the month's
remittances and loads reconcile. Quote only figures from the sheet."""

_ANALYST_INSTRUCTION = """\
You are the exceptions stage. Given the reconciliation note and the fact sheet,
name in one or two sentences the exceptions worth the owner's attention and what
they are worth.
Reconciliation: {reconciliation}
Quote only figures from the sheet."""

_NARRATOR_INSTRUCTION = NARRATOR_INSTRUCTION + """
Reconciliation: {reconciliation}
Exceptions: {exceptions}
"""


def build_report_pipeline(models=None, name: str = "archon_report"):
    """The ADK `SequentialAgent` that writes the month up.

    Three stages passing state forward by `output_key`: reconciler, then
    exceptions analyst, then narrator. Every stage sees the same fact sheet and
    the stage before it, and none of them sees a document, so none of them can
    contradict the books.

    Construction needs no key. Only running it against real Gemini does.
    """
    from google.adk.agents import LlmAgent, SequentialAgent

    resolved = models or [DEFAULT_MODEL, DEFAULT_MODEL, DEFAULT_MODEL]
    if len(resolved) != 3:
        raise ValueError("models must be exactly three ADK models or model names")

    return SequentialAgent(
        name=name,
        sub_agents=[
            LlmAgent(name="reconciler", model=resolved[0],
                     instruction=_RECONCILER_INSTRUCTION, output_key="reconciliation"),
            LlmAgent(name="exceptions", model=resolved[1],
                     instruction=_ANALYST_INSTRUCTION, output_key="exceptions"),
            LlmAgent(name="narrator", model=resolved[2],
                     instruction=_NARRATOR_INSTRUCTION, output_key="summary"),
        ],
    )


def run_report_pipeline(facts: str, models=None, app_name: str = "archon-report") -> dict:
    """Run the reporting pipeline over a fact sheet and return each stage."""
    import asyncio

    from google.adk.runners import InMemoryRunner
    from google.genai import types

    pipeline = build_report_pipeline(models=models)
    runner = InMemoryRunner(agent=pipeline, app_name=app_name)
    user, session_id = "owner", "report-1"
    _create_session(runner, app_name, user, session_id)

    message = types.Content(role="user", parts=[types.Part(
        text=f"Write up this month for the owner.\n\n{facts}")])
    for _ in runner.run(user_id=user, session_id=session_id, new_message=message):
        pass

    async def _state():
        session = await runner.session_service.get_session(
            app_name=app_name, user_id=user, session_id=session_id)
        return dict(session.state)

    state = asyncio.new_event_loop().run_until_complete(_state())
    return {
        "reconciliation": state.get("reconciliation", ""),
        "exceptions": state.get("exceptions", ""),
        "summary": state.get("summary", ""),
    }


def gemini_narrator(models=None):
    """A narrator for `run_close`, backed by the ADK reporting pipeline.

    Returns a callable that takes the fact sheet and returns English. On any
    failure it returns an empty string, and `run_close` keeps the deterministic
    summary. A model being down is not a reason for a month not to close.
    """

    def narrate_with_gemini(facts: str) -> str:
        try:
            return run_report_pipeline(facts, models=models).get("summary", "")
        except Exception:  # pragma: no cover - network, quota, auth
            return ""

    return narrate_with_gemini


# ── extraction against Gemini, for artifacts the deterministic parser cannot ──

def extract_with_gemini(text: str, source_file: str, period: str, client=None) -> Document:
    """Ask Gemini for the structured fields of one artifact.

    The deterministic parser in `extract.py` handles the label blocks OCR
    leaves behind and is what the bundled demo and CI use. This is a second
    path over the same TEXT, for artifacts whose wording the parser has never
    seen. It is not a vision call: the argument is a string.

    An artifact the model will not commit to comes back UNREADABLE, which the
    close reports as a finding. That is the correct outcome and it is enforced
    here rather than trusted: a response with no usable `doc_type` becomes
    UNREADABLE rather than being coerced into a family.
    """
    from google import genai
    from google.genai import types

    from ..domain.extract import EXTRACTION_INSTRUCTION, EXTRACTION_SCHEMA
    from ..domain.models import DocType

    client = client or genai.Client()
    response = client.models.generate_content(
        model=DEFAULT_MODEL,
        contents=f"{EXTRACTION_INSTRUCTION}\n\n---\n{text}",
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=EXTRACTION_SCHEMA,
            temperature=0,
        ),
    )

    try:
        fields = json.loads(response.text)
    except (TypeError, ValueError):
        fields = {}

    try:
        doc_type = DocType(fields.get("doc_type", ""))
    except ValueError:
        doc_type = DocType.UNREADABLE

    doc = Document(doc_type=doc_type, period=period, source_file=source_file)
    for key, value in fields.items():
        if key != "doc_type" and hasattr(doc, key) and value is not None:
            setattr(doc, key, value)
    if doc_type is DocType.UNREADABLE and not doc.failure_reason:
        doc.failure_reason = "the model would not commit to a document type"
    return doc
