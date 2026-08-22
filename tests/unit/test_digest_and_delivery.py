"""The owner's month-end letter, and the seam that gets it to them.

The asymmetry these tests defend is the architecture: a letter to a broker is
composed and filed unsent, a letter to the owner is composed and delivered. If
either half drifts, one of these goes red.
"""
from __future__ import annotations

import smtplib

import pytest

from archon.adapters.delivery import (
    FiledDelivery,
    Receipt,
    SmtpDelivery,
    get_deliverer,
    owner_address,
)
from archon.adapters.store import LocalStore
from archon.domain.digest import compose, subject_line
from archon.runtime.close import run_close
from tests.conftest import PERIOD, load, remittance


def close(documents, **kw):
    return run_close(period=PERIOD, documents=documents, company="Bell Ridge Haulage",
                     store=LocalStore(), **kw)


# ── what the letter says ─────────────────────────────────────────────────────

def test_the_subject_carries_the_number_that_decides_whether_it_is_read(documents):
    result = close(documents)

    assert result.digest.subject == (
        "2026-07 closed. 612.85 was quietly leaking, 5 letters ready"
    )


def test_a_clean_month_leads_with_profit_instead(documents):
    """Nothing to chase, so the subject stops pretending there is."""
    docs = [load("L-1", 1000.0), remittance("R-1", [("L-1", 1000.0, 0.0, None)], fee=30.0)]
    result = close(docs)

    assert "nothing outstanding" in result.digest.subject
    assert "recoverable" not in result.digest.subject


def test_a_blocked_close_says_so_in_the_subject_rather_than_burying_it(documents):
    broken = list(documents)
    from archon.domain.models import DocType

    next(d for d in broken
         if d.doc_type is DocType.BROKER_REMITTANCE).remittance_total = 1.0

    result = close(broken)

    assert result.digest.subject == "2026-07: the close did not pass its own checks"
    assert "do not rely on them" in result.digest.body


def test_the_letter_names_what_was_already_done_and_what_is_left(documents):
    body = close(documents).digest.body

    assert "WHAT I ALREADY DID (5 letters written and filed)" in body
    assert "WHAT I NEED FROM YOU" in body
    assert "I have not sent any of them, and I will not." in body


def test_the_letter_stops_listing_and_says_how_many_it_did_not_list(documents):
    """The literal is written out on purpose. `f"and {5 - TOP_ACTIONS} more"`
    was the original, and it is true for every value of TOP_ACTIONS, so it
    tested nothing. See tests/unit/test_window_boundaries.py."""
    body = close(documents).digest.body

    assert "and 2 more, all in the app." in body


def test_the_letter_separates_a_leak_from_a_receivable(documents):
    """One blended total made the product look nine times better than it is."""
    body = close(documents).digest.body

    assert "612.85" in body and "was leaking away quietly" in body
    assert "4,900.00" in body and "nobody has paid" in body
    assert "1,865.00" in body and "Recovers nothing" in body


def test_every_figure_in_the_letter_came_from_the_close_that_produced_it(documents):
    result = close(documents)

    assert result.digest.net_profit == result.statements.net_profit
    assert result.digest.recoverable == result.recoverable
    assert result.digest.action_count == len(result.drafts)
    assert result.digest.run_id == result.run_id


def test_the_letter_counts_its_own_step_in_the_trail(documents):
    """It is composed inside the last step, so the journal has not recorded that
    step yet. The owner should be told ten, not nine."""
    result = close(documents)

    assert f"{len(result.journal.steps)} steps" in result.digest.body


def test_the_watch_list_holds_only_things_no_letter_was_written_for(documents):
    result = close(documents)
    body = result.digest.body

    assert "WORTH A LOOK, NOTHING WRITTEN" in body
    for draft in result.drafts:
        section = body.split("WORTH A LOOK, NOTHING WRITTEN")[1].split("WHAT I NEED")[0]
        assert draft.reference not in section


def test_a_month_with_nothing_wrong_asks_the_owner_for_nothing():
    docs = [load("L-1", 1000.0), remittance("R-1", [("L-1", 1000.0, 0.0, None)], fee=30.0)]

    assert "Nothing. The month is closed" in close(docs).digest.body


def test_the_subject_helper_is_pure_and_usable_on_its_own():
    class _S:
        net_profit = 1234.5

    assert subject_line("2026-07", _S(), [], "closed").endswith(
        "1,234.50 profit, nothing outstanding")


def test_compose_falls_back_to_a_readable_company_name(documents):
    result = run_close(period=PERIOD, documents=documents, store=LocalStore())

    assert compose(result, recipient="x@example.com").company == "your firm"


# ── getting it there ─────────────────────────────────────────────────────────

def test_the_default_deliverer_composes_and_sends_nothing(documents):
    """What the demo, the tests and CI all use. No credential, no socket."""
    deliverer = FiledDelivery()
    result = close(documents, deliverer=deliverer)

    assert result.receipt.channel == "filed"
    assert result.receipt.delivered is False
    assert "nothing left this machine" in result.receipt.detail
    assert deliverer.filed[0].subject == result.digest.subject


def test_smtp_delivery_sends_the_composed_letter(documents):
    """A delivery seam whose only implementation is a recorder is a stub with
    good manners, so the real one is exercised against an injected transport."""
    sent = []

    class FakeSMTP:
        def __init__(self, host, port):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def send_message(self, message):
            sent.append(("send", message["To"], message["Subject"], message.get_content()))

    result = close(documents, deliverer=SmtpDelivery(
        host="mail.example", port=2525, sender="archon@example", transport=FakeSMTP))

    assert result.receipt.delivered is True
    assert sent[0] == ("connect", "mail.example", 2525)
    assert sent[1][1] == "owner@bellridgehaulage.example"
    assert sent[1][2] == result.digest.subject
    assert "WHAT I NEED FROM YOU" in sent[1][3]


def test_smtp_logs_in_only_when_it_has_credentials(documents):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            calls.append("starttls")

        def login(self, user, password):
            calls.append(("login", user))

        def send_message(self, message):
            calls.append("send")

    close(documents, deliverer=SmtpDelivery(host="h", transport=FakeSMTP))
    assert calls == ["send"]

    calls.clear()
    close(documents, deliverer=SmtpDelivery(
        host="h", user="u", password="p", transport=FakeSMTP))
    assert calls == ["starttls", ("login", "u"), "send"]


def test_a_mail_server_being_down_does_not_stop_the_month_closing(documents):
    class DeadSMTP:
        def __init__(self, host, port):
            raise smtplib.SMTPConnectError(421, "service not available")

    result = close(documents, deliverer=SmtpDelivery(host="h", transport=DeadSMTP))

    assert result.outcome == "closed"
    assert result.receipt.delivered is False
    assert "delivery failed" in result.receipt.detail
    assert result.digest is not None          # still readable in the app


def test_a_deliverer_that_raises_outright_does_not_stop_the_month_closing(documents):
    class Exploding:
        channel = "exploding"

        def deliver(self, digest):
            raise RuntimeError("boom")

    result = close(documents, deliverer=Exploding())

    assert result.outcome == "closed"
    assert result.receipt.delivered is False
    assert "RuntimeError" in result.receipt.detail
    notify = next(s for s in result.journal.steps if s.name == "notify")
    assert notify.status == "ok"              # the step completed; delivery did not


def test_the_password_never_reaches_a_receipt_or_the_stored_digest(documents):
    class FakeSMTP:
        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, message):
            pass

    secret = "hunter2-do-not-leak"
    result = close(documents, deliverer=SmtpDelivery(
        host="h", user="u", password=secret, transport=FakeSMTP))

    assert secret not in result.receipt.detail
    assert secret not in str(result.digest.to_dict())
    assert secret not in str(result.to_dict())


def test_the_deliverer_is_chosen_by_configuration_not_by_a_flag(monkeypatch):
    monkeypatch.delenv("ARCHON_SMTP_HOST", raising=False)
    assert get_deliverer().channel == "filed"

    monkeypatch.setenv("ARCHON_SMTP_HOST", "mail.example")
    monkeypatch.setenv("ARCHON_SMTP_PORT", "2525")
    deliverer = get_deliverer()
    assert deliverer.channel == "smtp"
    assert deliverer.port == 2525


def test_the_owner_address_is_configurable(monkeypatch):
    monkeypatch.setenv("ARCHON_OWNER_EMAIL", "someone@example.com")
    assert owner_address() == "someone@example.com"


def test_an_explicit_owner_email_overrides_the_environment(documents):
    result = close(documents, owner_email="direct@example.com")

    assert result.digest.recipient == "direct@example.com"


# ── the boundary this whole feature had to respect ───────────────────────────

def test_delivering_to_the_owner_does_not_send_anything_to_a_counterparty(documents):
    """The two edges, asserted together. The owner gets their books; the broker
    gets nothing until a person presses send."""
    class FakeSMTP:
        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def send_message(self, message):
            assert message["To"] == "owner@bellridgehaulage.example"

    result = close(documents, deliverer=SmtpDelivery(host="h", transport=FakeSMTP))

    assert result.receipt.delivered is True
    assert {d.status for d in result.drafts} == {"filed"}


def test_the_digest_is_persisted_alongside_the_books(documents):
    store = LocalStore()
    result = run_close(period=PERIOD, documents=documents, company="Bell Ridge Haulage",
                       store=store)

    assert result.stored["digest"].endswith("2026-07#digest")
    assert store.load_close("Bell Ridge Haulage", "2026-07#digest")["subject"]


def test_a_receipt_serialises_for_the_page():
    receipt = Receipt(channel="smtp", delivered=True, detail="ok", recipient="a@b.c")

    assert receipt.to_dict() == {
        "channel": "smtp", "delivered": True, "detail": "ok", "recipient": "a@b.c",
    }


@pytest.mark.parametrize("attribute", ["subject", "body", "recipient"])
def test_the_letter_is_never_empty(documents, attribute):
    assert getattr(close(documents).digest, attribute).strip()
