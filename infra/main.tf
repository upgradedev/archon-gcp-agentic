/*
 * D1: every cloud resource this product needs, declared.
 *
 * Nothing created by hand in a console survives to the submission, so this file
 * is the whole deployment and `scripts/deploy.sh` builds the image and calls it
 * rather than creating resources itself.
 *
 * The least-privilege posture from `archon/adapters/auth.py` is expressed here
 * rather than only in code:
 *
 *   - the service is public, because a judge opens the page with no account
 *   - `/events` is the only route that can write, and the push subscription is
 *     the only caller that can authenticate to it, using its own service
 *     account and an OIDC token with this service's URL as the audience
 *   - the runtime service account gets Firestore and Storage access and
 *     nothing else. It is not the default compute account, which is editor
 *
 *   terraform -chdir=infra init
 *   terraform -chdir=infra apply -var project_id=YOUR_PROJECT
 *   terraform -chdir=infra destroy -var project_id=YOUR_PROJECT
 */

terraform {
  required_version = ">= 1.6"

  # State lives in the project it describes, not on somebody's laptop.
  #
  # It was a local file, and that is what stopped this deployment being
  # automatable: a pipeline cannot apply a plan it cannot read, and the state
  # was gitignored precisely because it must not be committed. So it moved to a
  # versioned bucket in the same project. Versioning is not decoration here;
  # it is the only way back from an apply that corrupts state.
  #
  # DELIBERATELY EMPTY, and this is load bearing. Naming the bucket here makes
  # every caller of this directory share one state object, and one of those
  # callers is `cloudbuild.yaml`, whose whole job is `destroy -> apply ->
  # verify -> destroy` against a THROWAWAY project. Its guard refuses to run
  # against the production project; it has nothing to say about a state bucket.
  # So a lifecycle run would have loaded production state, applied it under a
  # different project id, and written an empty state over it on the final
  # destroy, leaving the judge-facing infrastructure unmanaged.
  #
  # Every caller therefore passes its own bucket at init time:
  #
  #   terraform init \\
  #     -backend-config="bucket=${PROJECT}-tfstate" \\
  #     -backend-config="prefix=archon"
  #
  # which makes the state per project, and isolation the default rather than
  # something each caller has to remember.
  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

variable "project_id" {
  type        = string
  description = "The Google Cloud project to deploy into."
}

variable "region" {
  type        = string
  default     = "us-central1"
  description = "Region for Cloud Run, Firestore and the mail bucket."
}

variable "service_name" {
  type    = string
  default = "archon"
}

variable "image" {
  type        = string
  description = "Container image to deploy. deploy.sh builds this and passes it in."
}

variable "company" {
  type    = string
  default = "Bell Ridge Haulage"
}

variable "deletion_protection" {
  type        = bool
  default     = false
  description = <<-EOT
    Whether the database and the service refuse to be destroyed.

    False here on purpose. This entry's claim is that the whole environment can
    be rebuilt from nothing, and that claim is only worth anything if a teardown
    actually tears down. A real deployment holding a customer's books sets this
    true and accepts a deliberate human step before a destroy.
  EOT
}

variable "owner_email" {
  type        = string
  default     = ""
  description = "Where the month-end digest goes. Empty leaves it composed but undelivered."
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  # THE audience, computed once and used in both places that need it.
  #
  # Cloud Run answers on two URLs: this project-number form and a hashed form
  # that `google_cloud_run_v2_service.uri` returns. Both reach the service, and
  # a token minted for one does NOT verify against the other.
  #
  # The first terraform-managed run had the subscription signing for the hashed
  # form while the service verified the number form, so every push was refused
  # with a 403 that looked exactly like a trigger nobody pulled. The env var
  # cannot reference the service's own uri without a cycle, which is why it was
  # hand-computed and why the two drifted. One local, used twice, cannot drift.
  service_url = "https://${var.service_name}-${data.google_project.this.number}.${var.region}.run.app"

  services = [
    "run.googleapis.com",
    "firestore.googleapis.com",
    "pubsub.googleapis.com",
    "storage.googleapis.com",
    "eventarc.googleapis.com",
    "aiplatform.googleapis.com",
  ]
}

resource "google_project_service" "enabled" {
  for_each = toset(local.services)
  service  = each.value

  # A destroy that switches the project's APIs off would take unrelated things
  # with it, so teardown leaves them enabled.
  disable_on_destroy = false
}

# ── memory ───────────────────────────────────────────────────────────────────
# Firestore rather than a managed SQL instance, and the reason is the shape of
# the business: this firm closes its books twelve times a year, so what matters
# is that idle costs nothing. Cloud SQL bills by the hour whether a truck moved
# or not.

resource "google_firestore_database" "archon" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  # Both of these default to true, and both silently break a teardown: the
  # destroy runs, reports most resources gone, empties the state, and leaves
  # the database standing. Found by running it rather than reading it.
  #
  # False is correct for an environment that has to be provably rebuildable
  # from nothing. A production deployment holding a firm's books would set
  # both to true and accept that a teardown needs a deliberate human step.
  # Firestore spells it differently from Cloud Run, which is its own small trap:
  # the database takes `delete_protection_state` and has no `deletion_protection`.
  delete_protection_state = var.deletion_protection ? "DELETE_PROTECTION_ENABLED" : "DELETE_PROTECTION_DISABLED"

  depends_on = [google_project_service.enabled]
}

# ── the mail bucket, and the trigger that makes the close unattended ─────────

resource "google_storage_bucket" "mail" {
  name                        = "${var.project_id}-archon-mail"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true

  depends_on = [google_project_service.enabled]
}

resource "google_pubsub_topic" "mail" {
  name       = "archon-mail"
  depends_on = [google_project_service.enabled]
}

data "google_storage_project_service_account" "gcs" {
  depends_on = [google_project_service.enabled]
}

# Cloud Storage publishes as its own agent, so it needs permission on the topic.
resource "google_pubsub_topic_iam_member" "gcs_publisher" {
  topic  = google_pubsub_topic.mail.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
}

resource "google_storage_notification" "mail" {
  bucket         = google_storage_bucket.mail.name
  topic          = google_pubsub_topic.mail.id
  payload_format = "JSON_API_V1"
  event_types    = ["OBJECT_FINALIZE"]

  depends_on = [google_pubsub_topic_iam_member.gcs_publisher]
}

# ── identities ───────────────────────────────────────────────────────────────
# Two, deliberately. The service runs as one and can reach the data. The push
# subscription authenticates as the other and can reach nothing but the route.

resource "google_service_account" "runtime" {
  account_id   = "archon-runtime"
  display_name = "Archon Cloud Run runtime"
  description  = "Reads the mail bucket and writes the books. Not the default compute account."
}

resource "google_service_account" "pusher" {
  account_id   = "archon-pusher"
  display_name = "Archon Pub/Sub push identity"
  description  = "Mints the OIDC token on /events. Holds no data permission at all."
}

# Pub/Sub mints the OIDC token AS the pusher account, and it cannot do that
# without being allowed to impersonate it. `gcloud pubsub subscriptions create`
# grants this silently, which is why the hand-built version worked and this file
# did not: the first terraform-managed run produced an unbroken stream of 403s
# on /events until this binding existed.
#
# It is the reason IaC has to be exercised end to end rather than plan-checked.
# A plan cannot tell you about a permission the CLI was granting behind your back.
data "google_project" "number" {}

resource "google_service_account_iam_member" "pubsub_mints_pusher_tokens" {
  service_account_id = google_service_account.pusher.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.number.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_project_iam_member" "runtime_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_storage_bucket_iam_member" "runtime_reads_mail" {
  bucket = google_storage_bucket.mail.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

# ── the service ──────────────────────────────────────────────────────────────

resource "google_cloud_run_v2_service" "archon" {
  name     = var.service_name
  location = var.region

  # Same trap as the database: a protected service cannot be destroyed and the
  # teardown fails half way through, after the state has already been emptied.
  deletion_protection = var.deletion_protection

  template {
    service_account = google_service_account.runtime.email
    timeout         = "120s"

    # Scale to zero between months. That is the whole cost argument, and in
    # the v2 resource it belongs inside the template, not beside it.
    scaling {
      min_instance_count = 0
      max_instance_count = 4
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "ARCHON_COMPANY"
        value = var.company
      }
      env {
        name  = "ARCHON_OWNER_EMAIL"
        value = var.owner_email
      }
      # Setting the audience is what turns /events from open to verified. It is
      # the service's own URL, which is what Pub/Sub will mint a token for.
      env {
        name  = "ARCHON_EVENTS_AUDIENCE"
        value = local.service_url
      }
      env {
        name  = "ARCHON_EVENTS_CALLER"
        value = google_service_account.pusher.email
      }
    }
  }

  depends_on = [google_project_service.enabled]
}

data "google_project" "this" {}

# Public, on purpose. A judge opens the page with no account, and the anonymous
# route runs against an ephemeral store so it cannot change anything durable.
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.archon.name
  location = google_cloud_run_v2_service.archon.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ── delivery of the trigger ──────────────────────────────────────────────────

resource "google_pubsub_subscription" "push" {
  name  = "archon-mail-push"
  topic = google_pubsub_topic.mail.name

  ack_deadline_seconds = 120

  depends_on = [google_service_account_iam_member.pubsub_mints_pusher_tokens]

  push_config {
    push_endpoint = "${local.service_url}/events"

    # This is the other half of the auth control: the token /events verifies.
    oidc_token {
      service_account_email = google_service_account.pusher.email
      audience              = local.service_url
    }
  }

  # A message that will never succeed should stop being retried rather than
  # closing the same month forever.
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
}

output "service_url" {
  value       = local.service_url
  description = "The page a judge opens."
}

output "mail_bucket" {
  value       = google_storage_bucket.mail.name
  description = "Drop a month under mail/YYYY-MM/ and the close fires."
}

output "events_audience" {
  value       = local.service_url
  description = "What /events verifies tokens against."
}
