"""The deploy pipeline, asserted rather than trusted.

A CD workflow is code that runs once in a while, under conditions nobody
reproduces locally, against the one environment that matters. Its failure modes
are quiet: a guard that skips instead of failing, a checkout that takes the tip
of a branch instead of the commit that passed, an action pinned to a tag that
someone else can move.

This repository has already shipped one fail-open gate. `pytest | tee` swallows
the exit code and `grep "[0-9]+ passed"` matches inside "3 failed, 9 passed", so
two jobs reported success over red. These are the assertions that would have
caught the same shape in the deploy path.

Nothing here needs a network, a cloud, or a runner. It reads the YAML.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
DEPLOY = WORKFLOWS / "deploy.yml"


@pytest.fixture(scope="module")
def deploy() -> dict:
    parsed = yaml.safe_load(DEPLOY.read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on:` as the boolean True, which is a real
    # trap: `parsed["on"]` raises KeyError on a perfectly valid workflow.
    parsed["on"] = parsed.pop(True, parsed.get("on"))
    return parsed


@pytest.fixture(scope="module")
def deploy_text() -> str:
    return DEPLOY.read_text(encoding="utf-8")


# ── what must be true before anything is deployed ────────────────────────────

def test_the_deploy_waits_for_ci_rather_than_racing_it(deploy):
    """A `push` trigger starts the deploy and CI at the same moment on the same
    commit, and the deploy usually wins. Then a commit that fails its tests is
    already serving by the time the red X arrives."""
    triggers = deploy["on"]

    assert "push" not in triggers, "a push trigger races CI to production"
    assert triggers["workflow_run"]["workflows"] == ["CI"]
    assert triggers["workflow_run"]["branches"] == ["main"]


def test_a_failed_ci_run_cannot_reach_production(deploy):
    condition = " ".join(deploy["jobs"]["deploy"]["if"].split())

    assert "github.event.workflow_run.conclusion == 'success'" in condition, \
        "the deploy job does not check that CI actually passed"


def test_it_deploys_the_commit_ci_passed_and_not_the_tip_of_main(deploy):
    """By the time this starts, main may carry a newer commit whose CI has not
    finished. Checking out the branch would deploy an untested commit through a
    gate built to prevent exactly that.

    The whole chain is walked, not just the checkout: the ref is a step output,
    and a step output that stopped reading `head_sha` would leave the checkout
    looking perfectly correct while deploying the wrong commit.
    """
    steps = deploy["jobs"]["deploy"]["steps"]

    checkout = next(s for s in steps
                    if str(s.get("uses", "")).startswith("actions/checkout"))
    ref = checkout["with"]["ref"]
    assert ref, "the checkout takes the default branch"

    # `${{ steps.target.outputs.sha }}` -> the step whose id is `target`.
    referenced = re.search(r"steps\.([A-Za-z0-9_-]+)\.outputs", ref)
    assert referenced, f"the checkout ref is not resolved from a step: {ref!r}"

    producer = next(s for s in steps if s.get("id") == referenced.group(1))
    assert "workflow_run.head_sha" in producer["run"], \
        "the deployed SHA is not the one CI passed"

    # And the image is tagged from that same commit, so what is running can be
    # traced back to a commit rather than to whenever the build happened.
    apply_step = next(s for s in steps if "terraform" in str(s.get("run", "")))
    assert f"steps.{referenced.group(1)}.outputs.short" in apply_step["run"]


def test_missing_configuration_fails_instead_of_skipping(deploy_text):
    """The fail-open shape, in its natural habitat. A deploy job that quietly
    does nothing reports a green tick over a service nobody updated."""
    guard = deploy_text.split("Refuse to run unconfigured")[1].split("- uses:")[0]

    assert "exit 1" in guard, "the guard does not fail when unconfigured"
    assert "GCP_WORKLOAD_IDENTITY_PROVIDER" in guard
    assert "GCP_DEPLOY_SERVICE_ACCOUNT" in guard


# ── credentials ──────────────────────────────────────────────────────────────

def test_no_service_account_key_is_used_anywhere(deploy_text):
    """The obvious way to let CI reach GCP is a downloaded JSON key pasted into
    a repository secret. It never expires and nobody schedules its rotation."""
    assert "credentials_json" not in deploy_text
    assert "workload_identity_provider" in deploy_text
    assert "service_account_key" not in deploy_text


def test_the_token_permissions_are_the_two_it_needs(deploy):
    assert deploy["permissions"] == {"contents": "read", "id-token": "write"}


def test_the_federation_is_restricted_to_this_repository():
    """Without the attribute condition the provider trusts every GitHub Actions
    token in existence, so any repository could impersonate the deploy account.
    This is the single most important line in the setup script."""
    setup = (ROOT / "scripts" / "setup-cd.sh").read_text(encoding="utf-8")

    assert "--attribute-condition=" in setup
    assert "assertion.repository == '${REPO}'" in setup
    assert "attribute.repository/${REPO}" in setup, \
        "the workloadIdentityUser binding is not scoped to the repository"


# ── the deploy itself ────────────────────────────────────────────────────────

def test_two_deploys_cannot_run_at_once_and_neither_is_cancelled(deploy):
    """A terraform apply killed halfway leaves state describing a world that
    does not exist."""
    assert deploy["concurrency"]["cancel-in-progress"] is False
    assert deploy["concurrency"]["group"]


def test_terraform_applies_rather_than_a_step_creating_resources(deploy_text):
    """A resource created by a pipeline step is a resource nobody can review,
    reproduce or remove."""
    assert "terraform -chdir=infra apply" in deploy_text
    assert "gcloud run deploy" not in deploy_text, \
        "this bypasses terraform and puts the state out of step with the world"


def test_terraform_state_is_remote_because_a_pipeline_cannot_read_a_laptop():
    main_tf = (ROOT / "infra" / "main.tf").read_text(encoding="utf-8")

    assert 'backend "gcs"' in main_tf, "state is local, so nothing but this machine can apply"


def test_the_deploy_opens_the_page_before_calling_itself_done(deploy_text):
    """A deploy that reports success without opening the page has proved
    nothing. `--fail` matters: a 404 body still greps false on its own."""
    verify = deploy_text.split("The judge's route actually serves")[1]

    assert "curl --fail" in verify
    assert "closes the month while nobody is watching" in verify
    assert "api/close/2026-07" in verify
    assert "python scripts/readiness.py" in deploy_text


def test_the_verification_asserts_the_console_and_not_just_the_old_page(deploy_text):
    """The page became eight sections and a switcher. A check that only greps
    the headline would pass against the page that headline used to be on."""
    verify = deploy_text.split("The judge's route actually serves")[1]

    assert "data-panel=" in verify
    assert 'id="period"' in verify


# ── the whole workflow directory ─────────────────────────────────────────────

@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_rather_than_a_tag(workflow):
    """A tag is a movable pointer. `@v3` today and `@v3` next week can be
    different code, and the second one runs with this project's deploy rights."""
    unpinned = [
        line.strip() for line in workflow.read_text(encoding="utf-8").splitlines()
        if re.search(r"^\s*(-\s+)?uses:\s*[^./]", line)
        and not re.search(r"@[0-9a-f]{40}\s*$", line.split("#")[0].strip())
    ]

    assert unpinned == [], f"{workflow.name} has unpinned actions: {unpinned}"
