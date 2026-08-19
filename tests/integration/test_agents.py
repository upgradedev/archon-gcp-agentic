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

from archon.agents import CloseSession
from archon.close import run_close
from archon.journal import FixedClock
from archon.narrator import facts_sheet
from archon.store import LocalStore
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


def test_the_agent_path_and_the_deterministic_path_agree_exactly():
    """The equivalence that lets the deterministic path be the reference. If
    the agent ever drifts, this goes red rather than the drift shipping."""
    tools = session()
    tools.verify_and_file()

    reference = run_close(period=PERIOD, documents=tools.documents,
                          company="Bell Ridge Haulage", store=LocalStore(),
                          clock=FixedClock(), raw_texts=tools.raw)

    assert tools.result.to_dict() == reference.to_dict()


def test_every_draft_the_tools_report_is_filed_and_none_is_sent():
    drafts = session().draft_corrections()["drafts"]

    assert drafts
    assert {d["status"] for d in drafts} == {"filed"}


# ── the real ADK agent, driven by a scripted model ───────────────────────────

def test_the_adk_agent_constructs_with_the_six_tools():
    pytest.importorskip("google.adk")
    from archon.agents import build_close_agent
    from tests.adk_fakes import ScriptedLlm

    agent = build_close_agent(session(), model=ScriptedLlm([("text", "done")]))

    assert agent.name == "archon_close"
    assert [tool.__name__ for tool in agent.tools] == [
        "take_in_mail", "post_journal", "allocate_remittances",
        "triage_exceptions", "draft_corrections", "verify_and_file",
    ]


def test_a_string_model_without_a_key_refuses_rather_than_failing_later():
    pytest.importorskip("google.adk")
    from archon.agents import build_close_agent

    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        build_close_agent(session(), model="gemini-2.5-flash")


def test_the_agent_really_calls_its_tools_and_closes_the_month(monkeypatch):
    """Genuine ADK function calling: the scripted model emits six tool calls,
    ADK dispatches each one, and a real month comes out the other end."""
    pytest.importorskip("google.adk")
    from archon.agents import run_agent_close
    from tests.adk_fakes import ScriptedLlm

    model = ScriptedLlm([
        ("call", "take_in_mail", {}),
        ("call", "post_journal", {}),
        ("call", "allocate_remittances", {}),
        ("call", "triage_exceptions", {}),
        ("call", "draft_corrections", {}),
        ("call", "verify_and_file", {}),
        ("text", "July is closed. Five letters are waiting for you to send."),
    ])

    result, final = run_agent_close(period=PERIOD, company="Bell Ridge Haulage",
                                    model=model, store=LocalStore(), clock=FixedClock())

    assert result is not None
    assert result.outcome == "closed"
    assert len(result.drafts) == 5
    assert "closed" in final


def test_the_agent_is_never_asked_anything_by_its_own_instruction():
    """The instruction is part of the product claim, so it is asserted.

    Compared with whitespace collapsed, because the instruction is hard-wrapped
    and a reflow is not a change of meaning.
    """
    from archon.agents import CLOSE_INSTRUCTION

    instruction = " ".join(CLOSE_INSTRUCTION.split())

    assert "Do not ask the user anything" in instruction
    assert "Do not stop part way to report progress" in instruction
    assert "Never calculate, total or estimate a number yourself" in instruction


# ── the reporting SequentialAgent ────────────────────────────────────────────

def test_the_report_pipeline_is_three_stages_passing_state_forward():
    pytest.importorskip("google.adk")
    from archon.agents import build_report_pipeline
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
    from archon.agents import build_report_pipeline

    with pytest.raises(ValueError, match="exactly three"):
        build_report_pipeline(models=["only-one"])


def test_the_pipeline_runs_end_to_end_offline_and_hands_state_between_stages():
    pytest.importorskip("google.adk")
    from archon.agents import run_report_pipeline
    from tests.adk_fakes import ScriptedText

    tools = session()
    tools.verify_and_file()
    facts = facts_sheet(tools.result.statements, tools.result.findings,
                        tools.result.gates, tools.result.drafts)

    stages = run_report_pipeline(facts, models=[
        ScriptedText("everything reconciles"),
        ScriptedText("three errors worth chasing"),
        ScriptedText("July closed with 1,994.24 of profit."),
    ])

    assert stages["reconciliation"] == "everything reconciles"
    assert stages["exceptions"] == "three errors worth chasing"
    assert stages["summary"] == "July closed with 1,994.24 of profit."


def test_the_gemini_narrator_returns_the_pipeline_summary():
    pytest.importorskip("google.adk")
    from archon.agents import gemini_narrator
    from tests.adk_fakes import ScriptedText

    narrator = gemini_narrator(models=[
        ScriptedText("x"), ScriptedText("y"), ScriptedText("the summary"),
    ])

    assert narrator("PERIOD 2026-07") == "the summary"


def test_the_gemini_narrator_returns_nothing_rather_than_raising():
    """So a close never fails because the reporting path was unusable."""
    from archon.agents import gemini_narrator

    assert gemini_narrator(models=["only-one-model"])("facts") == ""


def test_the_narrator_instruction_forbids_inventing_a_figure():
    from archon.narrator import NARRATOR_INSTRUCTION

    assert "Report only figures that appear in the fact sheet" in NARRATOR_INSTRUCTION
