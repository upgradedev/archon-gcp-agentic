"""What actually closes a month on the deployed route.

The defect these exist to stop recurring: the service imported `run_close` and
called it directly, so the ADK agent was real, tested, documented and never
once executed in production. The sponsor claim rests on the agent being the
control flow, and nothing in the suite could tell that it was not.

`--agent` on the CLI had the same shape. Its help said it drove the close
through the agent; it swapped in the narrator and called the deterministic
orchestrator. A flag that quietly does something smaller than it claims is how
a claim stops being true without anybody editing the claim.

None of this needs a model or a network: the call graph is the assertion.
"""
from __future__ import annotations

import pytest

from archon.adapters import service
from archon.adapters.store import LocalStore


@pytest.fixture
def agent_on(monkeypatch):
    monkeypatch.setattr(service, "USE_AGENT", True)


@pytest.fixture
def spy(monkeypatch):
    """Records that the agent path was entered, and with what."""
    calls: list[dict] = []

    def fake_run_agent_close(**kwargs):
        calls.append(kwargs)
        from archon.runtime.close import run_close
        from archon.runtime.mailbox import read_period

        documents, raw = read_period(kwargs["period"])
        result = run_close(period=kwargs["period"], documents=documents,
                           company=kwargs.get("company"), store=kwargs.get("store"),
                           raw_texts=raw, previous=kwargs.get("previous"))
        return result, "closed"

    monkeypatch.setattr("archon.adapters.agents.run_agent_close", fake_run_agent_close)
    return calls


# ── the production route ─────────────────────────────────────────────────────

def test_the_close_route_drives_the_agent_when_it_is_switched_on(agent_on, spy):
    """The regression. If this goes green while `spy` is empty, production has
    fallen back to deterministic-only execution and the sponsor claim is
    describing code that does not run."""
    payload = service._close("2026-07", store=LocalStore())

    assert spy, "the service closed the month without ever entering the agent path"
    assert payload["driver"] == "adk-agent"


def test_the_deterministic_path_says_so_rather_than_letting_it_be_assumed():
    payload = service._close("2026-07", store=LocalStore())

    assert payload["driver"] == "deterministic"


def test_the_agent_is_handed_what_it_needs_to_produce_the_same_payload(agent_on, spy):
    """`previous` is why this matters. Without it the agent closes a month with
    no comparison behind it, so the console's trends panel renders empty on the
    live route while it fills on every other one."""
    payload = service._close("2026-07", store=LocalStore())

    assert spy[0]["previous"] is not None
    assert spy[0]["company"] == service.COMPANY
    assert payload["comparison"] is not None
    assert payload["register"]["receivables"]


def test_a_model_failure_falls_back_rather_than_taking_the_button_down(
        agent_on, monkeypatch, caplog):
    """The public route is what the readiness gate probes as a veto. A model
    that refuses must cost a code path, not the demo."""
    def explode(**_kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("archon.adapters.agents.run_agent_close", explode)

    payload = service._close("2026-07", store=LocalStore())

    assert payload["driver"] == "deterministic"
    assert payload["outcome"] == "closed"
    assert "falling back" in caplog.text
    assert "model unavailable" in caplog.text


def test_an_agent_that_returns_nothing_is_a_fallback_not_a_crash(agent_on, monkeypatch):
    monkeypatch.setattr("archon.adapters.agents.run_agent_close",
                        lambda **_kw: (None, "I could not"))

    assert service._close("2026-07", store=LocalStore())["driver"] == "deterministic"


# ── what a judge reads without opening the code ──────────────────────────────

def test_health_reports_the_path_that_actually_runs(monkeypatch):
    monkeypatch.setattr(service, "USE_AGENT", False)
    assert service.health()["close_path"] == "deterministic"

    monkeypatch.setattr(service, "USE_AGENT", True)
    assert service.health()["close_path"] == "adk-agent"


def test_health_names_the_model_the_agent_would_actually_call(monkeypatch):
    """Reported from the same constant the agent reads, so health cannot name
    one model while the close reaches for another."""
    from archon.adapters.agents import DEFAULT_MODEL

    monkeypatch.setattr(service, "USE_AGENT", True)
    assert service.health()["model"] == DEFAULT_MODEL

    monkeypatch.setattr(service, "USE_AGENT", False)
    monkeypatch.setattr(service, "USE_GEMINI", False)
    assert service.health()["model"] is None


def test_the_model_is_one_this_project_can_actually_reach():
    """A model id nobody verified is an outage waiting for the judge.

    Every `gemini-3*` and `gemini-3.5*` candidate returns 404 from this
    project's Vertex endpoint; the 2.5 family returns 200. So the pin stays on
    a family that exists, and this test is where anyone raising it has to
    re-check availability first rather than trusting a version number.
    """
    from archon.adapters.agents import DEFAULT_MODEL

    assert DEFAULT_MODEL.startswith("gemini-"), DEFAULT_MODEL
    assert not DEFAULT_MODEL.startswith("gemini-3"), (
        f"{DEFAULT_MODEL} is not available in this project; probe the endpoint "
        "before changing this pin"
    )


# ── the command line ─────────────────────────────────────────────────────────

def test_the_agent_flag_drives_the_agent_rather_than_only_the_narrator(monkeypatch):
    """It claimed to for three weeks while doing something else."""
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        from archon.runtime.close import run_close
        from archon.runtime.mailbox import read_period

        documents, raw = read_period(kwargs["period"])
        return run_close(period=kwargs["period"], documents=documents,
                         company=kwargs.get("company"), store=LocalStore(),
                         raw_texts=raw), "closed"

    monkeypatch.setattr("archon.adapters.agents.run_agent_close", fake)
    monkeypatch.setattr("archon.adapters.agents.gemini_narrator", lambda *a, **k: None)

    from archon.cli import main

    assert main(["--period", "2026-07"]) == 0
    assert calls == [], "the plain path must not reach for a model"

    assert main(["--period", "2026-07", "--agent"]) == 0
    assert calls, "--agent never entered the agent path"


def test_an_agent_that_cannot_close_exits_non_zero(monkeypatch, capsys):
    """A pipeline that treats 'the agent gave up' as success is a pipeline that
    will one day report a month closed that nobody closed."""
    monkeypatch.setattr("archon.adapters.agents.run_agent_close",
                        lambda **_kw: (None, "I could not reach the model"))
    monkeypatch.setattr("archon.adapters.agents.gemini_narrator", lambda *a, **k: None)

    from archon.cli import main

    assert main(["--period", "2026-07", "--agent"]) == 2
    assert "did not produce a close" in capsys.readouterr().err
