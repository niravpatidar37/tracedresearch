# ---------------------------------------------------------------- VPC for Direct VPC egress + Memorystore
#
# Egress model (documented in docs/RUNBOOK.md "Egress"):
# - Cloud Run uses Direct VPC egress with egress = PRIVATE_RANGES_ONLY: only RFC 1918 / private destinations
#   enter this VPC (that is how the app reaches Memorystore). Public destinations leave through Cloud Run's
#   own internet egress.
# - The worker's fetcher must reach the open web by design, so neither Cloud NAT nor an egress firewall can
#   allowlist its destinations. The SSRF guard in code (fetch/ssrf.py: public IPs only, DNS pinning,
#   redirect re-validation, metadata IPs blocked) is the control for that path.
# - Inside the VPC the firewall below only allows the Memorystore port; every other private destination is
#   denied, as defence in depth behind the SSRF guard.
# - The metadata server (169.254.169.254) is not reachable through the VPC and cannot be firewalled from
#   Cloud Run; the SSRF guard blocks it and the runtime service accounts hold no project roles, so a leaked
#   token can read at most that workload's own secrets.

resource "google_compute_network" "vpc" {
  project                 = var.project_id
  name                    = "${local.prefix}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "run" {
  project                  = var.project_id
  name                     = "${local.prefix}-run"
  region                   = var.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true

  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "allow_redis_egress" {
  project            = var.project_id
  name               = "${local.prefix}-allow-redis-egress"
  network            = google_compute_network.vpc.id
  direction          = "EGRESS"
  priority           = 1000
  target_tags        = [local.run_tag]
  destination_ranges = [var.redis_reserved_ip_range]

  allow {
    protocol = "tcp"
    ports    = ["6378"] # Memorystore with in-transit encryption
  }

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "deny_private_egress" {
  project            = var.project_id
  name               = "${local.prefix}-deny-private-egress"
  network            = google_compute_network.vpc.id
  direction          = "EGRESS"
  priority           = 2000
  target_tags        = [local.run_tag]
  destination_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"]

  deny {
    protocol = "all"
  }

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}
