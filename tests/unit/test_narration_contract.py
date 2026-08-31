"""The video script has to be valid before anyone spends a credit rendering it.

`generate-narration.py` calls a paid text-to-speech API and only then checks the
script against its contract. A field one character too long, an id with a
capital in it, or a beat order that does not match the capture, and the run
fails after the money is gone.

So every rule the generator enforces is asserted here, offline, in the ordinary
test job. None of this needs a key, a network or a cent. It is the cheapest
possible place to find out that the script is wrong.

The rules come from `generate-narration.py` and `capture-production.mjs`. Where
a number appears in both, it is written out here rather than imported, because a
contract that reads its own bound from the code it is checking cannot fail.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = json.loads((ROOT / "video" / "narration.json").read_text(encoding="utf-8"))
SEGMENTS = SPEC["segments"]

#: The beats, in the order the capture holds them. Written out, not imported.
EXPECTED_BEATS = ["hook", "surface", "trigger", "cloud", "live", "sponsor",
                  "found", "letters", "gates", "evidence", "close"]

#: The generator's own bounds: 20 to 800 characters per field, ids matching
#: [a-z][a-z-]{1,24}, each beat 3 to 40 seconds of measured speech, and the
#: whole cut 90 to 174 seconds.
MIN_FIELD, MAX_FIELD = 20, 800
ID_PATTERN = re.compile(r"^[a-z][a-z-]{1,24}$")

#: A rough spoken rate, used only to catch a beat that is obviously too long or
#: too short before a render. The real durations are measured, not estimated,
#: which is why this is deliberately wide.
WORDS_PER_SECOND = 2.6
BEAT_MIN_SECONDS, BEAT_MAX_SECONDS = 3, 40
#: Raised from 174 when the submission rules turned out to ask for about four
#: minutes and to REQUIRE the video demonstrate the backend running on Google
#: Cloud. The old ceiling was an editorial choice made before anyone had read
#: them; four beats were added to meet the requirement rather than to fill time.
CUT_MIN_SECONDS, CUT_MAX_SECONDS = 90, 250


def test_the_beats_are_the_ones_the_capture_holds():
    """If these drift apart the capture aborts, but only after a paid render."""
    assert [segment["id"] for segment in SEGMENTS] == EXPECTED_BEATS


@pytest.mark.parametrize("segment", SEGMENTS, ids=lambda s: s["id"])
def test_every_id_matches_the_generators_pattern(segment):
    assert ID_PATTERN.match(segment["id"]), segment["id"]


@pytest.mark.parametrize("segment", SEGMENTS, ids=lambda s: s["id"])
@pytest.mark.parametrize("field", ["speechText", "captionText"])
def test_every_field_is_within_the_length_the_generator_accepts(segment, field):
    value = segment[field]

    assert isinstance(value, str)
    assert MIN_FIELD <= len(value) <= MAX_FIELD, (
        f"{segment['id']}.{field} is {len(value)} characters, "
        f"outside {MIN_FIELD}-{MAX_FIELD}"
    )


@pytest.mark.parametrize("segment", SEGMENTS, ids=lambda s: s["id"])
def test_no_beat_is_obviously_too_long_or_too_short_to_render(segment):
    words = len(segment["speechText"].split())
    seconds = words / WORDS_PER_SECOND

    assert BEAT_MIN_SECONDS <= seconds <= BEAT_MAX_SECONDS, (
        f"{segment['id']} is about {seconds:.0f}s of speech ({words} words), "
        f"outside the {BEAT_MIN_SECONDS}-{BEAT_MAX_SECONDS}s the generator allows"
    )


def test_the_whole_cut_is_plausibly_inside_the_publication_window():
    """Wide on purpose: the real total is measured, never estimated. This only
    catches a script that could not possibly fit."""
    words = sum(len(segment["speechText"].split()) for segment in SEGMENTS)
    seconds = words / WORDS_PER_SECOND

    assert CUT_MIN_SECONDS * 0.6 <= seconds <= CUT_MAX_SECONDS, (
        f"about {seconds:.0f}s of speech across {len(SEGMENTS)} beats, which cannot "
        f"land inside {CUT_MIN_SECONDS}-{CUT_MAX_SECONDS}s"
    )


@pytest.mark.parametrize("segment", SEGMENTS, ids=lambda s: s["id"])
def test_speech_is_spelled_out_for_a_voice_and_captions_are_not(segment):
    """The two fields exist because they are read by different things.

    A caption carries the exact string a judge checks. Speech carries the form
    a text-to-speech engine pronounces correctly, so an acronym is spaced and a
    period is words. Catching an unspaced acronym in speech here is cheaper
    than hearing it in a rendered take.
    """
    speech = segment["speechText"]

    for acronym in ("ADK", "GCP", "API", "CSV", "PDF", "OIDC"):
        assert acronym not in speech, (
            f"{segment['id']} speaks {acronym!r} unspaced; a voice will run it together"
        )
    assert not re.search(r"\b\d{4}-\d{2}\b", speech), (
        f"{segment['id']} speaks a raw period like 2026-07; write it as words"
    )


def test_the_voice_and_the_captions_do_not_contradict_each_other():
    """Different wording is fine and expected. A different claim is not."""
    for segment in SEGMENTS:
        speech_numbers = set(re.findall(r"\d[\d,]*\.?\d*", segment["speechText"]))
        caption_numbers = set(re.findall(r"\d[\d,]*\.?\d*", segment["captionText"]))
        stray = speech_numbers - caption_numbers

        assert not stray, (
            f"{segment['id']} speaks {stray} which the caption does not show; "
            f"a viewer reading along would see a different figure"
        )


def test_the_provider_and_voice_are_pinned():
    """A voice that changes between takes cannot be re-rendered one beat at a
    time, which is the whole reason the pipeline exists."""
    assert SPEC["provider"] == "elevenlabs"
    assert SPEC["elevenLabs"]["voiceId"]
    assert SPEC["elevenLabs"]["modelId"]
    assert SPEC["elevenLabs"]["outputFormat"]
    # `voice` is the fallback engine's name and stays a plain string. Both are
    # pinned so a re-render of one beat matches the takes either side of it.
    assert isinstance(SPEC["voice"], str) and SPEC["voice"]
    assert 0.5 <= float(SPEC["speakingRate"]) <= 1.5


def test_nothing_in_the_script_is_still_a_placeholder():
    """Serialised, for the same reason as its sibling in test_claims_agree.

    Both guards read two named fields, and the template instructions that
    shipped for months lived in a third key neither of them looked at.
    """
    import json

    everything = json.dumps(SPEC)

    assert not re.search(r"CHANGE-ME|FILL:|TODO|<[a-z]", everything, re.IGNORECASE), (
        "a placeholder survives in video/narration.json"
    )


#: The composer allows the generator's ceiling plus the trim lead it adds.
COMPOSER_LEAD_ALLOWANCE = 4


def test_the_cut_bounds_here_are_the_ones_the_generator_enforces():
    """This file mirrors `video/generate-narration.py`'s bounds so they can be
    checked offline, and a mirror that drifts is worse than no mirror.

    It drifted once, in the cheapest possible way to notice and the most
    expensive way to pay for: the ceiling was raised here and left there, so a
    run synthesised eleven segments through a paid API and then refused them on
    the line after. The generator is the authority; this reads its bound rather
    than restating it.
    """
    source = (ROOT / "video" / "generate-narration.py").read_text(encoding="utf-8")

    match = re.search(r"if not (\d+) <= offset < (\d+):", source)

    assert match, "the generator's cut bound is no longer written the way this reads it"
    low, high = int(match.group(1)), int(match.group(2))
    assert (low, high) == (CUT_MIN_SECONDS, CUT_MAX_SECONDS), (
        f"the generator enforces {low}-{high}s, this file mirrors "
        f"{CUT_MIN_SECONDS}-{CUT_MAX_SECONDS}s"
    )


def test_the_composer_allows_exactly_what_the_generator_produces():
    """The cut length is written in THREE places -- the generator, this file,
    and `video/build-video.py` -- and I moved them one at a time.

    A run synthesised eleven segments through a paid API and was refused by the
    generator. The fix moved that bound, and a later run recorded the whole
    journey, composed it, and was refused by the composer instead. Each failure
    cost a full cycle to learn one number.

    The composer's ceiling is the generator's plus the lead it prepends, so
    this reads both out of their sources rather than restating either.
    """
    composer = (ROOT / "video" / "build-video.py").read_text(encoding="utf-8")

    match = re.search(r"if not (\d+) <= duration < (\d+)", composer)

    assert match, "the composer's duration contract is no longer written the way this reads it"
    low, high = int(match.group(1)), int(match.group(2))
    assert low == CUT_MIN_SECONDS, f"composer floor {low}, generator floor {CUT_MIN_SECONDS}"
    assert high == CUT_MAX_SECONDS + COMPOSER_LEAD_ALLOWANCE, (
        f"composer ceiling {high}, expected {CUT_MAX_SECONDS} + {COMPOSER_LEAD_ALLOWANCE} "
        f"= {CUT_MAX_SECONDS + COMPOSER_LEAD_ALLOWANCE}"
    )
