locals {
  prefix = "${var.name}-${var.env}"
  labels = merge({ app = "research-assistant", env = var.env, managed-by = "terraform" }, var.labels)
  services = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "redis.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "cloudscheduler.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
  ])
  run_tag = "${local.prefix}-run" # network tag on every Cloud Run network interface (firewall target)

  # Secret Manager secrets: created EMPTY; values are added out of band (`gcloud secrets versions add`),
  # so no secret value ever appears in Terraform code, plan output or state.
  secrets = {
    groq-api-key      = "Groq API key used by the worker for LLM calls"
    research-api-keys = "Comma-separated X-API-Key values accepted by the API (>= 16 chars each)"
    redis-auth        = "Memorystore AUTH string (copy from `gcloud redis instances get-auth-string`)"
    smoke-api-key     = "Dedicated X-API-Key used only by the deploy smoke test (>= 16 chars); readable by the CI deployer"
  }
  # Least privilege: which runtime identity may read which secret (resource-level IAM, nothing project-wide).
  secret_readers = {
    "api/research-api-keys" = { sa = "api", secret = "research-api-keys" }
    "api/redis-auth"        = { sa = "api", secret = "redis-auth" }
    "api/smoke-api-key"     = { sa = "api", secret = "smoke-api-key" }
    "scaler/redis-auth"     = { sa = "scaler", secret = "redis-auth" }
    "worker/groq-api-key"   = { sa = "worker", secret = "groq-api-key" }
    "worker/redis-auth"     = { sa = "worker", secret = "redis-auth" }
    "migrate/redis-auth"    = { sa = "migrate", secret = "redis-auth" }
  }

  redis_url = "rediss://${google_redis_instance.state.host}:${google_redis_instance.state.port}/0"
  common_env = {
    REDIS_URL          = local.redis_url
    REDIS_CA_CERT_PEM  = google_redis_instance.state.server_ca_certs[0].cert # public CA cert, not a secret
    LOG_FORMAT         = "json"
    ENV                = var.env # the app refuses LLM_PROVIDER=fake when ENV is staging or prod
    AGENT_KILL_SWITCH  = var.kill_switch ? "1" : "0"
    PORTFOLIO_ENV_FILE = "/nonexistent" # never read a local secrets file in the cloud
  }
  api_env = merge(local.common_env, {
    SERVICE_ROLE       = "api"
    RATE_LIMIT_PER_MIN = tostring(var.rate_limit_per_min)
  })
  worker_env = merge(local.common_env, {
    SERVICE_ROLE        = "worker"
    LLM_PROVIDER        = "groq"
    LLM_SPEND_BACKEND   = "redis"
    LLM_CACHE_BACKEND   = "redis"
    LLM_SPEND_CAP_USD   = tostring(var.llm_spend_cap_usd)
    P2_PROJECT_CAP_USD  = tostring(var.llm_project_cap_usd)
    MAX_CONCURRENT_RUNS = tostring(var.worker_max_concurrent_runs)
    SHUTDOWN_GRACE_S    = "6"
  }, var.extra_env)
  migrate_env = merge(local.common_env, { SERVICE_ROLE = "api" })
  scaler_env = merge(local.common_env, {
    SERVICE_ROLE         = "api" # no LLM client, no LLM key
    MAX_CONCURRENT_RUNS  = tostring(var.worker_max_concurrent_runs)
    SCALER_MIN_INSTANCES = tostring(var.worker_min_instances)
    SCALER_MAX_INSTANCES = tostring(var.worker_max_instances)
    WORKER_POOL_NAME     = "projects/${var.project_id}/locations/${var.region}/workerPools/${local.prefix}-worker"
  })
  runtime_accounts = {
    api       = "No project-level roles. Reads only the Secret Manager secrets it needs."
    worker    = "No project-level roles. Reads only the Secret Manager secrets it needs."
    migrate   = "No project-level roles. Reads only the Redis AUTH secret."
    scaler    = "Queue-depth scaler job: reads the Redis AUTH secret; run.developer on the worker pool only."
    scheduler = "Cloud Scheduler caller: run.invoker on the scaler job only."
  }
}

resource "google_project_service" "apis" {
  for_each           = local.services
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------- Artifact Registry
resource "google_artifact_registry_repository" "images" {
  #checkov:skip=CKV_GCP_84:Images hold no secrets (gitleaks + .dockerignore); Google-managed encryption at rest is accepted. CMEK would add a KMS key ring, rotation and cost for no confidentiality gain here.
  project       = var.project_id
  location      = var.region
  repository_id = local.prefix
  format        = "DOCKER"
  description   = "research-assistant images (${var.env}); deployed by digest"
  labels        = local.labels

  docker_config {
    immutable_tags = true
  }

  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 20
    }
  }
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days; the 20 most recent are kept regardless (rollback targets)
    }
  }

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- service accounts (one per workload)
resource "google_service_account" "runtime" {
  for_each     = local.runtime_accounts
  project      = var.project_id
  account_id   = "${local.prefix}-${each.key}"
  display_name = "research-assistant ${each.key} (${var.env}) runtime"
  description  = each.value
}

# CI deployer may deploy revisions that run as these identities (actAs) - nothing else is granted here.
resource "google_service_account_iam_member" "deployer_act_as" {
  for_each           = var.deployer_service_account == "" ? toset([]) : toset(["api", "worker", "migrate", "scaler"])
  service_account_id = google_service_account.runtime[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account}"
}

# The deploy smoke test authenticates with its own key; the deployer can read that one secret and nothing else.
resource "google_secret_manager_secret_iam_member" "deployer_smoke_key" {
  count     = var.deployer_service_account == "" ? 0 : 1
  project   = var.project_id
  secret_id = google_secret_manager_secret.app["smoke-api-key"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.deployer_service_account}"
}

resource "google_artifact_registry_repository_iam_member" "deployer_push" {
  count      = var.deployer_service_account == "" ? 0 : 1
  project    = var.project_id
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${var.deployer_service_account}"
}
