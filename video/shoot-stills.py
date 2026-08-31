#!/usr/bin/env python3
"""Render the gallery stills: the thumbnail, the opening slides, the live close.

    python video/shoot-stills.py <out-dir> [app-url]

Devpost shows the first image as the project's thumbnail in a gallery of
thousands, at roughly 300px wide, so these are 3:2 and sized to survive that.
The app frames are captured from the DEPLOYED service, not a local one, for the
same reason the video is: a still of a laptop proves nothing.

Nothing here is composed by hand. The slides come from video/deck.html and the
app frames come from whatever the deployment is currently serving, so a still
that disagrees with the product is a still that cannot be produced.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

if sys.platform == "win32":  # Playwright's sync API needs subprocess support
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
GALLERY = {"width": 1800, "height": 1200}          # 3:2, as Devpost asks
WIDE = {"width": 1920, "height": 1080}

#: (id, panel to open, element to frame). None opens nothing and frames the page.
APP_STILLS = [
    ("04-the-close", "runner", "#trail"),
    ("05-one-payment-eight-loads", "alloc", "#alloc"),
    ("06-what-it-found", "findings", "#findings"),
    ("07-letters-filed-unsent", "letters", "#drafts"),
    ("08-it-can-refuse", "checks", "#gates"),
]


def main() -> int:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "stills")
    app_url = (sys.argv[2] if len(sys.argv) > 2
               else "https://archon-70489367760.us-central1.run.app/")
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--force-device-scale-factor=1"])

        # 1. The thumbnail, 3:2 because it is the one image the gallery shows.
        page = browser.new_page(viewport=GALLERY, device_scale_factor=1)
        page.goto((HERE / "thumbnail.html").as_uri(), wait_until="networkidle")
        page.wait_for_timeout(600)
        page.screenshot(path=str(out / "01-thumbnail.png"))
        print("01-thumbnail.png")

        # 2. The architecture diagram, which the rules ask for by name and a
        #    form wants as a file. The README carries the same structure as
        #    mermaid for GitHub to render inline; this is the version that can
        #    be uploaded.
        page.set_viewport_size({"width": 1920, "height": 940})
        page.goto((HERE / "architecture.html").as_uri(), wait_until="networkidle")
        page.wait_for_timeout(700)
        page.screenshot(path=str(out / "00-architecture.png"))
        print("00-architecture.png")

        # 2b. And the infrastructure half of it: not what the system does, but
        #     who is allowed to do it. Every role on it is read off main.tf.
        page.set_viewport_size({"width": 1920, "height": 920})
        page.goto((HERE / "infra.html").as_uri(), wait_until="networkidle")
        page.wait_for_timeout(700)
        page.screenshot(path=str(out / "00-infrastructure.png"))
        print("00-infrastructure.png")

        # 3. The opening slides, held long enough for their choreography to land.
        page.set_viewport_size(WIDE)
        page.goto((HERE / "deck.html").as_uri(), wait_until="networkidle")
        page.wait_for_function("() => !!window.archonDeck", timeout=15_000)
        for index, (stage, settle) in enumerate(((1, 11_000), (2, 5_000)), start=2):
            page.evaluate(f"() => window.archonDeck.show({stage})")
            page.wait_for_timeout(settle)
            name = f"0{index}-slide-{stage}.png"
            page.screenshot(path=str(out / name))
            print(name)
        page.close()

        # 4. The deployment itself, panel by panel.
        page = browser.new_page(viewport=WIDE, device_scale_factor=1)
        page.goto(app_url, wait_until="networkidle", timeout=60_000)
        page.wait_for_function(
            "() => document.querySelectorAll('#trail .step').length === 11",
            timeout=60_000)
        for name, panel, target in APP_STILLS:
            tab = page.locator(f'.tab[data-panel="{panel}"]')
            if not tab.evaluate("el => el.classList.contains('on')"):
                tab.click()
                page.wait_for_timeout(500)
            page.locator(target).scroll_into_view_if_needed()
            page.wait_for_timeout(700)
            page.screenshot(path=str(out / f"{name}.png"))
            print(f"{name}.png")

        browser.close()

    for shot in sorted(out.glob("*.png")):
        print(f"  {shot.name:<34} {shot.stat().st_size / 1024:>7.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
