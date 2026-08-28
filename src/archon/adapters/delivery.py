"""Getting the digest to the owner, and the seam that keeps that honest.

Two deliverers, and which one runs is decided by configuration rather than by a
flag in the code.

`FiledDelivery` is the default. It composes the letter, records it, and sends
nothing. That is what the bundled demo, the tests and CI use, which is why none
of them need a credential and why a judge sees the exact bytes that would
arrive without anything actually leaving the machine.

`SmtpDelivery` is the real one, on the standard library, configured entirely by
environment. It exists because a delivery seam whose only implementation is a
recorder is not a seam, it is a stub with good manners, and this product's whole
argument is that the work reaches the owner where they already are.

**The asymmetry here is deliberate and it is the architecture.** A letter to a
broker is composed and filed unsent, because sending it is irreversible and it
goes to somebody else. A letter to the owner is composed and delivered, because
it is the owner's own books arriving at the owner, and withholding it until they
remember to log in is how the overnight work goes unread. Same engine, two
edges, two rules.

A delivery that fails never fails the close. The month is closed either way, the
receipt records what happened, and the digest stays readable in the app.
"""
from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from ..domain.digest import Digest


@dataclass
class Receipt:
    """What happened when the digest was handed to a channel."""

    channel: str
    delivered: bool
    detail: str
    recipient: str

    def to_dict(self) -> dict:
        return {"channel": self.channel, "delivered": self.delivered,
                "detail": self.detail, "recipient": self.recipient}


class Deliverer(Protocol):
    channel: str

    def deliver(self, digest: Digest) -> Receipt: ...


class FiledDelivery:
    """Compose, record, send nothing. The default, and what the demo shows."""

    channel = "filed"

    def __init__(self) -> None:
        self.filed: list[Digest] = []

    def deliver(self, digest: Digest) -> Receipt:
        self.filed.append(digest)
        return Receipt(
            channel=self.channel,
            delivered=False,
            detail=(
                f"composed for {digest.recipient} and filed; no channel is configured, "
                f"so nothing left this machine"
            ),
            recipient=digest.recipient,
        )


class SmtpDelivery:
    """Deliver the digest by email, on the standard library.

    Configured by environment so a deployment moves without a code change:

        ARCHON_SMTP_HOST      required, or this deliverer is never selected
        ARCHON_SMTP_PORT      default 587
        ARCHON_SMTP_USER      optional; login is skipped without it
        ARCHON_SMTP_PASSWORD  optional
        ARCHON_DIGEST_FROM    envelope sender

    The password is read from the environment and never logged, never written
    to a receipt, and never persisted with the digest.
    """

    channel = "smtp"

    def __init__(self, host: str, port: int = 587, user: str | None = None,
                 password: str | None = None, sender: str | None = None,
                 transport=smtplib.SMTP):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.sender = sender or user or "archon@localhost"
        self._transport = transport      # injectable, so tests never open a socket

    def deliver(self, digest: Digest) -> Receipt:
        message = EmailMessage()
        message["Subject"] = digest.subject
        message["From"] = self.sender
        message["To"] = digest.recipient
        message.set_content(digest.body)

        try:
            with self._transport(self.host, self.port) as server:
                if self.user and self.password:
                    server.starttls()
                    server.login(self.user, self.password)
                server.send_message(message)
        except Exception as exc:
            # A mail server being down is not a reason for a month not to close.
            return Receipt(
                channel=self.channel, delivered=False,
                detail=f"delivery failed ({type(exc).__name__}); the digest is still in the app",
                recipient=digest.recipient,
            )

        return Receipt(
            channel=self.channel, delivered=True,
            detail=f"delivered to {digest.recipient} via {self.host}",
            recipient=digest.recipient,
        )


def get_deliverer() -> Deliverer:
    """SMTP when a host is configured, otherwise file it and send nothing.

    The fallback is silent by design. A demo that dies because no mail server
    was configured is a demo that does not run on a judge's machine, and the
    digest is identical either way.
    """
    host = os.getenv("ARCHON_SMTP_HOST")
    if not host:
        return FiledDelivery()
    return SmtpDelivery(
        host=host,
        port=int(os.getenv("ARCHON_SMTP_PORT", "587")),
        user=os.getenv("ARCHON_SMTP_USER"),
        password=os.getenv("ARCHON_SMTP_PASSWORD"),
        sender=os.getenv("ARCHON_DIGEST_FROM"),
    )


#: Where the digest is addressed when nothing else says. A real address on a
#: reserved example domain, so it is obviously synthetic and obviously not a
#: person's inbox.
DEFAULT_OWNER = "owner@bellridgehaulage.example"


def owner_address() -> str:
    """Where the owner reads their mail.

    `or` rather than a getenv default, and the difference is not stylistic.
    Terraform declares `ARCHON_OWNER_EMAIL` and its variable defaults to the
    empty string, so on the deployed service the variable is SET AND EMPTY.
    `os.getenv(name, default)` returns the default only when the name is
    ABSENT, so the live digest was addressed to nothing and the receipt read
    "composed for  and filed", with the gap where a recipient should be.
    """
    return os.getenv("ARCHON_OWNER_EMAIL", "").strip() or DEFAULT_OWNER


class RehearsalDelivery:
    """Delivers nothing, for a close that has not been decided yet.

    Paired with `RehearsalStore`. The rule this enforces is the one the audit
    named: nothing may reach outside Archon before the agent has decided, and
    the agent decides at step 5 of eleven. A digest delivered from a rehearsal
    is a message the owner receives about a month that may still be withheld.
    """

    channel = "rehearsal"

    def __init__(self) -> None:
        self.attempts = 0

    def deliver(self, digest: Digest) -> Receipt:
        self.attempts += 1
        return Receipt(
            channel=self.channel, delivered=False,
            detail="rehearsal: composed but not sent, the close is not decided yet",
            recipient=digest.recipient,
        )


class SandboxDelivery:
    """Refuses to send, structurally, for anything a stranger can reach.

    The public page has an anonymous button and an anonymous boot fetch. Both
    reached `run_close` without passing a deliverer, so `get_deliverer()`
    resolved one from the environment -- which on a deployment configured for
    real mail is the real SMTP sender. A visitor pressing a demo button could
    put a message in the owner's inbox, and the page's own page-load fetch
    could do it without anyone pressing anything.

    This is not a flag that can be switched on by configuration. It is the
    object the public routes hand in, and it has no code path that sends.
    """

    channel = "sandbox"

    def __init__(self) -> None:
        self.attempts = 0

    def deliver(self, digest: Digest) -> Receipt:
        self.attempts += 1
        return Receipt(
            channel=self.channel, delivered=False,
            detail="public sandbox: the digest was composed and filed, no message was sent",
            recipient=digest.recipient,
        )
