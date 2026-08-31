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
import re
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
    """A page that has asked for motion.

    Headless Chromium reports `prefers-reduced-motion: reduce` by default, and
    the page honours that by switching the run trail's stagger off entirely.
    That is correct behaviour and it is tested below, but it is not the state
    the video is recorded in, so the journey asks for motion explicitly rather
    than inheriting whatever the runner happens to prefer.
    """
    viewport = getattr(request, "param", {"width": 1280, "height": 800})
    context = browser.new_context(viewport=viewport, reduced_motion="no-preference")
    page = context.new_page()
    yield page
    context.close()


@pytest.fixture
def still_page(browser):
    """A page from a visitor who has asked for less motion."""
    context = browser.new_context(viewport={"width": 1280, "height": 800},
                                  reduced_motion="reduce")
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

    # Eleven steps, each one a thing the agent did while nobody watched.
    steps = page.locator("#trail .step")
    expect(steps).to_have_count(11, timeout=30_000)
    expect(steps.first).to_contain_text("Take in the month's mail")
    expect(steps.nth(2)).to_contain_text("Split each remittance across the loads it settles")
    expect(steps.last).to_contain_text("Write the owner their month-end letter")

    expect(page.locator("#status")).to_contain_text("closed")


@pytest.mark.parametrize("page", VIEWPORTS, indirect=True)
def test_the_allocation_beat_is_visible_and_its_identity_closes(page, base_url):
    """The differentiator, on screen. One payment, eight loads, residual zero.

    The tab is opened rather than assumed. `to_contain_text` reads textContent
    and passes on a `display:none` subtree, so before the console had panels
    this test would have gone green over an allocation nobody could reach.
    """
    page.goto(base_url, wait_until="networkidle")
    page.locator("#run").click()
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)

    page.locator('.tab[data-panel="alloc"]').click()

    expect(page.locator("#alloc")).to_be_visible()
    expect(page.locator("#alloc")).to_contain_text("identity closes, residual 0.00")
    expect(page.locator("#alloc tbody tr")).to_have_count(9)      # header row + 8 loads
    expect(page.locator("#alloc")).to_contain_text("short paid")  # L-7105


@pytest.mark.parametrize("page", VIEWPORTS, indirect=True)
def test_the_owners_letter_is_shown_and_the_brokers_letters_are_not_sent(page, base_url):
    """Both edges of the autonomy boundary, on the page a judge reads."""
    page.goto(base_url, wait_until="networkidle")
    page.locator("#run").click()
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)

    page.locator('.tab[data-panel="letters"]').click()
    expect(page.locator("#digest")).to_be_visible()

    # The subject says what was leaking, not what is "recoverable". That
    # wording changed when the money language was corrected, and this
    # assertion had been left behind pointing at the old, inflated claim.
    expect(page.locator("#digest")).to_contain_text("was quietly leaking, 5 letters ready")
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
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)

    # The button disables itself while the close runs and re-enables in a
    # `finally`, which lands after the steps are in the DOM. Asserting on the
    # count alone raced that, so wait for the control to come back first.
    expect(page.locator("#run")).to_be_enabled(timeout=30_000)
    assert page.locator("button:disabled").count() == 0
    for selector in ("#trail", "#stats", "#alloc", "#findings", "#drafts",
                     "#digest", "#gates", "#trucks", "#hero-line", "#origin",
                     "#mailbox", "#phases", "#chart-waterfall", "#run-stats"):
        assert page.locator(selector).inner_html().strip(), f"{selector} rendered empty"


@pytest.mark.parametrize("page", VIEWPORTS, indirect=True)
def test_the_page_never_scrolls_sideways(page, base_url):
    """Wide tables scroll inside their own containers. The page does not."""
    page.goto(base_url, wait_until="networkidle")
    page.locator("#run").click()
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)

    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1, f"the page scrolls sideways by {overflow}px"


def test_the_page_serves_a_closed_month_before_anyone_presses_anything(page, base_url):
    """A judge arriving at a cold container sees a result, not an empty state."""
    page.goto(base_url, wait_until="networkidle")

    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)
    expect(page.locator("#status")).to_contain_text("last run")


def test_the_console_is_clean(page, base_url):
    """A judge who opens devtools should not find the demo throwing."""
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(base_url, wait_until="networkidle")
    page.locator("#run").click()
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)
    expect(page.locator("#run")).to_be_enabled(timeout=30_000)

    assert errors == []


@pytest.mark.parametrize("page", VIEWPORTS, indirect=True)
def test_the_content_security_policy_blocks_nothing_the_page_needs(page, base_url):
    """A closed policy over a page that needs inline is a broken page.

    This is the regression that shipped once: tightening `style-src` to 'self'
    blocked the run trail's stagger, so every step appeared at once and the
    close stopped being watchable. Nothing in the unit suite could see it,
    because the policy and the page were only ever tested apart.
    """
    violations = []
    page.add_init_script(
        "window.__cspViolations = [];"
        "document.addEventListener('securitypolicyviolation',"
        " e => window.__cspViolations.push(e.violatedDirective + ' blocked ' + e.blockedURI));"
    )
    page.on("console", lambda msg: violations.append(msg.text)
            if msg.type == "error" else None)

    page.goto(base_url, wait_until="networkidle")
    page.locator("#run").click()
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)
    expect(page.locator("#run")).to_be_enabled(timeout=30_000)

    violations += page.evaluate("() => window.__cspViolations")

    assert violations == []


@pytest.mark.parametrize("page", VIEWPORTS, indirect=True)
def test_the_run_trail_still_staggers(page, base_url):
    """The thing the policy broke, asserted directly. The stagger is what makes
    an unattended run watchable rather than a wall of text appearing at once."""
    page.goto(base_url, wait_until="networkidle")
    page.locator("#run").click()
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)

    delays = page.eval_on_selector_all(
        "#trail .step",
        "els => els.map(e => getComputedStyle(e).animationDelay)")

    assert delays[0] == "0s"
    assert delays[1] != delays[0], "the steps all land at once"
    assert len(set(delays)) >= 5, f"expected a stagger, got {sorted(set(delays))}"


def test_no_element_carries_an_inline_style_attribute(page, base_url):
    """`style-src 'self'` refuses a style attribute as firmly as a <style>
    block, so the page must not produce one at any point."""
    page.goto(base_url, wait_until="networkidle")
    page.locator("#run").click()
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)

    styled = page.eval_on_selector_all(
        "[style]", "els => els.map(e => e.tagName + '.' + e.className)")

    assert styled == [], f"inline style attributes present: {styled}"


def test_a_visitor_who_asks_for_less_motion_gets_none(still_page, base_url):
    """The stagger is presentation. Someone who has asked for less motion still
    gets every step, immediately, with nothing moving.

    This is why the journey above has to ask for motion explicitly: headless
    Chromium prefers reduced motion, so without saying so the test was
    measuring the accessible path and asserting the animated one.
    """
    still_page.goto(base_url, wait_until="networkidle")
    still_page.locator("#run").click()
    expect(still_page.locator("#trail .step")).to_have_count(11, timeout=30_000)

    # Polled rather than read once. `renderTrail` replaces the whole of
    # `#trail`, so a single `eval_on_selector_all` can land on nodes that were
    # detached between the count assertion and the read, and
    # `getComputedStyle` on a detached node returns "" for every property. That
    # is what made this flake: the failure said `{''} == {'1'}`, which is not a
    # step at the wrong opacity, it is a step that was not in the document.
    still_page.wait_for_function(
        """() => {
            const steps = [...document.querySelectorAll('#trail .step')];
            return steps.length === 11
                && steps.every(s => getComputedStyle(s).opacity === '1');
        }""",
        timeout=10_000,
    )

    opacities = still_page.eval_on_selector_all(
        "#trail .step", "els => els.map(e => getComputedStyle(e).opacity)")

    assert set(opacities) == {"1"}, "a step is invisible to a reduced-motion visitor"


# ── the console shell ────────────────────────────────────────────────────────
#
# The page stopped being one long scroll and became a set of panels with a
# period switcher, because an owner arrives with one question out of eight and
# should not scroll past the other seven to reach it. That is new behaviour and
# these are the assertions that keep it honest.

#: Every tab, and the landmark that proves its panel actually rendered.
PANELS = [
    ("overview", "#stats"), ("runner", "#trail"), ("mailbox", "#mailbox"),
    ("alloc", "#alloc"), ("register", "#register"),
    ("findings", "#findings"), ("letters", "#drafts"), ("trends", "#trends"),
    ("trucks", "#trucks"), ("checks", "#gates"),
]


@pytest.mark.parametrize("page", VIEWPORTS, indirect=True)
def test_every_tab_opens_a_panel_that_has_something_in_it(page, base_url):
    """Eight sections, walked. A tab that opens an empty pane is a dead end."""
    page.goto(base_url, wait_until="networkidle")
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)

    for name, landmark in PANELS:
        page.locator(f'.tab[data-panel="{name}"]').click()
        expect(page.locator(f"#panel-{name}")).to_be_visible()
        expect(page.locator(landmark)).to_be_visible()
        assert page.locator(landmark).inner_text().strip(), f"{name} opened empty"
        # Exactly one panel at a time, or the tabs are decoration.
        assert page.locator(".panel:visible").count() == 1


@pytest.mark.parametrize("page", VIEWPORTS, indirect=True)
def test_a_tile_opens_the_ledger_its_number_came_out_of(page, base_url):
    """A figure an owner cannot drill into is a figure they take on trust."""
    page.goto(base_url, wait_until="networkidle")
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)

    page.locator('#stats [data-goto="findings"]').first.click()

    expect(page.locator("#panel-findings")).to_be_visible()
    expect(page.locator("#panel-overview")).to_be_hidden()
    expect(page.locator('.tab[data-panel="findings"]')).to_have_class(re.compile(r"\bon\b"))


def test_the_charts_are_drawn_without_a_single_inline_style(page, base_url):
    """The bars and the donut are the newest place `style="width:62%"` wants to
    happen, and the policy refuses it. Assert the geometry is attributes."""
    page.goto(base_url, wait_until="networkidle")
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)

    widths = page.eval_on_selector_all(
        "#chart-expense circle.seg", "els => els.map(e => e.getAttribute('stroke-dasharray'))")
    assert widths and all(w for w in widths), "the donut drew no segments"

    page.locator('.tab[data-panel="trucks"]').click()
    bars = page.eval_on_selector_all(
        "#chart-trucks rect", "els => els.map(e => e.getAttribute('width'))")
    assert bars, "the fleet chart drew no bars"
    assert all(float(w) <= 100.0 for w in bars), f"a bar overflows its viewBox: {bars}"

    assert page.eval_on_selector_all("[style]", "els => els.length") == 0


def test_the_period_switcher_offers_every_month_with_mail(page, base_url):
    """Two closed months are on file. A console that can only show one of them
    is a demo of one month, not a set of books."""
    page.goto(base_url, wait_until="networkidle")

    options = page.eval_on_selector_all(
        "#period option", "els => els.map(e => e.value)")

    assert len(options) >= 2, f"only {options} on offer"
    assert options == sorted(options, reverse=True), "the newest month is not first"


def test_the_earliest_month_says_there_is_nothing_behind_it(page, base_url):
    """The edge every chart renderer gets wrong once.

    The first month on file has no month before it, so `comparison` is null.
    Drawing an empty axis would read as a real zero, which is a different and
    wrong claim, and a naive percentage against a missing month is an infinity.
    """
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(base_url, wait_until="networkidle")
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)

    earliest = page.eval_on_selector_all(
        "#period option", "els => els.map(e => e.value).sort()[0]")
    page.select_option("#period", earliest)

    expect(page.locator("#status")).to_contain_text(f"showing {earliest}", timeout=30_000)

    page.locator('.tab[data-panel="trends"]').click()
    expect(page.locator("#trend-line")).to_contain_text("nothing behind it")

    # Every other panel still has to hold a real month.
    for name, landmark in PANELS:
        page.locator(f'.tab[data-panel="{name}"]').click()
        assert page.locator(landmark).inner_text().strip() or name == "trends", \
            f"{name} rendered empty for {earliest}"

    assert errors == []


# ── the hero, the origin, and the replay ─────────────────────────────────────
#
# The first fold now answers "what happened and how much" in one line, and the
# origin card ties the books to bytes, a driver, a model and a build. These are
# the newest judge-facing claims, so they get browser assertions like the rest.

def test_the_hero_line_tells_the_whole_story_in_one_sentence(page, base_url):
    page.goto(base_url, wait_until="networkidle")
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)

    hero = page.locator("#hero-line")
    expect(hero).to_be_visible()
    expect(hero).to_contain_text("One payment.")
    expect(hero).to_contain_text("$")


def test_the_origin_card_names_the_mailbox_the_driver_and_the_run(page, base_url):
    """Provenance, not decoration: a local run must say it read the bundled
    sample and was driven deterministically, because that is the truth here."""
    page.goto(base_url, wait_until="networkidle")
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)

    origin = page.locator("#origin")
    expect(origin).to_be_visible()
    expect(origin).to_contain_text("bundled synthetic sample")
    expect(origin).to_contain_text("deterministic")
    expect(origin).to_contain_text("Run")


def test_replay_re_renders_without_re_executing_anything(page, base_url):
    """The watchable version of the run must not masquerade as the production
    trigger. The status line says so in as many words, and no network request
    for a close leaves the page."""
    page.goto(base_url, wait_until="networkidle")
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)

    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)

    page.locator("#replay").click()

    expect(page.locator("#trail .step")).to_have_count(11)
    expect(page.locator("#status")).to_contain_text("nothing was re-executed")
    assert posts == [], f"replay reached the server: {posts}"

    delays = page.eval_on_selector_all(
        "#trail .step", "els => els.map(e => getComputedStyle(e).animationDelay)")
    assert len(set(delays)) >= 5, "the replayed trail lost its stagger"


@pytest.mark.parametrize("width", [1920, 1280, 1100, 900, 375])
def test_a_tile_badge_never_lands_on_top_of_its_own_label(browser, base_url, width):
    """Caught on film, which is the worst place to catch anything.

    `.card .badge` was `position: absolute; top: 13px; right: 14px`, so it was
    out of flow and free to sit ON a long label rather than beside it. On the
    author's machine "LEAKING AWAY" cleared "5 letters ready" by 6.5px at 1920
    and nothing looked wrong. On the Linux runner that records the demo the
    glyphs are wider, the clearance went negative, and the badge printed across
    the last letters of the word — on the money-found tile, at 1920x1080, in
    the video a judge watches.

    Exactly the fragility that produced the 4px sideways scroll: a layout that
    depends on the font the machine happens to resolve. The structural fix is
    the badge being in flow, and this asserts the property rather than the
    clearance, so it holds at any width and in any font.

    1920 is here because that is the capture width; the rest are widths the
    grid reflows at, where the cards are narrowest and a collision is likeliest.
    """
    context = browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    try:
        page.goto(base_url, wait_until="networkidle")
        expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)

        collisions = page.eval_on_selector_all(
            ".card",
            """cards => cards.flatMap(card => {
                 const k = card.querySelector('.k'), b = card.querySelector('.badge');
                 if (!k || !b) return [];
                 const kr = k.getBoundingClientRect(), br = b.getBoundingClientRect();
                 const x = Math.min(kr.right, br.right) - Math.max(kr.left, br.left);
                 const y = Math.min(kr.bottom, br.bottom) - Math.max(kr.top, br.top);
                 return (x > 0.5 && y > 0.5)
                   ? [{label: k.textContent.trim(), badge: b.textContent.trim(),
                       overlap: Math.round(x)}]
                   : [];
               })""")

        assert collisions == [], (
            f"at {width}px a badge is printed over its own label: {collisions}"
        )
    finally:
        context.close()


def test_a_visitor_can_work_the_one_human_gate(page, base_url):
    """The control the page claims, made operable and honest about its limits.

    The letters panel said "a person presses send" and gave nobody anything to
    press. A control claim with nothing to control is the worst version of it:
    a judge reads the sentence, looks for the button, and concludes the
    approval boundary is a paragraph.

    So it is a real decision with a real state change and a real audit line,
    and it says three true things about itself in the copy beside it: no
    message is sent, the decision is not kept, and the approver on a configured
    deployment would be a person rather than "sandbox visitor".
    """
    page.goto(base_url, wait_until="networkidle")
    expect(page.locator("#trail .step")).to_have_count(11, timeout=30_000)
    page.locator('.tab[data-panel="letters"]').click()

    drafts = page.locator("[data-draft]")
    expect(drafts.first).to_be_visible()
    expect(page.locator('[data-state="0"]')).to_have_text("filed, not sent")

    page.locator('[data-decide="approve"][data-index="0"]').click()
    expect(page.locator('[data-state="0"]')).to_have_text("approved, not sent")

    audit = page.locator('[data-audit="0"]')
    expect(audit).to_contain_text("approved by sandbox visitor")
    expect(audit).to_contain_text("nothing was sent")

    page.locator('[data-decide="reject"][data-index="1"]').click()
    expect(page.locator('[data-state="1"]')).to_have_text("rejected")
    expect(page.locator('[data-audit="1"]')).to_contain_text("rejected by sandbox visitor")
    expect(page.locator('[data-audit="1"]')).to_contain_text("no letter leaves")

    # The claim the whole gate rests on: a decision on this page cannot reach a
    # third party. Asserted by what the page says, because the page is what a
    # judge reads, and by the absence of any request leaving on the click.
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    page.locator('[data-decide="approve"][data-index="2"]').click()
    expect(page.locator('[data-state="2"]')).to_have_text("approved, not sent")
    assert posts == [], f"an approval reached the server: {posts}"


def test_the_guided_tour_never_opens_by_itself(page, base_url):
    """A judge arrives at ten panels with nobody beside them, so the tour
    exists. It must still be opt-in.

    The CI journey above clicks `#run` and the tab strip, and the video capture
    scrolls to `#digest`, `#origin` and `#alloc`. A card that opened on load
    would sit on top of all of them, and this repository has already been
    bitten once by a helpful thing that appeared uninvited.
    """
    page.goto(base_url, wait_until="networkidle")

    expect(page.locator("#tour")).to_be_hidden()
    expect(page.locator("#tour-start")).to_be_visible()


def test_the_guided_tour_walks_every_stop_and_leaves_nothing_behind(page, base_url):
    """Eight stops, each focusing exactly one thing, and closing puts the page
    back the way it was found."""
    page.goto(base_url, wait_until="networkidle")
    page.locator("#tour-start").click()

    total = page.evaluate("() => window.archonTour.length")
    assert total == 8

    seen = []
    for n in range(total):
        expect(page.locator("#tour-count")).to_have_text(f"{n + 1} / {total}")
        assert page.locator("#tour-title").inner_text().strip(), f"stop {n + 1} has no title"
        focused = page.eval_on_selector_all(".tour-focus", "els => els.map(e => e.id)")
        assert len(focused) == 1, f"stop {n + 1} focused {focused}"
        # The class landing on one node is not the same as a visitor seeing a
        # ring. Two stops shipped green under the count alone: they opened one
        # panel and highlighted an element on another, which show() had just
        # set to display:none. A quarter of the tour highlighted air.
        expect(page.locator(".tour-focus")).to_be_visible()
        seen.append(focused[0])
        page.locator("#tour-next").click()

    expect(page.locator("#tour")).to_be_hidden()
    assert page.eval_on_selector_all(".tour-focus", "els => els.length") == 0, (
        "the focus ring outlived the tour")
    assert "alloc" in seen and "drafts" in seen and "gates" in seen, seen


def test_the_guided_tour_adds_no_inline_style(page, base_url):
    """The tour highlights by adding a CLASS, never a computed rectangle.

    Positioning a card against a moving target is the obvious way to build
    this and it is the one way that cannot ship here: `style-src 'self'`
    refuses an inline style attribute, so the card is fixed by CSS and the
    focus ring is a class on the target.
    """
    page.goto(base_url, wait_until="networkidle")
    page.locator("#tour-start").click()
    for _ in range(7):
        page.locator("#tour-next").click()

    styled = page.eval_on_selector_all(
        "[style]", "els => els.map(e => e.tagName + '.' + e.className)")

    assert styled == [], f"the tour produced inline styles: {styled}"
