# Non-secret settings only. Set project_id/region/image/deployer via -var or TF_VAR_* (see docs/RUNBOOK.md).
subnet_cidr             = "10.30.0.0/24"
redis_reserved_ip_range = "10.30.8.0/29"
redis_tier              = "STANDARD_HA"
redis_memory_gb         = 1

api_min_instances = 1
api_max_instances = 10
api_concurrency   = 40
api_cpu           = "1"
api_memory        = "512Mi"

worker_instances           = 2
worker_cpu                 = "1"
worker_memory              = "1Gi"
worker_max_concurrent_runs = 2

kill_switch         = false
llm_spend_cap_usd   = 25
llm_project_cap_usd = 20
deletion_protection = true
