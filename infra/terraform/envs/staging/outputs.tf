output "api_url" {
  description = "API URL."
  value       = module.research_assistant.api_url
}

output "api_service" {
  description = "API service name."
  value       = module.research_assistant.api_service
}

output "worker_pool" {
  description = "Worker pool name."
  value       = module.research_assistant.worker_pool
}

output "migrate_job" {
  description = "Migration job name."
  value       = module.research_assistant.migrate_job
}

output "image_repository" {
  description = "Docker repository path."
  value       = module.research_assistant.image_repository
}

output "redis_instance" {
  description = "Memorystore instance name."
  value       = module.research_assistant.redis_instance
}

output "secret_ids" {
  description = "Secrets to populate."
  value       = module.research_assistant.secret_ids
}

output "service_accounts" {
  description = "Runtime service accounts."
  value       = module.research_assistant.service_accounts
}
