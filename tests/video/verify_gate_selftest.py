#!/usr/bin/env python3
"""Regression test for the demo-video A/V/caption SYNC GATE (scripts/verify_video_sync.py).

Why this exists
---------------
The demo video is assembled by a MONOLITHIC ffmpeg compose step (title + burned
captions + terminal screencast + live web segment + outro, then the muxed voiceover).
That style is drift-prone, so the pipeline does NOT trust it: scripts/verify_video_sync.py
is a fail-closed gate that measures the SHIPPED mp4 (real pixels via Sobel-edge density
for segment order, real audio/video stream durations, caption windows) and fails CI on
any drift or mis-order. The standing rule is: never ship a demo video without that gate.

But a detect-only gate is worth exactly its proven ability to FAIL. If the gate silently
stopped tripping (a refactor inverts a comparison, a threshold drifts, a check is skipped),
a drifted video could sail through and the rule would be violated without anyone noticing.
This test guards the guard: it synthesizes tiny mp4s with ffmpeg — one CORRECT video and
three deliberately-BROKEN ones — drives the real gate against each, and asserts:

  * the correct video PASSES (exit 0), and
  * each broken video FAILS (exit 1) for the RIGHT reason (the expected gate label).

It renders NO real narration (no ElevenLabs/edge-tts, no keys, no network) and never
touches the build assembly — it only feeds synthetic fixtures to the gate. Dependencies
are exactly what the gate itself needs in CI: ffmpeg/ffprobe on PATH and Pillow.

Broken cases proven to trip the gate:
  1. BAD_ORDER          — outro concatenated BEFORE the web segment  -> 'order-pixels'
  2. BAD_CAPTION_BLEED  — a caption window ends after the outro starts -> 'captions-no-outro-bleed'
  3. BAD_AV_MISMATCH    — audio stream 1s longer than the video       -> 'av-duration'

Run: python tests/video/verify_gate_selftest.py   (exit 0 = gate is correct)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
GATE = os.path.join(REPO, "scripts", "verify_video_sync.py")

FPS = 30
# Solid emerald card -> low Sobel-edge density -> the gate classifies it as a "card".
CARD_SRC = "color=c=0x0d2b22:s=640x360:r=30"
# testsrc2 is detail-dense -> high edge density -> classified as "content" (terminal/browser).
CONTENT_SRC = "testsrc2=s=640x360:r=30"

# Fixed synthetic geometry (seconds). Kept tiny so the whole test renders in a few seconds.
TITLE_DUR = 1.0
SCREENCAST_DUR = 4.0
WEB_DUR = 2.0
OUTRO_DUR = 1.0
A_MAIN = 3.5            # narration length; content-seam = SCREENCAST_DUR - A_MAIN = 0.5s (<=1s)
TOTAL = TITLE_DUR + SCREENCAST_DUR + WEB_DUR + OUTRO_DUR


def ff(args: list[str]) -> None:
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("ffmpeg failed:\n" + " ".join(args) + "\n" + r.stderr)


def _segment(kind: str, dur: float, out: str) -> None:
    src = CARD_SRC if kind == "card" else CONTENT_SRC
    ff(["-f", "lavfi", "-i", src, "-t", f"{dur}", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "ultrafast", "-r", "30", out])


def build_case(order, work, *, cap_bleed=False, audio_longer=False):
    """Assemble a synthetic mp4 + its manifest + caption windows.

    order: ordered list of (name, kind, dur). The MANIFEST always declares the CORRECT
    logical geometry (title/screencast/web/outro) — the gate reads the real pixels, so a
    mis-ordered concat is caught even though the manifest constant looks right.
    """
    parts = []
    for i, (name, kind, dur) in enumerate(order):
        p = os.path.join(work, f"seg_{i}.mp4")
        _segment(kind, dur, p)
        parts.append(p)
    listf = os.path.join(work, "concat.txt")
    with open(listf, "w", encoding="utf-8") as fh:
        fh.write("".join(f"file '{p}'\n" for p in parts))
    silent = os.path.join(work, "silent.mp4")
    ff(["-f", "concat", "-safe", "0", "-i", listf, "-c", "copy", silent])

    # Audio: a sine tone over the content span [title_end, web_end] so the web beat is
    # audible and there is no >1s silent gap inside content; title + outro stay silent
    # (the gate exempts the leading title card and the trailing/outro tail).
    content_start = TITLE_DUR
    content_end = TITLE_DUR + SCREENCAST_DUR + WEB_DUR
    tspan = content_end - content_start
    adur = (TOTAL + 1.0) if audio_longer else TOTAL
    tone = os.path.join(work, "tone.wav")
    ff(["-f", "lavfi", "-i", f"sine=frequency=300:duration={tspan}",
        "-ar", "44100", "-ac", "2", tone])
    audio = os.path.join(work, "audio.wav")
    ff(["-i", tone, "-af", f"adelay={int(content_start * 1000)}:all=1,apad",
        "-t", f"{adur}", "-ar", "44100", "-ac", "2", audio])

    mp4 = os.path.join(work, "final.mp4")
    # NB: no -shortest — we want the audio and video streams to keep their own durations
    # (the audio_longer case relies on a genuine a:0 > v:0 stream mismatch).
    ff(["-i", silent, "-i", audio, "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", mp4])

    manifest = {
        "fps": FPS, "title_dur": TITLE_DUR, "screencast_dur": SCREENCAST_DUR,
        "web_dur": WEB_DUR, "outro_dur": OUTRO_DUR,
        "segments": ["title", "screencast", "web", "outro"],
        "a_main": A_MAIN, "a_web": 1.0, "vo_delay": TITLE_DUR, "have_vo": True,
    }
    mpath = os.path.join(work, "video_manifest.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)

    outro_start = TITLE_DUR + SCREENCAST_DUR + WEB_DUR
    if cap_bleed:
        # One caption that ends AFTER the outro begins -> must trip captions-no-outro-bleed.
        windows = [[TITLE_DUR + 0.1, outro_start + 0.5, "bleeds into outro"]]
    else:
        windows = [[TITLE_DUR + 0.1, TITLE_DUR + 0.5, "cap a"],
                   [TITLE_DUR + 0.6, TITLE_DUR + SCREENCAST_DUR - 0.2, "cap b"]]
    wpath = os.path.join(work, "caption_windows.json")
    with open(wpath, "w", encoding="utf-8") as fh:
        json.dump(windows, fh)
    return mp4, mpath, wpath


def run_gate(mp4, manifest, windows):
    env = dict(os.environ, VIDEO_MANIFEST=manifest, CAPTION_WINDOWS=windows)
    r = subprocess.run([sys.executable, GATE, mp4],
                       capture_output=True, text=True, env=env)
    fails = re.findall(r"::error::  - (\S+)", r.stdout + r.stderr)
    return r.returncode, fails, r.stdout + r.stderr


GOOD = [("title", "card", TITLE_DUR), ("screencast", "content", SCREENCAST_DUR),
        ("web", "content", WEB_DUR), ("outro", "card", OUTRO_DUR)]
# Outro concatenated BEFORE the web segment — the classic "outro not last" defect.
BAD_ORDER = [("title", "card", TITLE_DUR), ("screencast", "content", SCREENCAST_DUR),
             ("outro", "card", OUTRO_DUR), ("web", "content", WEB_DUR)]

# (label, build-kwargs, order, expect_pass, expected_fail_label)
CASES = [
    ("GOOD", {}, GOOD, True, None),
    ("BAD_ORDER(outro-before-web)", {}, BAD_ORDER, False, "order-pixels"),
    ("BAD_CAPTION_BLEED", dict(cap_bleed=True), GOOD, False, "captions-no-outro-bleed"),
    ("BAD_AV_MISMATCH", dict(audio_longer=True), GOOD, False, "av-duration"),
]


def main() -> int:
    if not os.path.exists(GATE):
        raise SystemExit(f"::error::gate script not found: {GATE}")
    if subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0:
        raise SystemExit("::error::ffmpeg not on PATH — required for the sync-gate selftest")

    results = []
    with tempfile.TemporaryDirectory() as root:
        for label, kwargs, order, expect_pass, want_fail in CASES:
            work = os.path.join(root, re.sub(r"\W+", "_", label))
            os.makedirs(work, exist_ok=True)
            mp4, mpath, wpath = build_case(order, work, **kwargs)
            rc, fails, log = run_gate(mp4, mpath, wpath)
            passed = rc == 0
            ok = passed == expect_pass
            # For a broken case, also require the RIGHT gate label to have tripped, so the
            # test proves the SPECIFIC guarantee — not merely that the gate failed somehow.
            if ok and want_fail is not None:
                ok = want_fail in fails
            results.append((label, expect_pass, want_fail, rc, fails, ok))
            print(f"[{'OK ' if ok else 'FAIL'}] {label:32s} "
                  f"expect_pass={expect_pass} rc={rc} failing={fails or '-'}")
            if not ok:
                print("---- gate output ----\n" + log + "\n---------------------")

    print("\n==== sync-gate selftest summary ====")
    all_ok = all(r[5] for r in results)
    for label, ep, wf, rc, fails, ok in results:
        want = f"expect fail '{wf}'" if wf else "expect PASS"
        print(f"  {'OK ' if ok else 'BAD'} {label:32s} {want:22s} -> rc={rc} {fails or ''}")
    if not all_ok:
        print("::error::sync-gate selftest FAILED — verify_video_sync.py no longer trips "
              "as expected. The demo-video sync gate is not trustworthy; fix before shipping.")
        return 1
    print("sync-gate selftest: the A/V/caption gate correctly PASSES a good video and "
          "FAILS on mis-order, caption bleed, and A/V mismatch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
