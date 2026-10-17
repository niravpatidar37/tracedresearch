variable "project_id" {
  description = "GCP project id."
  type        = string
}

variable "region" {
  description = "GCP region."
  type        = string
}

variable "image" {
  description = "Image by digest for the first apply (CD deploys later digests)."
  type        = string
}

variable "deployer_service_account" {
  description = "CI deployer service account email (Workload Identity Federation)."
  type        = string
  default     = ""
}

variable "subnet_cidr" {
  description = "Direct VPC egress subnet."
  type        = string
}

variable "redis_reserved_ip_range" {
  description = "Memorystore reserved /29."
  type        = string
}

variable "redis_tier" {
  description = "BASIC or STANDARD_HA."
  type        = string
}

variable "redis_memory_gb" {
  description = "Memorystore GiB."
  type        = number
}

variable "api_min_instances" {
  description = "API min instances."
  type        = number
}

variable "api_max_instances" {
  description = "API max instances."
  type        = number
}

variable "api_concurrency" {
  description = "API concurrency per instance."
  type        = number
}

variable "api_cpu" {
  description = "API CPU limit."
  type        = string
}

variable "api_memory" {
  description = "API memory limit."
  type        = string
}

variable "worker_instances" {
  description = "Worker pool instances."
  type        = number
}

variable "worker_cpu" {
  description = "Worker CPU limit."
  type        = string
}

variable "worker_memory" {
  description = "Worker memory limit."
  type        = string
}

variable "worker_max_concurrent_runs" {
  description = "Concurrent runs per worker instance."
  type        = number
}

variable "kill_switch" {
  description = "AGENT_KILL_SWITCH."
  type        = bool
  default     = false
}

variable "llm_spend_cap_usd" {
  description = "Monthly global LLM cap (USD)."
  type        = number
}

variable "llm_project_cap_usd" {
  description = "Monthly service LLM cap (USD)."
  type        = number
}

variable "deletion_protection" {
  description = "Protect stateful resources from destroy."
  type        = bool
}
