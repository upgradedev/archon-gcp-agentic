"""A7: the public identity cannot write.

The demo has to stay open. A judge opens the page with no account, presses one
button, and watches a month close. That is the whole point of it, and putting a
login in front of it would cost more than the control is worth.

But an anonymous request that writes to the durable store is exactly the thing
the principle of least privilege exists to prevent, and until this module the
public button did precisely that: it closed a month into Firestore.

So the two paths are separated by what they can reach, not by what they are
called.

**The public path runs against an ephemeral store.** `POST /api/close/{period}`
does the identical close over the identical documents and produces the identical
books, then throws the result away. Nothing a stranger does survives the request.

**The trusted path writes.** `POST /events` is the unattended trigger, it is
reached by Pub/Sub rather than by a person, and it carries a Google-signed OIDC
token that this module verifies before anything is persisted.

The verification is deliberately fail-closed. When an audience is configured, a
request without a valid token for that audience is refused, and a missing
verification library is a refusal rather than a pass. When no audience is
configured, the route is open and `/api/health` says so out loud, because a
security posture nobody can read from the outside is one nobody checks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

#: Set to the Cloud Run service URL to require a Google-signed OIDC token on
#: `/events`. Unset, the route is open and health says `"events_auth": "open"`.
AUDIENCE_ENV = "ARCHON_EVENTS_AUDIENCE"

#: Optional. When set, the token's `email` claim must match, so that only the
#: push subscription's own service account can trigger a close.
CALLER_ENV = "ARCHON_EVENTS_CALLER"


@dataclass(frozen=True)
class Verdict:
    """Whether a request may write, and why not when it may not."""

    allowed: bool
    reason: str
    caller: str | None = None


def audience() -> str | None:
    return os.getenv(AUDIENCE_ENV) or None


def required() -> bool:
    """Auth is required exactly when an audience has been configured."""
    return audience() is not None


def posture() -> str:
    """What `/api/health` reports, so the posture is visible from outside."""
    return "verified-oidc" if required() else "open"


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def verify_push_request(authorization: str | None, verifier=None) -> Verdict:
    """Decide whether an `/events` request may close a period.

    `verifier` is injectable so the whole decision is testable without a
    network call and without a Google credential.
    """
    expected = audience()
    if expected is None:
        return Verdict(True, "no audience configured, the route is open")

    token = _bearer(authorization)
    if token is None:
        return Verdict(False, "no bearer token")

    verify = verifier or _google_verifier()
    if verify is None:
        # Fail closed. A deployment that asked for verification and cannot
        # verify must refuse, not wave the request through.
        return Verdict(False, "token verification is unavailable")

    try:
        claims = verify(token, expected)
    except Exception as exc:
        return Verdict(False, f"token rejected ({type(exc).__name__})")

    if not isinstance(claims, dict):
        return Verdict(False, "token produced no claims")

    caller = claims.get("email")
    wanted = os.getenv(CALLER_ENV)
    if wanted and caller != wanted:
        return Verdict(False, f"caller {caller!r} is not the configured service account")

    return Verdict(True, "verified", caller=caller)


def _google_verifier():
    """The real verifier, or None when the library is absent."""
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
    except ImportError:  # pragma: no cover - exercised by injecting a verifier
        return None

    def verify(token: str, expected_audience: str) -> dict:
        return id_token.verify_oauth2_token(
            token, google_requests.Request(), expected_audience
        )

    return verify
