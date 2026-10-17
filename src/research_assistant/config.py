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

    @classmethod
    def from_env(cls) -> Settings:
        load_env()
        keys = tuple(k.strip() for k in os.environ.get("RESEARCH_API_KEYS", "").split(",") if k.strip())
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
        )
