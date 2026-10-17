"""SSRF guard tests. Each would fail if the corresponding control were removed."""

from __future__ import annotations

import socket

import httpx
import pytest

from research_assistant.fetch.fetcher import SafeFetcher
from research_assistant.fetch.ssrf import BlockedURL, is_public_ip, parse_target, resolve_public

PUBLIC_IP = "93.184.215.14"
HTML = (
    "<html><body><article>" + "<p>" + ("Public page sentence about solar power. " * 20) + "</p></article></body></html>"
)


def resolver_for(mapping: dict[str, list[str]]):  # type: ignore[no-untyped-def]
    def resolve(host, port, *a, **k):  # type: ignore[no-untyped-def]
        if host not in mapping:
            raise socket.gaierror("nx")
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
            for ip in mapping[host]
        ]

    return resolve


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "127.8.9.10",
        "10.1.2.3",
        "172.16.5.4",
        "192.168.1.1",
        "169.254.169.254",
        "169.254.1.1",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "fe80::1",
        "fc00::1",
        "fd00:ec2::254",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "64:ff9b::a9fe:a9fe",
        "2002:7f00:1::1",
        "::",
        # only caught by ipaddress.is_global (not in the explicit deny list): documentation ranges
        "192.0.2.10",
        "198.51.100.7",
        "203.0.113.9",
        "2001:db8::1",
    ],
)
def test_non_public_ips_rejected(ip: str) -> None:
    assert not is_public_ip(ip)


@pytest.mark.parametrize("ip", ["93.184.215.14", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_ips_allowed(ip: str) -> None:
    assert is_public_ip(ip)


@pytest.mark.parametrize(
    "url,reason",
    [
        ("file:///etc/passwd", "scheme_not_allowed"),
        ("gopher://example.com/", "scheme_not_allowed"),
        ("ftp://example.com/x", "scheme_not_allowed"),
        ("javascript:alert(1)", "scheme_not_allowed"),
        ("http://user:pw@example.com/", "userinfo_not_allowed"),
        ("http://example.com:6379/", "port_not_allowed"),
        ("http://example.com:22/", "port_not_allowed"),
        ("http:///nohost", "missing_host"),
    ],
)
def test_parse_target_rejects(url: str, reason: str) -> None:
    with pytest.raises(BlockedURL) as ei:
        parse_target(url)
    assert ei.value.reason.startswith(reason)


def test_resolution_blocks_private_and_mixed_answers() -> None:
    r = resolver_for(
        {
            "internal.example": ["10.0.0.5"],
            "mixed.example": [PUBLIC_IP, "127.0.0.1"],
            "ok.example": [PUBLIC_IP],
        }
    )
    with pytest.raises(BlockedURL):
        resolve_public("internal.example", 80, r)
    with pytest.raises(BlockedURL):
        resolve_public("mixed.example", 80, r)  # any non-public answer blocks (rebinding defence)
    assert resolve_public("ok.example", 80, r) == PUBLIC_IP


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "2130706433", "0x7f000001"])
def test_real_resolver_blocks_loopback_spellings(host: str) -> None:
    try:
        with pytest.raises(BlockedURL):
            resolve_public(host, 80)
    except socket.gaierror:
        pytest.skip("resolver does not parse this spelling")


def _fetcher(handler, mapping):  # type: ignore[no-untyped-def]
    return SafeFetcher(resolver=resolver_for(mapping), transport=httpx.MockTransport(handler), max_bytes=50_000)


def test_connects_to_pinned_ip_with_host_header_and_sni() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=HTML)

    out = _fetcher(handler, {"site.example": [PUBLIC_IP]}).fetch("https://site.example/a?b=1")
    assert out.status == "ok" and "solar power" in out.text
    assert seen[0].url.host == PUBLIC_IP  # DNS pinned: no second lookup by the HTTP stack
    assert seen[0].headers["host"] == "site.example"
    assert seen[0].extensions.get("sni_hostname") == "site.example"


@pytest.mark.parametrize(
    "location",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://internal.example/admin",
        "http://[::1]/",
        "file:///etc/passwd",
        "http://site.example:8080/",
    ],
)
def test_redirects_are_revalidated(location: str) -> None:
    hits: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        if req.url.host == PUBLIC_IP:
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="SECRET")

    out = _fetcher(handler, {"site.example": [PUBLIC_IP], "internal.example": ["10.0.0.7"]}).fetch(
        "http://site.example/"
    )
    assert out.status == "blocked"
    assert len(hits) == 1  # the internal target was never contacted


def test_redirect_cap() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "/again"})

    out = _fetcher(handler, {"site.example": [PUBLIC_IP]}).fetch("http://site.example/")
    assert out.status == "blocked" and out.detail == "too_many_redirects"


def test_content_type_allowlist() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")

    assert _fetcher(handler, {"s.example": [PUBLIC_IP]}).fetch("http://s.example/").status == "unsupported"


def test_size_cap_declared_and_streamed() -> None:
    big = b"<p>" + b"a" * 60_000 + b"</p>"

    def declared(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html", "content-length": str(len(big))}, content=big)

    def streamed(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=iter([big[:30_000], big[30_000:]]))

    assert _fetcher(declared, {"s.example": [PUBLIC_IP]}).fetch("http://s.example/").status == "too_large"
    assert _fetcher(streamed, {"s.example": [PUBLIC_IP]}).fetch("http://s.example/").status == "too_large"


@pytest.mark.parametrize(
    "code,status",
    [
        (429, "rate_limited"),
        (402, "paywalled"),
        (401, "paywalled"),
        (403, "blocked"),
        (500, "error"),
        (404, "error"),
    ],
)
def test_http_status_mapping(code: int, status: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(code, headers={"content-type": "text/html"}, text="x")

    assert _fetcher(handler, {"s.example": [PUBLIC_IP]}).fetch("http://s.example/").status == status


def test_timeout_is_a_status_not_an_exception() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    assert _fetcher(handler, {"s.example": [PUBLIC_IP]}).fetch("http://s.example/").status == "timeout"


def test_paywall_detection() -> None:
    html = "<html><body><p>Subscribe to continue reading this article.</p></body></html>"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    assert _fetcher(handler, {"s.example": [PUBLIC_IP]}).fetch("http://s.example/").status == "paywalled"


def test_dns_failure_is_blocked_status() -> None:
    out = _fetcher(lambda r: httpx.Response(200), {}).fetch("http://nx.example/")
    assert out.status == "blocked" and out.detail == "dns_failure"


@pytest.mark.parametrize(
    "host,ip",
    [
        ("metadata.google.internal", "169.254.169.254"),  # Cloud Run / GCE metadata server (SA tokens)
        ("metadata", "169.254.169.254"),
        ("metadata.google.internal", "fd20:ce::254"),  # GCP IPv6 metadata address
        ("redis.internal.example", "10.20.0.3"),  # a Memorystore-style private address in the VPC
    ],
)
def test_cloud_metadata_and_vpc_targets_are_blocked(host: str, ip: str) -> None:
    """On Cloud Run the metadata server is always reachable from the container and the VPC routes private
    ranges to Memorystore, so this in-code guard is the control (network egress rules cannot express it)."""
    hits: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return httpx.Response(200, headers={"content-type": "text/html"}, text="TOKEN")

    out = _fetcher(handler, {host: [ip]}).fetch(f"http://{host}/computeMetadata/v1/instance/service-accounts/")
    assert out.status == "blocked" and hits == []
