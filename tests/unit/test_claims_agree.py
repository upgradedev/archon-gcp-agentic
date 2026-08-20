"""The cheapest points on the board: judge-facing claims must agree.



A figure that appears on more than one surface is a figure that will one day

disagree with itself. The README says one test count, the video narration says

another, and a judge who checks finds the entry contradicting itself before

they have looked at any code.



This is not hypothetical. The README shipped saying 178 tests while the suite

was 205 and the narration already said 205, because a search-and-replace missed

one line whose capitalisation differed. Nothing caught it: every test passed,

lint was clean, CI was green, and the number was still wrong on the surface a

judge reads first.



So the surfaces check each other here, in the test job, where the real values

are available. The readiness gate cannot do this: it installs nothing on

purpose, so it can never know how many tests there are.

"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

README = (ROOT / "README.md").read_text(encoding="utf-8")

NARRATION = json.loads((ROOT / "video" / "narration.json").read_text(encoding="utf-8"))

#: The page is three files now, and all three are judge-facing: a stranger can

#: open any of them. House style and the no-other-competition rule apply to the

#: whole surface, not just the markup.

PAGE = "\n".join(

    (ROOT / "web" / name).read_text(encoding="utf-8")

    for name in ("index.html", "app.js", "styles.css")

)



SPEECH = " ".join(s["speechText"] for s in NARRATION["segments"])

CAPTIONS = " ".join(s["captionText"] for s in NARRATION["segments"])





def _readme_test_count() -> int:

    """What the README claims the suite is.



    Deliberately the SIZE of the suite rather than how many passed. "N passing"

    depends on which optional dependencies happen to be installed, so it was

    true in CI and false locally for no visible reason. The count of tests is

    the same everywhere.

    """

    match = re.search(r"\|\s*Tests, all offline\s*\|\s*([\d,]+)\s*\|", README)

    assert match, "the README evidence table no longer states a test count"

    return int(match.group(1).replace(",", ""))





@lru_cache(maxsize=1)

def _suite_size() -> int:

    """How many tests the whole suite has, however this run was invoked.



    Deliberately not `request.session.testscollected`: that counts only what

    the current invocation selected, so running one file would compare the

    README against nine tests and fail for the wrong reason. Collection is

    cheap and it is the only number that means what the README claims.

    """

    proc = subprocess.run(

        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests"],

        cwd=ROOT, capture_output=True, text=True,

    )

    # `-q` is already in addopts, so collection prints per-file counts and no

    # grand total. Sum the per-file lines, and fall back to the total that a

    # single `-q` would have printed, so this keeps working if addopts changes.

    per_file = re.findall(r"(?m)^tests[/\\]\S+\.py: (\d+)$", proc.stdout)

    if per_file:

        return sum(int(n) for n in per_file)



    match = re.search(r"(\d+) tests? collected", proc.stdout)

    assert match, "could not read a collected count from:\n" + proc.stdout[-500:]

    return int(match.group(1))





def test_the_readme_test_count_is_the_real_one():

    """The claim that drifted once already, now pinned to the real total."""

    stated, actual = _readme_test_count(), _suite_size()



    assert stated == actual, (

        f"README claims {stated} tests, the suite has {actual}. "

        f"Update the evidence table in README.md."

    )





def test_the_narration_carries_no_test_count_at_all():

    """This test used to demand the opposite, and that was the mistake.



    Requiring the count on both surfaces meant every added test changed the

    video script. Rendered audio would then need re-cutting to within one

    frame, which is what the sync gate holds it to. The figure moved eight

    times in the session that wrote this file.



    So the count lives in the README, where it carries the command that

    produced it and costs nothing to correct, and the voice never says it.

    """

    assert "offline tests" not in SPEECH + CAPTIONS

    assert not re.search(r"\b\d{2,}\s+(offline\s+)?tests?\b", CAPTIONS)





def test_the_close_length_agrees_across_every_surface():

    """Ten steps. It has already been nine, and three surfaces had to change."""

    from archon.adapters.store import LocalStore
    from archon.runtime.close import run_close
    from archon.runtime.mailbox import read_period



    documents, _ = read_period("2026-07")

    steps = len(run_close(period="2026-07", documents=documents,

                          store=LocalStore()).journal.steps)



    assert steps == 10

    assert f"{steps}. " in README or f"{steps} steps" in README

    assert "Ten steps" in CAPTIONS or f"{steps} steps" in CAPTIONS





def test_the_money_figures_agree_between_the_product_and_the_readme():

    """Every headline figure a judge could check against a live run."""

    from archon.adapters.store import LocalStore
    from archon.runtime.close import run_close
    from archon.runtime.mailbox import read_period



    documents, _ = read_period("2026-07")

    result = run_close(period="2026-07", documents=documents,

                       company="Bell Ridge Haulage", store=LocalStore())

    statements = result.statements



    for figure in (f"{statements.revenue:,.2f}",

                   f"{statements.operating_expenses:,.2f}",

                   f"{statements.net_profit:,.2f}",

                   f"{result.recoverable:,.2f}"):

        assert figure in README, f"the README does not carry {figure}"





def test_the_segment_is_named_identically_wherever_it_appears():

    """Name a segment, never a market. And name the same one every time."""

    segment = "owner-operator trucking firms"



    assert segment in README

    assert "owner-operator trucking firm" in CAPTIONS

    assert "trucking firm" in PAGE or "haulier" in PAGE





def test_no_judge_facing_surface_names_another_competition():

    forbidden = re.compile(r"cockroach|backblaze|qwen|nebius|xprize|kaggle|mitos|kerdon",

                           re.IGNORECASE)



    for name, text in (("README.md", README), ("narration", SPEECH + CAPTIONS),

                       ("web/index.html", PAGE)):

        match = forbidden.search(text)

        assert match is None, f"{name} names {match.group(0)!r}"





def test_no_judge_facing_surface_reads_as_generated():

    """The house style, asserted rather than remembered."""

    banned = re.compile(r"—|leverage|robust|seamless|comprehensive|in today's world",

                        re.IGNORECASE)



    for name, text in (("README.md", README), ("narration", SPEECH + CAPTIONS),

                       ("web/index.html", PAGE)):

        match = banned.search(text)

        assert match is None, f"{name} contains {match.group(0)!r}"





def test_the_readme_never_claims_compliance():

    """Cite a control ID or write nothing. Never the word itself."""

    assert not re.search(r"compliant|conformity", README, re.IGNORECASE)





def test_the_video_script_carries_no_placeholder():

    for segment in NARRATION["segments"]:

        both = segment["captionText"] + segment["speechText"]

        assert "<" not in both and ">" not in both, f"{segment['id']} still has a placeholder"





# ── G4 and F7, over the whole repository rather than four files ──────────────



#: Other entries by this author. A judge cloning this repository must not be

#: reading the names of our other competition submissions, and a build artifact

#: named after a different project is not "every file relevant and current".

OTHER_ENTRIES = re.compile(

    r"datahub|cockroach|backblaze|qwen|nebius|xprize|kaggle|cinemory|claimscene"

    r"|mitos|kerdon",

    re.IGNORECASE,

)



#: These two files CONTAIN the forbidden list, because they are what searches

#: for it. Excluding them is not a hole: a name can only hide here by being

#: added to a pattern, which is a visible change to a gate.

CHECKERS = {"readiness.py", "test_claims_agree.py", "test_narration_contract.py"}



SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules",

             "htmlcov", ".venv"}





def _repo_files():

    """Every file a judge would receive, and only those.



    `git ls-files` rather than walking the tree, because the tree also holds

    generated output. `readiness.json` is the gate's own artifact and echoes

    its forbidden-word list back as evidence, so a filesystem walk failed on

    the output of the very check it was running. It is gitignored, a judge

    never sees it, and it is not part of the submission.

    """

    tracked = subprocess.run(

        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True

    ).stdout.splitlines()



    for name in tracked:

        if not name:

            continue

        path = ROOT / name

        if not path.is_file() or path.name in CHECKERS:

            continue

        try:

            yield path, path.read_text(encoding="utf-8")

        except (UnicodeDecodeError, OSError):

            continue





def test_no_file_in_the_repository_names_another_entry():

    """The check that missed the video pipeline.



    It used to read the README, the three page files and the narration. The

    video pipeline was copied in from the submission kit carrying another

    entry's name in six files, including the name of the .mp4 it produced, and

    nothing looked. A judge clones the whole repository, so the gate reads the

    whole repository.

    """

    offenders = []

    for path, text in _repo_files():

        for match in OTHER_ENTRIES.finditer(text):

            line = text[: match.start()].count("\n") + 1

            offenders.append(f"{path.relative_to(ROOT)}:{line} {match.group(0)!r}")



    assert offenders == [], "another entry is named in:\n  " + "\n  ".join(offenders)





def test_no_file_in_the_repository_carries_a_todo():

    offenders = []

    for path, text in _repo_files():

        if path.suffix in (".md", ".py", ".js", ".mjs", ".html", ".css", ".yml", ".tf"):

            for match in re.finditer(r"\b(TODO|FIXME|TBD|XXX)\b", text):

                line = text[: match.start()].count("\n") + 1

                offenders.append(f"{path.relative_to(ROOT)}:{line} {match.group(0)}")



    assert offenders == [], "unfinished markers in:\n  " + "\n  ".join(offenders)





def test_the_narration_voices_no_number_that_moves():

    """The narration may only voice numbers that are fixed by the corpus.



    The first version of this banned the words "hundred" and "thousand"

    outright, which was too blunt: it caught the money figures, and those come

    from committed synthetic data that is as stable as the repository itself.

    The distinction that matters is whether a figure moves when the code

    changes. A test count does, and it moved eight times in one session. The

    short pay does not.

    """

    from archon.adapters.store import LocalStore
    from archon.runtime.close import run_close
    from archon.runtime.mailbox import read_period



    documents, _ = read_period("2026-07")

    result = run_close(period="2026-07", documents=documents,

                       company="Bell Ridge Haulage", store=LocalStore())

    real_amounts = {f.amount for f in result.findings}



    # Every figure the voice actually says, and what it has to be worth.

    spoken_amounts = {

        "two hundred dollars light": 200.00,

        "four hundred and twelve dollars": 412.85,

        "one thousand eight hundred and sixty five dollars": 1865.00,

    }

    for phrase, amount in spoken_amounts.items():

        if phrase in SPEECH.lower():

            assert amount in real_amounts, (

                f"the voice says {phrase!r}, but no finding is worth {amount:,.2f}. "

                f"Findings are worth: {sorted(real_amounts)}"

            )



    # And no count of anything the suite decides.

    assert "offline tests" not in SPEECH

