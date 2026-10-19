"""Shared test helpers: fixture corpus, recorded/fake LLM responders, runner factory."""

from __future__ import annotations

from typing import Any

import fakeredis

from research_assistant.agents.researcher import ResearchEnv
from research_assistant.config import Budgets, Models
from research_assistant.graph import Runner
from research_assistant.llm.base import LLMRequest
from research_assistant.llm.fake import FakeLLM
from research_assistant.llm.scripted import (  # noqa: F401 - re-exported for tests
    FID_RE,
    PAGE_RE,
    extraction_responder,
    plan_responder,
    review_accept,
    writer_responder,
)
from research_assistant.search.providers import FixtureCorpus
from research_assistant.store import RunStore

MODELS = Models()


def page(url: str, title: str, *paras: str) -> dict[str, str]:
    body = "".join(f"<p>{p}</p>" for p in paras)
    return {
        "url": url,
        "title": title,
        "html": f"<html><head><title>{title}</title></head><body><article>"
        f"<h1>{title}</h1>{body}</article></body></html>",
    }


CORPUS = [
    page(
        "https://energy.example.org/solar-efficiency",
        "Solar panel efficiency records",
        "The record efficiency for a single-junction silicon solar cell reached 26.8 percent in 2024.",
        "Commercial monocrystalline solar panels typically convert between 20 and 23 percent of sunlight into "
        "electricity, according to industry testing laboratories.",
        "Perovskite tandem solar cells have exceeded 33 percent efficiency in laboratory conditions.",
    ),
    page(
        "https://news.example.com/perovskite",
        "Perovskite tandem cells break records",
        "Researchers reported perovskite-silicon tandem solar cells with 33.9 percent efficiency in a certified "
        "laboratory test, a new record for the technology.",
        "Durability remains the main obstacle, since perovskite layers degrade faster than silicon under heat "
        "and humidity in outdoor conditions.",
    ),
    page(
        "https://news.example.com/solar-costs",
        "Solar module prices fall",
        "Average solar module prices fell below 0.15 dollars per watt in 2024, driven by manufacturing "
        "overcapacity in China.",
        "Cheaper modules have shifted the cost of solar installations toward labor, permitting and inverters.",
    ),
    page(
        "https://news.example.com/solar-third",
        "Third page same domain",
        "This third page on the same news domain discusses solar panel efficiency and module prices in detail "
        "to test the per-domain cap of the researcher.",
    ),
    page(
        "https://lab.example.net/silicon",
        "Silicon solar cell physics",
        "The Shockley-Queisser limit sets a theoretical maximum efficiency of about 33.7 percent for a "
        "single-junction solar cell under standard sunlight.",
        "Silicon cells lose efficiency as temperature rises, typically around 0.4 percent per degree Celsius.",
    ),
]


def corpus(extra: list[dict[str, str]] | None = None) -> FixtureCorpus:
    return FixtureCorpus([*CORPUS, *(extra or [])])


def review_reject(req: LLMRequest) -> dict[str, Any]:
    return {"answered": False, "missing": "more detail", "follow_up_query": "solar efficiency detail"}


def default_llm(**overrides: Any) -> FakeLLM:
    responses: dict[str, Any] = {
        "PlanOutput": plan_responder(2),
        "ExtractionOutput": extraction_responder,
        "ReviewOutput": review_accept,
        "ReportDraft": writer_responder,
    }
    responses.update(overrides)
    return FakeLLM(responses)


def make_store() -> RunStore:
    return RunStore(fakeredis.FakeRedis(decode_responses=True))


def make_runner(
    llm: FakeLLM | None = None,
    budgets: Budgets | None = None,
    store: RunStore | None = None,
    env: ResearchEnv | None = None,
    **kw: Any,
) -> Runner:
    c = corpus()
    return Runner(
        store=store or make_store(),
        llm=llm or default_llm(),
        models=MODELS,
        budgets=budgets or Budgets(),
        env=env or ResearchEnv(search=c, fetcher=c),
        sleep=lambda s: None,
        **kw,
    )
