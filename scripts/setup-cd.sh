#!/usr/bin/env bash
# Let GitHub Actions deploy this project, without a key existing anywhere.
#
#   ./scripts/setup-cd.sh
#
# Run this ONCE, as someone who owns the project. It is idempotent: running it
# again re-asserts the same state and changes nothing.
#
# ── why federation rather than a service account key ────────────────────────
#
# The obvious way to let CI reach GCP is to download a service account JSON key
# and paste it into a repository secret. That key is a permanent credential
# sitting in two places, it does not expire, and rotating it is a task nobody
# schedules. Workload identity federation removes it entirely: GitHub mints a
# short-lived OIDC token for a specific workflow run, Google trades it for an
# access token that lasts an hour, and there is no long-lived secret to leak.
#
# ── the line that actually matters ──────────────────────────────────────────
#
# The attribute condition below. Without it, the provider trusts every GitHub
# Actions token in existence, so ANY repository on GitHub could impersonate the
# deploy account and push a container into this project. With it, only this
# repository can. This is the single most important line in the file.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-upgradegr-archon-agentic}"
REPO="${REPO:-upgradedev/archon-gcp-agentic}"
POOL="${POOL:-github}"
PROVIDER="${PROVIDER:-github-oidc}"
DEPLOYER="${DEPLOYER:-archon-deploy}"
BRANCH="${BRANCH:-main}"
SA="${DEPLOYER}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "==> project ${PROJECT_ID}, repository ${REPO}"
gcloud config set project "${PROJECT_ID}" >/dev/null

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"

gcloud services enable iamcredentials.googleapis.com sts.googleapis.com \
  cloudresourcemanager.googleapis.com --project "${PROJECT_ID}"

# ── the deploy identity ─────────────────────────────────────────────────────

if ! gcloud iam service-accounts describe "${SA}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${DEPLOYER}" --project "${PROJECT_ID}" \
    --display-name "Archon CD (GitHub Actions)"
fi

# What terraform needs to own `infra/main.tf` end to end, and nothing wider.
# This account is powerful inside this one project on purpose: it creates
# service accounts and sets IAM, because the deployment declares both. It has
# no access to any other project and no key.
for ROLE in \
  roles/run.admin \
  roles/iam.serviceAccountAdmin \
  roles/iam.serviceAccountUser \
  roles/resourcemanager.projectIamAdmin \
  roles/serviceusage.serviceUsageAdmin \
  roles/storage.admin \
  roles/pubsub.admin \
  roles/eventarc.admin \
  roles/datastore.owner \
  roles/cloudbuild.builds.editor \
  roles/artifactregistry.writer
do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${SA}" --role "${ROLE}" \
    --condition=None --quiet >/dev/null
  echo "    granted ${ROLE}"
done

# ── the federation ──────────────────────────────────────────────────────────

if ! gcloud iam workload-identity-pools describe "${POOL}" \
     --location=global --project "${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "${POOL}" \
    --location=global --project "${PROJECT_ID}" \
    --display-name="GitHub Actions"
fi

if ! gcloud iam workload-identity-pools providers describe "${PROVIDER}" \
     --location=global --workload-identity-pool="${POOL}" \
     --project "${PROJECT_ID}" >/dev/null 2>&1; then
  # `attribute-condition` is the whole security boundary. Read the header.
  gcloud iam workload-identity-pools providers create-oidc "${PROVIDER}" \
    --location=global --workload-identity-pool="${POOL}" \
    --project "${PROJECT_ID}" \
    --display-name="GitHub OIDC" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository == '${REPO}' && assertion.ref == 'refs/heads/${BRANCH}'"
fi

POOL_ID="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}"

# Only the main branch of that repository may become the deploy account, and
# this line is the one that has to say so.
#
# It bound `attribute.repository/${REPO}`, which is EVERY ref of that
# repository: a branch, a tag, a pull request. `attribute.ref` was mapped on
# the provider above and then never used, and the comment here claimed a
# restriction that nothing enforced. Anyone able to push a branch could have
# minted the deploy identity.
#
# Both halves now: the provider refuses a token whose repository or ref is
# wrong, and the binding names the ref rather than the repository.
gcloud iam service-accounts add-iam-policy-binding "${SA}" \
  --project "${PROJECT_ID}" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/${POOL_ID}/attribute.ref/refs/heads/${BRANCH}" \
  --quiet >/dev/null

PROVIDER_RESOURCE="${POOL_ID}/providers/${PROVIDER}"

cat <<SUMMARY

Done. No key was created and none exists.

Now set these two REPOSITORY VARIABLES (not secrets: neither is confidential,
and a variable is visible in logs, which is what you want for these).

  Settings > Secrets and variables > Actions > Variables > New repository variable

  GCP_WORKLOAD_IDENTITY_PROVIDER
    ${PROVIDER_RESOURCE}

  GCP_DEPLOY_SERVICE_ACCOUNT
    ${SA}

Or from the command line:

  gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --body "${PROVIDER_RESOURCE}"
  gh variable set GCP_DEPLOY_SERVICE_ACCOUNT --body "${SA}"

After that, every push to main that passes CI deploys itself.
SUMMARY
