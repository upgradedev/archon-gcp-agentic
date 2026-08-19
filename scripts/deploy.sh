#!/usr/bin/env bash
# Deploy Archon to Cloud Run, and wire the trigger that makes it unattended.
#
#   PROJECT_ID=your-project ./scripts/deploy.sh
#
# What this creates, and why each piece is there:
#
#   Cloud Run service   archon           the API, the page, and the /events sink
#   Firestore database  (default)        the run journal, the books, the drafts
#   GCS bucket          <project>-archon-mail   where a month's documents land
#   Pub/Sub topic       archon-mail      object-finalize notifications
#   Eventarc/push sub   archon-mail-push delivers them to /events
#
# Take the bucket, the topic and the subscription away and Archon still works,
# but only when somebody presses a button. Those three are the unattended part.
#
# Everything is idempotent: re-running reconciles rather than duplicating.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-archon}"
BUCKET="${BUCKET:-${PROJECT_ID}-archon-mail}"
TOPIC="${TOPIC:-archon-mail}"
SUBSCRIPTION="${SUBSCRIPTION:-archon-mail-push}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> project ${PROJECT_ID}, region ${REGION}"
gcloud config set project "${PROJECT_ID}" >/dev/null

echo "==> enabling the services this needs"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  pubsub.googleapis.com \
  storage.googleapis.com \
  aiplatform.googleapis.com

# Firestore in Native mode. Free tier: 50,000 reads, 20,000 writes and 20,000
# deletes a day, 1 GiB stored, and nothing at all when idle. A haulier closes
# their books twelve times a year, so idle cost is the number that matters.
echo "==> Firestore (skipped if the default database already exists)"
gcloud firestore databases create --location="${REGION}" 2>/dev/null \
  || echo "    already there"

echo "==> building and deploying ${SERVICE}"
gcloud run deploy "${SERVICE}" \
  --source "${REPO_ROOT}" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 4 \
  --timeout 120 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT_ID},ARCHON_COMPANY=Bell Ridge Haulage"

SERVICE_URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" \
  --format 'value(status.url)')"
echo "==> serving at ${SERVICE_URL}"

echo "==> the mail bucket and its notifications"
gcloud storage buckets create "gs://${BUCKET}" --location="${REGION}" 2>/dev/null \
  || echo "    already there"
gcloud pubsub topics create "${TOPIC}" 2>/dev/null || echo "    topic already there"

# One notification per bucket, or every re-run adds another and every dropped
# document closes the month twice.
if ! gcloud storage buckets notifications list "gs://${BUCKET}" 2>/dev/null \
     | grep -q "${TOPIC}"; then
  gcloud storage buckets notifications create "gs://${BUCKET}" \
    --topic="${TOPIC}" --event-types=OBJECT_FINALIZE
else
  echo "    notification already there"
fi

gcloud pubsub subscriptions create "${SUBSCRIPTION}" \
  --topic="${TOPIC}" \
  --push-endpoint="${SERVICE_URL}/events" \
  --ack-deadline=120 2>/dev/null \
  || gcloud pubsub subscriptions update "${SUBSCRIPTION}" \
       --push-endpoint="${SERVICE_URL}/events"

cat <<SUMMARY

Deployed.

  page          ${SERVICE_URL}/
  health        ${SERVICE_URL}/api/health
  close July    curl -X POST ${SERVICE_URL}/api/close/2026-07

To watch it fire with nobody touching it, drop a document into the bucket under
a folder named for the period:

  gcloud storage cp corpus/2026-07/remittance-MFX-RA-4417.txt \\
      gs://${BUCKET}/mail/2026-07/

Then read the run back:

  curl ${SERVICE_URL}/api/close/2026-07 | python -m json.tool | head -40

SUMMARY
