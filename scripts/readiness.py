#!/usr/bin/env python3
"""Submission readiness gate. Python 3.9+, standard library only, no install step.

WHAT THIS IS
    One file you copy into a new entry, edit the CONFIG block at the top, and
    run. It scores weighted judging criteria out of 100, fetches the live judge
    URL and refuses to pass while that URL is not serving, prints user-gated
    items separately, writes readiness.json, and sets an exit code CI can gate on.

THE TWO RULES IT EXISTS TO ENFORCE
    1. A readiness gate that never opens a socket is not evidence. The gate this
       replaces was 10,092 lines of regex over source files with zero fetch
       calls, and it stayed green through a two-day total outage of the demo.
       Here the live probe is a VETO: it is not one weighted line item that a
       high score can outvote. If the judge URL is not serving, the verdict is
       FAIL at any score.
    2. user-gated is a third status, never a pass. A user-gated check is
       removed from its criterion's denominator, is never counted as passed,
       and prints in its own block with the manual step still to be done.

EXIT CODES
    0   weighted score >= THRESHOLD and every live check served as expected
    1   gate failed: score below THRESHOLD, or a live check did not serve
    2   nothing was verified: --offline was passed, or the config is still
        carrying the placeholder judge URL, or the weights do not sum to 100
        within half a point, or readiness.py is not one directory below the root.
        readiness.json records submission_grade false. Never treat 2 as a pass.
    An unreachable URL is exit 1, not exit 2. From a CI runner "unreachable" and
    "down" look identical, and a judge cannot tell them apart either.

    No flag in this script can produce exit 0 without a real 200 from a real
    server. --offline is for local iteration and is structurally unable to go
    green.

USAGE
    python readiness.py                  # full gate, probes live, writes readiness.json
    python readiness.py --offline        # skip the probe, exit 2, not submission grade
    python readiness.py --selftest       # prove the gate has teeth, exit 0 if it does
    python readiness.py --out PATH --quiet

PROVENANCE
    Structure, three-state model, evidence primitives, readiness.json artifact:
        repos/nebius/scripts/readiness.py (579 lines)
    Explicit numeric weights per criterion, 95 percent gate threshold, the
    third "user-gated" status as a first-class value:
        repos/archon-datahub/scripts/readiness.ts (2,110 lines)
    Rejected as the base: repos/cockroachdb/scripts/readiness.ts, 10,092 lines,
    verified zero fetch calls (grep -cE "fetch\\(|axios|http\\.get|https\\.get"
    returns 0). Its live probe lives in a separate scheduled workflow that
    cannot turn a pull request red.
    Changed on the way in, deliberately:
      - nebius made the live probe user_gated, so an outage could not fail it.
        Here it is a veto.
      - nebius accepted 401 and 403 as "live". A judge who gets 403 has no demo.
        Here only the expected status counts, and a body substring must match.
      - nebius scored a criterion with no automatable checks as 100 percent.
        Here it scores 0.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Anchored to this file, never to the working directory, so the gate behaves the
# same from CI, from a subprocess and from a shell in any folder.
REPO_ROOT = Path(__file__).resolve().parents[1]

PASS, FAIL, GATED = "pass", "fail", "user-gated"

PLACEHOLDER_URL = "https://CHANGE-ME.example.com/"


# ==========================================================================
# CONFIG. Edit everything between here and END CONFIG. Nothing below it.
# ==========================================================================

PROJECT = "Archon GCP (Agentic) - All Things Agentic, The Taskmaster"

# Fail the gate below this weighted percentage. Do not lower it to go green.
THRESHOLD = 95.0

# The live surface a judge opens with no account and no install. Both entries
# stay on the placeholder until the Cloud Run service is deployed, and the gate
# is red until then. That is the correct state for an entry with no live demo.
DEMO_URL = "https://archon-70489367760.us-central1.run.app/"

LIVE_CHECKS = [
    {
        "id": "live-judge-demo",
        "desc": "The page a judge opens and presses one button on",
        "url": DEMO_URL,
        "expect_status": 200,
        "expect_text": "It closes the month while nobody is watching.",
    },
    {
        "id": "live-close-runs",
        "desc": "The data plane: a real close, not a cached page",
        "url": DEMO_URL + "api/close/2026-07",
        "expect_status": 200,
        "expect_text": '"outcome"',
    },
]

# Weighted criteria.
#
# PROVENANCE, and it is uneven on purpose. The 40 on autonomous action is the
# organizer's own weighting, given to us as a verified constraint. The other
# three weights are OURS: an even split of the remaining 60, chosen because the
# organizer publishes no per-criterion weights we have confirmed against the
# live page. They are an ESTIMATE and carry that label until someone reads the
# criteria page and replaces them.
CRITERIA = [
    {
        "key": "autonomous-action",
        "title": "Autonomous high-value action, not chat (organizer weight)",
        "weight": 40,
        "checks": [
            ("act-close-is-unattended",
             "The close runs end to end with no human step",
             lambda: file_contains("src/archon/runtime/close.py",
                                   r"Nobody is asked anything at any point")),
            ("act-ten-steps",
             "The chore is a real multi-step sequence, not one call",
             lambda: file_contains("tests/integration/test_close.py",
                                   r'"intake", "post", "allocate", "reconcile", "triage"')),
            ("act-agent-drives-tools",
             "The ADK agent really calls its tools, proven offline in CI",
             lambda: file_contains("tests/integration/test_agents.py",
                                   r"test_the_agent_really_calls_its_tools_and_closes_the_month")),
            ("act-writes-artifacts",
             "The agent produces corrective documents, not only findings",
             lambda: file_contains("src/archon/domain/drafts.py", r"class DraftKind|DRAFT_FOR_FINDING")),
            ("act-unattended-trigger",
             "An object landing in the bucket closes the month with nobody present",
             lambda: file_contains("src/archon/adapters/service.py", r'@app.post\("/events"\)')),
            ("act-reaches-the-owner",
             "The result lands on a surface the owner already opens, unprompted",
             lambda: file_contains("src/archon/runtime/close.py",
                                   r'run\.step\("notify"')),
            ("act-human-gate-is-outbound-only",
             "The one human gate sits on the outbound edge and is asserted",
             lambda: file_contains("tests/integration/test_close.py",
                                   r"def test_no_draft_is_ever_sent")),
        ],
    },
    {
        "key": "deliverables",
        "title": "The submission deliverables exist (ESTIMATE: our weight, not theirs)",
        "weight": 20,
        "checks": [
            ("dlv-repo-licence",
             "Public repository with a licence",
             lambda: file_exists("LICENSE")),
            ("dlv-readme-demo-url",
             "README carries the live demo URL",
             lambda: file_contains("README.md", re.escape(DEMO_URL))),
            ("dlv-description",
             "The entry description is drafted in the repo, 1200 characters or more",
             lambda: file_contains("README.md",
                                   r"## The submission description[\s\S]{1200,}")),
            ("dlv-description-no-em-dash",
             "The description carries no em dash, which is the owner's house style",
             lambda: file_absent("README.md", "\u2014")),
            ("dlv-readme-spinup",
             "README carries spin-up instructions, which the rules require",
             lambda: file_contains("README.md", r"python -m archon\.cli")),
            ("dlv-video-url",
             "Public video URL recorded in the README",
             lambda: file_contains("README.md",
                                   r"https://(www\.)?youtu(\.be|be\.com)/\S+")),
            ("dlv-form-submitted",
             "Entry form flipped to Submitted",
             lambda: gated("Open the Devpost form, confirm it reads Submitted, "
                           "and record the date in the workspace STATE.md")),
        ],
    },
    {
        "key": "sponsor",
        "title": "Google is load-bearing (ESTIMATE: our weight, not theirs)",
        "weight": 20,
        "checks": [
            ("spn-framework-in-code",
             "A Google Agent Framework is imported by shipped code, not only by docs",
             lambda: file_contains("src/archon/adapters/agents.py", r"from google\.adk")),
            ("spn-infra-in-code",
             "A Google Cloud infrastructure service is wired into shipped code",
             lambda: file_contains("src/archon/adapters/store.py", r"from google\.cloud import firestore")),
            ("spn-named-in-readme",
             "The README says in one sentence what breaks without them",
             lambda: file_contains("README.md", r"Remove Google ADK")),
            ("spn-deployable",
             "The service is deployable to Cloud Run from this repository",
             lambda: file_exists("Dockerfile")),
        ],
    },
    {
        "key": "quality",
        "title": "Build, tests and honesty (ESTIMATE: our weight, not theirs)",
        "weight": 20,
        "checks": [
            ("qly-ci-any-branch",
             "CI triggers on push to any branch, not only the trunk",
             lambda: workflow_triggers_on_any_branch(".github/workflows/ci.yml")),
            ("qly-readiness-any-branch",
             "The readiness gate itself triggers on push to any branch",
             lambda: workflow_triggers_on_any_branch(".github/workflows/readiness.yml")),
            ("qly-ci-has-test-job",
             "CI runs a real test job",
             lambda: file_contains(".github/workflows/ci.yml", r"(?m)^  test:")),
            ("qly-gates-proven-to-fail",
             "Every close gate is shown failing, not only passing",
             lambda: file_contains("tests/unit/test_validation.py",
                                   r"def test_g1_fails", r"def test_g2_fails",
                                   r"def test_g3_catches", r"def test_g4_fails",
                                   r"def test_g5_fails")),
            ("qly-no-other-contest",
             "No other competition or private codebase is named in a judge-facing file",
             # `devpost` is deliberately not in this list: the challenge's own
             # URL lives on that host and belongs in the README.
             lambda: file_absent("README.md",
                                 r"kaggle|nebius|xprize|agents league|backblaze|"
                                 r"cockroach|mitos|kerdon")),
            ("qly-no-todo",
             "No TODO or FIXME left in judge-facing docs",
             lambda: file_absent("README.md", r"(TODO|FIXME|TBD)")),
            ("qly-no-selfscore",
             "No self-awarded score presented as a measurement",
             lambda: file_absent("README.md", r"(9\.\d|10/10|score of \d)")),
        ],
    },
]

# ==========================================================================
# END CONFIG. Below here is the engine. Copy it unchanged.
# ==========================================================================


# --------------------------------------------------------------------------
# Evidence primitives. Each returns (status, evidence-string).
# --------------------------------------------------------------------------
def _read(relpath):
    p = REPO_ROOT / relpath
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def file_exists(relpath):
    p = REPO_ROOT / relpath
    if p.is_file():
        return PASS, "%s: present (%d bytes)" % (relpath, p.stat().st_size)
    return FAIL, "%s: missing" % relpath


def file_contains(relpath, *patterns):
    """Pass only if the file exists and every regex matches it."""
    text = _read(relpath)
    if text is None:
        return FAIL, "%s: missing" % relpath
    missing = [p for p in patterns if not re.search(p, text)]
    if missing:
        return FAIL, "%s: no match for %s" % (relpath, ", ".join(missing))
    return PASS, "%s: matched %s" % (relpath, ", ".join(patterns))


def file_absent(relpath, pattern):
    """Pass only if the file exists and the forbidden pattern is not in it."""
    text = _read(relpath)
    if text is None:
        return FAIL, "%s: missing" % relpath
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        return FAIL, "%s: forbidden text present: %r" % (relpath, m.group(0))
    return PASS, "%s: clean of /%s/" % (relpath, pattern)


def json_field(relpath, dotted, expected=None):
    """Pass if a dotted path resolves, and equals expected when one is given."""
    text = _read(relpath)
    if text is None:
        return FAIL, "%s: missing" % relpath
    try:
        node = json.loads(text)
    except ValueError as exc:
        return FAIL, "%s: not valid JSON (%s)" % (relpath, exc)
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return FAIL, "%s: no field %s" % (relpath, dotted)
        node = node[part]
    if expected is not None and node != expected:
        return FAIL, "%s: %s is %r, expected %r" % (relpath, dotted, node, expected)
    return PASS, "%s: %s = %r" % (relpath, dotted, node)


_CMD_CACHE = {}


def command_ok(cmd, timeout=900):
    """Run a command in the repo root. Pass on exit 0. Memoised per command."""
    key = tuple(cmd)
    if key in _CMD_CACHE:
        status, evidence = _CMD_CACHE[key]
        return status, evidence + " (cached)"
    printable = " ".join(cmd)
    try:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True,
                              text=True, timeout=timeout)
    except Exception as exc:
        result = (FAIL, "%s: could not run (%s)" % (printable, type(exc).__name__))
        _CMD_CACHE[key] = result
        return result
    if proc.returncode == 0:
        tail = (proc.stdout or "").strip().splitlines()
        result = (PASS, "%s: exit 0 | %s" % (printable, tail[-1] if tail else ""))
    else:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
        result = (FAIL, "%s: exit %d | %s"
                  % (printable, proc.returncode, tail[-1] if tail else ""))
    _CMD_CACHE[key] = result
    return result


def workflow_triggers_on_any_branch(relpath):
    """Pass if the workflow fires on push to any branch.

    Two of our pull requests sat with zero CI runs because push was pinned to
    main. A `branches:` list under `push:` is that bug.

    Handles the three shapes that are all correct YAML: `on: [push, ...]`,
    `on: push`, and the block form at any indent. Calibrated against real files:
    it passes qwen-memoryagent's ci.yml and this kit's own ci.yml, and fails
    gcp, cockroachdb and archon-datahub, which pin push to a branch list.
    """
    text = _read(relpath)
    if text is None:
        return FAIL, "%s: missing" % relpath
    pinned = ("%s: push is pinned to a branch list, so a feature branch gets "
              "zero CI runs" % relpath)

    lines = [l for l in text.split("\n")
             if l.strip() and not l.lstrip().startswith("#")]
    on_idx = next((i for i, l in enumerate(lines) if re.match(r"^on:", l)), None)
    if on_idx is None:
        return FAIL, "%s: no on: trigger block" % relpath

    inline = lines[on_idx].split(":", 1)[1].strip()
    if inline:
        # on: [push, pull_request]  or  on: push
        if "push" in re.findall(r"[\w-]+", inline):
            return PASS, "%s: `on: %s` fires on any branch" % (relpath, inline)
        return FAIL, "%s: `on: %s` has no push trigger" % (relpath, inline)

    body = []
    for l in lines[on_idx + 1:]:
        if not l.startswith((" ", "\t")):
            break
        body.append(l)

    push_i, indent, rest = None, "", ""
    for i, l in enumerate(body):
        m = re.match(r"^(\s+)push:\s*(.*)$", l)
        if m:
            push_i, indent, rest = i, m.group(1), m.group(2).strip()
            break
    if push_i is None:
        return FAIL, "%s: no push trigger" % relpath
    if rest:
        # push: {branches: [main]}
        return (FAIL, pinned) if "branches" in rest else \
               (PASS, "%s: push fires on any branch" % relpath)
    for l in body[push_i + 1:]:
        if len(l) - len(l.lstrip()) <= len(indent):
            break
        if re.match(r"^\s*branches(-ignore)?:", l):
            return FAIL, pinned
    return PASS, "%s: push fires on any branch" % relpath


def gated(manual_step):
    """A user-gated item. Never a pass, never counted, always printed."""
    return GATED, manual_step


# --------------------------------------------------------------------------
# The live probe. This is the part the anti-pattern gate did not have.
# --------------------------------------------------------------------------
def probe(url, expect_status=200, expect_text=None, attempts=3,
          retry_seconds=10, timeout=20):
    """Fetch a URL. Return (ok, evidence).

    Strict on purpose:
      - only the expected status counts, so 401 and 403 are failures
      - urllib follows redirects, so the final host is compared with the
        requested host and a hop to another host fails
      - a body substring must match, because a 200 from a CDN error page or a
        login wall proves nothing about the demo
    """
    want_host = urllib.parse.urlsplit(url).hostname or ""
    last = "no attempt made"
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(
                url, method="GET",
                headers={"User-Agent": "submission-kit-readiness/1.0",
                         "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status = r.status
                final = r.geturl()
                body = r.read(200_000).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            status, final, body = e.code, url, ""
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            reason = getattr(e, "reason", e)
            last = "unreachable (%s: %s)" % (type(e).__name__, reason)
            if attempt < attempts:
                time.sleep(retry_seconds)
            continue

        got_host = urllib.parse.urlsplit(final).hostname or ""
        if status != expect_status:
            last = "HTTP %d, expected %d" % (status, expect_status)
        elif got_host != want_host:
            last = "redirected off host to %s" % final
        elif expect_text and expect_text not in body:
            last = ("HTTP %d but the body does not contain %r (%d bytes read)"
                    % (status, expect_text, len(body)))
        else:
            return True, ("HTTP %d from %s, body contains %r"
                          % (status, final, expect_text))
        if attempt < attempts:
            time.sleep(retry_seconds)
    return False, "%s after %d attempt(s)" % (last, attempts)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def run_checks(criteria):
    """Run every check and attach status and evidence in place."""
    for crit in criteria:
        crit["results"] = []
        for cid, desc, fn in crit["checks"]:
            try:
                status, evidence = fn()
            except Exception as exc:
                status, evidence = FAIL, "check raised %r" % (exc,)
            if status is True:
                status = PASS
            elif status is False:
                status = FAIL
            if status not in (PASS, FAIL, GATED):
                status, evidence = FAIL, "check returned unknown status %r" % (status,)
            crit["results"].append({"id": cid, "desc": desc,
                                    "status": status, "evidence": evidence})
    return criteria


def criterion_pct(crit):
    """Percentage over the automatable checks only.

    User-gated checks leave the denominator, which is right: the gate cannot
    know them. A criterion where every check is user-gated scores 0, not 100.
    Scoring it 100 would hand out the criterion's whole weight for no evidence,
    which is how a gate quietly stops measuring anything.
    """
    auto = [r for r in crit["results"] if r["status"] != GATED]
    if not auto:
        return 0.0
    return sum(1 for r in auto if r["status"] == PASS) / len(auto) * 100.0


def weighted_total(criteria):
    total_weight = sum(c["weight"] for c in criteria)
    if total_weight <= 0:
        return 0.0
    return sum(c["weight"] * criterion_pct(c) / 100.0
               for c in criteria) / total_weight * 100.0


# --------------------------------------------------------------------------
# Config validation. A copied kit that was never edited must not score.
# --------------------------------------------------------------------------
def config_problems():
    problems = []
    weight_sum = sum(c["weight"] for c in CRITERIA)
    # A tolerance, not equality: six equally weighted criteria are 16.67 each
    # and sum to 100.02, which is a correct rubric, not a config error. The
    # score itself divides by the real total, so any sum is arithmetically fine;
    # this only catches a weight someone forgot to update.
    if abs(weight_sum - 100) > 0.5:
        problems.append("criterion weights sum to %s, which is not 100 within "
                        "tolerance. Fix the weights, do not let the gate "
                        "rescale them silently." % weight_sum)
    if not LIVE_CHECKS:
        problems.append("LIVE_CHECKS is empty. A gate that never opens a socket "
                        "is not evidence.")
    for lc in LIVE_CHECKS:
        if "CHANGE-ME" in lc["url"] or lc["url"].startswith(PLACEHOLDER_URL):
            problems.append("%s still points at the placeholder URL %s"
                            % (lc["id"], lc["url"]))
        if lc.get("expect_text") and "CHANGE-ME" in lc["expect_text"]:
            problems.append("%s still carries the placeholder body string"
                            % lc["id"])
    if not (REPO_ROOT / ".git").exists():
        problems.append(
            "no .git under %s, so REPO_ROOT is wrong and every file check would "
            "report 'missing'. readiness.py must sit exactly one directory "
            "below the repo root, for example <repo>/scripts/readiness.py."
            % REPO_ROOT)
    ids = [cid for c in CRITERIA for cid, _, _ in c["checks"]] + \
          [lc["id"] for lc in LIVE_CHECKS]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append("duplicate check ids: %s" % ", ".join(dupes))
    return problems


# --------------------------------------------------------------------------
# Self-test. Proof the gate has teeth, run in CI, per our own rule that every
# gate ships with a proof that it fails.
# --------------------------------------------------------------------------
def selftest():
    failures = []

    ok, evidence = probe("http://127.0.0.1:1/", attempts=1, retry_seconds=0,
                         timeout=2)
    if ok:
        failures.append("a closed port was reported as live")
    print("  live probe against a dead port -> %s | %s"
          % ("FAIL as expected" if not ok else "PASSED, WRONG", evidence))

    broken = run_checks([{
        "key": "t", "title": "t", "weight": 100,
        "checks": [("t-1", "a check that cannot pass",
                    lambda: (FAIL, "deliberately broken"))],
    }])
    pct = weighted_total(broken)
    if pct >= THRESHOLD:
        failures.append("a failing check still scored %.2f" % pct)
    print("  one failing check at weight 100 -> %.2f percent, below the %.0f gate"
          % (pct, THRESHOLD))

    all_gated = run_checks([{
        "key": "g", "title": "g", "weight": 100,
        "checks": [("g-1", "user gated", lambda: gated("do it by hand"))],
    }])
    gpct = criterion_pct(all_gated[0])
    if gpct != 0.0:
        failures.append("an all-user-gated criterion scored %.2f, expected 0" % gpct)
    print("  criterion with only user-gated checks -> %.2f percent" % gpct)

    raiser = run_checks([{
        "key": "r", "title": "r", "weight": 100,
        "checks": [("r-1", "raises", lambda: 1 / 0)],
    }])
    if raiser[0]["results"][0]["status"] != FAIL:
        failures.append("a check that raised was not counted as a failure")
    print("  a check that raises -> %s" % raiser[0]["results"][0]["status"])

    # The one that matters. A perfect score with a dead demo must still be a
    # non-zero exit, or the live check is a line item and not a veto.
    global CRITERIA, LIVE_CHECKS, config_problems
    saved = (CRITERIA, LIVE_CHECKS, config_problems)
    try:
        # The synthetic config would trip the placeholder and repo-root guards,
        # which exit 2. That would satisfy a "not zero" assert for the wrong
        # reason, so the guards are stood down for this one case and the exit
        # code has to come from the veto itself.
        config_problems = lambda: []
        CRITERIA = [{"key": "s", "title": "s", "weight": 100,
                     "checks": [("s-1", "passes", lambda: (PASS, "ok"))]}]
        LIVE_CHECKS = [{"id": "s-live", "desc": "dead demo",
                        "url": "http://127.0.0.1:1/", "expect_status": 200,
                        "expect_text": "anything",
                        "attempts": 1, "retry_seconds": 0, "timeout": 2}]
        import contextlib
        import io
        import tempfile
        tmp = str(Path(tempfile.gettempdir()) / "readiness-selftest.json")
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--quiet", "--out", tmp])
    finally:
        CRITERIA, LIVE_CHECKS, config_problems = saved
    if code != 1:
        failures.append("100 percent score with a dead demo returned exit %d, "
                        "expected 1. The live check is not acting as a veto."
                        % code)
    print("  100 percent score + dead demo -> exit %d (must be 1, the veto)" % code)

    if failures:
        print("\nSELFTEST FAILED")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nSELFTEST PASSED: the gate fails when it should.")
    return 0


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
BAR = "=" * 74
RULE = "-" * 74
ICON = {PASS: "PASS", FAIL: "FAIL", GATED: "GATE"}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Submission readiness gate.")
    ap.add_argument("--out", default=str(REPO_ROOT / "readiness.json"),
                    help="path for readiness.json")
    ap.add_argument("--offline", action="store_true",
                    help="skip the live probe. Always exits 2, never a pass.")
    ap.add_argument("--quiet", action="store_true",
                    help="print the summary only")
    ap.add_argument("--selftest", action="store_true",
                    help="prove the gate fails when it should, then exit")
    args = ap.parse_args(argv)

    if args.selftest:
        print("Readiness gate self-test")
        return selftest()

    problems = config_problems()
    if problems:
        print(BAR)
        print(" READINESS GATE CONFIG ERROR. Nothing was scored.")
        print(BAR)
        for p in problems:
            print("  - " + p)
        print(BAR)
        print("readiness: NOT VERIFIED (exit 2). Edit the CONFIG block.")
        return 2

    criteria = run_checks(CRITERIA)
    pct = weighted_total(criteria)

    live_results = []
    if args.offline:
        live_attempted = False
        live_verified = False
    else:
        live_attempted = True
        for lc in LIVE_CHECKS:
            ok, evidence = probe(lc["url"], lc.get("expect_status", 200),
                                 lc.get("expect_text"),
                                 attempts=lc.get("attempts", 3),
                                 retry_seconds=lc.get("retry_seconds", 10),
                                 timeout=lc.get("timeout", 20))
            live_results.append({"id": lc["id"], "desc": lc["desc"],
                                 "url": lc["url"],
                                 "status": PASS if ok else FAIL,
                                 "evidence": evidence})
        live_verified = all(r["status"] == PASS for r in live_results)

    score_ok = pct >= THRESHOLD
    if not live_attempted:
        verdict, code = "NOT VERIFIED", 2
    elif score_ok and live_verified:
        verdict, code = "PASS", 0
    else:
        verdict, code = "FAIL", 1

    if not args.quiet:
        print()
        print(BAR)
        print(" READINESS GATE: %s" % PROJECT)
        print(BAR)
        for crit in criteria:
            print("\n[%5.1f%% of weight %d] %s"
                  % (criterion_pct(crit), crit["weight"], crit["title"]))
            for r in crit["results"]:
                print("   %s  %-24s %s" % (ICON[r["status"]], r["id"], r["desc"]))
                print("         -> %s" % r["evidence"])

        print("\n" + RULE)
        print(" LIVE JUDGE SURFACE. This is a veto, not a weighted line item.")
        print(" No score can outvote it. A dead demo is a failed submission.")
        if not live_attempted:
            print("   SKIP  --offline was passed. This run is not submission evidence.")
        for r in live_results:
            print("   %s  %-24s %s" % (ICON[r["status"]], r["id"], r["desc"]))
            print("         -> %s" % r["evidence"])

        gated_items = [(c, r) for c in criteria for r in c["results"]
                       if r["status"] == GATED]
        print("\n" + RULE)
        print(" USER-GATED. Not scored, never a pass. Someone must do these.")
        if not gated_items:
            print("   (none)")
        for crit, r in gated_items:
            print("   GATE  %-24s %s" % (r["id"], r["desc"]))
            print("         -> manual step: %s" % r["evidence"])

        print("\n" + BAR)
        print(" Per criterion: " + " | ".join(
            "%s %.0f%% (w%d)" % (c["key"], criterion_pct(c), c["weight"])
            for c in criteria))
        print(" WEIGHTED COMPLETENESS: %6.2f%%   threshold %.0f%%  -> %s"
              % (pct, THRESHOLD, "ok" if score_ok else "BELOW GATE"))
        print(" LIVE VERIFIED:         %s"
              % ("yes" if live_verified else
                 ("not attempted" if not live_attempted else "NO")))
        print(" VERDICT:               %s (exit %d)" % (verdict, code))
        print(BAR)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": PROJECT,
        "threshold_pct": THRESHOLD,
        "weighted_completeness_pct": round(pct, 2),
        "score_above_threshold": score_ok,
        "live_attempted": live_attempted,
        "live_verified": live_verified,
        # True only for a run that probed the live surface and found it serving.
        # A run that skipped the probe, and a run that found the demo dead, are
        # both ungraded evidence.
        "submission_grade": live_attempted and live_verified,
        "verdict": verdict,
        "exit_code": code,
        "criteria": [
            {"key": c["key"], "title": c["title"], "weight": c["weight"],
             "pct": round(criterion_pct(c), 2), "checks": c["results"]}
            for c in criteria
        ],
        "live": live_results,
        "user_gated": [r for c in criteria for r in c["results"]
                       if r["status"] == GATED],
    }
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("readiness: %.2f%% weighted, live %s -> %s (exit %d). Wrote %s"
          % (pct, "verified" if live_verified else "NOT verified",
             verdict, code, args.out))
    return code


if __name__ == "__main__":
    sys.exit(main())
