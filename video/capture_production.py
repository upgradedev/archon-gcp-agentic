"""Record the judge's journey at 1920x1080, one hold per narration beat.

The choreography is not invented here. It is the sequence already asserted at
both viewports by `tests/e2e/test_browser_journey.py`, which runs on every push:
open the page, read the origin receipts, replay the recorded run, watch eleven
steps land, read the owner's letter, see the five counterparty letters marked
filed rather than sent. The
video shows exactly what that test proves, so the cut cannot drift away from the
product without CI going red first.

**Python rather than Node, deliberately.** The kit's capture was a `.mjs` that
needed npm, a `package.json` and a lockfile in a repository that otherwise has
no JavaScript toolchain at all. Playwright's Python binding records video just
as well, it is already a dependency because the browser journey uses it, and one
language means one set of versions to keep honest.

It records the DEPLOYED service. The workflow refuses to film until the live
health endpoint reports the exact release being recorded and the adk-agent
close path, so the pixels on film come from the URL a judge opens, showing the
run that production actually persisted, provenance card included.

    ARCHON_VIDEO_ROOT=/tmp/video ARCHON_RELEASE_SHA=<40 hex> \\
        python video/capture_production.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

#: The beats, in the order this file holds them. The narration must agree.
EXPECTED_BEATS = ["hook", "surface", "trigger", "cloud", "live", "sponsor",
                  "found", "letters", "gates", "evidence", "close"]

VIEWPORT = {"width": 1920, "height": 1080}

#: Seconds of still page after the last beat. Covers the trim lead and
#: encoder rounding, so -shortest never clips the ending.
TAIL_SECONDS = 4.0


def _fail(message: str) -> None:
    raise SystemExit(message)


def main() -> int:
    root = os.environ.get("ARCHON_VIDEO_ROOT")
    release_sha = os.environ.get("ARCHON_RELEASE_SHA", "")
    app_url = os.environ.get("ARCHON_APP_URL", "http://127.0.0.1:8080/")

    if not root:
        _fail("ARCHON_VIDEO_ROOT is required.")
    if not re.fullmatch(r"[a-f0-9]{40}", release_sha):
        _fail("ARCHON_RELEASE_SHA must be the exact 40 character commit being recorded.")

    root_path = Path(root)
    capture_dir = root_path / "capture"
    capture_dir.mkdir(parents=True, exist_ok=False)

    timing = json.loads((root_path / "narration" / "timing.json").read_text(encoding="utf-8"))
    holds = {scene["id"]: float(scene["holdSeconds"]) for scene in timing["scenes"]}

    # The narration and the journey have to agree beat for beat. If either moves
    # without the other, the cut goes silent over live pixels or holds on a
    # frozen frame, so this fails before recording rather than after.
    actual = [scene["id"] for scene in timing["scenes"]]
    if actual != EXPECTED_BEATS:
        _fail(f"narration beats {actual} do not match the journey {EXPECTED_BEATS}")

    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--force-device-scale-factor=1"])
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            record_video_dir=str(capture_dir / "raw"),
            record_video_size=VIEWPORT,
            # Headless Chromium prefers reduced motion, and the page honours
            # that by switching the stagger off. Without asking for motion the
            # recording would show all ten steps appearing at once, which is
            # the one thing this video exists not to show.
            reduced_motion="no-preference",
        )
        page = context.new_page()

        # Anything the page throws during the recording is a defect in the take,
        # and a take with a broken page is not a take. Policy violations count:
        # one of them once blocked the run trail's stagger, which is the single
        # thing this video exists to show.
        page.on("pageerror", lambda exc: errors.append(f"page:{type(exc).__name__}"))
        page.on("console",
                lambda msg: errors.append(f"console:{msg.text[:80]}")
                if msg.type == "error" else None)
        page.add_init_script(
            "document.addEventListener('securitypolicyviolation',"
            " e => console.error('csp:' + e.violatedDirective));"
        )

        capture_started = time.monotonic()
        page.goto(app_url, wait_until="networkidle", timeout=60_000)
        page.get_by_role(
            "heading", name=re.compile("It closes the month while nobody is watching")
        ).wait_for(timeout=30_000)
        timeline_started = time.monotonic()

        def hold(beat: str, action) -> None:
            started = time.monotonic()
            action()
            remaining = holds[beat] - (time.monotonic() - started)
            if remaining > 0:
                page.wait_for_timeout(remaining * 1000)

        #: Which section of the console holds each landmark. The page is a set
        #: of panels now rather than one long scroll, and an inactive panel is
        #: `display:none`, which `scroll_into_view_if_needed` refuses to act on.
        #: So the beat opens the tab first and then scrolls, exactly as a
        #: visitor does. Getting this wrong does not produce a bad take, it
        #: produces a timeout, which is the failure mode to prefer.
        PANEL_OF = {
            "#trail": "runner", "#stats": "overview", "#origin": "overview",
            "#register": "register", "#alloc": "alloc", "#findings": "findings",
            "#digest": "letters", "#drafts": "letters",
            "#trends": "trends", "#trucks": "trucks", "#gates": "checks",
        }

        def scroll_to(selector: str) -> None:
            panel = PANEL_OF.get(selector)
            if panel:
                tab = page.locator(f'.tab[data-panel="{panel}"]')
                if not tab.evaluate("el => el.classList.contains('on')"):
                    tab.click()
                    page.wait_for_timeout(350)
            page.locator(selector).scroll_into_view_if_needed()
            page.wait_for_timeout(400)

        # 1. The problem, on the page that states it.
        hold("hook", lambda: page.evaluate("() => window.scrollTo({top: 0})"))

        # 2. The surface the owner already opens. On arrival the page has
        #    replayed the last close, so the letter is there before any press.
        hold("surface", lambda: scroll_to("#digest"))

        # 3. Nobody presses anything, and nothing on film pretends otherwise.
        #    The proof of the trigger is the origin card: the gs:// object and
        #    generation that fired, the Pub/Sub message that delivered it, the
        #    agent that drove the close, the model, and the build. This beat
        #    used to stage a button press as a stand-in; now it shows the
        #    receipts of the run that actually happened unattended.
        hold("trigger", lambda: scroll_to("#origin"))

        # 4. Google Cloud, demonstrated rather than asserted. The submission
        #    rules REQUIRE this beat: the video must show the backend running on
        #    Google Cloud. The address bar carries the .run.app host, and the
        #    health route is the deployment answering for itself -- the release
        #    it is serving, `firestore` as the store it persists to, and the
        #    agent close path. Then straight back to the console.
        #
        #    Filmed from the live endpoint rather than a console screenshot
        #    because this workflow holds no browser session for the Cloud
        #    Console, and a still nobody can reproduce proves less than a URL
        #    every viewer can open themselves.
        def cloud():
            page.goto(app_url + "api/health", wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(holds["cloud"] * 620)
            page.goto(app_url, wait_until="networkidle", timeout=60_000)
            page.get_by_role(
                "heading", name=re.compile("It closes the month while nobody is watching")
            ).wait_for(timeout=30_000)

        hold("cloud", cloud)

        # 4. The chore, running. Eleven steps, landing one at a time.
        #
        #    This presses "Watch the agent", which re-renders the trail of the
        #    run Pub/Sub already triggered. It does NOT press "Run fresh close",
        #    and that is deliberate rather than a shortcut:
        #
        #    The narration over this beat opens "Eleven steps, unattended." A
        #    human pressing a button on camera under that word is a different
        #    claim needing different audio. The origin card in beat 3 is what
        #    proves the trigger, and it proves it with the gs:// object, the
        #    generation, the Pub/Sub message, the model and the build.
        #
        #    It is also the only version that can be filmed reliably. A fresh
        #    close is a live thinking-model round trip inside a fixed hold: run
        #    long and the film cuts away on a half-drawn trail, run short and
        #    there is dead air. The public route allows three closes per address
        #    per ten minutes, and the capture is one address, so a retry inside
        #    the window would be refused mid-film.
        #
        #    The page states the re-execution claim in its own words: the hero
        #    context and the runner panel both say the replay executes nothing,
        #    and the status line written on the press says no model was called.
        def watch():
            page.locator("#replay").click()
            scroll_to("#trail")
            page.wait_for_function(
                "() => document.querySelectorAll('#trail .step').length === 11",
                timeout=60_000,
            )

        hold("live", watch)

        # 5. What the agent did that matching cannot: one payment split across
        #    eight loads, with the identity closing.
        hold("sponsor", lambda: scroll_to("#alloc"))

        # 7. What it found on its own, one detector at a time.
        hold("found", lambda: scroll_to("#findings"))

        # 8. The letters, every one filed and none sent. This is the beat the
        #    narration spends the longest on, because "it wrote them and did not
        #    send them" is the claim a viewer is most entitled to disbelieve.
        hold("letters", lambda: scroll_to("#drafts"))

        # 9. The gates, and that a close can be refused by them.
        hold("gates", lambda: scroll_to("#gates"))

        # 10. The receipts: hashes, run id, release, the eleven-step trail.
        def evidence():
            scroll_to("#trail")
            page.wait_for_timeout(holds["evidence"] * 500)
            scroll_to("#origin")

        hold("evidence", evidence)

        # 11. Back to the statement the whole thing is answering.
        def ending():
            page.evaluate("() => window.scrollTo({top: 0, behavior: 'smooth'})")
            page.wait_for_timeout(400)

        hold("close", ending)

        # A tail, and it is not decoration.
        #
        # The composer runs ffmpeg with -shortest, so if the capture is even a
        # fraction shorter than the trim lead plus the narration, the last beat
        # is cut off BOTH streams. The first real run missed by 0.104 seconds:
        # the holds sum to exactly the narration length, leaving nothing for
        # encoder rounding.
        #
        # The gate's own words were "record a longer capture, do not widen this
        # tolerance", which is the right instruction. So the capture now ends
        # with the page sitting still for a moment, which is also how a take
        # should end rather than cutting on the last syllable.
        page.wait_for_timeout(TAIL_SECONDS * 1000)

        trim_lead = max(0.0, timeline_started - capture_started)
        video = page.video
        if video is None:
            _fail("Playwright did not create a video recorder.")
        context.close()
        raw_path = Path(video.path())
        browser.close()

    final_path = capture_dir / "production.webm"
    raw_path.replace(final_path)
    payload = final_path.read_bytes()

    receipt = {
        "schemaVersion": "archon.submission-video-capture/v1",
        "releaseSha": release_sha,
        "appUrl": app_url,
        "sceneCount": len(EXPECTED_BEATS),
        "trimLeadSeconds": round(trim_lead, 3),
        "timelineSeconds": float(timing["totalSeconds"]),
        "pageErrors": errors,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    (capture_dir / "capture-receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )

    if errors:
        _fail(f"the journey emitted {len(errors)} browser error(s): {errors}")

    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
