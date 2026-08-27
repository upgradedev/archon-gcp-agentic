"""Reproduction of the audit's section-6 claim about the anonymous close routes.

The claim is compound -- "the anonymous public `POST /api/close/{period}` (and a
GET fallback with side effects) can send SMTP, write owner state, and make
unbounded Gemini calls, with no rate limit" -- so it is driven apart here and
each half answered separately through the real FastAPI application.

`tests/unit/test_least_privilege.py` already proves the half that does not
hold: the public routes are handed a `LocalStore()` and the durable store is
untouched. Nothing here repeats that assertion for its own sake; the one test
below that looks at the store uses a recording store instead, so it can say
"no write of any kind, under any key" rather than "these four keys are absent",
and it is written as a PASSING test because the behaviour is correct.

What that file never looks at is the OTHER thing a close touches. The store is
handed in. The **deliverer is not.** `service._close` (line 138) calls
`run_close` without a `deliverer=`, so `close.py:232` falls through to
`delivery.get_deliverer()`, which returns a real `SmtpDelivery` the moment
`ARCHON_SMTP_HOST` is set in the environment. Step 10 of the close then mails
the owner. That seam runs outside the store control that the service's own
docstring calls "the whole of the least-privilege control".

Everything here is offline. The SMTP transport is injected -- `SmtpDelivery`
takes one for exactly this reason -- so the real `EmailMessage` is built and the
real `send_message` is called against a fake socket. Nothing is stubbed at the
level of the thing under test: the mail is counted where the standard library
would have put it on the wire.

A note on why the fake counts rather than explodes. `close.py:414` and
`delivery.py:118` are both bare `except Exception`, so a transport that raises
is swallowed twice over and comes back as a clean 200 with a `delivered=False`
receipt -- indistinguishable from "nothing was sent". Every mail assertion
below is therefore cross-checked against the receipt in the response body, so a
broken harness reads as a broken harness and not as an absence of the defect.

And a note on how far the mail finding actually reaches today, because the
difference matters. `infra/main.tf` sets `ARCHON_OWNER_EMAIL` but never sets
`ARCHON_SMTP_HOST`, so the deployed service currently resolves `FiledDelivery`
and nothing leaves the container. That is a deployment-configuration accident,
not a control: the route has no authentication, no bound and no deliverer of
its own, so a single environment variable is the whole distance between the
current posture and a stranger mailing the owner. The bound is asserted below
rather than asserted about, in
`test_with_no_smtp_host_configured_the_same_request_only_files`.

The model spend is not bounded that way. `infra/main.tf:329-335` sets both
`ARCHON_AGENT_CLOSE` and `ARCHON_USE_GEMINI` from `var.agent_close`, which
defaults to `"1"`, so both switches are ON in the deployed shape and the
anonymous route is a live billing surface right now.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from archon.adapters import agents, delivery, service  # noqa: E402
from archon.adapters.store import LocalStore  # noqa: E402

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


def recipients(messages) -> list[str]:
    return [str(m["To"]) for m in messages]


def periods_mailed(messages) -> list[str]:
    """Every digest subject opens with the period it closed."""
    return [str(m["Subject"]).split(":")[0].split(" ")[0] for m in messages]


def harness_is_wired(body: dict) -> None:
    """Guard: prove the injected transport really carried the send.

    Both layers around the send swallow exceptions, so a fake that is subtly
    wrong produces zero counted mails AND a 200. If this guard trips, the
    failure below would have been the harness, not the service.
    """
    receipt = body.get("receipt") or {}
    assert receipt.get("channel") == "smtp", (
        f"harness not wired: the close used the {receipt.get('channel')!r} "
        f"channel, so no SMTP path was exercised at all")
    assert receipt.get("delivered") is True, (
        f"harness not wired: SmtpDelivery reported {receipt.get('detail')!r}, "
        f"which means the injected transport raised rather than sent")


# ── 1. an anonymous POST reaches SMTP ────────────────────────────────────────

def test_an_anonymous_post_makes_the_service_send_the_owner_real_mail(api, sent):
    """The whole claim in one request: no token, no session, mail on the wire.

    `close_period` hands in an ephemeral store and stops there. It never hands
    in a deliverer, so step 10 of the close resolves one from the environment
    and mails the owner on behalf of a stranger.
    """
    body = api.post(f"/api/close/{PERIOD}").json()

    harness_is_wired(body)
    assert recipients(sent) == [], (
        f"an unauthenticated POST /api/close/{PERIOD} put {len(sent)} email(s) "
        f"on the wire to {set(recipients(sent))} via smtp.bellridge.example; "
        f"the ephemeral store bounded what the run could WRITE and bounded "
        f"nothing about what it could SEND")


# ── 2. the GET fallback has the same side effect, on a bound ─────────────────

def test_a_get_on_a_period_that_is_not_stored_also_sends_mail(api, sent):
    """`GET /api/close/{period}` is not a read when the store is cold.

    It falls through to a full close, and a full close ends in step 10. A
    crawler, a link preview or a browser prefetch is enough.
    """
    body = api.get(f"/api/close/{PERIOD}").json()

    harness_is_wired(body)
    assert recipients(sent) == [], (
        f"a plain GET /api/close/{PERIOD} sent {len(sent)} email(s); a GET is "
        f"supposed to be safe, and this one closed two months and mailed the "
        f"owner about {periods_mailed(sent)}")


def test_a_get_is_free_of_side_effects_once_the_period_is_in_the_store(
        api, durable, sent):
    """The bound on the finding above, stated rather than assumed.

    `read_close` returns the stored copy when there is one, and only closes
    when there is not. This is the correct behaviour and it passes: the side
    effect is on the COLD path, which is every GET on a container that has just
    scaled up from zero, and none afterwards.
    """
    durable.save_close(COMPANY, PERIOD, {"period": PERIOD, "outcome": "closed",
                                         "run_id": "already-filed"})

    body = api.get(f"/api/close/{PERIOD}").json()

    assert body["run_id"] == "already-filed"      # served, not recomputed
    assert sent == []


def test_with_no_smtp_host_configured_the_same_request_only_files(api, monkeypatch):
    """How far the mail finding reaches today, measured rather than argued.

    `infra/main.tf` never sets `ARCHON_SMTP_HOST`, so `get_deliverer()` returns
    `FiledDelivery` and the deployed service composes the owner's letter and
    sends nothing. This passes, and it is the honest bound on the four failures
    around it: the anonymous route reaches the delivery seam unconditionally,
    and one environment variable is all that stands between that and a send.
    """
    monkeypatch.delenv("ARCHON_SMTP_HOST", raising=False)

    receipt = api.post(f"/api/close/{PERIOD}").json()["receipt"]

    assert receipt["channel"] == "filed"
    assert receipt["delivered"] is False


# ── 3. one press, two months, two letters ────────────────────────────────────

def test_one_anonymous_press_closes_two_months_and_sends_two_letters(api, sent):
    """The amplification nobody asked for.

    `_close` calls `_previous_statements`, which finds nothing stored for
    2026-06 and quietly closes it too -- with a `LocalStore()`, correctly, and
    with no deliverer, which is the point. That second close runs its own step
    10. One anonymous request, two full closes, two emails.
    """
    body = api.post(f"/api/close/{PERIOD}").json()

    harness_is_wired(body)
    assert periods_mailed(sent) == [], (
        f"one anonymous request produced {len(sent)} owner letter(s), for "
        f"{periods_mailed(sent)}: the requested month plus {PREVIOUS}, which "
        f"was closed only to have something to compare against")


# ── 4. nothing bounds how often a stranger may do it ─────────────────────────

def test_nothing_at_all_bounds_how_often_a_stranger_may_press_the_button(api, sent):
    """No rate limit, no quota, no cooldown, no per-caller bound.

    There is no limiter middleware, no dependency, no marker and no lock on
    either public route -- the only middleware on the app is `security_headers`.
    `infra/main.tf` caps `max_instance_count` at 4, which bounds how many
    containers exist, not how much work each request costs.
    """
    presses = 12
    codes = [api.post(f"/api/close/{PERIOD}").status_code for _ in range(presses)]

    refused = [c for c in codes if c == 429]
    assert refused, (
        f"{presses} anonymous closes in a row were all answered {set(codes)} "
        f"with nothing refused, having run {presses * 2} full closes and sent "
        f"{len(sent)} owner emails; there is no 429, no cooldown and no quota "
        f"on the public route")


# ── 5. an anonymous request can spend model calls ────────────────────────────

def test_an_anonymous_request_can_reach_the_gemini_narrator(api, monkeypatch):
    """With `ARCHON_USE_GEMINI=1` the public button is a billing surface.

    `USE_GEMINI` is a module constant read at import, so it is set on the
    module rather than the environment -- setting the env var here would do
    nothing and the test would pass for the wrong reason.

    Each invocation counted below is one `run_report_pipeline` call, which
    `build_report_pipeline` composes as a three-stage `SequentialAgent`
    (reconciler, exceptions, narrator). That structure is read from
    `agents.py:455-470`; what is measured here is the reachability and the
    linear scaling.
    """
    calls: list[str] = []

    def counting_narrator(models=None):
        def narrate(facts: str) -> str:
            calls.append(facts)
            return "phrased by a model"
        return narrate

    monkeypatch.setattr(agents, "gemini_narrator", counting_narrator)
    monkeypatch.setattr(service, "USE_GEMINI", True)

    for _ in range(3):
        api.post(f"/api/close/{PERIOD}")

    assert calls == [], (
        f"three anonymous requests spent {len(calls)} model invocation(s) with "
        f"no credential presented and nothing capping the count; the spend "
        f"scales one-for-one with requests from the open internet")


def test_an_anonymous_request_drives_the_whole_adk_agent_when_it_is_switched_on(
        api, monkeypatch):
    """And this is the switch the deployment actually ships with.

    `infra/main.tf` sets `ARCHON_AGENT_CLOSE` from `var.agent_close`, default
    `"1"`, so `_close` routes the PUBLIC button through `run_agent_close`: an
    ADK agent loop over seven tools on a thinking model, under a Cloud Run
    request timeout raised to 600s specifically because that close runs for
    minutes.

    The fake returns no result, so the route falls back to the deterministic
    close and still answers 200 -- which is the same shape a real agent run
    has from outside, and is why nothing about the response tells a caller
    they just spent a model run.
    """
    started: list[str] = []

    def counting_agent_close(**kwargs):
        started.append(kwargs["period"])
        return None, "no result"

    monkeypatch.setattr(agents, "run_agent_close", counting_agent_close)
    monkeypatch.setattr(service, "USE_AGENT", True)

    codes = [api.post(f"/api/close/{PERIOD}").status_code for _ in range(3)]

    assert codes == [200, 200, 200]          # nothing about the reply says no
    assert started == [], (
        f"three anonymous requests each started a full ADK agent close "
        f"({started}); on the deployed default that is three thinking-model "
        f"runs of several minutes each, from callers who presented nothing, "
        f"against a service capped at 4 instances")


# ── 6. the half of the claim that does NOT hold ──────────────────────────────

def test_the_public_path_writes_nothing_durable_under_any_key(monkeypatch):
    """Stated the strong way, and it passes.

    `test_least_privilege.py` asserts four specific keys are absent after the
    button is pressed. This records every write the durable store is asked to
    perform instead, so it also covers the keys that file never named: the
    `{period}#digest` record, the raw documents from step 1, and anything the
    previous-period close might have filed. Nothing arrives, on either route.

    So "write owner state" is not reproduced for the STORE. The mail in the
    tests above is an owner-facing side effect an anonymous caller triggers,
    but it does not go through this seam, which is exactly why the store
    control did not stop it.
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
