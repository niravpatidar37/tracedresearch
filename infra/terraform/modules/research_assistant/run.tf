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

  scaling {
    scaling_mode          = "MANUAL"
    manual_instance_count = var.worker_instances
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
