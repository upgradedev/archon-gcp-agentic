"""The Google ADK layer: the agent that actually performs the close.

Everything in the modules beside this one is deterministic machinery. This is
where a Google Agent Framework picks that machinery up and runs it, and it is
the piece the sponsor requirement is about, so it is worth being precise about
what would break without it.

**Remove ADK and there is no agent.** The six tools below are the close, one
step each, and it is the ADK `Agent` that decides to call them, in what order,
and when the month is finished. Delete it and what is left is a function
somebody has to remember to call, on a schedule somebody has to maintain, with
a summary nobody wrote. The unattended part of "closes the month unattended"
lives in this file.

**Remove Firestore and the run has no memory.** `store.py` holds the trail, the
books and the filed drafts between a Cloud Run container that exits and an
owner who looks on Monday.

Three deliberate design decisions:

**The tools are the steps, not one do-everything call.** An agent handed a
single `close_the_month()` tool is a button with extra latency. Handed six, it
sequences a real chore, and the run journal records what it chose to do.

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
import os

from ..domain.models import Document
from ..domain.narrator import NARRATOR_INSTRUCTION
from ..runtime.close import CloseResult, run_close
from ..runtime.journal import Clock
from ..runtime.mailbox import read_period

#: Gemini model used unless one is injected. Overridable so a deployment can
#: move without a code change.
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

CLOSE_INSTRUCTION = """\
You are Archon, the bookkeeper for a small trucking firm. You have been asked to
close a month, and nobody is watching. Work through it and finish.

Call the tools in this order, once each:

  1. take_in_mail        - read the period's documents
  2. post_journal        - post the double-entry books
  3. allocate_remittances - split each broker remittance across the loads it settles
  4. triage_exceptions   - find what is missing or does not add up
  5. draft_corrections   - write the corrective documents
  6. verify_and_file     - check the close against its gates and file it

Do not ask the user anything. Do not stop part way to report progress. Do not
skip a step because an earlier one found problems: a month with problems is
exactly the month that needs closing.

When verify_and_file returns, tell the owner in two or three sentences what the
month came to, what still needs their attention, and what you already did about
it. Use only figures the tools returned to you. Never calculate, total or
estimate a number yourself.
"""


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
                 store=None, clock: Clock | None = None):
        self.period = period
        self.company = company
        self.store = store
        self.clock = clock
        self.documents: list[Document] = []
        self.raw: dict[str, str] = {}
        self.result: CloseResult | None = None

    # ── the six tools, in the order the agent is told to call them ───────────

    def take_in_mail(self) -> dict:
        """Read every document waiting for the period being closed.

        Returns:
            A count of the artifacts found, broken down by document family.
        """
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
        return {
            "exceptions": [
                {"kind": f.kind.value, "severity": f.severity, "reference": f.reference,
                 "amount": f.amount, "message": f.message}
                for f in self.result.findings
            ]
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
        self._ensure_run()
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

    def _ensure_run(self) -> None:
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
        self.result = run_close(
            period=self.period, documents=self.documents, company=self.company,
            store=self.store, clock=self.clock, raw_texts=self.raw,
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
            session.draft_corrections,
            session.verify_and_file,
        ],
    )


def run_agent_close(period: str, company: str | None = None, model=None,
                    store=None, clock: Clock | None = None,
                    app_name: str = "archon") -> tuple[CloseResult | None, str]:
    """Let the ADK agent drive the close. Returns the result and its final word.

    This is the path the demo shows and the video records: one instruction in,
    six tool calls the agent chose to make, a closed month out.
    """
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    session = CloseSession(period=period, company=company, store=store, clock=clock)
    agent = build_close_agent(session, model=model)
    runner = InMemoryRunner(agent=agent, app_name=app_name)
    user, session_id = "owner", f"close-{period}"
    _create_session(runner, app_name, user, session_id)

    message = types.Content(
        role="user",
        parts=[types.Part(text=f"Close the books for {period}. Do not ask me anything.")],
    )
    final = ""
    for event in runner.run(user_id=user, session_id=session_id, new_message=message):
        if event.is_final_response() and event.content and event.content.parts:
            text = event.content.parts[0].text
            if text:
                final = text
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
    leaves behind and is what the bundled demo and CI use. This is the path for
    the real world, where a rate confirmation is a photograph of a fax.

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
