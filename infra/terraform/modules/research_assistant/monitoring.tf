# ---------------------------------------------------------------- monitoring as code
#
# Uptime check on /healthz, SLO + multi-window burn-rate alerts, 5xx ratio, p95 latency over the SLO, instance
# saturation, and the app-specific signals (queue depth, oldest queued run, degraded-report rate, Redis memory,
# scaler failures). Every resource and argument here exists in hashicorp/google 8.7 (checked with
# `terraform providers schema -json`). App metrics are log-based: the JSON logs carry `env` and one-line
# `queue_stats ...` / `run_finished ...` messages (src/research_assistant/scaler.py, graph.py).

locals {
  api_name      = google_cloud_run_v2_service.api.name
  api_host      = trimprefix(google_cloud_run_v2_service.api.uri, "https://")
  api_resource  = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${local.api_name}\""
  worker_filter = "resource.type = \"cloud_run_worker_pool\" AND resource.labels.worker_pool_name = \"${google_cloud_run_v2_worker_pool.worker.name}\""
  request_count = "metric.type = \"run.googleapis.com/request_count\" AND ${local.api_resource}"
  user_metric   = "logging.googleapis.com/user"
  channels      = google_monitoring_notification_channel.email[*].name
  runbook       = "Runbook: docs/RUNBOOK.md (On-call checklist). SLOs: docs/SLO.md."
}

resource "google_monitoring_notification_channel" "email" {
  count        = var.alert_email == "" ? 0 : 1
  project      = var.project_id
  display_name = "research-assistant ${var.env} on-call"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

# ---------------------------------------------------------------- uptime
resource "google_monitoring_uptime_check_config" "healthz" {
  project      = var.project_id
  display_name = "${local.prefix}-api /healthz"
  timeout      = "10s"
  period       = "60s"

  http_check {
    path         = "/healthz"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = local.api_host
    }
  }

  content_matchers {
    content = "\"status\":\"ok\""
    matcher = "CONTAINS_STRING"
  }
}

resource "google_monitoring_alert_policy" "uptime" {
  project               = var.project_id
  display_name          = "${local.prefix}: /healthz uptime check failing"
  combiner              = "OR"
  severity              = "CRITICAL"
  notification_channels = local.channels

  conditions {
    display_name = "uptime check failed from 2+ locations"
    condition_threshold {
      filter          = "metric.type = \"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type = \"uptime_url\" AND metric.labels.check_id = \"${google_monitoring_uptime_check_config.healthz.uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "120s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }
    }
  }

  documentation {
    content   = "The API's /healthz is failing from several uptime locations. ${local.runbook}"
    mime_type = "text/markdown"
  }
}

# ---------------------------------------------------------------- availability SLO + burn-rate alerts
resource "google_monitoring_custom_service" "api" {
  project      = var.project_id
  service_id   = "${local.prefix}-api"
  display_name = "research-assistant ${var.env} API"
}

resource "google_monitoring_slo" "availability" {
  project             = var.project_id
  service             = google_monitoring_custom_service.api.service_id
  slo_id              = "availability"
  display_name        = "API availability (non-5xx, kill-switch 503s excluded)"
  goal                = var.slo_availability_goal
  rolling_period_days = 30

  request_based_sli {
    good_total_ratio {
      total_service_filter = local.request_count
      bad_service_filter   = "${local.request_count} AND metric.labels.response_code_class = \"5xx\" AND metric.labels.response_code != \"503\""
    }
  }
}

# Multi-window, multi-burn-rate (Google SRE workbook): page on fast burn (1 h AND 5 min windows above 14.4x,
# i.e. 2% of the 30-day budget in an hour), ticket on slow burn (6 h AND 30 min above 6x).
resource "google_monitoring_alert_policy" "burn_fast" {
  project               = var.project_id
  display_name          = "${local.prefix}: availability SLO fast burn (page)"
  combiner              = "AND"
  severity              = "CRITICAL"
  notification_channels = local.channels

  conditions {
    display_name = "burn rate > 14.4 over 1 h"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${google_monitoring_slo.availability.name}\", \"3600s\")"
      comparison      = "COMPARISON_GT"
      threshold_value = 14.4
      duration        = "0s"
    }
  }
  conditions {
    display_name = "burn rate > 14.4 over 5 min"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${google_monitoring_slo.availability.name}\", \"300s\")"
      comparison      = "COMPARISON_GT"
      threshold_value = 14.4
      duration        = "0s"
    }
  }

  documentation {
    content   = "Error budget burning >= 14.4x (2% of the monthly budget per hour). ${local.runbook}"
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "burn_slow" {
  project               = var.project_id
  display_name          = "${local.prefix}: availability SLO slow burn (ticket)"
  combiner              = "AND"
  severity              = "WARNING"
  notification_channels = local.channels

  conditions {
    display_name = "burn rate > 6 over 6 h"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${google_monitoring_slo.availability.name}\", \"21600s\")"
      comparison      = "COMPARISON_GT"
      threshold_value = 6
      duration        = "0s"
    }
  }
  conditions {
    display_name = "burn rate > 6 over 30 min"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${google_monitoring_slo.availability.name}\", \"1800s\")"
      comparison      = "COMPARISON_GT"
      threshold_value = 6
      duration        = "0s"
    }
  }

  documentation {
    content   = "Error budget burning >= 6x (5% of the monthly budget in 6 h). ${local.runbook}"
    mime_type = "text/markdown"
  }
}

# ---------------------------------------------------------------- 5xx ratio and latency
resource "google_monitoring_alert_policy" "error_ratio" {
  project               = var.project_id
  display_name          = "${local.prefix}: API 5xx ratio > 2%"
  combiner              = "OR"
  severity              = "ERROR"
  notification_channels = local.channels

  conditions {
    display_name = "5xx / all requests > 2% for 5 min"
    condition_threshold {
      filter             = "${local.request_count} AND metric.labels.response_code_class = \"5xx\""
      denominator_filter = local.request_count
      comparison         = "COMPARISON_GT"
      threshold_value    = 0.02
      duration           = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
      denominator_aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    content   = "More than 2% of API requests return 5xx (includes kill-switch 503s: check /readyz first). ${local.runbook}"
    mime_type = "text/markdown"
  }
}

# Cloud Run's built-in request_latencies includes SSE streams (minutes long), so the latency SLO uses a
# log-based distribution over the request logs with /events excluded.
resource "google_logging_metric" "api_latency" {
  project         = var.project_id
  name            = "${local.prefix}_api_latency_non_sse"
  description     = "Latency of non-streaming API requests (request logs, /events excluded), seconds."
  filter          = "${local.api_resource} AND log_id(\"run.googleapis.com/requests\") AND NOT httpRequest.requestUrl:\"/events\""
  value_extractor = "REGEXP_EXTRACT(httpRequest.latency, \"([0-9.]+)s\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "s"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 20
      growth_factor      = 2
      scale              = 0.005
    }
  }
}

resource "google_monitoring_alert_policy" "latency_p95" {
  project               = var.project_id
  display_name          = "${local.prefix}: API p95 latency over the SLO"
  combiner              = "OR"
  severity              = "WARNING"
  notification_channels = local.channels

  conditions {
    display_name = "p95 of non-SSE requests > ${var.slo_latency_p95_s}s for 10 min"
    condition_threshold {
      filter          = "metric.type = \"${local.user_metric}/${google_logging_metric.api_latency.name}\" AND resource.type = \"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.slo_latency_p95_s
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_PERCENTILE_95"
      }
    }
  }

  documentation {
    content   = "Non-streaming API p95 latency is above ${var.slo_latency_p95_s}s. Usually Redis latency or CPU saturation. ${local.runbook}"
    mime_type = "text/markdown"
  }
}

# ---------------------------------------------------------------- saturation
resource "google_monitoring_alert_policy" "saturation" {
  project               = var.project_id
  display_name          = "${local.prefix}: instance saturation"
  combiner              = "OR"
  severity              = "WARNING"
  notification_channels = local.channels

  conditions {
    display_name = "API container CPU p99 > 85% for 10 min"
    condition_threshold {
      filter          = "metric.type = \"run.googleapis.com/container/cpu/utilizations\" AND ${local.api_resource}"
      comparison      = "COMPARISON_GT"
      threshold_value = 0.85
      duration        = "600s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_PERCENTILE_99"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }
  conditions {
    display_name = "API at max instances for 15 min"
    condition_threshold {
      filter          = "metric.type = \"run.googleapis.com/container/instance_count\" AND ${local.api_resource}"
      comparison      = "COMPARISON_GT"
      threshold_value = var.api_max_instances - 0.5
      duration        = "900s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
  conditions {
    display_name = "worker container CPU p99 > 90% for 15 min"
    condition_threshold {
      filter          = "metric.type = \"run.googleapis.com/container/cpu/utilizations\" AND ${local.worker_filter}"
      comparison      = "COMPARISON_GT"
      threshold_value = 0.9
      duration        = "900s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_PERCENTILE_99"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }
  conditions {
    display_name = "worker container memory p99 > 90% for 10 min"
    condition_threshold {
      filter          = "metric.type = \"run.googleapis.com/container/memory/utilizations\" AND ${local.worker_filter}"
      comparison      = "COMPARISON_GT"
      threshold_value = 0.9
      duration        = "600s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_PERCENTILE_99"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }

  documentation {
    content   = "Instances are saturated. API: raise api_max_instances. Worker: lower worker_max_concurrent_runs or raise worker_max_instances. ${local.runbook}"
    mime_type = "text/markdown"
  }
}

# ---------------------------------------------------------------- app-specific: queue, degraded reports, Redis
resource "google_logging_metric" "queue_depth" {
  project         = var.project_id
  name            = "${local.prefix}_queue_depth"
  description     = "Run ids waiting in ra:queue, logged every minute by the scaler job (queue_stats)."
  filter          = "jsonPayload.env = \"${var.env}\" AND jsonPayload.message =~ \"^queue_stats \""
  value_extractor = "REGEXP_EXTRACT(jsonPayload.message, \"queue_depth=([0-9]+)\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "1"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 12
      growth_factor      = 2
      scale              = 1
    }
  }
}

resource "google_logging_metric" "oldest_queued_age" {
  project         = var.project_id
  name            = "${local.prefix}_oldest_queued_age"
  description     = "Age of the oldest queued run in seconds (queue_stats, every minute)."
  filter          = "jsonPayload.env = \"${var.env}\" AND jsonPayload.message =~ \"^queue_stats \""
  value_extractor = "REGEXP_EXTRACT(jsonPayload.message, \"oldest_queued_age_s=([0-9]+)\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "s"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 14
      growth_factor      = 2
      scale              = 5
    }
  }
}

resource "google_logging_metric" "runs_finished" {
  project     = var.project_id
  name        = "${local.prefix}_runs_finished"
  description = "Finished research runs by status and whether the report was the degraded fallback (run_finished)."
  filter      = "jsonPayload.env = \"${var.env}\" AND jsonPayload.message =~ \"^run_finished \""
  label_extractors = {
    status          = "REGEXP_EXTRACT(jsonPayload.message, \"status=([a-z]+)\")"
    fallback        = "REGEXP_EXTRACT(jsonPayload.message, \"fallback=(True|False)\")"
    fallback_reason = "REGEXP_EXTRACT(jsonPayload.message, \"fallback_reason=([a-z0-9_-]+)\")"
  }

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "status"
      value_type  = "STRING"
      description = "completed | failed"
    }
    labels {
      key         = "fallback"
      value_type  = "STRING"
      description = "True when the report is the deterministic degraded fallback"
    }
    labels {
      key         = "fallback_reason"
      value_type  = "STRING"
      description = "why the writer was not used (no_findings, writer_output_invalid, ...)"
    }
  }
}

resource "google_monitoring_alert_policy" "queue" {
  project               = var.project_id
  display_name          = "${local.prefix}: run queue backlog"
  combiner              = "OR"
  severity              = "ERROR"
  notification_channels = local.channels

  conditions {
    display_name = "queue depth > ${var.alert_queue_depth} for 10 min"
    condition_threshold {
      filter          = "metric.type = \"${local.user_metric}/${google_logging_metric.queue_depth.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.alert_queue_depth
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_PERCENTILE_99"
      }
    }
  }
  conditions {
    display_name = "oldest queued run older than ${var.alert_oldest_queued_age_s}s for 10 min"
    condition_threshold {
      filter          = "metric.type = \"${local.user_metric}/${google_logging_metric.oldest_queued_age.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.alert_oldest_queued_age_s
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_PERCENTILE_99"
      }
    }
  }
  conditions {
    display_name = "no queue_stats for 10 min (scaler not running)"
    condition_absent {
      filter   = "metric.type = \"${local.user_metric}/${google_logging_metric.queue_depth.name}\""
      duration = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_COUNT"
      }
    }
  }

  documentation {
    content   = "Runs are waiting. Check the kill switch (/readyz), the scaler job executions, worker_max_instances and Groq 429s. ${local.runbook}"
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "fallback_rate" {
  project               = var.project_id
  display_name          = "${local.prefix}: degraded-report rate above the grounded-answer SLO"
  combiner              = "OR"
  severity              = "WARNING"
  notification_channels = local.channels

  conditions {
    display_name = "fallback reports / completed runs > ${var.alert_fallback_rate} over 1 h"
    condition_threshold {
      filter             = "metric.type = \"${local.user_metric}/${google_logging_metric.runs_finished.name}\" AND metric.labels.status = \"completed\" AND metric.labels.fallback = \"True\""
      denominator_filter = "metric.type = \"${local.user_metric}/${google_logging_metric.runs_finished.name}\" AND metric.labels.status = \"completed\""
      comparison         = "COMPARISON_GT"
      threshold_value    = var.alert_fallback_rate
      duration           = "0s"
      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
      denominator_aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    content   = "Too many runs end with the degraded (no-writer) report. Break down by metric label fallback_reason: writer_output_invalid / writer_model_unavailable = LLM provider issue; *_budget = budgets too tight. ${local.runbook}"
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "redis_memory" {
  project               = var.project_id
  display_name          = "${local.prefix}: Memorystore memory > 80%"
  combiner              = "OR"
  severity              = "ERROR"
  notification_channels = local.channels

  conditions {
    display_name = "memory usage ratio > 0.8 for 10 min"
    condition_threshold {
      filter          = "metric.type = \"redis.googleapis.com/stats/memory/usage_ratio\" AND resource.type = \"redis_instance\" AND resource.labels.instance_id = \"${google_redis_instance.state.id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.8
      duration        = "600s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  documentation {
    content   = "maxmemory-policy is noeviction: writes fail when Redis is full. ${local.runbook}"
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "scaler_failures" {
  project               = var.project_id
  display_name          = "${local.prefix}: scaler job executions failing"
  combiner              = "OR"
  severity              = "WARNING"
  notification_channels = local.channels

  conditions {
    display_name = "failed scaler executions in 15 min"
    condition_threshold {
      filter          = "metric.type = \"run.googleapis.com/job/completed_execution_count\" AND resource.type = \"cloud_run_job\" AND resource.labels.job_name = \"${google_cloud_run_v2_job.scaler.name}\" AND metric.labels.result = \"failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 3
      duration        = "0s"
      aggregations {
        alignment_period     = "900s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    content   = "The worker pool is not being resized. The pool keeps its last count. Check the scaler job logs. ${local.runbook}"
    mime_type = "text/markdown"
  }
}
