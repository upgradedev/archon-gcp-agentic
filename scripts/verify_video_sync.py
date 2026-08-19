#!/usr/bin/env python3
"""Permanent A/V/caption SYNC GATE for the demo video (fails CI on any drift/mis-order).

Runs AFTER the final mp4 is composed. It measures the shipped artifact with ffprobe /
ffmpeg and asserts the four guarantees the video must always hold. It is STRICT and
self-explanatory: every failure prints WHICH assertion broke and the numbers behind it,
so an out-of-sync or mis-ordered video can never ship again.

Inputs:
  argv[1]            final mp4 (default demo/final-media/memoryagent-demo.mp4)
  VIDEO_MANIFEST     json emitted by the compose step (default video_manifest.json):
                       { fps, title_dur, screencast_dur, web_dur, outro_dur,
                         segments:[...ordered names...], a_main, a_web, vo_delay, have_vo }
  CAPTION_WINDOWS    json of absolute caption windows (default caption_windows.json):
                       [[abs_start, abs_end, text], ...]

Guarantees (task spec):
  1. audio_dur ~= video_dur (<=1.5/fps) OR audio ends at/before video with no >1s silent
     tail in CONTENT; and structurally the screencast never outlasts the narration by >1s
     (no silent+frozen content seam).
  2. every caption window is inside [0, video_dur], monotonic, non-overlapping, and the
     last content caption ends BEFORE the outro card begins (no bleed into web/outro).
  3. segment ORDER is exactly title -> screencast(content) -> web -> outro (outro last),
     and the segment durations reconstruct the measured video length.
  4. the web-beat narration is non-silent during the web-segment window.
"""
import json
import os
import re
import subprocess
import sys

TITLE = "title"
SCREENCAST = "screencast"
WEB = "web"
OUTRO = "outro"
EXPECTED_ORDER = [TITLE, SCREENCAST, WEB, OUTRO]


class Gate:
    def __init__(self):
        self.failures = []

    def check(self, ok, label, detail):
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label} :: {detail}")
        if not ok:
            self.failures.append(f"{label} :: {detail}")


def ffprobe_dur(path, stream=None):
    cmd = ["ffprobe", "-v", "error"]
    if stream:
        cmd += ["-select_streams", stream]
    cmd += ["-show_entries", "format=duration" if not stream else "stream=duration",
            "-of", "default=nw=1:nk=1", path]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip().splitlines()
    for line in out:
        line = line.strip()
        if line and line.upper() != "N/A":
            return float(line)
    return None


def silence_intervals(path, noise_db=-40, min_d=1.0):
    """Return list of (start, end_or_None) silent intervals; end None => runs to EOF."""
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
         "-af", f"silencedetect=noise={noise_db}dB:d={min_d}", "-f", "null", "-"],
        capture_output=True, text=True)
    log = p.stderr
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    intervals = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        intervals.append((s, e))
    return intervals


def max_volume(path, start, dur):
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start}", "-t", f"{dur}",
         "-i", path, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True)
    m = re.search(r"max_volume:\s*(-?[0-9.]+)\s*dB", p.stderr)
    return float(m.group(1)) if m else None


# card (title/outro) vs content (screencast/browser) split by frame edge density.
# Measured on real frames: solid emerald cards ~1.4-2.3, terminal/browser ~7.6-14.3.
# Threshold 4.5 keeps a 2-5x margin on both sides.
EDGE_CARD_CONTENT_THRESHOLD = 4.5


def frame_edge_mean(path, t):
    """Mean Sobel-edge magnitude of the frame at time t (detail density).

    Reads the ACTUAL pixels of the shipped mp4 — this is what makes the order gate
    measure the video instead of trusting a hardcoded manifest constant. A frozen
    solid card scores low; a text-dense terminal/browser frame scores high.
    """
    from PIL import Image, ImageFilter, ImageStat  # provided in CI (pillow>=10)

    png = f".sync_frame_{t:.3f}.png"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t}", "-i", path,
         "-frames:v", "1", png],
        capture_output=True, text=True)
    if not os.path.exists(png):
        return None
    try:
        im = Image.open(png).convert("L")
        return ImageStat.Stat(im.filter(ImageFilter.FIND_EDGES)).mean[0]
    finally:
        try:
            os.remove(png)
        except OSError:
            pass


def frame_kind(path, t):
    em = frame_edge_mean(path, t)
    if em is None:
        return None, None
    return ("content" if em > EDGE_CARD_CONTENT_THRESHOLD else "card"), em


def main():
    mp4 = sys.argv[1] if len(sys.argv) > 1 else "demo/final-media/memoryagent-demo.mp4"
    manifest_path = os.environ.get("VIDEO_MANIFEST", "video_manifest.json")
    windows_path = os.environ.get("CAPTION_WINDOWS", "caption_windows.json")

    for p in (mp4, manifest_path, windows_path):
        if not os.path.exists(p):
            sys.exit(f"::error::verify_video_sync: required input missing: {p}")

    with open(manifest_path, encoding="utf-8") as fh:
        man = json.load(fh)
    with open(windows_path, encoding="utf-8") as fh:
        windows = json.load(fh)

    fps = float(man.get("fps", 30))
    title_dur = float(man["title_dur"])
    screencast_dur = float(man["screencast_dur"])
    web_dur = float(man["web_dur"])
    outro_dur = float(man["outro_dur"])
    a_main = float(man["a_main"])
    vo_delay = float(man.get("vo_delay", title_dur))
    have_vo = bool(man.get("have_vo", True))
    segments = list(man["segments"])

    # Segment boundaries in ABSOLUTE video time.
    title_end = title_dur
    screencast_end = title_end + screencast_dur          # end of terminal content
    web_end = screencast_end + web_dur                   # end of web content
    outro_start = web_end
    outro_end = outro_start + outro_dur
    expected_video = outro_end

    video_dur = ffprobe_dur(mp4, "v:0") or ffprobe_dur(mp4)
    audio_dur = ffprobe_dur(mp4, "a:0")
    frame_tol = 1.5 / fps

    g = Gate()
    print("== segment timeline (absolute seconds) ==")
    print(f"  title     : 0.000 -> {title_end:.3f}")
    print(f"  screencast: {title_end:.3f} -> {screencast_end:.3f}  (content, a_main={a_main:.3f})")
    print(f"  web       : {screencast_end:.3f} -> {web_end:.3f}")
    print(f"  outro     : {outro_start:.3f} -> {outro_end:.3f}")
    print(f"  measured video={video_dur} audio={audio_dur} fps={fps}")
    print()

    # ---- req 3: ORDER (measured from the actual pixels, not a manifest constant) ----
    print("== req3: segment order + duration reconstruction ==")
    g.check(segments == EXPECTED_ORDER, "order-manifest",
            f"segments={segments} expected={EXPECTED_ORDER}")
    # Authoritative order check: sample the MIDPOINT of each segment window and assert the
    # content-type sequence is card -> content -> content -> card (title, screencast, web,
    # outro). This reads the shipped frames, so a re-ordered concat (e.g. the defect-1
    # outro-before-web bug) is caught even though the manifest constant looks right.
    probes = [
        (TITLE, title_end / 2.0, "card"),
        (SCREENCAST, title_end + screencast_dur / 2.0, "content"),
        (WEB, screencast_end + web_dur / 2.0, "content"),
        (OUTRO, outro_start + outro_dur / 2.0, "card"),
    ]
    order_ok = True
    seq = []
    for name, t, expected_kind in probes:
        kind, em = frame_kind(mp4, t)
        seq.append(f"{name}@{t:.1f}s={kind}({em:.1f})" if em is not None else f"{name}=NA")
        if kind != expected_kind:
            order_ok = False
            print(f"    {name} frame at {t:.2f}s is '{kind}' (edge={em}), expected '{expected_kind}'")
    g.check(order_ok, "order-pixels",
            "content-type sequence [card,content,content,card] :: " + " | ".join(seq))
    g.check(all(float(man.get(f"{n}_dur", man.get(n, 1))) > 0
                for n in ("title", "screencast", "web", "outro")
                if f"{n}_dur" in man),
            "durations>0", "all segment durations positive")
    g.check(video_dur is not None and abs(video_dur - expected_video) <= 0.75,
            "duration-reconstruct",
            f"measured={video_dur} sum-of-segments={expected_video:.3f} tol=0.75")
    g.check(video_dur is not None and video_dur < 175.0,
            "publication-budget<175s", f"video={video_dur}")

    # ---- req 1: A/V ----
    print("== req1: audio vs video (no silent+frozen content seam) ==")
    seam = screencast_dur - a_main
    g.check(seam <= 1.0 + 1e-6, "content-seam<=1s",
            f"screencast_dur-a_main = {screencast_dur:.3f}-{a_main:.3f} = {seam:.3f}s")
    if have_vo and audio_dur is not None and video_dur is not None:
        clause_a = abs(audio_dur - video_dur) <= frame_tol
        clause_b_len = audio_dur <= video_dur + 0.35
        # internal (non-trailing) silent gaps > 1s inside CONTENT are forbidden.
        internal_gap = None
        for (s, e) in silence_intervals(mp4, noise_db=-40, min_d=1.0):
            if e is None:
                continue  # trailing tail (web lead-out + silent outro) is exempt
            if e >= (video_dur - 0.25):
                continue  # effectively trailing
            if e <= (vo_delay + 0.25):
                continue  # leading title card is legitimately silent
            if s < (outro_start - 0.05) and (e - s) > 1.0:
                internal_gap = (round(s, 3), round(e, 3), round(e - s, 3))
                break
        g.check(clause_a or (clause_b_len and internal_gap is None), "av-duration",
                f"|audio-video|={abs((audio_dur or 0)-(video_dur or 0)):.3f} "
                f"(tol {frame_tol:.3f}) | audio<=video+0.35={clause_b_len} | "
                f"internal_content_gap={internal_gap}")
    else:
        g.check(True, "av-duration", "no voiceover (have_vo=false) — skipped")

    # ---- req 2: captions ----
    print("== req2: caption windows in-bounds/monotonic/non-overlap/no-outro-bleed ==")
    prev_end = -1.0
    ok_bounds = ok_mono = ok_overlap = ok_bleed = True
    last_end = 0.0
    for idx, w in enumerate(windows):
        s, e = float(w[0]), float(w[1])
        last_end = max(last_end, e)
        if not (0.0 <= s < e <= (video_dur or 1e9) + 1e-6):
            ok_bounds = False
            print(f"    window {idx} out of [0,{video_dur}] : [{s},{e}]")
        if s + 1e-6 < prev_end:
            ok_mono = False
            print(f"    window {idx} start {s} < prev end {prev_end} (non-monotonic)")
        if s + 1e-6 < prev_end:
            ok_overlap = False
        if e > outro_start + 1e-6:
            ok_bleed = False
            print(f"    window {idx} end {e} bleeds past outro_start {outro_start}")
        prev_end = e
    g.check(ok_bounds, "captions-in-bounds", f"count={len(windows)} video={video_dur}")
    g.check(ok_mono and ok_overlap, "captions-monotonic-nonoverlap", "starts sorted, no overlap")
    g.check(ok_bleed, "captions-no-outro-bleed",
            f"last caption end={last_end:.3f} < outro_start={outro_start:.3f}")

    # ---- req 4: web beat non-silent during web window ----
    print("== req4: web-beat narration non-silent during web segment ==")
    if have_vo:
        probe_start = screencast_end + min(0.5, web_dur * 0.3)
        probe_dur = max(0.6, web_dur - 1.0)
        mv = max_volume(mp4, probe_start, probe_dur)
        g.check(mv is not None and mv > -50.0, "web-beat-audible",
                f"max_volume in web window [{probe_start:.3f}+{probe_dur:.3f}s] = {mv} dB (>-50)")
    else:
        g.check(True, "web-beat-audible", "no voiceover — skipped")

    print()
    if g.failures:
        print("::error::verify_video_sync FAILED:")
        for f in g.failures:
            print(f"::error::  - {f}")
        return 1
    print("verify_video_sync: ALL SYNC GATES PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
