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
EXPECTED_BEATS = ["title", "problem", "value",
                  "surface", "trigger", "cloud", "live", "sponsor",
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


def test_every_copy_of_the_cut_ceiling_agrees():
    """One number, written in four places, and I moved them one at a time.

    The generator refuses to synthesise outside it. `build-video.py` refuses
    to compose outside it. This file mirrors it so the rule can be checked
    offline. And the workflow jq refuses to upload outside it.

    Each was found by a separate full cycle, in that order: the first paid
    ElevenLabs for eleven segments and was refused by the generator; the
    second recorded the whole journey and was refused by the composer; the
    third composed a finished file and was refused at the upload gate.
    Nothing failed that was not a duplicated constant.

    So this reads all four out of their own sources rather than restating
    any. The composer and the workflow allow the generator ceiling plus the
    trim lead the composer prepends.
    """
    generator = (ROOT / "video" / "generate-narration.py").read_text(encoding="utf-8")
    composer = (ROOT / "video" / "build-video.py").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows"
                / "submission-video.yml").read_text(encoding="utf-8")

    gen = re.search(r"if not (\d+) <= offset < (\d+):", generator)
    comp = re.search(r"if not (\d+) <= duration < (\d+)", composer)
    # DOTALL: the two jq clauses sit on separate lines and the gap is indent.
    flow = re.search(r"\.durationSeconds >= (\d+).*?\.durationSeconds < (\d+)",
                     workflow, re.S)

    assert gen, "the generator bound is not written the way this reads it"
    assert comp, "the composer bound is not written the way this reads it"
    assert flow, "the workflow bound is not written the way this reads it"

    ceiling = CUT_MAX_SECONDS + COMPOSER_LEAD_ALLOWANCE
    found = {
        "generator": (int(gen.group(1)), int(gen.group(2))),
        "composer": (int(comp.group(1)), int(comp.group(2))),
        "workflow": (int(flow.group(1)), int(flow.group(2))),
        "this file": (CUT_MIN_SECONDS, CUT_MAX_SECONDS),
    }
    expected = {
        "generator": (CUT_MIN_SECONDS, CUT_MAX_SECONDS),
        "composer": (CUT_MIN_SECONDS, ceiling),
        "workflow": (CUT_MIN_SECONDS, ceiling),
        "this file": (CUT_MIN_SECONDS, CUT_MAX_SECONDS),
    }

    drifted = {k: (found[k], expected[k]) for k in expected if found[k] != expected[k]}

    assert not drifted, f"the cut bounds have drifted apart: {drifted}"
