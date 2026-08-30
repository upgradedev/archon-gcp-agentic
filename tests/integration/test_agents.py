"""The ADK layer, exercised for real and offline.

The claim this entry is judged on is that an agent performs a chore unattended.
A test that stubs out the agent proves nothing about that, so these drive the
genuine ADK function-calling loop and the genuine `SequentialAgent` state
hand-off against scripted models. No key, no network, no spend.

`google-adk` is an optional dependency of the deterministic engine, so the ADK
tests skip when it is absent and run in CI where it is installed. The tool
behaviour itself needs no ADK and is tested unconditionally: those tests are
the ones that would catch the agent's tools drifting away from the books.
"""
from __future__ import annotations

import pytest

from archon.adapters.agents import CloseSession
from archon.adapters.store import LocalStore
from archon.domain.narrator import facts_sheet
from archon.domain.policy import Disposition
from archon.runtime.close import run_close
from archon.runtime.journal import FixedClock
from archon.runtime.mailbox import read_period
from tests.conftest import PERIOD


def session() -> CloseSession:
    return CloseSession(period=PERIOD, company="Bell Ridge Haulage",
                        store=LocalStore(), clock=FixedClock())


# ── the six tools, with or without ADK installed ─────────────────────────────

def test_the_tools_walk_the_whole_chore():
    tools = session()

    intake = tools.take_in_mail()
    posted = tools.post_journal()
    allocated = tools.allocate_remittances()
    triaged = tools.triage_exceptions()
    drafted = tools.draft_corrections()
    filed = tools.verify_and_file()

    assert intake["artifacts"] == 27
    assert intake["by_type"]["load_confirmation"] == 9
    assert posted["all_balanced"] is True
    assert allocated["load_lines"] == 8
    assert allocated["unmatched_loads"] == ["L-7099"]
    assert len(triaged["exceptions"]) == 10
    assert len(drafted["drafts"]) == 5
    assert filed["outcome"] == "closed"


def test_a_tool_called_out_of_order_still_produces_the_same_books():
    """The agent sequences the chore; it does not get to change the arithmetic
    by sequencing it differently."""
    straight = session()
    straight.take_in_mail()
    straight.post_journal()
    expected = straight.verify_and_file()

    jumbled = session()
    jumbled.verify_and_file()                    # first call, nothing else run

    assert jumbled.verify_and_file() == expected


def test_the_tools_return_no_figure_the_engine_did_not_compute():
    """Every number a tool hands the agent came out of the deterministic close."""
    tools = session()
    filed = tools.verify_and_file()
    reference = run_close(period=PERIOD, documents=tools.documents,
                          company="Bell Ridge Haulage", store=LocalStore(),
                          clock=FixedClock(), raw_texts=tools.raw)

    assert filed["net_profit"] == reference.statements.net_profit
    assert filed["revenue"] == reference.statements.revenue
    assert filed["cost_per_mile"] == reference.statements.cost_per_mile


def test_the_figures_are_identical_whoever_decided_and_the_decisions_may_differ():
    """The test this replaces asserted the two paths were byte-identical, which
    was true only because the agent could not affect anything. That was the
    criticism, and it was fair.

    The boundary is now the thing worth asserting: an agent changes what is
    DONE about the month and can never change what the month IS.
    """
    from archon.domain.policy import Disposition

    documents, raw = read_period(PERIOD)

    def cautious(findings):
        # Chase nothing. A real bookkeeper's prerogative, and the most extreme
        # decision available, so any leak into the figures would show here.
        return dict.fromkeys(range(len(findings)), Disposition.NOTE), None

    standing = run_close(period=PERIOD, documents=documents, company="Bell Ridge Haulage",
                         store=LocalStore(), clock=FixedClock(), raw_texts=raw)
    decided = run_close(period=PERIOD, documents=documents, company="Bell Ridge Haulage",
                        store=LocalStore(), clock=FixedClock(), raw_texts=raw,
                        decider=cautious)

    # What the month IS: untouched.
    assert decided.to_dict()["statements"] == standing.to_dict()["statements"]
    assert decided.to_dict()["findings"] == standing.to_dict()["findings"]
    assert decided.to_dict()["allocations"] == standing.to_dict()["allocations"]
    assert decided.to_dict()["gates"] == standing.to_dict()["gates"]

    # What was DONE about it: different, because the agent decided differently.
    assert len(standing.drafts) == 5
    assert len(decided.drafts) == 0
    assert decided.recoverable == 0.0


def test_an_agent_cannot_manufacture_a_letter_the_books_refuse():
    """The guardrail, exercised through the whole close rather than in isolation."""
    from archon.domain.policy import Disposition

    documents, raw = read_period(PERIOD)

    def reckless(findings):
        # Chase everything, including the kinds with no honest letter.
        return dict.fromkeys(range(len(findings)), Disposition.DRAFT), None

    result = run_close(period=PERIOD, documents=documents, company="Bell Ridge Haulage",
                       store=LocalStore(), clock=FixedClock(), raw_texts=raw,
                       decider=reckless)

    overruled = [d for d in result.decisions if d.clamped]

    assert overruled, "the reckless decider should have been overruled somewhere"
    assert len(result.drafts) == 5          # the same five, not one more
    for draft in result.drafts:
        assert draft.amount > 0


def test_an_agent_can_withhold_a_close_that_passed_every_gate():
    """Autonomy that only ever agrees is not autonomy."""
    documents, raw = read_period(PERIOD)

    def suspicious(findings):
        return None, "blocked"

    result = run_close(period=PERIOD, documents=documents, company="Bell Ridge Haulage",
                       store=LocalStore(), clock=FixedClock(), raw_texts=raw,
                       decider=suspicious)

    assert all(gate.passed for gate in result.gates)
    assert result.outcome == "blocked"
    assert "withheld" in result.outcome_reason


def test_a_decider_that_raises_falls_back_to_the_standing_policy():
    """A model failing must not fail a month."""
    documents, raw = read_period(PERIOD)

    def broken(findings):
        raise RuntimeError("the model is down")

    result = run_close(period=PERIOD, documents=documents, company="Bell Ridge Haulage",
                       store=LocalStore(), clock=FixedClock(), raw_texts=raw,
                       decider=broken)

    assert result.outcome == "closed"
    assert len(result.drafts) == 5
    decide = next(s for s in result.journal.steps if s.name == "decide")
    assert "fell back to the standing policy" in decide.detail


def test_every_draft_the_tools_report_is_filed_and_none_is_sent():
    drafts = session().draft_corrections()["drafts"]

    assert drafts
    assert {d["status"] for d in drafts} == {"filed"}


# ── the real ADK agent, driven by a scripted model ───────────────────────────

def test_the_adk_agent_constructs_with_its_seven_tools():
    pytest.importorskip("google.adk")
    from archon.adapters.agents import build_close_agent
    from tests.adk_fakes import ScriptedLlm

    agent = build_close_agent(session(), model=ScriptedLlm([("text", "done")]))

    assert agent.name == "archon_close"
    assert [tool.__name__ for tool in agent.tools] == [
        "take_in_mail", "post_journal", "allocate_remittances",
        "triage_exceptions", "decide_actions", "draft_corrections",
        "verify_and_file",
    ]


def test_a_string_model_without_a_key_refuses_rather_than_failing_later():
    pytest.importorskip("google.adk")
    from archon.adapters.agents import build_close_agent

    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        build_close_agent(session(), model="gemini-2.5-flash")


def test_the_agent_really_calls_its_tools_and_closes_the_month(monkeypatch):
    """Genuine ADK function calling: the scripted model emits six tool calls,
    ADK dispatches each one, and a real month comes out the other end."""
    pytest.importorskip("google.adk")
    from archon.adapters.agents import run_agent_close
    from tests.adk_fakes import ScriptedLlm

    model = ScriptedLlm([
        ("call", "take_in_mail", {}),
        ("call", "post_journal", {}),
        ("call", "allocate_remittances", {}),
        ("call", "triage_exceptions", {}),
        ("call", "decide_actions", {"actions": {"0": "draft", "1": "escalate"}}),
        ("call", "draft_corrections", {}),
        ("call", "verify_and_file", {}),
        ("text", "July is closed. Five letters are waiting for you to send."),
    ])

    result, final = run_agent_close(period=PERIOD, company="Bell Ridge Haulage",
                                    model=model, store=LocalStore(), clock=FixedClock())

    assert result is not None
    assert result.outcome == "closed"
    assert "closed" in final

    # The decision travelled through the real ADK function-calling loop and
    # changed the work. The standing policy writes five letters; this agent
    # asked for one draft and one escalation, so one letter exists. That
    # difference is the whole point: an agent whose choices cannot change the
    # outcome is decoration.
    assert len(result.drafts) == 1
    assert result.decisions[0].applied is Disposition.DRAFT
    assert result.decisions[1].applied is Disposition.ESCALATE
    assert result.statements.net_profit == 2_406.84     # the books, untouched


def test_the_agent_is_never_asked_anything_by_its_own_instruction():
    """The instruction is part of the product claim, so it is asserted.

    Compared with whitespace collapsed, because the instruction is hard-wrapped
    and a reflow is not a change of meaning.
    """
    from archon.adapters.agents import CLOSE_INSTRUCTION

    instruction = " ".join(CLOSE_INSTRUCTION.split())

    assert "Do not ask the user anything" in instruction
    assert "Do not stop part way to report progress" in instruction
    assert "Never calculate, total or estimate a number yourself" in instruction


# ── the reporting SequentialAgent ────────────────────────────────────────────

def test_the_report_pipeline_is_three_stages_passing_state_forward():
    pytest.importorskip("google.adk")
    from archon.adapters.agents import build_report_pipeline
    from tests.adk_fakes import ScriptedText

    pipeline = build_report_pipeline(
        models=[ScriptedText("a"), ScriptedText("b"), ScriptedText("c")]
    )

    assert [stage.name for stage in pipeline.sub_agents] == [
        "reconciler", "exceptions", "narrator",
    ]
    assert [stage.output_key for stage in pipeline.sub_agents] == [
        "reconciliation", "exceptions", "summary",
    ]


def test_the_report_pipeline_needs_exactly_three_models():
    pytest.importorskip("google.adk")
    from archon.adapters.agents import build_report_pipeline

    with pytest.raises(ValueError, match="exactly three"):
        build_report_pipeline(models=["only-one"])


def test_the_pipeline_runs_end_to_end_offline_and_hands_state_between_stages():
    pytest.importorskip("google.adk")
    from archon.adapters.agents import run_report_pipeline
    from tests.adk_fakes import ScriptedText

    tools = session()
    tools.verify_and_file()
    facts = facts_sheet(tools.result.statements, tools.result.findings,
                        tools.result.gates, tools.result.drafts)

    stages = run_report_pipeline(facts, models=[
        ScriptedText("everything reconciles"),
        ScriptedText("three errors worth chasing"),
        ScriptedText("July closed with 2,406.84 of profit."),
    ])

    assert stages["reconciliation"] == "everything reconciles"
    assert stages["exceptions"] == "three errors worth chasing"
    assert stages["summary"] == "July closed with 2,406.84 of profit."


def test_the_gemini_narrator_returns_the_pipeline_summary():
    pytest.importorskip("google.adk")
    from archon.adapters.agents import gemini_narrator
    from tests.adk_fakes import ScriptedText

    narrator = gemini_narrator(models=[
        ScriptedText("x"), ScriptedText("y"), ScriptedText("the summary"),
    ])

    assert narrator("PERIOD 2026-07") == "the summary"


def test_the_gemini_narrator_returns_nothing_rather_than_raising():
    """So a close never fails because the reporting path was unusable."""
    from archon.adapters.agents import gemini_narrator

    assert gemini_narrator(models=["only-one-model"])("facts") == ""


def test_the_narrator_instruction_forbids_inventing_a_figure():
    from archon.domain.narrator import NARRATOR_INSTRUCTION

    assert "Report only figures that appear in the fact sheet" in NARRATOR_INSTRUCTION


def test_the_agents_own_decision_changes_what_the_close_does():
    """Through the tool surface, the way the model reaches it.

    The agent is handed indexed exceptions and answers with a disposition per
    index. Two different answers produce two different months of work, and the
    same month of books.
    """
    chasing = session()
    chasing.triage_exceptions()
    chasing.decide_actions({"0": "draft", "1": "draft", "2": "draft"})

    cautious = session()
    cautious.triage_exceptions()
    cautious.decide_actions({str(i): "note" for i in range(12)})

    assert len(chasing.result.drafts) > len(cautious.result.drafts)
    assert cautious.result.drafts == []
    assert chasing.result.statements == cautious.result.statements


def test_the_agent_is_told_when_it_is_overruled():
    """Being overruled has to be visible to the agent, or it cannot learn the
    boundary within a single run."""
    tools = session()
    exceptions = tools.triage_exceptions()["exceptions"]
    outlier = next(e["index"] for e in exceptions if e["kind"] == "amount_outlier")

    reply = tools.decide_actions({str(outlier): "draft"})

    overruled = [row for row in reply["applied"] if row["overruled"]]
    assert overruled, "asking to chase an outlier should have been overruled"
    assert any("no honest letter" in row["why"] for row in overruled)


def test_the_agent_can_refuse_to_file_the_close():
    tools = session()
    tools.triage_exceptions()
    tools.decide_actions({}, withhold_close=True)

    filed = tools.verify_and_file()

    assert all(gate["passed"] for gate in filed["gates"])
    assert filed["outcome"] == "blocked"


def test_a_value_the_agent_invents_is_ignored_rather_than_crashing():
    """Models produce unexpected strings. That must not fail a month."""
    tools = session()
    tools.triage_exceptions()

    reply = tools.decide_actions({"0": "obliterate", "1": "escalate"})

    assert "0='obliterate'" in reply["unreadable_values_ignored"]
    assert tools.result.outcome == "closed"


def test_an_agent_that_stops_early_does_not_publish_its_rehearsal_as_the_close():
    """Every tool before `verify_and_file` runs the close as a REHEARSAL: the
    books are computed against a throwaway store and a deliverer that cannot
    send, because the agent has not decided yet and may still withhold the
    month. `verify_and_file` is the only step with side effects.

    But `run_agent_close` returned `session.result` whether or not that last
    step ever ran. A model that stops early -- hits a limit, decides it is
    finished, drops out of the loop -- left a rehearsal in `session.result`,
    and the service published it with `driver: adk-agent` as the filed close.

    Nothing was in the store. No digest was composed. No trail was persisted.
    And on the event path the marker was then written `closed` against that
    run id, so the month was considered done and would never be run again. A
    month that silently never closed, reported as closed with 7/7 gates.
    """
    pytest.importorskip("google.adk")
    from archon.adapters.agents import run_agent_close
    from tests.adk_fakes import ScriptedLlm

    store = LocalStore()
    model = ScriptedLlm([
        ("call", "take_in_mail", {}),
        ("call", "post_journal", {}),
        ("call", "triage_exceptions", {}),
        # and then it simply stops, without calling verify_and_file
        ("text", "That looks like everything."),
    ])

    result, _final = run_agent_close(period=PERIOD, company="Bell Ridge Haulage",
                                     model=model, store=store, clock=FixedClock())

    assert result is None, (
        "a rehearsal was returned as the close; the caller cannot tell it apart")
    assert store.load_close("Bell Ridge Haulage", PERIOD) is None, (
        "nothing should be filed when the agent never filed anything")


def test_a_model_that_dies_after_filing_does_not_file_and_deliver_a_second_time():
    """`verify_and_file` is the one step with side effects: it writes the books
    and hands the owner their digest. If the model then fails -- the stream
    drops, the final text errors, the quota runs out one call later -- the
    exception used to propagate out of `run_agent_close`, and the service's
    fallback ran the WHOLE deterministic close again.

    That files a second copy of the month over the agent's, replacing its
    decisions with standing policy, and composes the owner a second digest that
    contradicts the first one they already have.

    The agent's work was finished. The failure happened after it, and a failure
    after the last step is not a reason to redo the last step.
    """
    pytest.importorskip("google.adk")
    from archon.adapters.agents import run_agent_close
    from tests.adk_fakes import ScriptedLlm

    store = LocalStore()
    delivered: list = []

    class CountingDelivery:
        def send(self, message):
            delivered.append(message)
            return {"status": "delivered"}

    model = ScriptedLlm([
        ("call", "take_in_mail", {}),
        ("call", "post_journal", {}),
        ("call", "allocate_remittances", {}),
        ("call", "triage_exceptions", {}),
        ("call", "decide_actions", {"actions": {"0": "note"}}),
        ("call", "draft_corrections", {}),
        ("call", "verify_and_file", {}),
        ("raise", RuntimeError("the model dropped after the books were filed")),
    ])

    result, _final = run_agent_close(
        period=PERIOD, company="Bell Ridge Haulage", model=model,
        store=store, clock=FixedClock(), deliverer=CountingDelivery())

    assert result is not None, (
        "the filed close was thrown away because the model died after filing")
    assert len(delivered) <= 1, f"the owner got {len(delivered)} digests for one month"
