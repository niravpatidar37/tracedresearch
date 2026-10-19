variable "project_id" {
  description = "GCP project id."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Memorystore and Artifact Registry."
  type        = string
}

variable "env" {
  description = "Environment name (staging | prod); used in resource names and labels."
  type        = string
  validation {
    condition     = contains(["staging", "prod"], var.env)
    error_message = "env must be staging or prod."
  }
}

variable "name" {
  description = "Base name for resources."
  type        = string
  default     = "research"
}

variable "image" {
  description = "Image by digest (REGION-docker.pkg.dev/PROJECT/REPO/research-assistant@sha256:...). Needed for the first apply only; CD deploys later digests and Terraform ignores image drift."
  type        = string
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.image))
    error_message = "Pass the image by digest, not by tag."
  }
}

variable "deployer_service_account" {
  description = "Email of the CI deployer SA (Workload Identity Federation). Gets actAs on the runtime SAs only. Empty = skip."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------- network
variable "subnet_cidr" {
  description = "Subnet for Cloud Run Direct VPC egress (a /26 or larger is recommended by Cloud Run)."
  type        = string
  default     = "10.20.0.0/24"
}

variable "redis_reserved_ip_range" {
  description = "CIDR (/29) reserved for the Memorystore instance (DIRECT_PEERING)."
  type        = string
  default     = "10.20.8.0/29"
}

# ---------------------------------------------------------------- api service
variable "api_min_instances" {
  description = "Minimum API instances (0 = scale to zero)."
  type        = number
  default     = 0
}

variable "api_max_instances" {
  description = "Maximum API instances."
  type        = number
  default     = 4
}

variable "api_concurrency" {
  description = "Max concurrent requests per API instance (SSE streams hold a slot)."
  type        = number
  default     = 40
}

variable "api_cpu" {
  description = "API CPU limit."
  type        = string
  default     = "1"
}

variable "api_memory" {
  description = "API memory limit."
  type        = string
  default     = "512Mi"
}

variable "api_ingress" {
  description = "Cloud Run ingress for the API."
  type        = string
  default     = "INGRESS_TRAFFIC_ALL"
  validation {
    condition     = contains(["INGRESS_TRAFFIC_ALL", "INGRESS_TRAFFIC_INTERNAL_ONLY", "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"], var.api_ingress)
    error_message = "Unsupported ingress value."
  }
}

variable "api_public" {
  description = "Disable the Cloud Run invoker IAM check (public API; the app enforces X-API-Key + per-key rate limits)."
  type        = bool
  default     = true
}

variable "rate_limit_per_min" {
  description = "POST /research per API key per minute."
  type        = number
  default     = 10
}

# ---------------------------------------------------------------- worker pool
variable "worker_instances" {
  description = "Initial worker pool instance count; afterwards the queue-depth scaler job owns it."
  type        = number
  default     = 1
}

variable "worker_min_instances" {
  description = "Lower bound the queue-depth scaler keeps (0 lets an idle pool scale to zero; queued runs then wait <= 1 scheduler tick)."
  type        = number
  default     = 1
}

variable "worker_max_instances" {
  description = "Upper bound for the queue-depth scaler (caps cost and the parallel load on the LLM provider)."
  type        = number
  default     = 4
  validation {
    condition     = var.worker_max_instances >= var.worker_min_instances
    error_message = "worker_max_instances must be >= worker_min_instances."
  }
}

variable "scaler_schedule" {
  description = "Cloud Scheduler cron for the scaler job (Cloud Scheduler's finest granularity is one minute)."
  type        = string
  default     = "* * * * *"
}

variable "scaler_enabled" {
  description = "Run the queue-depth scaler (false pauses the scheduler job; the pool then stays at its current count)."
  type        = bool
  default     = true
}

variable "worker_cpu" {
  description = "Worker CPU limit."
  type        = string
  default     = "1"
}

variable "worker_memory" {
  description = "Worker memory limit."
  type        = string
  default     = "1Gi"
}

variable "worker_max_concurrent_runs" {
  description = "Research runs executed concurrently per worker instance."
  type        = number
  default     = 2
}

# ---------------------------------------------------------------- app controls
variable "kill_switch" {
  description = "AGENT_KILL_SWITCH: refuse new runs and pause in-flight runs before any LLM/tool call."
  type        = bool
  default     = false
}

variable "llm_spend_cap_usd" {
  description = "Monthly global LLM spend cap for this deployment (USD, enforced in code before every paid call)."
  type        = number
  default     = 25
}

variable "llm_project_cap_usd" {
  description = "Monthly LLM spend cap for this service (USD)."
  type        = number
  default     = 3
}

variable "extra_env" {
  description = "Extra non-secret env vars for the worker (e.g. budget overrides)."
  type        = map(string)
  default     = {}
  validation {
    # Defence in depth for the fake-LLM gate (the app refuses it anyway when ENV is staging/prod).
    condition     = !contains(keys(var.extra_env), "ENV") && lookup(var.extra_env, "LLM_PROVIDER", "groq") != "fake"
    error_message = "extra_env may not override ENV or set LLM_PROVIDER=fake."
  }
}

# ---------------------------------------------------------------- monitoring (monitoring.tf)
variable "alert_email" {
  description = "Email address for alert notifications. Empty = alert policies are created without a channel."
  type        = string
  default     = ""
}

variable "slo_availability_goal" {
  description = "API availability SLO (share of non-5xx responses; deliberate 503s excluded) over 30 days."
  type        = number
  default     = 0.995
}

variable "slo_latency_p95_s" {
  description = "p95 latency objective for non-streaming API requests, in seconds."
  type        = number
  default     = 0.3
}

variable "alert_queue_depth" {
  description = "Alert when the run queue stays deeper than this for 10 minutes."
  type        = number
  default     = 20
}

variable "alert_oldest_queued_age_s" {
  description = "Alert when the oldest queued run has waited longer than this (seconds) for 10 minutes."
  type        = number
  default     = 300
}

variable "alert_fallback_rate" {
  description = "Alert when more than this share of completed runs used the degraded (no-writer) report over 1 h. The grounded-answer SLO is 80%, so 0.2."
  type        = number
  default     = 0.2
}

# ---------------------------------------------------------------- redis
variable "redis_tier" {
  description = "Memorystore tier: BASIC (no replica) or STANDARD_HA (replica + automatic failover)."
  type        = string
  default     = "BASIC"
}

variable "redis_memory_gb" {
  description = "Memorystore capacity in GiB."
  type        = number
  default     = 1
}

variable "deletion_protection" {
  description = "Protect stateful resources and services from terraform destroy."
  type        = bool
  default     = true
}

variable "labels" {
  description = "Extra labels."
  type        = map(string)
  default     = {}
}
