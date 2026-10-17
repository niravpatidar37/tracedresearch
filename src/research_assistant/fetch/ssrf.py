"""SSRF guard: scheme/port allowlist, DNS resolution, and public-IP-only checks.

The fetcher connects to the *validated IP* (DNS pinning), so a second DNS answer (rebinding) cannot
redirect the connection to an internal address. Redirect targets are re-validated on every hop.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})

# Explicit deny ranges on top of ipaddress' is_global (defence in depth; some are already non-global).
_EXTRA_DENY = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local incl. 169.254.169.254 cloud metadata
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
        "fd00:ec2::254/128",  # AWS IPv6 metadata
        "64:ff9b::/96",  # NAT64 can embed internal IPv4
    )
]

Resolver = Callable[..., Any]


class BlockedURL(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str  # IDNA/ASCII host, no brackets
    port: int
    path: str  # path + query, never empty

    @property
    def origin_url(self) -> str:
        default = 443 if self.scheme == "https" else 80
        hostpart = f"[{self.host}]" if ":" in self.host else self.host
        port = "" if self.port == default else f":{self.port}"
        return f"{self.scheme}://{hostpart}{port}{self.path}"


def is_public_ip(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        for embedded in (ip.ipv4_mapped, ip.sixtofour, (ip.teredo or (None, None))[1]):
            if embedded is not None and not is_public_ip(str(embedded)):
                return False
    if any(ip in net for net in _EXTRA_DENY):
        return False
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def parse_target(url: str) -> Target:
    if len(url) > 2048:
        raise BlockedURL("url_too_long")
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedURL(f"scheme_not_allowed:{scheme or 'none'}")
    if parts.username is not None or parts.password is not None:
        raise BlockedURL("userinfo_not_allowed")
    host = parts.hostname
    if not host:
        raise BlockedURL("missing_host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise BlockedURL("bad_port") from exc
    if port not in ALLOWED_PORTS:
        raise BlockedURL(f"port_not_allowed:{port}")
    try:
        ascii_host = host.encode("idna").decode("ascii") if not _is_ip_literal(host) else host
    except UnicodeError as exc:
        raise BlockedURL("bad_hostname") from exc
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return Target(scheme=scheme, host=ascii_host.lower(), port=port, path=path)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def resolve_public(host: str, port: int, resolver: Resolver = socket.getaddrinfo) -> str:
    """Resolve host and return one IP. Raise BlockedURL if ANY resolved address is non-public."""
    try:
        infos = resolver(host, port, 0, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise BlockedURL("dns_failure") from exc
    ips = [str(info[4][0]) for info in infos]
    if not ips:
        raise BlockedURL("dns_no_addresses")
    for ip in ips:
        if not is_public_ip(ip):
            raise BlockedURL(f"non_public_address:{ip}")
    return ips[0]
