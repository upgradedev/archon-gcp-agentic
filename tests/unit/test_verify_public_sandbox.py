"""Independent re-derivation of the section-6 claim about the anonymous close routes.

Written to REFUTE `tests/unit/test_repro_public_sandbox.py`, and it did not
refute it. Every mechanism that file asserted was re-measured here through a
harness built differently on purpose, so that agreeing was evidence rather than
an echo. The routes have since been fixed, and the same differently-built
harness is now pointed at the fix: the measurements below are unchanged, only
the expected answers moved.

Three deliberate differences from the file under scrutiny, all still in force:

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

What the re-derivation found, and where each finding stands now:

*   **The send seam sat outside the store control.** `service._close` handed
    `run_close` a `LocalStore()` and no `deliverer=`, so `close.py:285` resolved
    one from the environment and step 10 mailed the owner. The store argument
    bounded what a public run could WRITE and said nothing about what it could
    SEND. `_close(..., public=True)` now hands in `delivery.SandboxDelivery`,
    an object with no `deliver` path that reaches a transport, so an anonymous
    close reports `channel: "sandbox", delivered: false` whatever
    `ARCHON_SMTP_HOST` happens to say.

*   **A GET had side effects.** `web/app.js:697` fires `GET /api/close/{period}`
    on every page load, and that route used to run a FULL close -- agent,
    narrator and step 10 included -- whenever the durable store held no record.
    It now runs a deterministic close with `allow_model=False`, sends nothing
    and stores nothing.

*   **Nothing bounded the rate.** Neither public route declared a dependency and
    the only middleware was `security_headers`, so a stranger holding down
    refresh was an unbounded model bill on a URL printed in a submission.
    `ratelimit.PUBLIC_CLOSES` now allows three fresh closes per address per ten
    minutes, two at a time, and refuses the rest with a 429 that points at the
    saved close.

*   **The sandbox had a second door.** The first fix covered the month that was
    asked for and nothing else.
    `service._previous_statements` closes the month BEFORE it when the durable
    store has never seen it, purely to source a trend comparison, and that call
    passed no `deliverer=` either -- so an anonymous press on the July button
    still mailed the owner a letter about JUNE. It now runs with `commit=False`
    (service.py:202), which swaps in `RehearsalStore` and `RehearsalDelivery`
    inside `run_close` rather than adding a second deliverer somebody has to
    remember to pass. A month closed only to be compared against is not a month
    being closed.

Two corrections to the reproduction's framing are recorded as passing tests
below rather than as prose:

*   The cold path is not "a container that scaled from zero". The deployed store
    is Firestore (`GOOGLE_CLOUD_PROJECT` is set in `main.tf`), which survives
    every cold start. The GET fallback fires when *the durable store has no
    record for the period*, which is a different and more persistent condition:
    the public POST never writes, so pressing the button never populates it, and
    only an authenticated `/events` push ever can.
*   The blast radius was bounded in one direction the reproduction does not
    state: the recipient is read from `ARCHON_OWNER_EMAIL`, never from the
    request, and an unknown period is refused with a 404 before any close runs.
    This was owner-flooding and denial-of-wallet, not an open relay.
"""
from __future__ import annotations

import importlib
import smtplib

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from archon.adapters import agents, delivery, service  # noqa: E402
from archon.adapters.store import LocalStore  # noqa: E402
from archon.domain.digest import Digest  # noqa: E402

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

    # The instrument proves it works before anything is measured with it. Every
    # assertion in this file is now an assertion of ABSENCE, and a suite that
    # asserts absence is only as good as its ability to observe presence: two
    # bare `except Exception` sit between the routes and this list, so a stub
    # that raised would read as a quiet, clean, entirely fictional "nothing left
    # the machine". Here the shipped `get_deliverer()` builds a real
    # `SmtpDelivery` from the environment set above and the real `deliver` runs,
    # so one letter lands on the counter. Then the counter is cleared, and what
    # a test finds in it afterwards was put there by the product.
    control = delivery.get_deliverer()
    assert control.channel == "smtp", (
        f"harness not wired: the environment resolves {control.channel!r}, so nothing "
        f"below can tell a sandboxed route from an unconfigured one")
    assert control.deliver(probe_digest()).delivered is True, (
        "harness not wired: the shipped SmtpDelivery could not hand a message to the "
        "stubbed smtplib.SMTP, so every empty count below is a false negative")
    assert to_addresses(posted) == [OWNER]
    posted.clear()

    return posted


@pytest.fixture
def deployed_flags(monkeypatch):
    """Re-import the service under the environment `infra/main.tf` declares.

    `main.tf:329-336` sets ARCHON_AGENT_CLOSE and ARCHON_USE_GEMINI from
    `var.agent_close`, whose default is `"1"`. Both are read into module
    constants at import time, so setting the variable and reloading is the only
    way to measure the deployed default rather than a hand-set flag.

    Reloading `service` does not rebind `ratelimit.PUBLIC_CLOSES`, which lives
    in a module of its own, so the limiter measured after a reload is the same
    process-wide object the deployed service uses.
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


def refused_by_the_public_sandbox(body: dict) -> None:
    """Guard: the receipt must say a deliverer that CANNOT send did the filing.

    This guard used to demand the opposite -- `channel == "smtp"` -- because the
    finding was that an anonymous close reached the real sender. It is inverted
    now, and it matters more inverted than it did before.

    `close.py:483` and `delivery.py` are both bare `except Exception`, so a stub
    that is subtly wrong is swallowed twice and returns a clean 200 with zero
    mails counted. Before the fix that was a false NEGATIVE dressed as a clean
    bill of health; now it is worse, because "nothing was sent" is also exactly
    what the fix looks like. The `wire` fixture answers that by proving it can
    still count a real send before it hands the counter over, and this guard
    re-reads the environment on the way past: `get_deliverer()` resolves a REAL
    `SmtpDelivery` here and the close still reports `sandbox`, which makes the
    silence a control rather than an accident of configuration.
    """
    resolved = delivery.get_deliverer()
    assert resolved.channel == "smtp", (
        f"harness not wired: the environment resolves {resolved.channel!r}, so a close "
        f"that sends nothing proves nothing about the route")

    receipt = body.get("receipt") or {}
    assert receipt.get("channel") == "sandbox", (
        f"the close used the {receipt.get('channel')!r} channel; the public routes are "
        f"supposed to hand in SandboxDelivery, which has no code path that sends")
    assert receipt.get("delivered") is False, (
        f"the sandbox reported delivered={receipt.get('delivered')!r} "
        f"({receipt.get('detail')!r}); it is not allowed to deliver anything")


def to_addresses(messages) -> list[str]:
    return [str(m["To"]) for m in messages]


def periods_mailed(messages) -> list[str]:
    """Every digest subject opens with the period it closed."""
    return [str(m["Subject"]).split(" ")[0].rstrip(":") for m in messages]


def probe_digest() -> Digest:
    """A minimal letter, for proving the counter still counts."""
    return Digest(
        period=PERIOD, company=COMPANY, recipient=OWNER,
        subject=f"{PERIOD}: harness probe", body="harness probe",
        outcome="closed", run_id="harness-probe", net_profit=0.0,
        recoverable=0.0, action_count=0,
    )


# ── 1. the send seam is inside the control now ───────────────────────────────

def test_an_unauthenticated_post_cannot_reach_the_smtp_send_path(api, wire):
    """No credential in, and structurally no `send_message` out.

    `close_period` used to hand `run_close` a `LocalStore()` and stop. It never
    passed `deliverer=`, so `close.py:285` fell through to
    `delivery.get_deliverer()` and step 10 mailed the owner. The store argument
    bounded what a public run may WRITE; it said nothing about what it may SEND,
    and the service's own docstring called the store "the whole of the
    least-privilege control", which is the sentence that stopped anyone looking
    further.

    The route now passes `public=True`, and `_close` (service.py:124) answers
    that with `SandboxDelivery`. That is an object with no code path to a
    transport rather than a flag configuration can switch back on, so this holds
    on a deployment wired for real mail -- which is what `wire` sets up.

    The counter is read whole rather than filtered to this period, because the
    request runs more than one close: see the next test.
    """
    body = api.post(f"/api/close/{PERIOD}").json()

    refused_by_the_public_sandbox(body)
    assert to_addresses(wire) == [], (
        f"one anonymous POST /api/close/{PERIOD}, with no token and no session, "
        f"handed {len(wire)} message(s) about {periods_mailed(wire)} to "
        f"smtplib.SMTP.send_message for {set(to_addresses(wire))}")


def test_one_anonymous_press_sends_no_letter_at_all(api, wire):
    """The amplification, measured from the subjects rather than argued.

    `_close` calls `_previous_statements`, which finds 2026-06 absent from the
    durable store and closes it -- a second full `run_close`, with its own step
    10 -- purely to have a figure to compare this month against. So one press
    used to put TWO letters in the owner's inbox: the month that was asked for,
    and a month nobody mentioned.

    This is the assertion that mattered most, because it is the one the first
    attempt at the fix did not satisfy. `public=True` reached the requested month
    and stopped there; the previous-period run passed no `deliverer=` either, so
    it still resolved the real `SmtpDelivery` from the environment and an
    anonymous press on the July button mailed the owner about June. The counter
    is therefore read whole here rather than filtered to this period -- a
    sandbox with a second door is not a sandbox, and a test that only looks at
    the door it already knows about will never find the other one.

    `_previous_statements` now runs `run_close(..., commit=False)`, which is a
    rehearsal: `RehearsalStore` and `RehearsalDelivery` in place of the real
    ones, so there is no deliverer for a future caller to forget.
    """
    body = api.post(f"/api/close/{PERIOD}").json()

    refused_by_the_public_sandbox(body)
    assert periods_mailed(wire) == [], (
        f"one anonymous request put {len(wire)} owner letter(s) on the wire, for "
        f"{periods_mailed(wire)}; if {PREVIOUS} is in that list, the close taken only "
        f"to source a trend comparison is delivering again")


# ── 2. the GET the product's own front page fires ────────────────────────────

def test_the_pages_own_boot_fetch_is_a_get_that_cannot_mail(api, durable, wire):
    """`web/app.js:697` fires `GET /api/close/{period}` on every page load.

    So this was never only a crawler or a link preview: it was the shipped page
    booting. `read_close` used to serve the stored copy when there was one and
    run a FULL close when there was not, and a full close ends in step 10. A
    visitor arriving at a period the durable store had never seen mailed the
    owner without pressing anything, and a refresh loop did it repeatedly.

    The fallback is now deterministic and sandboxed: `allow_model=False` refuses
    the agent and the narrator outright, `public=True` refuses the send, and the
    payload is stamped `driver: deterministic` so the page can say which run the
    reader is looking at. Nothing is stored either, which is what keeps a GET
    from quietly deciding what the next GET will read.

    The previous-period close runs on this route too, so the counter is read
    whole here for the same reason as on the POST.
    """
    body = api.get(f"/api/close/{PERIOD}").json()

    refused_by_the_public_sandbox(body)
    assert body["driver"] == "deterministic", (
        f"a plain GET answered with driver={body.get('driver')!r}; a GET is meant to be "
        f"safe, and the agent path is neither free nor deterministic")
    assert to_addresses(wire) == [], (
        f"a plain GET /api/close/{PERIOD} -- the request the front page makes by itself "
        f"on load -- mailed the owner about {periods_mailed(wire)}")
    assert durable.load_close(COMPANY, PERIOD) is None, (
        "the GET fallback wrote its own answer into the durable store, so the next "
        "reader would be served a close nobody asked for as though it had been filed")


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

def test_the_public_route_sends_nothing_with_or_without_smtp_configured(
        api, wire, monkeypatch):
    """The accident that used to stand in for the control, now measured against it.

    No file under `infra/` sets ARCHON_SMTP_HOST, so on the deployed shape
    `get_deliverer()` returned `FiledDelivery` and the letter an anonymous
    visitor triggered was composed and kept. That was the only thing between a
    stranger and the owner's inbox, and it was a deployment-configuration
    accident rather than a control: one variable was the whole distance to a
    send, and the route reached the delivery seam unconditionally either way.

    So the honest test is not "it files when no host is set". It is that setting
    the host changes nothing, because the public route no longer resolves a
    deliverer from the environment at all. The first press below runs with SMTP
    fully configured and the second with the host removed, and both come back
    `sandbox`.
    """
    configured = api.post(f"/api/close/{PERIOD}").json()["receipt"]

    monkeypatch.delenv("ARCHON_SMTP_HOST", raising=False)
    unconfigured = api.post(f"/api/close/{PERIOD}").json()["receipt"]

    assert configured["channel"] == "sandbox", (
        f"with ARCHON_SMTP_HOST set the anonymous close resolved "
        f"{configured['channel']!r}; the environment is not supposed to be able to "
        f"reach this route")
    assert unconfigured["channel"] == "sandbox"
    assert [configured["delivered"], unconfigured["delivered"]] == [False, False]
    assert to_addresses(wire) == []


def test_the_recipient_is_never_taken_from_the_request(api, monkeypatch):
    """PASSES. The bound the reproduction does not state.

    `close.py` reads the address from `delivery.owner_address()`, i.e. from
    ARCHON_OWNER_EMAIL. The only caller-controlled input on the route is the
    period in the path, and it cannot steer the envelope. So the abuse this file
    is about was flooding the owner's own inbox, never relaying mail to a third
    party, and that distinction is the difference between a denial-of-wallet and
    an open relay.

    Read off the receipt rather than off the wire. The receipt names the address
    the digest was addressed to whether or not the channel sent it, which is the
    same assertion about the same call and does not need a message to have
    escaped to make it.
    """
    hostile = "attacker@elsewhere.example"

    smuggled = api.post(f"/api/close/{PERIOD}?to={hostile}",
                        json={"to": hostile, "recipient": hostile}).json()

    assert smuggled["receipt"]["recipient"] == OWNER, (
        f"an address supplied in the query string and the body reached the envelope as "
        f"{smuggled['receipt']['recipient']!r}; the route would be a relay")

    # The operator's environment is the only lever, which is what makes the
    # assertion above a property of the route and not of one hard-coded string.
    monkeypatch.setenv("ARCHON_OWNER_EMAIL", "bookkeeper@bellridgehaulage.example")
    moved = api.post(f"/api/close/{PERIOD}").json()

    assert moved["receipt"]["recipient"] == "bookkeeper@bellridgehaulage.example"


def test_an_unknown_period_is_refused_before_any_close_runs(api, wire):
    """PASSES. The other bound: the path parameter is not a free lever.

    `_close` calls `read_period` first and turns its `FileNotFoundError` into a
    404, so a period with no mail costs a directory check and nothing else.
    """
    assert api.post("/api/close/2099-01").status_code == 404
    assert wire == []


# ── 4. the model spend, switched on the way terraform switches it ────────────

def test_the_terraform_default_reaches_the_adk_agent_but_only_three_times(
        deployed_flags, durable, monkeypatch):
    """`var.agent_close` defaults to `"1"`; this proves that reaches the route.

    The constant is not assigned here -- the environment variable is set and the
    module re-imported, which is what Cloud Run does. That the agent runs on an
    anonymous POST is the demo working as intended: the button on the judge's
    page is supposed to drive a real close, and the stub returning no result
    exercises the fallback that keeps a model outage from taking the button
    down.

    What was wrong was that nothing counted the presses. Seven tools on a
    thinking model under a 600s request timeout, started by a caller who
    presented nothing, as many times as they liked, on a URL printed in a
    submission. `ratelimit.PUBLIC_CLOSES` now allows three per address per ten
    minutes and refuses the rest with a 429 that names the free alternative, so
    the spend a stranger can start is a number rather than a limit of patience.
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

    answers = [client.post(f"/api/close/{PERIOD}") for _ in range(6)]

    codes = [a.status_code for a in answers]
    assert codes == [200, 200, 200, 429, 429, 429], (
        f"six anonymous presses on terraform's default were answered {codes}; three "
        f"through and three refused is the declared bound")
    assert started == [PERIOD] * 3, (
        f"the presses that got through spent {len(started)} ADK agent close(s) "
        f"({started}); the limiter is supposed to stop the spend, not just the reply")
    assert "saved close" in answers[-1].json()["detail"], (
        "the 429 refuses without telling the caller where the free copy is, which "
        "turns a bounded demo into a broken one")


def test_the_terraform_default_reaches_gemini_but_the_limiter_caps_the_bill(
        deployed_flags, durable, monkeypatch):
    """Counted at the Vertex call, with the shipped narrator in between.

    `agents.gemini_narrator` is NOT replaced: the real closure runs and calls
    `run_report_pipeline`, which is where `build_report_pipeline` composes the
    three-stage `SequentialAgent` and where the billing would happen. Only that
    function is counted, so this is the spend as an invoice would see it.

    Six anonymous presses, and the sixth costs nothing. That is the whole
    change: the pipeline still runs for a caller who presented no credential,
    because that is what the demo is, but the number of times it can is fixed
    and small rather than however long someone leaves a tab refreshing.
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

    codes = [client.post(f"/api/close/{PERIOD}").status_code for _ in range(6)]

    assert codes == [200, 200, 200, 429, 429, 429]
    assert len(runs) == 3, (
        f"six anonymous requests spent {len(runs)} reporting-pipeline run(s), each a "
        f"three-stage SequentialAgent against Vertex; three is the declared ceiling "
        f"and the refusals are supposed to cost nothing")


def test_the_single_terraform_variable_switches_both_spends_on_at_once(
        deployed_flags, durable, monkeypatch):
    """`var.agent_close` feeds BOTH env vars, so neither has ever been measured
    on its own in production. Measured together here, for one request.

    `_close` used to compose a narrator for the agent call and then, when the
    agent returned nothing, compose a SECOND one for the deterministic fallback,
    so a request that failed over paid for the attempt and then paid for the
    close: two Vertex pipelines to phrase one summary. The narrator is now built
    once at service.py:127 and handed to whichever path runs, so a fail-over
    costs one narration, not two.
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
    assert agent_runs == [PERIOD]
    assert len(pipeline_runs) == 1, (
        f"ONE anonymous POST that failed over from the agent to the deterministic close "
        f"spent {len(pipeline_runs)} Vertex reporting-pipeline run(s); the fallback is "
        f"supposed to reuse the narrator the attempt already built")


# ── 5. what bounds the rate ──────────────────────────────────────────────────

def test_a_stranger_who_keeps_pressing_is_refused(api, wire):
    """There was no limiter, no dependency, no cooldown, no lock and no quota.

    The only middleware on the app was `security_headers` and neither public
    route declared a dependency. `main.tf` caps `max_instance_count` at 4, which
    bounds how many containers exist rather than what each request costs, and
    sets no `max_instance_request_concurrency`, leaving Cloud Run's default of
    80. Twelve presses in a row were twelve closes, twelve model conversations
    and twelve letters, all answered 200.

    `close_period` (service.py:252) now checks `ratelimit.PUBLIC_CLOSES` before
    anything else runs. Three per address per ten minutes, two concurrent. The
    limiter is in-process and the README says so rather than implying a
    distributed one: with four instances the real ceiling is four times this.
    A stated bound is still a bound, and there was none.
    """
    presses = 12
    answers = [api.post(f"/api/close/{PERIOD}") for _ in range(presses)]

    codes = [a.status_code for a in answers]
    assert codes == [200, 200, 200] + [429] * 9, (
        f"{presses} anonymous closes in a row were answered {codes}; three through and "
        f"the rest refused is what the limiter declares")
    assert to_addresses(wire) == [], (
        f"the presses that got through mailed the owner about {periods_mailed(wire)}")
    assert "saved close" in answers[-1].json()["detail"], (
        "a refusal that does not name the free copy reads as the demo being broken")


# ── 6. the half of the audit's claim that does NOT hold ──────────────────────

def test_neither_public_route_writes_anything_durable(monkeypatch):
    """PASSES. "Writes owner state to the durable store" is not reproduced.

    Every write method of the durable store is recorded, so this covers the keys
    a named-key assertion would miss: the `{period}#digest` record, the raw
    documents step 1 files, and anything the previous-period close might save.
    Nothing arrives, on either route.

    Which was the point of the finding rather than a contradiction of it. The
    mail IS owner-facing state an anonymous caller creates -- it just does not
    travel through the seam the least-privilege control watches, which is why
    fixing the store was never going to fix the send.
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

        def save_drafts(self, run_id, drafts, company=None, period=None):
            self.writes.append(("save_drafts", run_id))
            return super().save_drafts(run_id, drafts)

    recording = RecordingStore()
    monkeypatch.setattr(service, "get_store", lambda: recording)
    client = TestClient(service.app)

    assert client.post(f"/api/close/{PERIOD}").json()["outcome"] == "closed"
    client.get(f"/api/close/{PERIOD}")

    assert recording.writes == []


def test_a_period_that_is_a_path_cannot_reach_the_filesystem():
    """`GET /api/close/%2e%2e` answered 200 and ran a close over `corpus/..`.

    The period segment went from an anonymous URL to a directory join with
    nothing in between asserting it is a period. `..` is not a month, it is a
    traversal, and the route read whatever `.txt` files sat above the corpus and
    reported them as a company's books. Nothing here needed authentication.

    The shape of the fix matters more than this one input: the check is on the
    format a period HAS, not on the characters a traversal happens to use, so
    it closes the encodings nobody thought to enumerate as well.
    """
    from fastapi.testclient import TestClient

    from archon.adapters.service import app

    client = TestClient(app)

    for hostile in ("%2e%2e", "..", "%2e%2e%2f", "2026-13", "2026-7", "not-a-month"):
        response = client.get(f"/api/close/{hostile}")
        assert response.status_code == 404, (
            f"{hostile!r} was accepted as a period and answered "
            f"{response.status_code}")


def test_health_reports_the_host_that_answered():
    """The one field on this payload a reader can check against their own
    address bar, and the reason it exists.

    The submission video records the viewport, not the browser chrome, so the
    `.run.app` host appears nowhere on film. Taking it from the request rather
    than a constant means the payload cannot claim a host it is not being
    served from.
    """
    from fastapi.testclient import TestClient

    from archon.adapters.service import app

    body = TestClient(app).get("/api/health").json()

    assert body["served_from"] == "http://testserver", body.get("served_from")
    assert body["status"] == "ok"


def test_health_reports_the_scheme_the_caller_actually_used():
    """`request.base_url` alone says `http` on Cloud Run.

    TLS terminates at the front end and the request reaches the app over plain
    HTTP, so the app's own view of the scheme is the internal one. The service
    is reachable only over HTTPS from outside, and this string goes on the
    submission video, so reporting `http://` would put a URL on film that does
    not work.

    The forwarded header is trusted for a display string and nothing else --
    no routing, no redirect, no authorisation reads it -- and a value that is
    not a scheme is ignored rather than echoed.
    """
    from fastapi.testclient import TestClient

    from archon.adapters.service import app

    client = TestClient(app)

    behind_edge = client.get("/api/health", headers={"x-forwarded-proto": "https"}).json()
    assert behind_edge["served_from"] == "https://testserver"

    for junk in ("javascript", "gopher://x", "", "  "):
        body = client.get("/api/health", headers={"x-forwarded-proto": junk}).json()
        assert body["served_from"] == "http://testserver", f"{junk!r} was echoed"
