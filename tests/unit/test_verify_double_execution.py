"""Independent verification: does ONE production close execute `run_close` twice?

The other agent's repro drives `run_agent_close` directly with an injected
store, deliverer and narrator. That proves the code does it; it does not prove
the shipped product lets it happen. This file closes that gap by driving the
real trigger — `POST /events`, the Pub/Sub push route a bucket upload lands on
— through `TestClient`, with the deployed switches (`ARCHON_AGENT_CLOSE=1`,
`ARCHON_USE_GEMINI=1`, which `infra/main.tf` sets from `var.agent_close`,
default `"1"`) turned on the way terraform turns them on.

Nothing here is hand-assembled: the mail is the bundled corpus that every other
test and the demo use, the envelope is the scheduler shape `_period_from_envelope`
documents, and the tool order is the order `CLOSE_INSTRUCTION` prescribes.

`test_the_shipped_configuration_sends_no_mail` is deliberately first: it scopes
the "the owner gets two emails" claim, which is true only where
`ARCHON_SMTP_HOST` is configured, and no file under `infra/` configures it.
"""
from __future__ import annotations

from collections import Counter

import pytest

from archon.adapters import delivery as delivery_mod
from archon.adapters.delivery import FiledDelivery, Receipt
from archon.adapters.store import LocalStore
from archon.runtime.close import run_close
from archon.runtime.mailbox import read_period
from tests.conftest import PERIOD

PREVIOUS = "2026-06"


class RecordingStore:
    """A real `LocalStore` that records every write made through it."""

    backend = "memory"

    def __init__(self) -> None:
        self._inner = LocalStore()
        self.calls: Counter = Counter()
        self.close_keys: list[str] = []
        #: (period-key, number of drafts in the payload) for each books write.
        self.close_payloads: list[tuple[str, int]] = []

    def put_document(self, name, content):
        self.calls["put_document"] += 1
        return self._inner.put_document(name, content)

    def save_run(self, run):
        self.calls["save_run"] += 1
        return self._inner.save_run(run)

    def save_close(self, company, period, payload):
        self.calls["save_close"] += 1
        self.close_keys.append(period)
        if isinstance(payload, dict) and "drafts" in payload:
            self.close_payloads.append((period, len(payload["drafts"])))
        return self._inner.save_close(company, period, payload)

    def save_drafts(self, run_id, drafts, company=None, period=None):
        # One call per completed `run_close`, so this counts whole executions.
        self.calls["save_drafts"] += 1
        return self._inner.save_drafts(run_id, drafts)

    def claim(self, company, key, payload):
        # The events route takes its dedupe marker through this seam, so the
        # recorder has to be the real atomic claim rather than a permissive
        # stub. A double that always said "yours" would quietly hand the race
        # back to whoever asked, which is the very thing the route relies on
        # `claim` to settle.
        self.calls["claim"] += 1
        return self._inner.claim(company, key, payload)

    def retake(self, company, key, expected_attempt, payload):
        # Same reasoning as `claim`: the route's compare-and-set on the attempt
        # counter is what stops a superseded worker overwriting the one that
        # holds the claim, so the double has to be the real one.
        self.calls["retake"] += 1
        return self._inner.retake(company, key, expected_attempt, payload)

    def load_close(self, company, period):
        return self._inner.load_close(company, period)

    def load_run(self, run_id):
        return self._inner.load_run(run_id)

    def load_drafts(self, run_id):
        return self._inner.load_drafts(run_id)


class RecordingDeliverer:
    """Whatever `get_deliverer()` would have returned, counting deliveries."""

    channel = "recording"

    def __init__(self) -> None:
        self.sent: list = []

    def deliver(self, digest) -> Receipt:
        self.sent.append(digest)
        return Receipt(channel=self.channel, delivered=True,
                       detail=f"delivered to {digest.recipient}",
                       recipient=digest.recipient)


class RecordingNarrator:
    """Stands where `gemini_narrator()` stands. One call = one 3-stage pipeline."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, facts: str) -> str:
        self.calls.append(facts)
        return ""      # keep the deterministic summary; the books are untouched


def _seed_previous_close(store: RecordingStore, company: str) -> None:
    """Close the month before, so `_previous_statements` reads it rather than
    quietly running a second whole close of its own and muddying the counts."""
    documents, raw = read_period(PREVIOUS)
    run_close(period=PREVIOUS, documents=documents, company=company, store=store,
              raw_texts=raw, deliverer=FiledDelivery())


def _wire_production_agent_path(monkeypatch, store, deliverer, narrator):
    """Turn on exactly what `infra/main.tf` turns on, and nothing else.

    `USE_AGENT`/`USE_GEMINI` are module constants read at import, so the env
    vars terraform sets are applied here as the attributes they became.
    `DEFAULT_MODEL` is replaced by the scripted model, which is the documented
    injection seam ("Models are injectable everywhere") and the only reason
    this runs with no key.
    """
    from archon.adapters import agents as agents_mod
    from archon.adapters import service as service_mod

    monkeypatch.setattr(service_mod, "USE_AGENT", True)
    monkeypatch.setattr(service_mod, "USE_GEMINI", True)
    monkeypatch.setattr(service_mod, "get_store", lambda: store)
    monkeypatch.setattr(delivery_mod, "get_deliverer", lambda: deliverer)
    monkeypatch.setattr(agents_mod, "gemini_narrator", lambda *a, **k: narrator)
    return service_mod, agents_mod


def _observed(store: RecordingStore, deliverer, narrator) -> dict:
    """Counters for ONE period's close. Dedupe-marker writes are keyed
    `2026-07#event-...` and are excluded, so nothing here is inflated by them."""
    books = [k for k in store.close_keys if k == PERIOD]
    digests = [k for k in store.close_keys if k == f"{PERIOD}#digest"]
    return {
        "whole-close executions": store.calls["save_drafts"],
        "owner letters delivered": len(deliverer.sent),
        "books written (save_close 2026-07)": len(books),
        "digests written (save_close 2026-07#digest)": len(digests),
        "run trails written (save_run)": store.calls["save_run"],
        "narrator (Gemini pipeline) invocations": len(narrator.calls),
    }


#: What one close costs, pinned by the control below.
ONE_CLOSE = {
    "whole-close executions": 1,
    "owner letters delivered": 1,
    "books written (save_close 2026-07)": 2,      # filed, then rewritten with the full trail
    "digests written (save_close 2026-07#digest)": 1,
    "run trails written (save_run)": 2,
    "narrator (Gemini pipeline) invocations": 1,
}


def test_the_shipped_configuration_sends_no_mail(monkeypatch):
    """Scope check for the delivery claim, before any of it is counted.

    With `ARCHON_SMTP_HOST` unset — and nothing under `infra/` sets it — the
    deliverer is `FiledDelivery`, which appends to a list and sends nothing.
    So a second delivery is a second composed-and-filed letter in the deployed
    default, and a second real email only where an operator has configured SMTP.
    """
    monkeypatch.delenv("ARCHON_SMTP_HOST", raising=False)
    assert isinstance(delivery_mod.get_deliverer(), FiledDelivery)

    monkeypatch.setenv("ARCHON_SMTP_HOST", "smtp.example.test")
    assert delivery_mod.get_deliverer().channel == "smtp"


def test_the_deterministic_close_costs_one_of_everything(monkeypatch):
    """The control: one `run_close` over the same mail, so the agent figures
    below read as a multiple rather than as a bare number."""
    store, deliverer, narrator = RecordingStore(), RecordingDeliverer(), RecordingNarrator()
    monkeypatch.setattr(delivery_mod, "get_deliverer", lambda: deliverer)
    documents, raw = read_period(PERIOD)

    result = run_close(period=PERIOD, documents=documents, company="Bell Ridge Haulage",
                       store=store, raw_texts=raw, narrator=narrator)

    assert result.outcome == "closed"
    assert _observed(store, deliverer, narrator) == ONE_CLOSE


def test_one_trigger_on_the_events_route_closes_the_month_once(monkeypatch):
    """The real entry point: one Pub/Sub push, one month, one close.

    This is the deployed trigger with the deployed switches. The only doubles
    injected are the ones that count what leaves the process; the store is a
    real `LocalStore`, the ADK function-calling loop is real, and the close is
    the real one.
    """
    pytest.importorskip("google.adk")
    from fastapi.testclient import TestClient

    from tests.adk_fakes import ScriptedLlm

    store, deliverer, narrator = RecordingStore(), RecordingDeliverer(), RecordingNarrator()
    service_mod, agents_mod = _wire_production_agent_path(
        monkeypatch, store, deliverer, narrator)
    _seed_previous_close(store, service_mod.COMPANY)

    monkeypatch.setattr(agents_mod, "DEFAULT_MODEL", ScriptedLlm([
        ("call", "take_in_mail", {}),
        ("call", "post_journal", {}),
        ("call", "allocate_remittances", {}),
        ("call", "triage_exceptions", {}),
        ("call", "decide_actions", {"actions": {"0": "draft", "1": "escalate"}}),
        ("call", "draft_corrections", {}),
        ("call", "verify_and_file", {}),
        ("text", "July is closed."),
    ]))

    # Everything before the trigger is setup. Only the push is measured.
    store.calls.clear()
    store.close_keys.clear()
    store.close_payloads.clear()
    deliverer.sent.clear()
    narrator.calls.clear()

    with TestClient(service_mod.app) as client:
        response = client.post("/events", json={
            "message": {"attributes": {"period": PERIOD}, "messageId": "push-1"},
        })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "closed" and body["outcome"] == "closed"

    # Proves the agent actually drove it. `_close` swallows any agent failure
    # and falls back to the deterministic path, which would show one execution
    # for the wrong reason.
    stored = store.load_close(service_mod.COMPANY, PERIOD)
    assert stored["driver"] == "adk-agent"

    assert _observed(store, deliverer, narrator) == ONE_CLOSE


def test_the_durably_filed_close_is_never_one_the_agent_did_not_decide(monkeypatch):
    """The crash window, stated on its own.

    Whatever else re-running costs, the record Firestore holds for the period
    should only ever have been written by a run that had the agent's judgement
    in it. Cloud Run instances exit; if the durable record is first written
    from a run made under standing policy and only later overwritten, an exit
    in between leaves the owner's books saying something the agent did not
    decide.
    """
    pytest.importorskip("google.adk")
    from fastapi.testclient import TestClient

    from tests.adk_fakes import ScriptedLlm

    store, deliverer, narrator = RecordingStore(), RecordingDeliverer(), RecordingNarrator()
    service_mod, agents_mod = _wire_production_agent_path(
        monkeypatch, store, deliverer, narrator)
    _seed_previous_close(store, service_mod.COMPANY)

    # The agent chases nothing: a bookkeeper's prerogative, and it makes the
    # standing-policy run and the decided run visibly different (5 drafts v 0).
    monkeypatch.setattr(agents_mod, "DEFAULT_MODEL", ScriptedLlm([
        ("call", "take_in_mail", {}),
        ("call", "post_journal", {}),
        ("call", "triage_exceptions", {}),
        ("call", "decide_actions", {"actions": {str(i): "note" for i in range(10)}}),
        ("call", "verify_and_file", {}),
        ("text", "July is closed, nothing chased."),
    ]))

    store.close_payloads.clear()
    with TestClient(service_mod.app) as client:
        response = client.post("/events", json={
            "message": {"attributes": {"period": PERIOD}, "messageId": "push-2"},
        })

    # Stated before the counting, so that a route which never got as far as the
    # close names itself as a routing problem rather than turning up below as
    # the much vaguer "no books were filed at all".
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "closed"

    books = [drafts for key, drafts in store.close_payloads if key == PERIOD]
    assert books, "no books were filed at all"
    assert books == [books[-1]] * len(books), (
        "the period's record was filed with a different set of drafts than the "
        f"one the agent decided on: successive draft counts written for {PERIOD} "
        f"were {books}, and the last is what the agent chose"
    )
    # Named rather than left implicit, because the equality above cannot tell a
    # record that only ever held the agent's judgement from one that held the
    # standing-policy result at every step. This month drafts five corrections
    # under standing policy and none once the agent has declined to chase them,
    # so a five in that list is the pre-decision run reaching Firestore.
    assert books[-1] == 0, (
        "the agent noted all ten exceptions and chased none, so the filed record "
        f"should carry no drafts; it carries {books[-1]}"
    )


def test_a_second_decide_actions_call_does_not_cost_another_close(monkeypatch):
    """N+1, not merely 2.

    `CLOSE_INSTRUCTION` tells the agent it may be overruled, and
    `decide_actions` returns each overrule with its reason, so a model
    revising its judgement once is ordinary behaviour rather than a fault.
    Each revision should cost a re-decision, not another whole chore.
    """
    pytest.importorskip("google.adk")
    from archon.adapters.agents import run_agent_close
    from tests.adk_fakes import ScriptedLlm

    store, deliverer, narrator = RecordingStore(), RecordingDeliverer(), RecordingNarrator()
    monkeypatch.setattr(delivery_mod, "get_deliverer", lambda: deliverer)

    model = ScriptedLlm([
        ("call", "take_in_mail", {}),
        ("call", "post_journal", {}),
        ("call", "triage_exceptions", {}),
        ("call", "decide_actions", {"actions": {"0": "draft"}}),
        ("call", "decide_actions", {"actions": {"0": "escalate"}}),   # revised
        ("call", "verify_and_file", {}),
        ("text", "done"),
    ])
    result, _final = run_agent_close(period=PERIOD, company="Bell Ridge Haulage",
                                     model=model, store=store, narrator=narrator)

    assert result is not None and result.outcome == "closed"
    assert store.calls["save_drafts"] == 1, (
        f"one close, two decisions, {store.calls['save_drafts']} whole executions "
        f"and {len(deliverer.sent)} owner letters"
    )
