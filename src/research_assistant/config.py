"""Runtime configuration, read from environment (optionally from the shared portfolio env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT = "p2-research"
DEFAULT_ENV_FILE = "F:/.secrets/portfolio.env"


def load_env() -> None:
    """Load secrets from PORTFOLIO_ENV_FILE if it exists. Never overrides already-set variables."""
    path = Path(os.environ.get("PORTFOLIO_ENV_FILE", DEFAULT_ENV_FILE))
    if path.is_file():
        load_dotenv(path, override=False)


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


ROLES = ("all", "api", "worker")
# Deployment environment (ENV). The deterministic fake LLM (LLM_PROVIDER=fake) is only allowed in FAKE_LLM_ENVS.
ENVS = ("dev", "ci", "test", "staging", "prod")
FAKE_LLM_ENVS = frozenset({"dev", "ci", "test"})
_TRUE = {"1", "true", "yes", "on"}


def _bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    """Fail fast on a typo'd enum env var instead of silently falling back."""
    value = os.environ.get(name, default).strip().lower() or default
    if value not in allowed:
        raise ValueError(f"{name}={value!r} is not one of {allowed}")
    return value


def _secret(name: str) -> str:
    """Secret-valued env var; placeholders such as '<unset>' count as unset."""
    value = os.environ.get(name, "").strip()
    return "" if value.startswith("<") else value


@dataclass(frozen=True)
class Budgets:
    """Hard limits enforced in graph code (never in prompts)."""

    max_sub_questions: int = 4
    max_searches_per_subq: int = 2
    max_pages_per_subq: int = 3
    max_results_per_domain: int = 2
    max_total_tokens: int = 40_000
    writer_token_reserve: int = 9_000
    max_wall_clock_s: float = 270.0
    max_send_backs_per_subq: int = 1
    max_run_usd: float = 0.10

    @classmethod
    def from_env(cls) -> Budgets:
        return cls(
            max_sub_questions=_int("BUDGET_MAX_SUB_QUESTIONS", cls.max_sub_questions),
            max_searches_per_subq=_int("BUDGET_MAX_SEARCHES_PER_SUBQ", cls.max_searches_per_subq),
            max_pages_per_subq=_int("BUDGET_MAX_PAGES_PER_SUBQ", cls.max_pages_per_subq),
            max_results_per_domain=_int("BUDGET_MAX_RESULTS_PER_DOMAIN", cls.max_results_per_domain),
            max_total_tokens=_int("BUDGET_MAX_TOTAL_TOKENS", cls.max_total_tokens),
            writer_token_reserve=_int("BUDGET_WRITER_TOKEN_RESERVE", cls.writer_token_reserve),
            max_wall_clock_s=_float("BUDGET_MAX_WALL_CLOCK_S", cls.max_wall_clock_s),
            max_send_backs_per_subq=_int("BUDGET_MAX_SEND_BACKS_PER_SUBQ", cls.max_send_backs_per_subq),
            max_run_usd=_float("BUDGET_MAX_RUN_USD", cls.max_run_usd),
        )


@dataclass(frozen=True)
class Models:
    """Role -> model mapping. Big model where reasoning quality matters, small where volume matters."""

    planner: str = "openai/gpt-oss-120b"
    writer: str = "openai/gpt-oss-120b"
    researcher: str = "openai/gpt-oss-20b"
    supervisor: str = "openai/gpt-oss-20b"

    @classmethod
    def from_env(cls) -> Models:
        return cls(
            planner=os.environ.get("PLANNER_MODEL", cls.planner),
            writer=os.environ.get("WRITER_MODEL", cls.writer),
            researcher=os.environ.get("RESEARCHER_MODEL", cls.researcher),
            supervisor=os.environ.get("SUPERVISOR_MODEL", cls.supervisor),
        )


@dataclass(frozen=True)
class Settings:
    redis_url: str = "redis://127.0.0.1:16379/0"
    llm_provider: str = "groq"
    spend_cap_usd: float = 25.0
    project_cap_usd: float = 3.0
    ledger_path: str = "F:/.llm-spend/ledger.jsonl"
    cache_dir: str = ".cache/llm"
    api_keys: tuple[str, ...] = ()
    rate_limit_per_min: int = 10
    max_concurrent_runs: int = 2
    lease_ttl_s: int = 15
    budgets: Budgets = field(default_factory=Budgets)
    models: Models = field(default_factory=Models)
    # --- deployment (Cloud Run) ---------------------------------------------------------------
    # all = API + in-process workers (local dev, tests); api = HTTP only, enqueues runs;
    # worker = consumes the Redis queue, serves only /healthz + /readyz (Cloud Run worker pool).
    role: str = "all"
    redis_password: str = ""  # Memorystore AUTH string (Secret Manager); kept out of REDIS_URL
    redis_ca_cert_pem: str = ""  # Memorystore server CA (public cert) for rediss:// verification
    kill_switch: bool = False  # AGENT_KILL_SWITCH: refuse new runs, pause runs before any LLM/tool call
    shutdown_grace_s: float = 6.0  # SIGTERM -> checkpoint/requeue window (Cloud Run gives 10 s)
    orphan_grace_s: float = 30.0  # never-started runs are only "orphaned" after this long in the queue
    spend_backend: str = "file"  # file = shared local JSONL ledger; redis = per-deployment monthly counters
    cache_backend: str = "disk"  # disk | redis (shared, TTL) | off
    cache_ttl_s: int = 86_400
    env: str = "dev"  # ENV: dev | ci | test | staging | prod (prod/staging refuse the fake LLM)

    def __post_init__(self) -> None:
        if self.llm_provider == "fake" and self.env not in FAKE_LLM_ENVS:
            raise ValueError(f"LLM_PROVIDER=fake is refused when ENV={self.env} (allowed: {sorted(FAKE_LLM_ENVS)})")

    @classmethod
    def from_env(cls) -> Settings:
        load_env()
        # Keys shorter than 16 chars (or placeholders like "<unset>") are ignored: the API then fails closed.
        # RESEARCH_SMOKE_API_KEY: a separate key (own Secret Manager secret) used only by the deploy smoke test.
        raw = [*os.environ.get("RESEARCH_API_KEYS", "").split(","), os.environ.get("RESEARCH_SMOKE_API_KEY", "")]
        keys = tuple(k.strip() for k in raw if len(k.strip()) >= 16 and not k.strip().startswith("<"))
        provider = os.environ.get("LLM_PROVIDER", "groq")
        return cls(
            redis_url=os.environ.get("REDIS_URL", cls.redis_url),
            llm_provider=provider,
            spend_cap_usd=_float("LLM_SPEND_CAP_USD", cls.spend_cap_usd),
            project_cap_usd=_float("P2_PROJECT_CAP_USD", cls.project_cap_usd),
            ledger_path=os.environ.get("LLM_SPEND_LEDGER", cls.ledger_path),
            cache_dir=os.environ.get("LLM_CACHE_DIR", cls.cache_dir),
            api_keys=keys,
            rate_limit_per_min=_int("RATE_LIMIT_PER_MIN", cls.rate_limit_per_min),
            max_concurrent_runs=_int("MAX_CONCURRENT_RUNS", cls.max_concurrent_runs),
            lease_ttl_s=_int("RUN_LEASE_TTL_S", cls.lease_ttl_s),
            budgets=Budgets.from_env(),
            models=Models.from_env(),
            role=_choice("SERVICE_ROLE", cls.role, ROLES),
            redis_password=_secret("REDIS_PASSWORD"),
            redis_ca_cert_pem=_secret("REDIS_CA_CERT_PEM"),
            kill_switch=_bool("AGENT_KILL_SWITCH"),
            shutdown_grace_s=_float("SHUTDOWN_GRACE_S", cls.shutdown_grace_s),
            orphan_grace_s=_float("ORPHAN_GRACE_S", cls.orphan_grace_s),
            spend_backend=_choice("LLM_SPEND_BACKEND", cls.spend_backend, ("file", "redis")),
            cache_backend=_choice("LLM_CACHE_BACKEND", cls.cache_backend, ("disk", "redis", "off")),
            cache_ttl_s=_int("LLM_CACHE_TTL_S", cls.cache_ttl_s),
            env=_choice("ENV", cls.env, ENVS),
        )
