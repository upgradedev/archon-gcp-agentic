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
    assert "terraform -chdir=infra ${VERB}" in deploy_text
    assert 'VERB="apply -auto-approve"' in deploy_text
    assert 'VERB="plan"' in deploy_text, "there is no way to read a diff before taking it"
    assert "gcloud run deploy" not in deploy_text, \
        "this bypasses terraform and puts the state out of step with the world"


def test_terraform_state_is_remote_because_a_pipeline_cannot_read_a_laptop():
    main_tf = (ROOT / "infra" / "main.tf").read_text(encoding="utf-8")

    assert 'backend "gcs"' in main_tf, "state is local, so nothing but this machine can apply"


def test_the_backend_names_no_bucket_and_every_caller_brings_its_own():
    """The near miss this file exists to stop recurring.

    The backend was first written with the production bucket hardcoded. That
    makes every caller of `infra/` share one state object, and one of those
    callers is `infra/cloudbuild.yaml`, whose entire purpose is
    `destroy -> apply -> verify -> destroy` against a throwaway project. Its
    guard refuses to run against the production *project*; it says nothing
    about a state bucket. A lifecycle run would have loaded production state,
    applied it under a different project id, and written an empty state over
    it on the final destroy, leaving the judge-facing infrastructure
    unmanaged while the URL a judge opens stayed up and looked perfectly fine.

    Asserting that `backend "gcs"` is present cannot see any of that.
    """
    main_tf = (ROOT / "infra" / "main.tf").read_text(encoding="utf-8")
    block = main_tf.split('backend "gcs"')[1].split("}")[0]

    assert "bucket" not in block, \
        "the backend names a bucket, so every caller shares one state object"
    assert "prefix" not in block

    # Which is only safe because each caller supplies its own at init time.
    callers = {
        "scripts/deploy.sh": "${PROJECT_ID}-tfstate",
        ".github/workflows/deploy.yml": "${PROJECT_ID}-tfstate",
        "infra/cloudbuild.yaml": "${_TF_PROJECT}-tfstate",
    }
    for path, bucket in callers.items():
        # Continuations are joined first, so a multi-line init reads as the
        # one command it actually is.
        raw = (ROOT / path).read_text(encoding="utf-8")
        text = raw.replace("\\\n", " ")
        inits = [line.strip() for line in text.splitlines()
                 if "terraform" in line and " init" in line
                 and not line.strip().startswith("#")]

        assert inits, f"{path} never inits terraform"
        for init in inits:
            assert "-backend-config" in init, f"{path} inits with no backend"
            assert bucket in init, f"{path} does not point at its own bucket"


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


@pytest.mark.parametrize("path", [
    ".github/workflows/deploy.yml", "infra/cloudbuild.yaml", "scripts/deploy.sh",
], ids=lambda p: p.rsplit("/", 1)[-1])
def test_no_shell_line_carries_a_two_character_backslash_n(path):
    """A line continuation that got written as the two characters backslash-n.

    This shipped once, in this file's own commit: an edit meant to break a
    terraform init across three lines emitted `-input=false \n  -backend-config`
    on one line instead. YAML parses, the workflow lints, and the assertion that
    `-backend-config` appears in the init passes, because it does appear. The
    command bash actually runs is a different command.

    Nothing else here can see it, because every other check works on the parsed
    structure and this is a defect in the text.
    """
    text = (ROOT / path).read_text(encoding="utf-8")
    offenders = [line.strip() for line in text.splitlines() if "\n" in line]

    assert offenders == [], f"{path} has a literal backslash-n: {offenders}"


def test_no_terraform_block_sets_the_same_argument_twice():
    """The defect this closes made the whole deployment pipeline unrunnable for
    three days, and nothing said so.

    `f868b19` raised Cloud Run's request ceiling to 600s because an agent close
    on a thinking model runs for minutes and a request cut off at 120s comes
    back as a Pub/Sub redelivery. It added `timeout = "600s"` to the template
    block and left the existing `timeout = "120s"` sitting three lines below
    it. Two arguments of the same name in one block is an HCL parse error, so
    from that commit terraform could not `init`, `plan` or `validate`, let
    alone apply, and the commit message went on claiming "the request timeout
    is now declared at 600s in terraform".

    Nothing caught it because nothing here runs terraform: it is not installed
    on the author's machine, the CD workflow dies eight seconds earlier on
    unset repository variables, and `infra/cloudbuild.yaml` refuses the
    production project by design. The file was the one artifact in this
    repository with no gate in front of it.

    A brace-depth scan is enough for this class and needs no HCL parser, which
    is the point: it runs in the offline suite on every push, where terraform
    never will.
    """
    import re

    for path in sorted((ROOT / "infra").glob("*.tf")):
        seen: list[dict[str, int]] = [{}]
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.split("#", 1)[0].split("//", 1)[0].strip()
            if not line:
                continue

            argument = re.match(r'^([A-Za-z_][\w-]*)\s*=\s*[^=]', line)
            if argument and seen:
                name = argument.group(1)
                first = seen[-1].get(name)
                assert first is None, (
                    f"{path.name} sets `{name}` twice in the same block, at lines "
                    f"{first} and {number}. Terraform refuses to parse the file, so "
                    f"every plan and apply fails before it reads a single resource."
                )
                seen[-1][name] = number

            # Depth last, so an argument is credited to the block it sits in.
            for _ in range(line.count("{") - line.count("}")):
                seen.append({})
            for _ in range(line.count("}") - line.count("{")):
                if len(seen) > 1:
                    seen.pop()


def test_cloud_run_keeps_the_ceiling_an_agent_close_needs():
    """The other half of the same defect. Fixing the duplicate meant deleting
    one of two lines, and deleting the wrong one is a green deploy that
    reintroduces the outage: an agent close runs past 120s, Cloud Run cuts the
    request, Pub/Sub reads the 504 as failure and redelivers, and the instance
    wedges. The live service already carries 600.
    """
    template = (ROOT / "infra" / "main.tf").read_text(encoding="utf-8")
    template = template.split('resource "google_cloud_run_v2_service"')[1]

    assert 'timeout         = "600s"' in template or 'timeout = "600s"' in template, (
        "the Cloud Run request timeout is no longer 600s; an agent close needs "
        "minutes and a shorter ceiling turns one into a redelivery loop"
    )
    assert '"120s"' not in template.split("scaling")[0]


def test_both_deploy_paths_pass_terraform_the_same_variables():
    """`variable "release"` defaults to "", so omitting it does not fail an
    apply — it silently sets ARCHON_RELEASE to nothing, and /api/health stops
    naming the build it is running. The workflow passed it; `scripts/deploy.sh`
    never did. Whoever reached for the script to reconcile drift would have
    blanked the stamp the video pipeline checks before it records.

    A default that turns an omission into a wrong value rather than an error is
    the whole reason this needs a test: nothing else can notice.
    """
    import re

    script = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")

    def passed(text):
        return {m.group(1) for m in re.finditer(r'-var "(\w+)=', text)}

    from_script = passed(script)
    from_workflow = passed(workflow)

    assert "release" in from_script, (
        "scripts/deploy.sh does not pass -var release, so an apply through it "
        "blanks ARCHON_RELEASE on the live service"
    )
    assert from_workflow <= from_script, (
        "the workflow passes variables the script does not, so the two paths "
        f"deploy differently: {sorted(from_workflow - from_script)}"
    )


def test_the_release_stamp_and_the_image_tag_cannot_disagree():
    """They are the same commit or the page lies about what it is running.
    Derived from one shell variable so they cannot drift apart."""
    script = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert 'RELEASE="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"' in script
    assert 'IMAGE="gcr.io/${PROJECT_ID}/${SERVICE}:${RELEASE}"' in script
    assert 'rev-parse --short HEAD)"\n' not in script.split("IMAGE=")[1][:120], (
        "the image tag computes its own SHA again instead of reusing RELEASE"
    )


def test_google_adk_is_a_production_dependency_and_not_an_optional_one():
    """The sponsor's product must be installed where the claim is made.

    The ADK tests guard themselves with `pytest.importorskip("google.adk")`,
    which is right for a contributor who has not installed an optional extra
    and was, for a while, the ONLY thing standing between this entry and a
    false claim. Measured by a reviewer: with `google.adk` made unimportable
    the whole suite still passed, zero failures, exit 0. Eighteen tests moved
    from the passed column to the skipped one and nothing went red.

    Two things now stop that. CI imports `google.adk` explicitly and fails the
    build if the ADK tests skipped, the same shape as the Firestore emulator
    job. And this asserts the dependency is DECLARED for production rather
    than for tests, so an image that ships without it cannot be built.
    """
    import tomllib

    declared = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["dependencies"]
    pinned = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert any(d.replace("_", "-").startswith("google-adk") for d in declared), (
        "google-adk is not in the runtime dependencies, so the agent path this "
        "entry is judged on would be absent from a built image"
    )
    assert "google-adk" in pinned.replace("_", "-")

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "an ADK test skipped; the sponsor claim proved nothing" in workflow, (
        "CI no longer fails when the ADK tests skip, which is the only thing "
        "that turns a missing framework from quiet into red"
    )
