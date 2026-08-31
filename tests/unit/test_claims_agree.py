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



    assert steps == 11

    assert f"{steps}. " in README or f"{steps} steps" in README

    assert "Eleven steps" in CAPTIONS or f"{steps} steps" in CAPTIONS





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
    """The WHOLE file, not the two fields anybody thought to name.

    This checked `captionText` and `speechText` and passed for months over a
    file whose first line read "Copy this to <your repo>/video/narration.json,
    then replace every <PLACEHOLDER>". The kit's instructions to whoever filled
    the template in shipped inside the submission, and the guard against
    exactly that was looking at two keys that never contained them.

    So it reads the serialised file. A placeholder anywhere is a placeholder.
    """
    import json

    everything = json.dumps(NARRATION)

    assert "<" not in everything and ">" not in everything, (
        "a placeholder survives somewhere in video/narration.json"
    )
    for banned in ("CHANGE-ME", "TODO", "FILL:", "your repo", "Copy this to"):
        assert banned.lower() not in everything.lower(), (
            f"video/narration.json still carries the kit's {banned!r}"
        )





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



def test_every_command_the_readme_tells_a_judge_to_run_exists():
    """The quickstart shipped broken once, and nothing caught it.

    The README said `python -m archon.cli`. After the package moved under
    `src/` that stopped resolving on a clean clone, so the first thing a judge
    would ever run failed. The readiness gate passed anyway, because it greped
    for the command's text rather than running it.

    This asserts the entry point exists and is the one documented. The gate
    now runs it for real; between them, a broken quickstart cannot ship.
    """
    quickstart = re.findall(r"(?m)^python (\S+)", README)

    assert "run.py" in quickstart, (
        f"the README's python commands are {quickstart}; the working entry "
        f"point is run.py"
    )
    assert (ROOT / "run.py").is_file()
    assert "python -m archon.cli" not in README, (
        "that form needs src/ on the path and fails on a clean clone"
    )


def test_every_landmark_the_video_scrolls_to_still_exists_on_the_page():
    """The capture script names selectors; nothing compiles them.

    `video/capture_production.py` opens a tab and then scrolls to a landmark,
    and both halves are strings. A `data-panel` value that no longer exists, or
    an id that moved, is not a syntax error: it is a Playwright timeout three
    minutes into a recording run, discovered while burning a take rather than
    while editing the page.

    The page became a set of panels in one commit and this file was edited by
    hand in the same one. That is exactly the pair that drifts.
    """
    capture = (ROOT / "video" / "capture_production.py").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    mapping = dict(re.findall(r'"#([a-z-]+)":\s*"([a-z]+)"', capture))
    assert mapping, "PANEL_OF was not found in the capture script"

    for landmark, panel in mapping.items():
        assert f'id="{landmark}"' in page, \
            f"the video scrolls to #{landmark}, which the page no longer has"
        assert f'data-panel="{panel}"' in page, \
            f"the video opens the {panel} tab, which the page no longer has"
        assert f'id="panel-{panel}"' in page, \
            f"the {panel} tab has no panel behind it"

    # And the beat that presses the button has to reach a tab that exists.
    for panel in re.findall(r'\.tab\[data-panel="([a-z]+)"\]', capture):
        assert f'data-panel="{panel}"' in page, \
            f"the capture clicks the {panel} tab, which the page no longer has"


def test_every_tab_on_the_page_has_a_panel_and_every_panel_has_a_tab():
    """A tab with nothing behind it is a dead control, and a panel with no tab
    is content nobody can reach. Both are silent in a browser."""
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    tabs = set(re.findall(r'data-panel="([a-z]+)"', page))
    panels = set(re.findall(r'id="panel-([a-z]+)"', page))

    assert tabs, "the page has no tab rail"
    assert tabs == panels, (
        f"tabs without panels: {sorted(tabs - panels)}; "
        f"panels without tabs: {sorted(panels - tabs)}"
    )


def test_every_panel_a_tile_opens_is_a_panel_that_exists():
    """The tiles are controls: each carries the name of the ledger it opens.
    A typo there is a tile that silently does nothing when pressed."""
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    panels = set(re.findall(r'id="panel-([a-z]+)"', page))
    targets = set(re.findall(r'^\s*\["([a-z]+)",\s*"[A-Z]', script, re.MULTILINE))

    assert targets, "no tile destinations were found in the renderer"
    assert targets <= panels, f"tiles point at panels that do not exist: {sorted(targets - panels)}"


def test_no_judge_facing_surface_names_eventarc():
    """It was named on four README surfaces, in the architecture diagram, in the
    spoken narration and in three docstrings, and there has never been an
    Eventarc trigger in this project.

    `infra/main.tf` creates a `google_storage_notification`, a Pub/Sub topic and
    a push subscription. `gcloud eventarc triggers list` returns zero items.
    Misnaming a Google service to Google's own judges is a cheap way to lose
    the one point that costs nothing to keep.
    """
    for name in ("README.md", "video/narration.json"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "ventarc" not in text, f"{name} still names Eventarc"


def test_no_surface_claims_a_vision_or_photograph_path():
    """`extract_with_gemini` takes a string. There is no image input anywhere in
    this repository, so a photograph of a fax is out of scope. The README used
    to say the Gemini vision path handled exactly that."""
    import archon.adapters.agents as agents

    source = (ROOT / "src" / "archon" / "adapters" / "agents.py").read_text(encoding="utf-8")
    for marker in ("inline_data", "inlineData", "from_uri", "image/"):
        assert marker not in source, f"an image path appeared: {marker}"

    assert "text" in agents.extract_with_gemini.__doc__.lower()

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "vision path" not in readme
    assert "no image path" in readme, "the README should say plainly that there is none"


def test_the_readme_never_claims_a_step_count_that_moved():
    """The count test asserts the right number appears somewhere. It cannot see
    a stale one still sitting two hundred lines away, and 'ten steps' and
    '10-step trail' both survived the move to eleven."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for stale in ("ten steps", "10-step", "nine steps", "9-step"):
        assert stale not in readme.lower(), f"README still says {stale!r}"


def test_the_quickstart_names_a_module_that_can_actually_be_imported():
    """It said `uvicorn archon.service:app` for weeks after the move to
    `archon.adapters.service`. The readiness gate greps command text, so it
    stayed green over a quickstart that raises ModuleNotFoundError."""
    import importlib
    import re

    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for module in re.findall(r"uvicorn\s+([a-z_.]+):app", readme):
        importlib.import_module(module)


def test_the_readme_console_block_is_what_the_command_actually_prints():
    """The block a judge reads before they run anything.

    It was hand-maintained and had drifted: it showed nine numbered steps where
    the close runs eleven, "26 entries posted" where the ledger posts 25, and a
    step 6 that skipped the agent's decision entirely. A reader who ran the
    command got a different program than the one the README described, which is
    the cheapest possible way to lose their trust.

    Asserted step by step rather than as one string, because the run id and the
    elapsed time move every run and a byte comparison would fail for the wrong
    reason. What must match is the shape: the same numbered steps, in the same
    order, with the same figures.
    """
    import subprocess
    import sys

    printed = subprocess.run(
        [sys.executable, "run.py"], cwd=ROOT, capture_output=True, text=True,
    ).stdout

    block = README.split("Over the bundled month, `python run.py`")[1]
    block = block.split("```")[1]

    steps = [ln.strip() for ln in block.splitlines() if ln.startswith(" + ")]
    assert len(steps) == 11, f"the README shows {len(steps)} steps; the close runs 11"

    for step in steps:
        assert step in printed, (
            f"the README claims a step the command does not print: {step!r}"
        )

    # And the figures under them, which are the part a judge checks.
    for figure in ("27 artifacts", "5/5 gates passed, 2 skipped", "2,477.85 at stake"):
        assert figure in printed and figure in block, (
            f"{figure!r} disagrees between the README and the command"
        )


def test_the_readme_counts_the_terraform_it_points_at():
    """The architecture prose states a resource count and the diagram states a
    max instance count. Both were wrong: seventeen against fifteen blocks, and
    three instances against four.

    Neither is a big number, and that is the point. A judge who opens
    `infra/main.tf` because the README told them it holds everything is exactly
    the reader this entry is written for, and the first thing they can check is
    whether the count agrees.
    """
    import re

    terraform = (ROOT / "infra" / "main.tf").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    blocks = len(re.findall(r'^resource "', terraform, re.M))
    max_instances = re.search(r"max_instance_count\s*=\s*(\d+)", terraform)

    assert max_instances, "max_instance_count is no longer written the way this reads it"
    assert f"{blocks} resource blocks" in readme, f"the README does not say {blocks} resource blocks"
    assert f"max {max_instances.group(1)} instances" in readme, (
        f"the diagram does not say max {max_instances.group(1)} instances")


#: The framing gate's J1: who this is for, said the same way everywhere.
PERSONA_MARKS = (
    "back-office software for finance teams",
    "three trucks",
    "kitchen table",
)


def test_the_persona_is_the_same_on_every_surface():
    """The kit's J1 asks for one named person rather than a market, and the
    cost of answering it is that the answer now lives on five surfaces.

    A framing sentence that appears in a README and nowhere else is not a
    framing sentence, it is a paragraph. This asserts the same person is on the
    page a judge opens, in the narration they hear, in the Devpost text and in
    the README, because the previous version of this claim drifted between
    exactly those surfaces and nobody noticed until an audit read them side by
    side.
    """
    surfaces = {
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        "web/index.html": (ROOT / "web" / "index.html").read_text(encoding="utf-8"),
        "video/narration.json": (ROOT / "video" / "narration.json").read_text(encoding="utf-8"),
    }

    missing = {name: [m for m in PERSONA_MARKS if m not in text]
               for name, text in surfaces.items()}
    missing = {name: gaps for name, gaps in missing.items() if gaps}

    assert not missing, f"the persona is not on every surface: {missing}"


def test_the_persona_does_not_invent_a_customer():
    """The one thing this row must never buy: a fabricated person.

    The kit's own evidence discipline requires a source for any claim about a
    named real person. The answer here is the builder describing themselves and
    the buyer described concretely, which is what the kit's winning example was
    -- "I sell sourdough from my apartment" is a builder, not a testimonial. A
    first name and a town would be a fabrication, so this fails if one appears
    beside the persona.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    hero = readme[readme.index("three trucks") - 400:readme.index("three trucks") + 400]

    for invented in ("named ", "a customer called", "told us", "says the owner"):
        assert invented not in hero.lower(), (
            f"{invented!r} beside the persona reads as a real person we cannot cite")


def test_no_surface_claims_the_agent_chooses_the_workflow():
    """The instruction says "Call the tools in this order" and then lists them.

    The page said Gemini chooses the workflow. It does not: the sequence is
    prescribed and what the agent decides is the disposition of each exception,
    which is the more interesting claim anyway and the one the rest of this
    repository is built to prove.
    """
    instruction = (ROOT / "src" / "archon" / "adapters" / "agents.py").read_text(encoding="utf-8")
    assert "Call the tools in this order" in instruction, (
        "the instruction no longer prescribes an order; this guard is now the wrong shape")

    for name, text in (("README.md", README), ("web/index.html", PAGE),
                       ("narration", SPEECH + CAPTIONS)):
        lowered = text.lower()
        assert "chooses the workflow" not in lowered, f"{name} says the agent chooses the workflow"
        assert "picks the workflow" not in lowered, f"{name} says the agent picks the workflow"
