"""Wire settings -> store, LLM, search, fetcher, runner."""

from __future__ import annotations

import os

from research_assistant.agents.researcher import ResearchEnv
from research_assistant.config import Settings
from research_assistant.control import KillSwitch
from research_assistant.fetch.fetcher import Fetcher, SafeFetcher
from research_assistant.graph import Runner
from research_assistant.llm.factory import make_llm
from research_assistant.search.providers import FixtureCorpus, make_search
from research_assistant.store import RunStore


def build_env() -> ResearchEnv:
    search = make_search()
    fetcher: Fetcher = search if isinstance(search, FixtureCorpus) else SafeFetcher()
    return ResearchEnv(search=search, fetcher=fetcher)


def build_runner(settings: Settings, store: RunStore | None = None) -> Runner:
    store = store or RunStore.from_settings(settings)
    llm, models = make_llm(settings, store.r)
    return Runner(
        store=store,
        llm=llm,
        models=models,
        budgets=settings.budgets,
        env=build_env(),
        lease_ttl_s=settings.lease_ttl_s,
        kill_switch=KillSwitch(settings, store),
        use_queue=settings.role != "all",
    )


def search_provider_name() -> str:
    if os.environ.get("SEARCH_FIXTURE_FILE"):
        return "fixture"
    return "tavily" if os.environ.get("TAVILY_API_KEY") else "ddgs"
