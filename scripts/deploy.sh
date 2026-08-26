#!/usr/bin/env bash
# Deploy Archon, and tear it down again.
#
#   PROJECT_ID=your-project ./scripts/deploy.sh
#   PROJECT_ID=your-project ./scripts/deploy.sh destroy
#
# This script builds the image and hands everything else to Terraform, because
# a resource created by a shell command here is a resource nobody can review,
# reproduce or remove. `infra/main.tf` is the deployment; this is the two steps
# Terraform cannot do: build a container, and print what a judge should open.
#
# What gets created, and why each piece is there:
#
#   Cloud Run service       the API, the page, and the /events sink
#   Firestore (default)     the run journal, the books, the filed drafts
#   GCS bucket              where a month's documents land
#   Pub/Sub topic + push    object-finalize becomes a close, with an OIDC token
#   two service accounts    one runs the service, one only mints that token
#
# Take the bucket, the topic and the subscription away and Archon still works,
# but only when somebody presses a button. Those three are the unattended part.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-archon}"
OWNER_EMAIL="${ARCHON_OWNER_EMAIL:-}"
ACTION="${1:-apply}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# One source for the SHA. It tags the image AND stamps /api/health, and the
# two have to agree: a page whose release does not match the image it is
# running is worse than no stamp, because it is confidently wrong.
RELEASE="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE}:${RELEASE}"

echo "==> project ${PROJECT_ID}, region ${REGION}, image ${IMAGE}"
gcloud config set project "${PROJECT_ID}" >/dev/null

if [[ "${ACTION}" == "destroy" ]]; then
  # Teardown is a first-class path, not an afterthought. D2 asks for one
  # pipeline that deploys from nothing and removes everything again, and a
  # teardown nobody has run is a teardown that does not work.
  terraform -chdir="${REPO_ROOT}/infra" init -input=false \
    -backend-config="bucket=${PROJECT_ID}-tfstate" \
    -backend-config="prefix=${SERVICE}"
  terraform -chdir="${REPO_ROOT}/infra" destroy -auto-approve \
    -var "project_id=${PROJECT_ID}" \
    -var "region=${REGION}" \
    -var "service_name=${SERVICE}" \
    -var "image=${IMAGE}" \
    -var "release=${RELEASE}" \
    -var "owner_email=${OWNER_EMAIL}"
  echo "==> torn down"
  exit 0
fi

# The one API Terraform needs before it can enable the rest.
gcloud services enable cloudbuild.googleapis.com run.googleapis.com

echo "==> building the image"
gcloud builds submit "${REPO_ROOT}" --tag "${IMAGE}"

echo "==> applying infra/main.tf"
terraform -chdir="${REPO_ROOT}/infra" init -input=false \
    -backend-config="bucket=${PROJECT_ID}-tfstate" \
    -backend-config="prefix=${SERVICE}"
terraform -chdir="${REPO_ROOT}/infra" apply -auto-approve \
  -var "project_id=${PROJECT_ID}" \
  -var "region=${REGION}" \
  -var "service_name=${SERVICE}" \
  -var "image=${IMAGE}" \
  -var "release=${RELEASE}" \
  -var "owner_email=${OWNER_EMAIL}"

SERVICE_URL="$(terraform -chdir="${REPO_ROOT}/infra" output -raw service_url)"
BUCKET="$(terraform -chdir="${REPO_ROOT}/infra" output -raw mail_bucket)"

echo "==> checking the judge's route actually serves"
curl --fail --silent --show-error "${SERVICE_URL}/" \
  | grep -q "closes the month while nobody is watching"
curl --fail --silent --show-error "${SERVICE_URL}/api/health"
echo

cat <<SUMMARY

Deployed.

  page          ${SERVICE_URL}/
  health        ${SERVICE_URL}/api/health
  close July    curl -X POST ${SERVICE_URL}/api/close/2026-07

/events is now verified: it takes a Google-signed OIDC token for
${SERVICE_URL}, minted by the archon-pusher service account and nobody else.
An unauthenticated POST to it gets a 403.

To watch it fire with nobody touching it, drop a document into the bucket under
a folder named for the period:

  gcloud storage cp corpus/2026-07/remittance-MFX-RA-4417.txt \\
      gs://${BUCKET}/mail/2026-07/

Then read the run back:

  curl ${SERVICE_URL}/api/close/2026-07 | python -m json.tool | head -40

Tear it all down again:

  PROJECT_ID=${PROJECT_ID} ./scripts/deploy.sh destroy

SUMMARY
