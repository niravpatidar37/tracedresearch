"""Pluggable web search. Providers return a SearchOutcome with a structured status, never raise."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from research_assistant.fetch.fetcher import FetchOutcome, extract_main_text
from research_assistant.schemas import SearchStatus


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str
    rank: int


@dataclass
class SearchOutcome:
    status: SearchStatus
    query: str
    provider: str
    hits: list[SearchHit] = field(default_factory=list)
    detail: str = ""
    latency_ms: float = 0.0


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, max_results: int) -> SearchOutcome: ...


_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_src$)")


def normalize_url(url: str) -> str:
    """Canonical form used for de-duplication (drops fragments and tracking params)."""
    p = urlsplit(url.strip())
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k.lower())])
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), host, path, query, ""))


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


# ------------------------------------------------------------------ DuckDuckGo (default)
class DDGSearch:
    name = "ddgs"

    def __init__(self, timeout_s: int = 10) -> None:
        self.timeout_s = timeout_s

    def search(self, query: str, max_results: int) -> SearchOutcome:
        from ddgs import DDGS
        from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

        t0 = time.perf_counter()
        try:
            raw = DDGS(timeout=self.timeout_s).text(query, max_results=max_results)
        except RatelimitException:
            return SearchOutcome(SearchStatus.rate_limited, query, self.name, detail="ddgs_ratelimit")
        except TimeoutException:
            return SearchOutcome(SearchStatus.timeout, query, self.name, detail="ddgs_timeout")
        except DDGSException as exc:
            msg = str(exc).lower()
            status = SearchStatus.no_results if "no results" in msg else SearchStatus.error
            return SearchOutcome(status, query, self.name, detail=str(exc)[:200])
        except Exception as exc:  # noqa: BLE001 - third-party lib; must never kill the run
            return SearchOutcome(SearchStatus.error, query, self.name, detail=type(exc).__name__)
        hits = [
            SearchHit(
                url=str(r.get("href", "")),
                title=str(r.get("title", "")),
                snippet=str(r.get("body", "")),
                rank=i,
            )
            for i, r in enumerate(raw or [])
            if str(r.get("href", "")).startswith(("http://", "https://"))
        ]
        latency = (time.perf_counter() - t0) * 1000
        if not hits:
            return SearchOutcome(SearchStatus.no_results, query, self.name, latency_ms=latency)
        return SearchOutcome(SearchStatus.ok, query, self.name, hits, latency_ms=latency)


# ------------------------------------------------------------------ Tavily (auto when TAVILY_API_KEY set)
class TavilySearch:
    name = "tavily"
    endpoint = "https://api.tavily.com/search"

    def __init__(self, api_key: str, http: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self.http = http or httpx.Client(timeout=15.0)

    def search(self, query: str, max_results: int) -> SearchOutcome:
        t0 = time.perf_counter()
        try:
            r = self.http.post(
                self.endpoint,
                json={"query": query, "max_results": max_results, "search_depth": "basic"},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.TimeoutException:
            return SearchOutcome(SearchStatus.timeout, query, self.name, detail="timeout")
        except httpx.HTTPError as exc:
            return SearchOutcome(SearchStatus.error, query, self.name, detail=type(exc).__name__)
        latency = (time.perf_counter() - t0) * 1000
        if r.status_code in (429, 432, 433):
            return SearchOutcome(SearchStatus.rate_limited, query, self.name, detail=f"http_{r.status_code}")
        if r.status_code in (401, 403):
            return SearchOutcome(SearchStatus.blocked, query, self.name, detail=f"http_{r.status_code}")
        if r.status_code >= 400:
            return SearchOutcome(SearchStatus.error, query, self.name, detail=f"http_{r.status_code}")
        results: list[dict[str, Any]] = (r.json() or {}).get("results", [])
        hits = [
            SearchHit(
                url=str(x.get("url", "")),
                title=str(x.get("title", "")),
                snippet=str(x.get("content", "")),
                rank=i,
            )
            for i, x in enumerate(results)
            if str(x.get("url", "")).startswith(("http://", "https://"))
        ]
        if not hits:
            return SearchOutcome(SearchStatus.no_results, query, self.name, latency_ms=latency)
        return SearchOutcome(SearchStatus.ok, query, self.name, hits, latency_ms=latency)


# ------------------------------------------------------------------ Fixture corpus (tests, red-team)
class FixtureCorpus:
    """Offline corpus: a JSON file {"pages": [{"url", "title", "html"}]}.

    Used by tests and by Project 6 to inject poisoned web content WITHOUT weakening the SSRF guard.
    """

    name = "fixture"

    def __init__(self, pages: list[dict[str, str]]) -> None:
        self.pages = pages

    @classmethod
    def from_file(cls, path: str | Path) -> FixtureCorpus:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(list(data["pages"]))

    def search(self, query: str, max_results: int) -> SearchOutcome:
        words = {w for w in re.findall(r"[a-z0-9]{3,}", query.lower())}
        scored = []
        for p in self.pages:
            hay = (p.get("title", "") + " " + p.get("html", "")).lower()
            score = sum(1 for w in words if w in hay)
            if score:
                scored.append((score, p))
        scored.sort(key=lambda t: -t[0])
        hits = [
            SearchHit(p["url"], p.get("title", ""), p.get("html", "")[:160], i)
            for i, (_, p) in enumerate(scored[:max_results])
        ]
        if not hits:
            return SearchOutcome(SearchStatus.no_results, query, self.name)
        return SearchOutcome(SearchStatus.ok, query, self.name, hits)

    def fetch(self, url: str) -> FetchOutcome:
        for p in self.pages:
            if p["url"] == url:
                if p.get("status"):
                    return FetchOutcome(p["status"], url, url, detail="fixture")
                title, text = extract_main_text(p["html"], url)
                if not text:
                    text = re.sub(r"<[^>]+>", " ", p["html"]).strip()
                return FetchOutcome("ok", url, url, p.get("title", title), text, 200)
        return FetchOutcome("error", url, url, detail="not_in_fixture")


def make_search() -> SearchProvider:
    """Tavily if TAVILY_API_KEY is set, fixture corpus if SEARCH_FIXTURE_FILE is set, else DuckDuckGo."""
    if fixture := os.environ.get("SEARCH_FIXTURE_FILE"):
        return FixtureCorpus.from_file(fixture)
    if key := os.environ.get("TAVILY_API_KEY"):
        return TavilySearch(key)
    return DDGSearch()
