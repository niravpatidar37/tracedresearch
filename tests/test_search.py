"""Search providers: statuses instead of exceptions, Tavily auto-selection, URL normalisation."""

from __future__ import annotations

import httpx
import pytest

from research_assistant.schemas import SearchStatus
from research_assistant.search import providers
from research_assistant.search.providers import DDGSearch, TavilySearch, domain_of, make_search, normalize_url


def test_normalize_url_dedupes_tracking_and_www() -> None:
    a = normalize_url("https://www.Example.com/a/?utm_source=x&id=3#frag")
    b = normalize_url("https://example.com/a?id=3")
    assert a == b
    assert domain_of("https://www.news.example.com/x") == "news.example.com"


@pytest.mark.parametrize(
    "exc_name,status",
    [
        ("RatelimitException", SearchStatus.rate_limited),
        ("TimeoutException", SearchStatus.timeout),
        ("DDGSException", SearchStatus.error),
    ],
)
def test_ddgs_exceptions_become_statuses(monkeypatch: pytest.MonkeyPatch, exc_name: str, status: SearchStatus) -> None:
    import ddgs
    import ddgs.exceptions as ex

    exc = getattr(ex, exc_name)

    class Boom:
        def __init__(self, *a, **k):  # type: ignore[no-untyped-def]
            pass

        def text(self, *a, **k):  # type: ignore[no-untyped-def]
            raise exc("boom")

    monkeypatch.setattr(ddgs, "DDGS", Boom)
    out = DDGSearch().search("q", 5)
    assert out.status == status and out.hits == []


def test_ddgs_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    import ddgs

    class Empty:
        def __init__(self, *a, **k):  # type: ignore[no-untyped-def]
            pass

        def text(self, *a, **k):  # type: ignore[no-untyped-def]
            return [{"href": "javascript:alert(1)", "title": "x", "body": "y"}]

    monkeypatch.setattr(ddgs, "DDGS", Empty)
    assert DDGSearch().search("q", 5).status == SearchStatus.no_results  # non-http(s) hits are dropped


@pytest.mark.parametrize(
    "code,status",
    [
        (429, SearchStatus.rate_limited),
        (432, SearchStatus.rate_limited),
        (401, SearchStatus.blocked),
        (500, SearchStatus.error),
    ],
)
def test_tavily_status_mapping(code: int, status: SearchStatus) -> None:
    t = TavilySearch("k", http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(code))))
    assert t.search("q", 5).status == status


def test_tavily_ok_and_auth_header() -> None:
    seen = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"results": [{"url": "https://a.example/x", "title": "A", "content": "c"}]})

    out = TavilySearch("secret", http=httpx.Client(transport=httpx.MockTransport(h))).search("q", 5)
    assert out.status == SearchStatus.ok and out.hits[0].url == "https://a.example/x"
    assert seen[0].headers["authorization"] == "Bearer secret"


def test_provider_auto_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARCH_FIXTURE_FILE", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert isinstance(make_search(), providers.DDGSearch)
    monkeypatch.setenv("TAVILY_API_KEY", "x")
    assert isinstance(make_search(), providers.TavilySearch)
