# ---------------------------------------------------------------- Cloud Run
#
# Why two workloads: POST /research returns 202 and the research run (minutes, many LLM + fetch calls) continues
# in the background. On a request-billed Cloud Run *service* CPU is throttled outside requests, so background
# work would stall. The API therefore only enqueues; a Cloud Run *worker pool* (always-allocated CPU, no
# ingress, pull-based) consumes the Redis queue. Trade-offs vs. a service with instance-based billing
# (cpu_idle = false) running the worker: see docs/RUNBOOK.md "Worker topology".

locals {
  probe_port = 8102
}

resource "google_cloud_run_v2_service" "api" {
  project              = var.project_id
  name                 = "${local.prefix}-api"
  location             = var.region
  ingress              = var.api_ingress
  invoker_iam_disabled = var.api_public
  deletion_protection  = var.deletion_protection
  labels               = local.labels

  template {
    service_account                  = google_service_account.runtime["api"].email
    max_instance_request_concurrency = var.api_concurrency
    timeout                          = "900s" # SSE progress streams; the app closes idle streams after 600 s
    labels                           = local.labels

    scaling {
      min_instance_count = var.api_min_instances
      max_instance_count = var.api_max_instances
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.run.id
        tags       = [local.run_tag]
      }
    }

    containers {
      image = var.image

      ports {
        container_port = local.probe_port
      }

      resources {
        limits = {
          cpu    = var.api_cpu
          memory = var.api_memory
        }
        cpu_idle          = true # request-based billing is fine: the API role does no background work
        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = local.api_env
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name = "RESEARCH_API_KEYS"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app["research-api-keys"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "REDIS_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app["redis-auth"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "RESEARCH_SMOKE_API_KEY" # accepted in addition to RESEARCH_API_KEYS; used by deploy.yml only
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app["smoke-api-key"].secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        initial_delay_seconds = 0
        period_seconds        = 3
        timeout_seconds       = 2
        failure_threshold     = 20
        http_get {
          path = "/readyz" # Redis reachable + keyspace migrated
          port = local.probe_port
        }
      }

      liveness_probe {
        period_seconds    = 15
        timeout_seconds   = 3
        failure_threshold = 3
        http_get {
          path = "/healthz" # process only: a Redis blip must not restart instances
          port = local.probe_port
        }
      }
    }
  }

  lifecycle {
    # CD owns the running image (deployed by digest) and the traffic split (canary / rollback).
    ignore_changes = [
      template[0].containers[0].image,
      template[0].revision,
      traffic,
      client,
      client_version,
    ]
  }

  depends_on = [google_secret_manager_secret_iam_member.readers]
}

resource "google_cloud_run_v2_worker_pool" "worker" {
  project             = var.project_id
  name                = "${local.prefix}-worker"
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.labels

  # MANUAL scaling with an external queue-depth scaler (the scaler job below sets manual_instance_count every
  # minute). The provider also offers scaling_mode = "AUTOMATIC" with min/max_instance_count, but nothing in it
  # sees the Redis queue; Google's documented queue-driven option is CREMA (see docs/RUNBOOK.md "Scaling").
  scaling {
    scaling_mode          = "MANUAL"
    manual_instance_count = var.worker_instances # initial count only; the scaler owns it afterwards
  }

  template {
    service_account = google_service_account.runtime["worker"].email
    labels          = local.labels

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.run.id
        tags       = [local.run_tag]
      }
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = var.worker_cpu
          memory = var.worker_memory
        }
      }

      dynamic "env" {
        for_each = local.worker_env
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name = "GROQ_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app["groq-api-key"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "REDIS_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app["redis-auth"].secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        initial_delay_seconds = 0
        period_seconds        = 3
        timeout_seconds       = 2
        failure_threshold     = 20
        http_get {
          path = "/readyz"
          port = local.probe_port
        }
      }

      liveness_probe {
        period_seconds    = 15
        timeout_seconds   = 3
        failure_threshold = 3
        http_get {
          path = "/healthz"
          port = local.probe_port
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      template[0].revision,
      instance_splits,
      scaling[0].manual_instance_count, # owned by the scaler job
      client,
      client_version,
    ]
  }

  depends_on = [google_secret_manager_secret_iam_member.readers]
}

# Versioned keyspace migrations run as a one-off job before each deploy (never at app startup).
resource "google_cloud_run_v2_job" "migrate" {
  project             = var.project_id
  name                = "${local.prefix}-migrate"
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.labels

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.runtime["migrate"].email
      max_retries     = 1
      timeout         = "300s"

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = google_compute_network.vpc.id
          subnetwork = google_compute_subnetwork.run.id
          tags       = [local.run_tag]
        }
      }

      containers {
        image   = var.image
        command = ["research-assistant"]
        args    = ["migrate"]

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        dynamic "env" {
          for_each = local.migrate_env
          content {
            name  = env.key
            value = env.value
          }
        }
        env {
          name = "REDIS_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app["redis-auth"].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [google_secret_manager_secret_iam_member.readers]
}

# ---------------------------------------------------------------- queue-depth scaler (Cloud Scheduler -> job)
#
# Every minute Cloud Scheduler calls the Cloud Run Admin API v2 `jobs.run` method for this job (OAuth token of a
# dedicated caller SA). The job (`research-assistant scale-workers`) reads the Redis queue, logs `queue_stats`
# (the source of the queue-depth / oldest-job-age metrics in monitoring.tf) and PATCHes the worker pool's
# scaling.manualInstanceCount within [worker_min_instances, worker_max_instances]. Code: src/research_assistant/scaler.py.
resource "google_cloud_run_v2_job" "scaler" {
  project             = var.project_id
  name                = "${local.prefix}-scaler"
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.labels

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.runtime["scaler"].email
      max_retries     = 0 # the next scheduled run is the retry
      timeout         = "50s"

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = google_compute_network.vpc.id
          subnetwork = google_compute_subnetwork.run.id
          tags       = [local.run_tag]
        }
      }

      containers {
        image   = var.image
        command = ["research-assistant"]
        args    = ["scale-workers"]

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        dynamic "env" {
          for_each = local.scaler_env
          content {
            name  = env.key
            value = env.value
          }
        }
        env {
          name = "REDIS_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app["redis-auth"].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [google_secret_manager_secret_iam_member.readers]
}

# The scaler may change the worker pool (resource-level role, nothing project-wide) ...
resource "google_cloud_run_v2_worker_pool_iam_member" "scaler_developer" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_worker_pool.worker.name
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.runtime["scaler"].email}"
}

# ... which requires actAs on the worker's runtime identity (only that one).
resource "google_service_account_iam_member" "scaler_act_as_worker" {
  service_account_id = google_service_account.runtime["worker"].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.runtime["scaler"].email}"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_scaler" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.scaler.name
  role     = "roles/run.invoker" # includes run.jobs.run
  member   = "serviceAccount:${google_service_account.runtime["scheduler"].email}"
}

resource "google_cloud_scheduler_job" "scaler" {
  project          = var.project_id
  region           = var.region
  name             = "${local.prefix}-scaler"
  description      = "Runs the queue-depth scaler job for the research worker pool"
  schedule         = var.scaler_schedule
  time_zone        = "Etc/UTC"
  attempt_deadline = "60s"
  paused           = !var.scaler_enabled

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.scaler.name}:run"

    oauth_token {
      service_account_email = google_service_account.runtime["scheduler"].email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_project_service.apis, google_cloud_run_v2_job_iam_member.scheduler_runs_scaler]
}
