terraform {
  required_version = ">= 1.9"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.7"
    }
  }
  # Remote state in GCS, configured at init time (no bucket name in code):
  #   terraform init -backend-config="bucket=<STATE_BUCKET>" -backend-config="prefix=research-assistant/staging"
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
  default_labels = {
    app = "research-assistant"
    env = "staging"
  }
}

module "research_assistant" {
  source = "../../modules/research_assistant"

  project_id               = var.project_id
  region                   = var.region
  env                      = "staging"
  image                    = var.image
  deployer_service_account = var.deployer_service_account

  subnet_cidr             = var.subnet_cidr
  redis_reserved_ip_range = var.redis_reserved_ip_range
  redis_tier              = var.redis_tier
  redis_memory_gb         = var.redis_memory_gb

  api_min_instances = var.api_min_instances
  api_max_instances = var.api_max_instances
  api_concurrency   = var.api_concurrency
  api_cpu           = var.api_cpu
  api_memory        = var.api_memory

  worker_instances           = var.worker_instances
  worker_cpu                 = var.worker_cpu
  worker_memory              = var.worker_memory
  worker_max_concurrent_runs = var.worker_max_concurrent_runs

  kill_switch         = var.kill_switch
  llm_spend_cap_usd   = var.llm_spend_cap_usd
  llm_project_cap_usd = var.llm_project_cap_usd
  deletion_protection = var.deletion_protection
}
