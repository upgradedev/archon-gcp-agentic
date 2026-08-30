"""Reproduction of the audit's section-6 claim about the anonymous close routes.

The claim was compound -- "the anonymous public `POST /api/close/{period}` (and
a GET fallback with side effects) can send SMTP, write owner state, and make
unbounded Gemini calls, with no rate limit" -- so it was driven apart here and
each half answered separately through the real FastAPI application.

`tests/unit/test_least_privilege.py` already proves the half that never held:
the public routes are handed a `LocalStore()` and the durable store is
untouched. Nothing here repeats that assertion for its own sake; the one test
below that looks at the store uses a recording store instead, so it can say
"no write of any kind, under any key" rather than "these four keys are absent".

What that file never looked at is the OTHER thing a close touches. The store
was handed in. The **deliverer was not.** `service._close` called `run_close`
with no `deliverer=`, so `close.py:285` fell through to
`delivery.get_deliverer()`, which returns a real `SmtpDelivery` the moment
`ARCHON_SMTP_HOST` is set in the environment. Step 10 of the close then mailed
the owner. That seam ran outside the store control that the service's own
docstring called "the whole of the least-privilege control", and the sentence
was the reason nobody looked further.

Both routes now hand in `SandboxDelivery`, an object with no code path that
sends, so the refusal is structural rather than a flag configuration can switch
back on. `GET /api/close/{period}` also stopped being a lever on a model: with
nothing stored it runs a deterministic close with `allow_model=False`, no
agent, no narrator, nothing stored. And `POST` is bounded by
`archon.adapters.ratelimit.PUBLIC_CLOSES`, three fresh closes per address per
ten minutes, so the spend a stranger can start no longer scales one-for-one
with requests from the open internet.

Everything here is offline. The SMTP transport is injected -- `SmtpDelivery`
takes one for exactly this reason -- so the real `EmailMessage` is built and the
real `send_message` is called against a fake socket. Nothing is stubbed at the
level of the thing under test: the mail is counted where the standard library
would have put it on the wire.

A note on why the fake counts rather than explodes, because it now cuts the
other way. `close.py:414` and `delivery.py:118` are both bare
`except Exception`, so a transport that raises is swallowed twice over and
comes back as a clean 200 with a `delivered=False` receipt. When the file
asserted mail was flowing that made a broken harness read as an absence of the
defect; now that it asserts mail is NOT flowing, it would make a broken harness
read as a clean bill of health, which is worse. `harness_can_send` therefore
proves the configured transport really carries a letter before any test claims
the wire is empty.

**One part of this finding is still live and one test below is left failing for
it.** `_close` sandboxes the close it was asked for, but it first calls
`_previous_statements`, which closes the month before -- a second full
`run_close`, with its own step 10, and with no `deliverer=`. That inner close
still resolves one from the environment, so on a deployment configured for real
mail one anonymous request still puts one owner letter on the wire, and the GET
that the front page fires on every load is not rate limited at all.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from archon.adapters import agents, delivery, ratelimit, service  # noqa: E402
from archon.adapters.store import LocalStore  # noqa: E402
from archon.domain.digest import Digest  # noqa: E402

PERIOD = "2026-07"
PREVIOUS = "2026-06"
COMPANY = "Bell Ridge Haulage"


# ── the harness ──────────────────────────────────────────────────────────────

@pytest.fixture
def durable():
    """One durable store for the life of a test, patched in as the real one."""
    return LocalStore()


@pytest.fixture
def api(durable, monkeypatch):
    monkeypatch.setattr(service, "get_store", lambda: durable)
    return TestClient(service.app)


@pytest.fixture
def sent(monkeypatch):
    """Configure SMTP the way a deployment would, and count what leaves.

    `ARCHON_SMTP_HOST` is read inside `get_deliverer()` at call time, so setting
    the environment is enough to select the real `SmtpDelivery`. Only the
    socket is faked: `SmtpDelivery.deliver` still builds the `EmailMessage`,
    still opens the transport as a context manager and still calls
    `send_message`, which is where these land.

    The environment is the hostile one on purpose. A public route that cannot
    send when no mail server is configured has proved nothing.
    """
    delivered: list = []

    class Transport:
        def __init__(self, host, port):
            self.host, self.port = host, port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, message):
            delivered.append(message)

    monkeypatch.setenv("ARCHON_SMTP_HOST", "smtp.bellridge.example")
    monkeypatch.setenv("ARCHON_SMTP_PORT", "587")
    monkeypatch.setenv("ARCHON_DIGEST_FROM", "archon@bellridge.example")
    monkeypatch.setenv("ARCHON_OWNER_EMAIL", "owner@bellridgehaulage.example")
    monkeypatch.delenv("ARCHON_SMTP_USER", raising=False)
    monkeypatch.delenv("ARCHON_SMTP_PASSWORD", raising=False)

    real = delivery.SmtpDelivery
    monkeypatch.setattr(
        delivery, "SmtpDelivery", lambda **kw: real(transport=Transport, **kw))
    return delivered


class Spend:
    """What a request cost in model calls, counted at both switches."""

    def __init__(self) -> None:
        self.narrations: list[str] = []
        self.agent_closes: list[str] = []


@pytest.fixture
def spend(monkeypatch):
    """Turn both model switches on and put a counter under each.

    `USE_GEMINI` and `USE_AGENT` are module constants read at import, so they
    are set on the module rather than the environment -- setting the env vars
    here would do nothing and the tests would pass for the wrong reason.

    Each narration counted is one `run_report_pipeline` call, which
    `build_report_pipeline` composes as a three-stage `SequentialAgent`
    (reconciler, exceptions, narrator). Each agent close counted is an ADK loop
    over seven tools on a thinking model, under a Cloud Run request timeout
    raised to 600s specifically because that close runs for minutes.
    """
    counted = Spend()

    def counting_narrator(models=None):
        def narrate(facts: str) -> str:
            counted.narrations.append(facts)
            return "phrased by a model"
        return narrate

    def counting_agent_close(**kwargs):
        counted.agent_closes.append(kwargs["period"])
        return None, "no result"

    monkeypatch.setattr(agents, "gemini_narrator", counting_narrator)
    monkeypatch.setattr(agents, "run_agent_close", counting_agent_close)
    monkeypatch.setattr(service, "USE_GEMINI", True)
    monkeypatch.setattr(service, "USE_AGENT", True)
    return counted


def recipients(messages) -> list[str]:
    return [str(m["To"]) for m in messages]


def periods_mailed(messages) -> list[str]:
    """Every digest subject opens with the period it closed."""
    return [str(m["Subject"]).split(":")[0].split(" ")[0] for m in messages]


def harness_can_send(sent: list) -> None:
    """Guard: prove the configured transport really would carry a letter.

    This guard used to read the receipt out of the response and demand it say
    `smtp`, which is the one thing a fixed public route can never say. So it is
    asked of the environment directly instead: `get_deliverer()` resolves the
    same real `SmtpDelivery` the fixture configured, a letter is handed to it,
    and the fake transport must count it.

    Without this, the assertions below would be worthless. Both layers around
    the send swallow exceptions, so a fake that is subtly wrong produces zero
    counted mails and a clean 200, which is exactly the reading a working
    sandbox produces. After this runs, a zero is the service refusing.
    """
    probe = Digest(
        period="0000-00", company=COMPANY, recipient=delivery.owner_address(),
        subject="0000-00 harness probe", body="no part of any close",
        outcome="closed", run_id="harness-probe", net_profit=0.0,
        recoverable=0.0, action_count=0,
    )

    receipt = delivery.get_deliverer().deliver(probe)

    assert (receipt.channel, receipt.delivered) == ("smtp", True), (
        f"harness not wired: the configured deliverer reported "
        f"{receipt.channel!r} / {receipt.detail!r}, so nothing below is "
        f"measuring a send at all")
    assert len(sent) == 1, (
        "harness not wired: the probe letter never reached the fake transport")
    sent.clear()


# ── 1. an anonymous POST cannot reach SMTP ───────────────────────────────────

def test_an_anonymous_post_cannot_make_the_service_send_the_owner_mail(api, sent):
    """The whole claim in one request: no token, no session, no send.

    `close_period` used to hand in an ephemeral store and stop there. It never
    handed in a deliverer, so step 10 of the close resolved one from the
    environment and mailed the owner on behalf of a stranger. The bound was on
    what the run could WRITE and there was none on what it could SEND.

    SMTP is fully configured here and the guard above proves it live, so the
    `sandbox` channel is the route refusing rather than the deployment being
    unconfigured.
    """
    harness_can_send(sent)

    receipt = api.post(f"/api/close/{PERIOD}").json()["receipt"]

    assert receipt["channel"] == "sandbox", (
        f"an unauthenticated POST /api/close/{PERIOD} reached the "
        f"{receipt['channel']!r} channel with smtp.bellridge.example "
        f"configured; the public routes must hand in a deliverer that has no "
        f"code path that sends")
    assert receipt["delivered"] is False


# ── 2. the GET fallback neither sends nor spends ─────────────────────────────

def test_a_get_on_a_period_that_is_not_stored_neither_sends_nor_spends(
        api, sent, spend):
    """`GET /api/close/{period}` was not a read when the store was cold.

    It fell through to a full close, and a full close ended in step 10 and, on
    the switches terraform ships, in an ADK agent loop and a Vertex narration
    first. `web/app.js` fires this on every page load, so a crawler, a link
    preview or a judge opening the page was enough, and a refresh loop was an
    unbounded bill from a route that reads.

    The fallback is now deterministic and hard-refused: `allow_model=False` is
    not a preference, so no agent and no narrator are composed at all, and the
    payload says which path produced it rather than leaving a caller to guess.
    Five loads are read here because one page open is not a rate.
    """
    harness_can_send(sent)

    for _ in range(5):
        body = api.get(f"/api/close/{PERIOD}").json()

    assert body["receipt"]["channel"] == "sandbox"
    assert body["receipt"]["delivered"] is False
    assert body["driver"] == "deterministic"
    assert (spend.agent_closes, spend.narrations) == ([], []), (
        f"five anonymous page loads spent {len(spend.agent_closes)} agent "
        f"close(s) and {len(spend.narrations)} narration(s) on a route that is "
        f"supposed to read; a GET is not a lever on a thinking model")


def test_a_get_is_free_of_side_effects_once_the_period_is_in_the_store(
        api, durable, sent):
    """The bound on the finding above, stated rather than assumed.

    `read_close` returns the stored copy when there is one, and only closes
    when there is not. This was correct behaviour before the fix and it is
    correct behaviour after it: the side effect was on the COLD path, which is
    every GET against a durable store with no record for the period, and none
    afterwards.
    """
    harness_can_send(sent)
    durable.save_close(COMPANY, PERIOD, {"period": PERIOD, "outcome": "closed",
                                         "run_id": "already-filed"})

    body = api.get(f"/api/close/{PERIOD}").json()

    assert body["run_id"] == "already-filed"      # served, not recomputed
    assert sent == []


def test_the_public_channel_is_the_sandbox_whether_or_not_smtp_is_configured(
        api, sent, monkeypatch):
    """The refusal is structural, which is the whole difference from before.

    `infra/main.tf` never sets `ARCHON_SMTP_HOST`, so `get_deliverer()` returned
    `FiledDelivery` and the deployed service composed the owner's letter and
    sent nothing. That was a deployment-configuration accident standing in for
    a control, and it was the honest bound on the failures around it: the
    anonymous route reached the delivery seam unconditionally and one
    environment variable was all that stood between that and a send.

    Now the same request answers `sandbox` with the variable set and with it
    unset, so there is no longer an environment that turns the send back on.
    """
    harness_can_send(sent)

    configured = api.post(f"/api/close/{PERIOD}").json()["receipt"]
    monkeypatch.delenv("ARCHON_SMTP_HOST", raising=False)
    unconfigured = api.post(f"/api/close/{PERIOD}").json()["receipt"]

    assert configured["channel"] == unconfigured["channel"] == "sandbox", (
        f"the channel moved with the environment: {configured['channel']!r} "
        f"configured, {unconfigured['channel']!r} unconfigured. A public route "
        f"whose ability to send depends on a variable is not bounded")
    assert (configured["delivered"], unconfigured["delivered"]) == (False, False)


# ── 3. one press, two months, and the letter that still goes ─────────────────

def test_one_anonymous_press_puts_no_letter_at_all_on_the_wire(api, sent):
    """The amplification nobody asked for, and the half of it still standing.

    `_close` calls `_previous_statements`, which finds nothing stored for
    2026-06 and quietly closes it too, purely to have something to compare
    against. That second close runs its own step 10. One anonymous request was
    therefore two full closes and two emails.

    The requested month is sandboxed now. The comparison close is not:
    `_previous_statements` calls `run_close` with a `LocalStore()` and no
    `deliverer=`, so `close.py:285` still resolves one from the environment and
    the month before is mailed to the owner by a stranger. This is left FAILING
    on purpose. Half a sandbox on a public route is not a sandbox, and the
    route that reaches this path on every page load is the unrated GET.
    """
    harness_can_send(sent)

    api.post(f"/api/close/{PERIOD}")

    assert periods_mailed(sent) == [], (
        f"one anonymous request put {len(sent)} owner letter(s) on the wire, "
        f"for {periods_mailed(sent)}, addressed to {set(recipients(sent))}: "
        f"not the month that was asked for, which is sandboxed, but {PREVIOUS}, "
        f"closed only to source a trend comparison and closed without a "
        f"deliverer")


# ── 4. how often a stranger may press it ─────────────────────────────────────

def test_a_stranger_who_keeps_pressing_is_refused(api):
    """There used to be no rate limit, no quota, no cooldown, no bound at all.

    No limiter middleware, no dependency, no marker and no lock on either
    public route: the only middleware on the app was `security_headers`.
    `infra/main.tf` caps `max_instance_count` at 4, which bounds how many
    containers exist, not how much work each request costs, so one visitor
    holding down refresh was an unbounded bill on a URL printed in a
    submission.

    `ratelimit.PUBLIC_CLOSES` is a sliding window per caller. It is in-process
    and says so: four Cloud Run instances means the real ceiling is four times
    this. A bound that is stated is the thing that did not exist before.

    The refusal has to be usable, so it is read as well as counted. A 429 that
    only says no turns a judge away from the demo; this one points at the saved
    close, which is the same books and costs nothing.
    """
    allowed = ratelimit.PUBLIC_CLOSES.per_caller
    presses = allowed + 9

    replies = [api.post(f"/api/close/{PERIOD}") for _ in range(presses)]
    codes = [reply.status_code for reply in replies]

    assert codes == [200] * allowed + [429] * (presses - allowed), (
        f"{presses} anonymous closes in a row were answered {codes}; the window "
        f"allows {allowed} and everything after it should be refused")
    assert "saved close" in replies[-1].json()["detail"], (
        f"the refusal reads {replies[-1].json()['detail']!r}, which leaves a "
        f"caller with nowhere to go")


# ── 5. how much an anonymous request may spend ───────────────────────────────

def test_the_gemini_narrations_an_anonymous_caller_can_start_are_bounded(
        api, spend):
    """With `ARCHON_USE_GEMINI=1` the public button was a billing surface.

    The spend scaled one-for-one with requests from the open internet: every
    press was another `run_report_pipeline` call against Vertex, from a caller
    who presented no credential, with nothing counting them.

    Twelve presses are made here rather than three, because the point is no
    longer that a narration happens. It is that the twelfth press cannot buy
    one: the count stops at the window and stops moving.
    """
    allowed = ratelimit.PUBLIC_CLOSES.per_caller

    for _ in range(allowed + 9):
        api.post(f"/api/close/{PERIOD}")

    assert len(spend.narrations) == allowed, (
        f"{allowed + 9} anonymous requests spent {len(spend.narrations)} model "
        f"invocation(s) against a window of {allowed}; the spend a stranger can "
        f"start must stop at the bound rather than track the request count")


def test_the_adk_agent_an_anonymous_caller_can_start_is_bounded(api, spend):
    """And this is the switch the deployment actually ships with.

    `infra/main.tf` sets `ARCHON_AGENT_CLOSE` from `var.agent_close`, default
    `"1"`, so `_close` routes the PUBLIC button through `run_agent_close`. Each
    one is an agent loop over seven tools on a thinking model, minutes long,
    against a service capped at 4 instances. Nothing about a 200 tells a caller
    they just spent one, which is why the count is taken here and not inferred
    from the replies.

    The fake returns no result, so the route falls back to the deterministic
    close and still answers 200 -- the same shape a real agent run has from
    outside, and the reason the fallback is worth exercising.
    """
    allowed = ratelimit.PUBLIC_CLOSES.per_caller

    codes = [api.post(f"/api/close/{PERIOD}").status_code
             for _ in range(allowed + 9)]

    assert codes.count(200) == allowed
    assert spend.agent_closes == [PERIOD] * allowed, (
        f"{allowed + 9} anonymous requests started {len(spend.agent_closes)} "
        f"full ADK agent close(s) ({spend.agent_closes}) against a window of "
        f"{allowed}; a refused press must cost no model run at all")


# ── 6. the half of the claim that never held ─────────────────────────────────

def test_the_public_path_writes_nothing_durable_under_any_key(monkeypatch):
    """Stated the strong way, and it passed then and passes now.

    `test_least_privilege.py` asserts four specific keys are absent after the
    button is pressed. This records every write the durable store is asked to
    perform instead, so it also covers the keys that file never named: the
    `{period}#digest` record, the raw documents from step 1, and anything the
    previous-period close might have filed. Nothing arrives, on either route.

    So "write owner state" was never reproduced for the STORE. The mail was an
    owner-facing side effect an anonymous caller triggered, but it did not go
    through this seam, which is exactly why the store control did not stop it.
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
