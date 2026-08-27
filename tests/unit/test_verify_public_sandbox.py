"""Independent re-derivation of the section-6 claim about the anonymous close routes.

Written to REFUTE `tests/unit/test_repro_public_sandbox.py`, and it does not
refute it. Every mechanism that file asserts is re-measured here through a
harness built differently on purpose, so that agreeing is evidence rather than
an echo.

Three deliberate differences from the file under scrutiny:

1.  **Nothing in `archon` is replaced to make the mail count.** That file swaps
    `delivery.SmtpDelivery` for a lambda that injects a fake transport. Here the
    product builds the real `SmtpDelivery` with its real default transport --
    `smtplib.SMTP`, bound as a default argument at class-definition time -- and
    only the *methods of the standard library class* are stubbed. So
    `get_deliverer()`, `SmtpDelivery.deliver`, the `EmailMessage` and the
    `send_message` call are all the shipped code, and the stub sits exactly
    where the TCP connection would be.

2.  **The model switches are turned on by the environment `infra/main.tf`
    declares, not by assigning the module constants.** `USE_AGENT` and
    `USE_GEMINI` are read at import, so the file under scrutiny sets
    `service.USE_AGENT = True` directly -- which proves the branch is reachable
    but not that terraform's default reaches it. Here the variables are set and
    the module is re-imported, so what is measured is `var.agent_close = "1"`
    arriving at the public route.

3.  **The Gemini count is taken one layer deeper.** That file replaces
    `agents.gemini_narrator`. Here the real `gemini_narrator()` runs and only
    `agents.run_report_pipeline` -- the function that actually constructs the
    three-stage `SequentialAgent` and calls Vertex -- is counted.

Two corrections to the reproduction's framing are recorded as passing tests
below rather than as prose:

*   The cold path is not "a container that scaled from zero". The deployed store
    is Firestore (`GOOGLE_CLOUD_PROJECT` is set in `main.tf`), which survives
    every cold start. The GET fallback fires when *the durable store has no
    record for the period*, which is a different and more persistent condition:
    the public POST never writes, so pressing the button never populates it, and
    only an authenticated `/events` push ever can.
*   The blast radius is bounded in one direction the reproduction does not
    state: the recipient is read from `ARCHON_OWNER_EMAIL`, never from the
    request, and an unknown period is refused with a 404 before any close runs.
    This is owner-flooding and denial-of-wallet, not an open relay.
"""
from __future__ import annotations

import importlib
import smtplib

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from archon.adapters import agents, service  # noqa: E402
from archon.adapters.store import LocalStore  # noqa: E402

PERIOD = "2026-07"
PREVIOUS = "2026-06"
COMPANY = "Bell Ridge Haulage"
OWNER = "owner@bellridgehaulage.example"


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture
def durable():
    """The durable store, standing in for Firestore.

    `store.get_store()` hands back a FRESH `LocalStore` on every call when no
    project is configured, so without this every read is a miss for a reason
    that has nothing to do with the finding. Pinning one instance is what makes
    the local suite behave like the deployed shape, where Firestore persists
    across calls and across containers.
    """
    return LocalStore()


@pytest.fixture
def api(durable, monkeypatch):
    monkeypatch.setattr(service, "get_store", lambda: durable)
    return TestClient(service.app)


@pytest.fixture
def wire(monkeypatch):
    """Configure SMTP by environment and stub the socket, not the product.

    `SmtpDelivery.__init__` takes `transport=smtplib.SMTP` as a default
    argument, evaluated once when the class was defined, so the deliverer holds
    the standard library class itself. Patching that class's methods therefore
    intercepts the send without any Archon object being substituted: the real
    `get_deliverer()` runs, the real `SmtpDelivery` is constructed from the real
    environment, the real `EmailMessage` is built, and the real
    `SmtpDelivery.deliver` calls `send_message` -- which lands here instead of
    on a TCP connection.
    """
    posted: list = []

    def no_connect(self, host="", port=0, *args, **kwargs):
        self._archon_host, self._archon_port = host, port

    def record(self, message, *args, **kwargs):
        posted.append(message)
        return {}

    monkeypatch.setattr(smtplib.SMTP, "__init__", no_connect)
    monkeypatch.setattr(smtplib.SMTP, "__enter__", lambda self: self)
    monkeypatch.setattr(smtplib.SMTP, "__exit__", lambda self, *exc: False)
    monkeypatch.setattr(smtplib.SMTP, "send_message", record)
    monkeypatch.setattr(smtplib.SMTP, "starttls", lambda self, *a, **k: (220, b"ok"))
    monkeypatch.setattr(smtplib.SMTP, "login", lambda self, *a, **k: (235, b"ok"))

    monkeypatch.setenv("ARCHON_SMTP_HOST", "smtp.bellridge.example")
    monkeypatch.setenv("ARCHON_SMTP_PORT", "587")
    monkeypatch.setenv("ARCHON_DIGEST_FROM", "archon@bellridge.example")
    monkeypatch.setenv("ARCHON_OWNER_EMAIL", OWNER)
    monkeypatch.delenv("ARCHON_SMTP_USER", raising=False)
    monkeypatch.delenv("ARCHON_SMTP_PASSWORD", raising=False)
    return posted


@pytest.fixture
def deployed_flags(monkeypatch):
    """Re-import the service under the environment `infra/main.tf` declares.

    `main.tf:329-336` sets ARCHON_AGENT_CLOSE and ARCHON_USE_GEMINI from
    `var.agent_close`, whose default is `"1"`. Both are read into module
    constants at import time, so setting the variable and reloading is the only
    way to measure the deployed default rather than a hand-set flag.
    """
    def apply(**env):
        for name in ("ARCHON_AGENT_CLOSE", "ARCHON_USE_GEMINI"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return importlib.reload(service)

    yield apply

    for name in ("ARCHON_AGENT_CLOSE", "ARCHON_USE_GEMINI"):
        monkeypatch.delenv(name, raising=False)
    importlib.reload(service)


def carried_by_real_smtp(body: dict) -> None:
    """Guard: the receipt must say the shipped SMTP path did the sending.

    `close.py:414` and `delivery.py:118` are both bare `except Exception`, so a
    stub that is subtly wrong is swallowed twice and returns a clean 200 with
    zero mails counted -- a false NEGATIVE dressed as a clean bill of health.
    Reading the receipt out of the response body makes a broken harness fail
    loudly as a broken harness.
    """
    receipt = body.get("receipt") or {}
    assert receipt.get("channel") == "smtp", (
        f"harness not wired: the close used the {receipt.get('channel')!r} channel, "
        f"so no SMTP code path was exercised and the counts below mean nothing")
    assert receipt.get("delivered") is True, (
        f"harness not wired: SmtpDelivery reported {receipt.get('detail')!r}, "
        f"so the stub raised instead of accepting the message")


def to_addresses(messages) -> list[str]:
    return [str(m["To"]) for m in messages]


def periods_mailed(messages) -> list[str]:
    """Every digest subject opens with the period it closed."""
    return [str(m["Subject"]).split(" ")[0].rstrip(":") for m in messages]


# ── 1. the send seam is outside the store control ────────────────────────────

def test_an_unauthenticated_post_reaches_the_shipped_smtp_send_path(api, wire):
    """No credential in, real `send_message` out.

    `close_period` (service.py:233) hands `run_close` a `LocalStore()` and
    stops. It never passes `deliverer=`, so `close.py:232` falls through to
    `delivery.get_deliverer()` and step 10 mails the owner. The store argument
    bounds what a public run may WRITE; it says nothing about what it may SEND.
    """
    body = api.post(f"/api/close/{PERIOD}").json()

    carried_by_real_smtp(body)
    assert to_addresses(wire) == [], (
        f"one anonymous POST /api/close/{PERIOD}, with no token and no session, "
        f"handed {len(wire)} message(s) to smtplib.SMTP.send_message for "
        f"{set(to_addresses(wire))}; the ephemeral store bounded the writes and "
        f"bounded nothing about the sends")


def test_one_anonymous_press_runs_two_closes_and_mails_twice(api, wire):
    """The amplification, measured from the subjects rather than argued.

    `_close` calls `_previous_statements`, which finds 2026-06 absent from the
    durable store and closes it -- a second full `run_close`, with its own step
    10 -- purely to have a figure to compare against.
    """
    body = api.post(f"/api/close/{PERIOD}").json()

    carried_by_real_smtp(body)
    assert periods_mailed(wire) == [], (
        f"one request produced {len(wire)} owner letter(s), for "
        f"{periods_mailed(wire)}: the month that was asked for plus {PREVIOUS}, "
        f"closed only to source a trend comparison")


# ── 2. the GET the product's own front page fires ────────────────────────────

def test_the_pages_own_boot_fetch_is_a_get_that_closes_and_mails(api, wire):
    """`web/app.js:736` fires `GET /api/close/{period}` on every page load.

    So this is not only a crawler or a link preview: it is the shipped page
    booting. `read_close` (service.py:236-245) serves the stored copy when there
    is one and runs a FULL close when there is not, and a full close ends in
    step 10.
    """
    body = api.get(f"/api/close/{PERIOD}").json()

    carried_by_real_smtp(body)
    assert to_addresses(wire) == [], (
        f"a plain GET /api/close/{PERIOD} -- the request the front page makes "
        f"by itself on load -- ran {len(wire)} close(s) and mailed the owner "
        f"about {periods_mailed(wire)}; a GET is meant to be safe")


def test_the_get_is_a_pure_read_once_the_period_is_in_the_durable_store(
        api, durable, wire):
    """The bound, stated rather than assumed. PASSES.

    And a correction to the reproduction's framing: the trigger is an empty
    DURABLE store, not a cold container. `main.tf` sets GOOGLE_CLOUD_PROJECT, so
    the deployed backend is Firestore, which outlives every instance. The
    condition is therefore not transient -- the public POST never writes, so no
    amount of button-pressing ever satisfies it, and only an authenticated
    `/events` push can.
    """
    durable.save_close(COMPANY, PERIOD, {"period": PERIOD, "outcome": "closed",
                                         "run_id": "already-filed"})
    durable.save_close(COMPANY, PREVIOUS, {"period": PREVIOUS, "outcome": "closed",
                                           "run_id": "june", "statements": {}})

    body = api.get(f"/api/close/{PERIOD}").json()

    assert body["run_id"] == "already-filed"
    assert wire == []


# ── 3. how far it reaches on the shape that is actually deployed ─────────────

def test_with_no_smtp_host_the_deployed_shape_composes_and_sends_nothing(
        api, monkeypatch):
    """PASSES, and it is the honest bound on everything above.

    No file under `infra/` sets ARCHON_SMTP_HOST, so `get_deliverer()` returns
    `FiledDelivery` and the live service composes the owner's letter and keeps
    it. That is a deployment-configuration accident standing in for a control:
    the route still reaches the delivery seam unconditionally, and one variable
    is the whole distance to a send.
    """
    monkeypatch.delenv("ARCHON_SMTP_HOST", raising=False)

    receipt = api.post(f"/api/close/{PERIOD}").json()["receipt"]

    assert receipt["channel"] == "filed"
    assert receipt["delivered"] is False


def test_the_recipient_is_never_taken_from_the_request(api, wire):
    """PASSES. The bound the reproduction does not state.

    `close.py:409` reads the address from `delivery.owner_address()`, i.e. from
    ARCHON_OWNER_EMAIL. The only caller-controlled input on the route is the
    period in the path, and it cannot steer the envelope. So the abuse is
    flooding the owner's own inbox, not relaying mail to a third party.
    """
    api.post(f"/api/close/{PERIOD}")

    assert wire, "expected the send path to have run at all"
    assert set(to_addresses(wire)) == {OWNER}


def test_an_unknown_period_is_refused_before_any_close_runs(api, wire):
    """PASSES. The other bound: the path parameter is not a free lever.

    `_close` calls `read_period` first and turns its `FileNotFoundError` into a
    404, so a period with no mail costs a directory check and nothing else.
    """
    assert api.post("/api/close/2099-01").status_code == 404
    assert wire == []


# ── 4. the model spend, switched on the way terraform switches it ────────────

def test_the_terraform_default_routes_the_public_button_into_the_adk_agent(
        deployed_flags, durable, monkeypatch):
    """`var.agent_close` defaults to `"1"`; this proves that reaches the route.

    The constant is not assigned here -- the environment variable is set and the
    module re-imported, which is what Cloud Run does. The stub returns no
    result, so the route falls back to the deterministic close and still answers
    200: from outside, nothing about the reply says a thinking-model run was
    just spent.
    """
    svc = deployed_flags(ARCHON_AGENT_CLOSE="1")
    assert svc.USE_AGENT is True, "the env var terraform sets did not reach the module"
    monkeypatch.setattr(svc, "get_store", lambda: durable)

    started: list[str] = []

    def counting_agent_close(**kwargs):
        started.append(kwargs["period"])
        return None, "no result"

    monkeypatch.setattr(agents, "run_agent_close", counting_agent_close)
    client = TestClient(svc.app)

    codes = [client.post(f"/api/close/{PERIOD}").status_code for _ in range(3)]

    assert codes == [200, 200, 200]
    assert started == [], (
        f"ARCHON_AGENT_CLOSE=1 alone -- terraform's default -- put three "
        f"anonymous requests into a full ADK agent close ({started}), seven "
        f"tools on a thinking model under a 600s request timeout, from callers "
        f"who presented nothing")


def test_the_terraform_default_reaches_the_real_gemini_report_pipeline(
        deployed_flags, durable, monkeypatch):
    """Counted at the Vertex call, with the shipped narrator in between.

    `agents.gemini_narrator` is NOT replaced: the real closure runs and calls
    `run_report_pipeline`, which is where `build_report_pipeline` composes the
    three-stage `SequentialAgent` and where the billing would happen. Only that
    function is counted.
    """
    svc = deployed_flags(ARCHON_USE_GEMINI="1")
    assert svc.USE_GEMINI is True, "the env var terraform sets did not reach the module"
    monkeypatch.setattr(svc, "get_store", lambda: durable)

    runs: list[str] = []

    def counting_pipeline(facts, models=None, app_name="archon-report"):
        runs.append(facts)
        return {"summary": "phrased by a model"}

    monkeypatch.setattr(agents, "run_report_pipeline", counting_pipeline)
    client = TestClient(svc.app)

    for _ in range(3):
        client.post(f"/api/close/{PERIOD}")

    assert runs == [], (
        f"three anonymous requests spent {len(runs)} reporting-pipeline run(s), "
        f"each a three-stage SequentialAgent against Vertex, with no credential "
        f"presented by the caller and nothing capping the count")


def test_the_single_terraform_variable_switches_both_spends_on_at_once(
        deployed_flags, durable, monkeypatch):
    """`var.agent_close` feeds BOTH env vars, so neither has ever been measured
    on its own in production. Measured together here, for one request.

    `_close` composes a narrator for the agent call (service.py:126) and, if the
    agent returns nothing, composes another for the deterministic fallback
    (service.py:140), so a request that fails over pays for the attempt and then
    pays for the close.
    """
    svc = deployed_flags(ARCHON_AGENT_CLOSE="1", ARCHON_USE_GEMINI="1")
    assert (svc.USE_AGENT, svc.USE_GEMINI) == (True, True)
    monkeypatch.setattr(svc, "get_store", lambda: durable)

    agent_runs: list[str] = []
    pipeline_runs: list[str] = []

    def counting_agent_close(**kwargs):
        agent_runs.append(kwargs["period"])
        return None, "no result"

    def counting_pipeline(facts, models=None, app_name="archon-report"):
        pipeline_runs.append(facts)
        return {"summary": "phrased by a model"}

    monkeypatch.setattr(agents, "run_agent_close", counting_agent_close)
    monkeypatch.setattr(agents, "run_report_pipeline", counting_pipeline)
    client = TestClient(svc.app)

    assert client.post(f"/api/close/{PERIOD}").status_code == 200
    assert (agent_runs, pipeline_runs) == ([], []), (
        f"ONE anonymous POST on terraform's declared default cost "
        f"{len(agent_runs)} ADK agent close(s) and {len(pipeline_runs)} Vertex "
        f"reporting-pipeline run(s); the caller presented no credential and the "
        f"response is an ordinary 200 that says nothing about the spend")


# ── 5. nothing bounds the rate ───────────────────────────────────────────────

def test_nothing_refuses_a_stranger_who_keeps_pressing(api, wire):
    """No limiter, no dependency, no cooldown, no lock, no quota.

    The only middleware on the app is `security_headers` (service.py:69), and
    neither public route declares a dependency. `main.tf` caps
    `max_instance_count` at 4, which bounds how many containers exist rather
    than what each request costs, and sets no
    `max_instance_request_concurrency`, leaving Cloud Run's default of 80.
    """
    presses = 12
    codes = [api.post(f"/api/close/{PERIOD}").status_code for _ in range(presses)]

    assert [c for c in codes if c == 429], (
        f"{presses} anonymous closes in a row were all answered {set(codes)}; "
        f"they ran {presses * 2} closes and put {len(wire)} owner letters on "
        f"the wire, and nothing anywhere returned a 429")


# ── 6. the half of the audit's claim that does NOT hold ──────────────────────

def test_neither_public_route_writes_anything_durable(monkeypatch):
    """PASSES. "Writes owner state to the durable store" is not reproduced.

    Every write method of the durable store is recorded, so this covers the keys
    a named-key assertion would miss: the `{period}#digest` record, the raw
    documents step 1 files, and anything the previous-period close might save.
    Nothing arrives, on either route.

    Which is the point of the finding rather than a contradiction of it. The
    mail IS owner-facing state an anonymous caller creates -- it just does not
    travel through the seam the least-privilege control watches.
    """
    class RecordingStore(LocalStore):
        def __init__(self):
            super().__init__()
            self.writes: list[tuple] = []

        def put_document(self, name, content):
            self.writes.append(("put_document", name))
            return super().put_document(name, content)

        def save_run(self, run):
            self.writes.append(("save_run", run["run_id"]))
            return super().save_run(run)

        def save_close(self, company, period, payload):
            self.writes.append(("save_close", company, period))
            return super().save_close(company, period, payload)

        def save_drafts(self, run_id, drafts):
            self.writes.append(("save_drafts", run_id))
            return super().save_drafts(run_id, drafts)

    recording = RecordingStore()
    monkeypatch.setattr(service, "get_store", lambda: recording)
    client = TestClient(service.app)

    assert client.post(f"/api/close/{PERIOD}").json()["outcome"] == "closed"
    client.get(f"/api/close/{PERIOD}")

    assert recording.writes == []
