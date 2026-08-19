"""End to end: the terminal path a cloner runs, and the service a judge opens."""
from __future__ import annotations

import json

import pytest

from archon.cli import main

# ── the CLI ──────────────────────────────────────────────────────────────────

def test_the_bundled_month_closes_from_a_clean_checkout(capsys):
    """The first thing anyone who clones this repository runs. No key, no
    credential, no network, no install beyond the standard library."""
    exit_code = main([])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "closed in" in out
    assert "10. Write the owner their month-end letter" in out
    assert "filed" in out


def test_the_cli_prints_the_whole_trail_in_order(capsys):
    main([])
    out = capsys.readouterr().out

    for title in ("Take in the month's mail", "Post the double-entry journal",
                  "Split each remittance across the loads it settles",
                  "Find what is missing or does not add up",
                  "Write the corrective documents",
                  "Check the close against its own gates"):
        assert title in out


def test_the_json_form_is_machine_readable(capsys):
    exit_code = main(["--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "closed"
    assert len(payload["journal"]["steps"]) == 10
    assert len(payload["drafts"]) == 5


def test_a_period_with_no_mail_raises_rather_than_closing_an_empty_month():
    with pytest.raises(FileNotFoundError):
        main(["--period", "1999-01"])


# ── the Cloud Run service ────────────────────────────────────────────────────

@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from archon.service import app

    return TestClient(app)


def test_health_says_which_backend_the_deploy_is_using(client):
    body = client.get("/api/health").json()

    assert body["status"] == "ok"
    assert body["store"] in ("memory", "firestore")
    assert "2026-07" in body["periods"]


def test_the_page_a_judge_opens_serves_and_names_the_product(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "ARCHON" in response.text
    assert "unattended" in response.text


def test_closing_a_period_over_the_api_returns_the_whole_run(client):
    body = client.post("/api/close/2026-07").json()

    assert body["outcome"] == "closed"
    assert len(body["journal"]["steps"]) == 10
    assert len(body["findings"]) == 10
    assert body["recoverable"] == 5512.85


def test_reading_a_period_serves_a_close_even_on_a_cold_container(client):
    """A judge arriving first should see a closed month, not an empty state."""
    body = client.get("/api/close/2026-07").json()

    assert body["outcome"] == "closed"


def test_a_period_with_no_mail_is_a_404_not_a_500(client):
    assert client.post("/api/close/1999-01").status_code == 404


def test_periods_lists_what_is_waiting(client):
    body = client.get("/api/periods").json()

    assert body["default"] == "2026-07"
    assert "2026-07" in body["periods"]


# ── the unattended trigger ───────────────────────────────────────────────────

def test_an_object_landing_in_the_bucket_closes_the_month(client):
    """The claim, at the trigger: nobody pressed anything."""
    import base64

    data = base64.b64encode(
        json.dumps({"name": "mail/2026-07/remittance-MFX-RA-4417.txt"}).encode()
    ).decode()

    body = client.post("/events", json={"message": {"data": data}}).json()

    assert body["status"] == "closed"
    assert body["period"] == "2026-07"
    assert body["outcome"] == "closed"
    assert body["drafts"] == 5


def test_a_scheduler_attribute_also_names_the_period(client):
    body = client.post("/events", json={"message": {"attributes": {"period": "2026-07"}}})

    assert body.json()["status"] == "closed"


def test_an_object_id_attribute_is_read_when_there_is_no_payload(client):
    body = client.post(
        "/events", json={"message": {"attributes": {"objectId": "mail/2026-07/x.txt"}}}
    )

    assert body.json()["status"] == "closed"


def test_an_event_for_a_period_with_no_mail_is_acknowledged_not_retried(client):
    response = client.post(
        "/events", json={"message": {"attributes": {"period": "1999-01"}}}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_a_malformed_envelope_is_acknowledged_rather_than_redelivered_forever(client):
    """Pub/Sub redelivers on a non-2xx. A message that will never parse would
    otherwise be retried until it expired, at the cost of one close per try."""
    for payload in ({}, {"message": {}}, {"message": {"data": "not base64 at all"}}):
        response = client.post("/events", json=payload)
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"


def test_a_body_that_is_not_json_is_acknowledged(client):
    response = client.post("/events", content=b"<xml/>",
                           headers={"Content-Type": "application/json"})

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_the_period_parser_finds_a_month_anywhere_in_the_object_path():
    from archon.service import _period_from_envelope

    assert _period_from_envelope(
        {"message": {"attributes": {"objectId": "tenants/bell-ridge/2026-07/x.pdf"}}}
    ) == "2026-07"
    assert _period_from_envelope(
        {"message": {"attributes": {"objectId": "no/period/here.pdf"}}}
    ) is None
    assert _period_from_envelope(
        {"message": {"attributes": {"objectId": "2026-13-not-a-month/x.pdf"}}}
    ) is None
