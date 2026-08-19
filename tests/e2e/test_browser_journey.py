"""C5: the exact path the video shows, walked by a browser.

Every other test in this repository asserts something about a function or a
JSON payload. None of them proves that a person who opens the page and presses
the button sees a month close, and that is the only journey a judge actually
performs.

So this walks it: open the page, press the one button, watch the ten steps
land, read the owner's letter, and confirm the five counterparty letters are
marked filed rather than sent. It runs at 375 and at 1280, because the demo is
shown on a phone and cut into a video, and a layout that only works at one of
those is a layout that has only been checked at one of those.

Playwright is not installed on the machine this was written on, by workspace
rule, so these skip locally and run in CI. The CI job asserts they did not
skip, because a browser test that silently stops running is worse than no
browser test: it reports green while proving nothing.
"""
from __future__ import annotations

import contextlib
import os
import socket
import threading
import time

import pytest

pytest.importorskip("playwright.sync_api")
pytest.importorskip("uvicorn")

#: The package being importable is not the same as a browser existing, and the
#: machine this was written on is not allowed to install one. So the gate is an
#: explicit opt-in that CI sets, and CI then asserts these did NOT skip. A
#: browser test that quietly stops running is worse than no browser test: it
#: reports green while proving nothing.
pytestmark = pytest.mark.skipif(
    not os.getenv("ARCHON_BROWSER_TESTS"),
    reason="set ARCHON_BROWSER_TESTS=1 with a playwright browser installed; CI does",
)

from playwright.sync_api import expect, sync_playwright  # noqa: E402

VIEWPORTS = [
    pytest.param({"width": 375, "height": 812}, id="375px-phone"),
    pytest.param({"width": 1280, "height": 800}, id="1280px-desktop"),
]


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def base_url():
    """The real service, served by uvicorn, exactly as it is in the container."""
    import uvicorn

    from archon.adapters.service import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    assert server.started, "the service did not come up"

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture
def page(browser, request):
    viewport = getattr(request, "param", {"width": 1280, "height": 800})
    context = browser.new_context(viewport=viewport)
    page = context.new_page()
    yield page
    context.close()


@pytest.mark.parametrize("page", VIEWPORTS, indirect=True)
def test_a_visitor_presses_one_button_and_watches_the_month_close(page, base_url):
    """The whole judge journey, and the whole video, in one test."""
    page.goto(base_url, wait_until="networkidle")

    expect(page.get_by_role("heading", name="It closes the month while nobody is watching.")
           ).to_be_visible()

    button = page.locator("#run")
    expect(button).to_be_enabled()
    button.click()

    # Ten steps, each one a thing the agent did while nobody watched.
    steps = page.locator("#trail .step")
    expect(steps).to_have_count(10, timeout=30_000)
    expect(steps.first).to_contain_text("Take in the month's mail")
    expect(steps.nth(2)).to_contain_text("Split each remittance across the loads it settles")
    expect(steps.last).to_contain_text("Write the owner their month-end letter")

    expect(page.locator("#status")).to_contain_text("closed")


@pytest.mark.parametrize("page", VIEWPORTS, indirect=True)
def test_the_allocation_beat_is_visible_and_its_identity_closes(page, base_url):
    """The differentiator, on screen. One payment, eight loads, residual zero."""
    page.goto(base_url, wait_until="networkidle")
    page.locator("#run").click()
    expect(page.locator("#trail .step")).to_have_count(10, timeout=30_000)

    expect(page.locator("#alloc")).to_contain_text("identity closes, residual 0.00")
    expect(page.locator("#alloc tbody tr")).to_have_count(9)      # header row + 8 loads
    expect(page.locator("#alloc")).to_contain_text("short paid")  # L-7105


@pytest.mark.parametrize("page", VIEWPORTS, indirect=True)
def test_the_owners_letter_is_shown_and_the_brokers_letters_are_not_sent(page, base_url):
    """Both edges of the autonomy boundary, on the page a judge reads."""
    page.goto(base_url, wait_until="networkidle")
    page.locator("#run").click()
    expect(page.locator("#trail .step")).to_have_count(10, timeout=30_000)

    expect(page.locator("#digest")).to_contain_text("is recoverable, 5 letters ready")
    expect(page.locator("#digest")).to_contain_text("WHAT I NEED FROM YOU")

    drafts = page.locator("#drafts .draft")
    expect(drafts).to_have_count(5)
    for index in range(5):
        expect(drafts.nth(index)).to_contain_text("filed, not sent")


@pytest.mark.parametrize("page", VIEWPORTS, indirect=True)
def test_nothing_is_disabled_without_a_reason_and_nothing_renders_empty(page, base_url):
    """H2 and H3, checked in a browser rather than by reading the CSS."""
    page.goto(base_url, wait_until="networkidle")
    page.locator("#run").click()
    expect(page.locator("#trail .step")).to_have_count(10, timeout=30_000)

    assert page.locator("button:disabled").count() == 0
    for selector in ("#trail", "#stats", "#alloc", "#findings", "#drafts",
                     "#digest", "#gates", "#trucks"):
        assert page.locator(selector).inner_html().strip(), f"{selector} rendered empty"


@pytest.mark.parametrize("page", VIEWPORTS, indirect=True)
def test_the_page_never_scrolls_sideways(page, base_url):
    """Wide tables scroll inside their own containers. The page does not."""
    page.goto(base_url, wait_until="networkidle")
    page.locator("#run").click()
    expect(page.locator("#trail .step")).to_have_count(10, timeout=30_000)

    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1, f"the page scrolls sideways by {overflow}px"


def test_the_page_serves_a_closed_month_before_anyone_presses_anything(page, base_url):
    """A judge arriving at a cold container sees a result, not an empty state."""
    page.goto(base_url, wait_until="networkidle")

    expect(page.locator("#trail .step")).to_have_count(10, timeout=30_000)
    expect(page.locator("#status")).to_contain_text("last run")


def test_the_console_is_clean(page, base_url):
    """A judge who opens devtools should not find the demo throwing."""
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(base_url, wait_until="networkidle")
    page.locator("#run").click()
    expect(page.locator("#trail .step")).to_have_count(10, timeout=30_000)

    assert errors == []
