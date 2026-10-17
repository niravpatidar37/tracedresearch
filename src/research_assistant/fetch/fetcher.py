"""Safe page fetcher + main-text extraction. Every failure is a structured status, never an exception."""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass
from typing import Protocol

import httpx
import trafilatura

from research_assistant.fetch.ssrf import BlockedURL, Resolver, Target, parse_target, resolve_public

ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
USER_AGENT = "Mozilla/5.0 (compatible; research-assistant/0.1; portfolio project)"
_PAYWALL_HINTS = re.compile(
    r"(subscribe to (continue|read)|subscribers only|paywall|already a subscriber|sign in to read|"
    r"create a free account to continue|this content is for (paid )?(members|subscribers))",
    re.IGNORECASE,
)


@dataclass
class FetchOutcome:
    status: str  # ok | blocked | timeout | rate_limited | paywalled | unsupported | too_large | empty | error
    url: str
    final_url: str = ""
    title: str = ""
    text: str = ""
    http_status: int = 0
    detail: str = ""
    latency_ms: float = 0.0


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchOutcome: ...


def extract_main_text(html: str, url: str) -> tuple[str, str]:
    """Return (title, main_text) using trafilatura; empty text if nothing extractable."""
    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True, favor_precision=True)
    title = ""
    meta = trafilatura.extract_metadata(html)
    if meta is not None and meta.title:
        title = str(meta.title)
    return title[:300], (text or "").strip()


class SafeFetcher:
    def __init__(
        self,
        *,
        resolver: Resolver = socket.getaddrinfo,
        transport: httpx.BaseTransport | None = None,
        max_redirects: int = 3,
        max_bytes: int = 2_000_000,
        timeout_s: float = 10.0,
        total_timeout_s: float = 15.0,
    ) -> None:
        self.resolver = resolver
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        self.total_timeout_s = total_timeout_s
        self.client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            follow_redirects=False,  # we follow manually so every hop is re-validated
            trust_env=False,  # env proxies would bypass DNS pinning
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8"},
        )

    def fetch(self, url: str) -> FetchOutcome:
        t0 = time.monotonic()
        deadline = t0 + self.total_timeout_s
        current = url
        try:
            for _hop in range(self.max_redirects + 1):
                target = parse_target(current)
                ip = resolve_public(target.host, target.port, self.resolver)
                outcome, location = self._one_hop(target, ip, url, deadline)
                if location is None:
                    outcome.latency_ms = (time.monotonic() - t0) * 1000
                    return outcome
                current = str(httpx.URL(target.origin_url).join(location))
            return self._done(FetchOutcome("blocked", url, current, detail="too_many_redirects"), t0)
        except BlockedURL as exc:
            return self._done(FetchOutcome("blocked", url, current, detail=exc.reason), t0)
        except httpx.TimeoutException:
            return self._done(FetchOutcome("timeout", url, current, detail="timeout"), t0)
        except TimeoutError:
            return self._done(FetchOutcome("timeout", url, current, detail="total_timeout"), t0)
        except httpx.HTTPError as exc:
            return self._done(FetchOutcome("error", url, current, detail=type(exc).__name__), t0)

    @staticmethod
    def _done(o: FetchOutcome, t0: float) -> FetchOutcome:
        o.latency_ms = (time.monotonic() - t0) * 1000
        return o

    def _one_hop(self, target: Target, ip: str, original: str, deadline: float) -> tuple[FetchOutcome, str | None]:
        ip_host = f"[{ip}]" if ":" in ip else ip
        default = 443 if target.scheme == "https" else 80
        port = "" if target.port == default else f":{target.port}"
        pinned_url = f"{target.scheme}://{ip_host}{port}{target.path}"
        host_header = target.host if not port else f"{target.host}{port}"
        logical = target.origin_url
        with self.client.stream(
            "GET",
            pinned_url,
            headers={"Host": host_header},
            extensions={"sni_hostname": target.host},
        ) as r:
            code = r.status_code
            if code in REDIRECT_CODES:
                loc = r.headers.get("location")
                if not loc:
                    return FetchOutcome(
                        "error", original, logical, http_status=code, detail="redirect_no_location"
                    ), None
                return FetchOutcome("ok", original, logical), loc
            if code == 429:
                return FetchOutcome("rate_limited", original, logical, http_status=code), None
            if code in (401, 402):
                return FetchOutcome("paywalled", original, logical, http_status=code), None
            if code == 403:
                return FetchOutcome("blocked", original, logical, http_status=code, detail="http_403"), None
            if code >= 400:
                return FetchOutcome("error", original, logical, http_status=code, detail=f"http_{code}"), None
            ctype = r.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if ctype not in ALLOWED_CONTENT_TYPES:
                return FetchOutcome("unsupported", original, logical, http_status=code, detail=ctype or "none"), None
            declared = r.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.max_bytes:
                return FetchOutcome("too_large", original, logical, http_status=code, detail="content_length"), None
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > self.max_bytes:
                    return FetchOutcome("too_large", original, logical, http_status=code, detail="stream"), None
                if time.monotonic() > deadline:
                    raise TimeoutError
            charset = r.charset_encoding or "utf-8"
        try:
            body = buf.decode(charset, errors="replace")
        except LookupError:
            body = buf.decode("utf-8", errors="replace")
        if ctype == "text/plain":
            title, text = "", body.strip()
        else:
            title, text = extract_main_text(body, logical)
        if len(text) < 200:
            status = "paywalled" if _PAYWALL_HINTS.search(body) else "empty"
            return FetchOutcome(status, original, logical, title, text, code, detail="little_text"), None
        if _PAYWALL_HINTS.search(text[:3000]) and len(text) < 1500:
            return FetchOutcome("paywalled", original, logical, title, text, code, detail="paywall_hint"), None
        return FetchOutcome("ok", original, logical, title, text, code), None
