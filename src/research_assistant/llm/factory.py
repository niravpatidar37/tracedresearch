"""Build the configured LLM client. Claude only activates when ANTHROPIC_API_KEY is present."""

from __future__ import annotations

import os
from pathlib import Path

from research_assistant.config import PROJECT, Models, Settings
from research_assistant.llm.anthropic_client import PLACEHOLDER_MODEL, AnthropicClient
from research_assistant.llm.base import LLMClient, LLMError
from research_assistant.llm.cache import DiskCache
from research_assistant.llm.openai_compat import OpenAICompatClient
from research_assistant.spend import SpendLedger


def make_ledger(settings: Settings) -> SpendLedger:
    return SpendLedger(
        path=Path(settings.ledger_path),
        project=PROJECT,
        global_cap_usd=settings.spend_cap_usd,
        project_cap_usd=settings.project_cap_usd,
    )


def make_llm(settings: Settings) -> tuple[LLMClient, Models]:
    ledger = make_ledger(settings)
    cache = DiskCache(settings.cache_dir)
    provider = settings.llm_provider
    if provider == "anthropic" or (provider == "auto" and os.environ.get("ANTHROPIC_API_KEY")):
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
        big = os.environ.get("ANTHROPIC_MODEL_BIG", PLACEHOLDER_MODEL)
        small = os.environ.get("ANTHROPIC_MODEL_SMALL", PLACEHOLDER_MODEL)
        return AnthropicClient(api_key=key, ledger=ledger, cache=cache), Models(big, big, small, small)
    if provider == "ollama":
        model = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
        client = OpenAICompatClient(
            provider="ollama",
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"),
            api_key=None,
            ledger=None,
            cache=cache,
            request_timeout_s=300.0,
        )
        return client, Models(model, model, model, model)
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise LLMError("GROQ_API_KEY is not set (or choose LLM_PROVIDER=ollama)")
    client = OpenAICompatClient(
        provider="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key=key,
        ledger=ledger,
        cache=cache,
    )
    return client, settings.models
