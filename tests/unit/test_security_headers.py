"""The headers the DAST scan found missing, pinned so they cannot go missing again.

Every assertion here corresponds to a finding a real scanner raised against the
running container. They were fixed rather than ignored, and these exist so that
a future change to the service cannot quietly drop one and pass CI while the
weekly scan goes red days later.
"""
from __future__ import annotations

import pytest

from archon.adapters.headers import CONTENT_SECURITY_POLICY, SECURITY_HEADERS


@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from archon.adapters.service import app

    return TestClient(app)


#: Each entry is a finding ZAP raised, by rule id, and the header that answers it.
FINDINGS = [
    pytest.param("X-Content-Type-Options", "nosniff", id="zap-10021"),
    pytest.param("X-Frame-Options", "DENY", id="zap-10020-anti-clickjacking"),
    pytest.param("Cross-Origin-Resource-Policy", "same-origin", id="zap-90004"),
]


@pytest.mark.parametrize("header, value", FINDINGS)
def test_the_page_carries_the_headers_the_scan_asked_for(client, header, value):
    assert client.get("/").headers.get(header) == value


@pytest.mark.parametrize("header, value", FINDINGS)
def test_the_api_carries_them_too(client, header, value):
    """A JSON route is as sniffable as an HTML one."""
    assert client.get("/api/health").headers.get(header) == value


def test_a_content_security_policy_is_set(client):
    assert client.get("/").headers.get("Content-Security-Policy") == CONTENT_SECURITY_POLICY


def test_a_permissions_policy_switches_off_what_the_page_does_not_use(client):
    policy = client.get("/").headers.get("Permissions-Policy", "")

    for capability in ("camera", "geolocation", "microphone", "payment"):
        assert f"{capability}=()" in policy


# ── what the policy actually permits ─────────────────────────────────────────

def test_the_policy_allows_nothing_from_another_origin():
    """The page is one self-contained file. Nothing should load from elsewhere."""
    assert "default-src 'self'" in CONTENT_SECURITY_POLICY
    assert "connect-src 'self'" in CONTENT_SECURITY_POLICY
    assert "https://" not in CONTENT_SECURITY_POLICY
    assert "*" not in CONTENT_SECURITY_POLICY


def test_the_policy_closes_framing_forms_objects_and_base_rewriting():
    for directive in ("frame-ancestors 'none'", "form-action 'none'",
                      "object-src 'none'", "base-uri 'none'"):
        assert directive in CONTENT_SECURITY_POLICY


def test_inline_is_allowed_only_for_script_and_style_and_that_is_deliberate():
    """The trade is written down in headers.py: a single self-contained page
    with no build step cannot carry a nonce. Everything else stays closed."""
    inline_directives = [part for part in CONTENT_SECURITY_POLICY.split("; ")
                         if "'unsafe-inline'" in part]

    assert sorted(inline_directives) == [
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
    ]


def test_no_directive_uses_unsafe_eval():
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY


# ── the mechanism ────────────────────────────────────────────────────────────

def test_a_route_that_sets_its_own_header_keeps_it():
    """`setdefault`, so a route with a reason to differ is not overwritten."""
    from archon.adapters.headers import apply

    existing = {"X-Frame-Options": "SAMEORIGIN"}
    apply(existing)

    assert existing["X-Frame-Options"] == "SAMEORIGIN"
    assert existing["X-Content-Type-Options"] == "nosniff"


def test_every_configured_header_reaches_the_wire(client):
    """If one is added to the dict and not sent, this catches it."""
    response = client.get("/")

    for header in SECURITY_HEADERS:
        assert response.headers.get(header), f"{header} never reached the response"


def test_headers_survive_an_error_response(client):
    """A 404 is served to strangers too."""
    response = client.post("/api/close/1999-01")

    assert response.status_code == 404
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
