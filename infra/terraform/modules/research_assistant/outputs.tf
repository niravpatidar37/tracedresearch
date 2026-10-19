output "api_url" {
  description = "Public URL of the API service."
  value       = google_cloud_run_v2_service.api.uri
}

output "api_service" {
  description = "Cloud Run service name (API)."
  value       = google_cloud_run_v2_service.api.name
}

output "worker_pool" {
  description = "Cloud Run worker pool name."
  value       = google_cloud_run_v2_worker_pool.worker.name
}

output "scaler_job" {
  description = "Cloud Run job that sets the worker pool size from the queue (run every minute by Cloud Scheduler)."
  value       = google_cloud_run_v2_job.scaler.name
}

output "slo_name" {
  description = "Availability SLO resource name (for select_slo_burn_rate)."
  value       = google_monitoring_slo.availability.name
}

output "migrate_job" {
  description = "Cloud Run job that applies keyspace migrations."
  value       = google_cloud_run_v2_job.migrate.name
}

output "image_repository" {
  description = "Docker repository path for images."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "redis_host" {
  description = "Memorystore private IP."
  value       = google_redis_instance.state.host
}

output "redis_instance" {
  description = "Memorystore instance name (for `gcloud redis instances get-auth-string`)."
  value       = google_redis_instance.state.name
}

output "secret_ids" {
  description = "Secret Manager secret ids to populate with `gcloud secrets versions add`."
  value       = { for k, s in google_secret_manager_secret.app : k => s.secret_id }
}

output "service_accounts" {
  description = "Runtime service account emails."
  value       = { for k, sa in google_service_account.runtime : k => sa.email }
}
