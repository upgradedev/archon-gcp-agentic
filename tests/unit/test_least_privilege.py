"""A7: the public identity cannot write, proven by attempting it.

The standard asks for a test that tries a write with the public credential and
asserts it is refused. Refused here means something stronger than a 403: the
anonymous route is structurally unable to reach the durable store, because it is
handed an ephemeral one. So the test presses the public button and then asserts
the durable store is still empty.

That control was not there until this file existed. The demo button closed a
month straight into Firestore, and anyone on the internet could have done it.
"""
from __future__ import annotations

import pytest

from archon.adapters import auth
from archon.adapters.store import LocalStore


@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from archon.adapters import service

    # One durable store for the life of the test, so "did anything land in it"
    # is a question that can be asked.
    durable = LocalStore()
    monkeypatch.setattr(service, "get_store", lambda: durable)
    return TestClient(service.app), durable


# ── the public route cannot write ────────────────────────────────────────────

def test_the_anonymous_close_button_leaves_the_durable_store_untouched(client):
    """Press it as a stranger would, then look at what a stranger changed."""
    api, durable = client

    body = api.post("/api/close/2026-07").json()

    assert body["outcome"] == "closed"          # it really did the work
    assert durable.load_close("Bell Ridge Haulage", "2026-07") is None
    assert durable.load_run(body["run_id"]) is None
    assert durable.load_drafts(body["run_id"]) == []


def test_the_anonymous_read_does_not_persist_the_close_it_runs(client):
    """A cold container serves a close rather than an empty state. It must not
    quietly promote that convenience run into the durable record."""
    api, durable = client

    api.get("/api/close/2026-07")

    assert durable.load_close("Bell Ridge Haulage", "2026-07") is None


def test_the_public_books_are_identical_to_the_ones_that_would_be_stored(client):
    """The control restricts what the run can touch, never what it computes."""
    api, _ = client
    from archon.runtime.close import run_close
    from archon.runtime.mailbox import read_period

    documents, raw = read_period("2026-07")
    reference = run_close(period="2026-07", documents=documents,
                          company="Bell Ridge Haulage", store=LocalStore(),
                          raw_texts=raw)

    public = api.post("/api/close/2026-07").json()

    assert public["statements"] == reference.to_dict()["statements"]
    assert public["findings"] == reference.to_dict()["findings"]
    assert public["run_id"] == reference.run_id


# ── the trusted route refuses an unverified caller ───────────────────────────

def test_events_is_open_when_no_audience_is_configured(client, monkeypatch):
    """Local and demo: the route works, and health says the posture out loud."""
    api, _ = client
    monkeypatch.delenv(auth.AUDIENCE_ENV, raising=False)

    assert auth.posture() == "open"
    assert api.get("/api/health").json()["events_auth"] == "open"
    assert api.post("/events", json={"message": {"attributes": {"period": "2026-07"}}}
                    ).json()["status"] == "closed"


def test_a_configured_deployment_refuses_an_unauthenticated_trigger(client, monkeypatch):
    """The write path, attempted with no credential at all."""
    api, durable = client
    monkeypatch.setenv(auth.AUDIENCE_ENV, "https://archon.example.run.app")

    response = api.post("/events", json={"message": {"attributes": {"period": "2026-07"}}})

    assert response.status_code == 403
    assert response.json()["reason"] == "no bearer token"
    assert durable.load_close("Bell Ridge Haulage", "2026-07") is None


def test_a_configured_deployment_reports_its_posture(client, monkeypatch):
    api, _ = client
    monkeypatch.setenv(auth.AUDIENCE_ENV, "https://archon.example.run.app")

    assert api.get("/api/health").json()["events_auth"] == "verified-oidc"


def test_refusal_happens_before_the_body_is_even_parsed(client, monkeypatch):
    """A refused caller must not get to exercise the parser either."""
    api, _ = client
    monkeypatch.setenv(auth.AUDIENCE_ENV, "https://archon.example.run.app")

    assert api.post("/events", content=b"not json at all",
                    headers={"Content-Type": "application/json"}).status_code == 403


# ── the verifier itself ──────────────────────────────────────────────────────

def test_no_audience_means_the_route_is_open():
    assert auth.verify_push_request(None, verifier=None).allowed is True


def test_a_valid_token_for_the_right_audience_is_allowed(monkeypatch):
    monkeypatch.setenv(auth.AUDIENCE_ENV, "aud")

    verdict = auth.verify_push_request(
        "Bearer good", verifier=lambda token, aud: {"email": "push@project.iam"})

    assert verdict.allowed
    assert verdict.caller == "push@project.iam"


@pytest.mark.parametrize("header", [None, "", "good", "Basic abc", "Bearer ", "Bearer"])
def test_anything_that_is_not_a_bearer_token_is_refused(monkeypatch, header):
    monkeypatch.setenv(auth.AUDIENCE_ENV, "aud")

    assert not auth.verify_push_request(header, verifier=lambda t, a: {}).allowed


def test_a_token_the_verifier_rejects_is_refused(monkeypatch):
    monkeypatch.setenv(auth.AUDIENCE_ENV, "aud")

    def reject(token, aud):
        raise ValueError("wrong audience")

    verdict = auth.verify_push_request("Bearer bad", verifier=reject)

    assert not verdict.allowed
    assert "ValueError" in verdict.reason


def test_a_missing_verification_library_fails_closed(monkeypatch):
    """The rule this exists for: a deployment that asked for verification and
    cannot verify must refuse, not wave the request through."""
    monkeypatch.setenv(auth.AUDIENCE_ENV, "aud")
    monkeypatch.setattr(auth, "_google_verifier", lambda: None)

    verdict = auth.verify_push_request("Bearer anything")

    assert not verdict.allowed
    assert verdict.reason == "token verification is unavailable"


def test_the_wrong_service_account_is_refused_even_with_a_valid_token(monkeypatch):
    monkeypatch.setenv(auth.AUDIENCE_ENV, "aud")
    monkeypatch.setenv(auth.CALLER_ENV, "push@project.iam")

    verdict = auth.verify_push_request(
        "Bearer good", verifier=lambda t, a: {"email": "someone-else@evil"})

    assert not verdict.allowed
    assert "not the configured service account" in verdict.reason


def test_the_audience_is_passed_through_to_the_verifier(monkeypatch):
    """A token valid for a different service must not be accepted here."""
    monkeypatch.setenv(auth.AUDIENCE_ENV, "https://archon.example.run.app")
    seen = {}

    auth.verify_push_request(
        "Bearer good",
        verifier=lambda token, aud: seen.update(audience=aud) or {"email": "x"})

    assert seen["audience"] == "https://archon.example.run.app"


def test_claims_that_are_not_a_mapping_are_refused(monkeypatch):
    monkeypatch.setenv(auth.AUDIENCE_ENV, "aud")

    assert not auth.verify_push_request("Bearer t", verifier=lambda t, a: "nope").allowed


def test_an_env_var_that_is_set_and_empty_still_falls_back(monkeypatch):
    """The trap that put a gap where a recipient should be.

    Terraform declares `ARCHON_OWNER_EMAIL` and its variable defaults to the
    empty string, so on the deployed service the variable was SET AND EMPTY.
    `os.getenv(name, default)` returns the default only when the name is
    ABSENT, so the live digest was addressed to nothing and its receipt read
    "composed for  and filed" with a hole in the sentence.

    Both states are asserted, because only one of them was ever tested.
    """
    from archon.adapters.delivery import DEFAULT_OWNER, owner_address

    monkeypatch.delenv("ARCHON_OWNER_EMAIL", raising=False)
    assert owner_address() == DEFAULT_OWNER

    monkeypatch.setenv("ARCHON_OWNER_EMAIL", "")
    assert owner_address() == DEFAULT_OWNER, "set-and-empty must not win"

    monkeypatch.setenv("ARCHON_OWNER_EMAIL", "   ")
    assert owner_address() == DEFAULT_OWNER, "whitespace is not an address"

    monkeypatch.setenv("ARCHON_OWNER_EMAIL", "books@example.test")
    assert owner_address() == "books@example.test"
