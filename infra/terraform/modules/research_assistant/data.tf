# ---------------------------------------------------------------- Memorystore for Redis (run state, queue, ledger)
#
# Private (VPC peering, no public IP), AUTH on, TLS in transit (SERVER_AUTHENTICATION, port 6378), RDB snapshots.
# maxmemory-policy=noeviction: this Redis holds durable run state (node snapshots have TTLs), so Redis must refuse
# writes when full rather than silently evict checkpoints; alert on memory usage instead (docs/SLO.md).
#
# NOTE: with auth_enabled the provider exports the AUTH string as the computed attribute `auth_string`, so it is
# stored in Terraform state (it is never in code or plan text). Keep the GCS state bucket private, versioned and
# CMEK/uniform-access protected; rotate the AUTH string after any state exposure (docs/RUNBOOK.md).
resource "google_redis_instance" "state" {
  project                 = var.project_id
  name                    = "${local.prefix}-state"
  display_name            = "research-assistant ${var.env} run state"
  region                  = var.region
  tier                    = var.redis_tier
  memory_size_gb          = var.redis_memory_gb
  redis_version           = "REDIS_7_2"
  authorized_network      = google_compute_network.vpc.id
  connect_mode            = "DIRECT_PEERING"
  reserved_ip_range       = var.redis_reserved_ip_range
  auth_enabled            = true
  transit_encryption_mode = "SERVER_AUTHENTICATION"
  deletion_protection     = var.deletion_protection
  labels                  = local.labels

  redis_configs = {
    maxmemory-policy = "noeviction"
  }

  persistence_config {
    persistence_mode    = "RDB"
    rdb_snapshot_period = "ONE_HOUR"
  }

  maintenance_policy {
    weekly_maintenance_window {
      day = "SUNDAY"
      start_time {
        hours   = 6
        minutes = 0
        seconds = 0
        nanos   = 0
      }
    }
  }

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- Secret Manager (empty containers, no versions)
resource "google_secret_manager_secret" "app" {
  for_each  = local.secrets
  project   = var.project_id
  secret_id = "${local.prefix}-${each.key}"
  labels    = local.labels
  annotations = {
    purpose = each.value
  }

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "readers" {
  for_each  = local.secret_readers
  project   = var.project_id
  secret_id = google_secret_manager_secret.app[each.value.secret].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime[each.value.sa].email}"
}
