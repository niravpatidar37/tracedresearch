"""HTTP hardening as plain ASGI middleware (safe for SSE streaming responses).

- SecurityHeadersMiddleware: adds conservative security headers to every HTTP response (JSON API, no HTML).
- BodySizeLimitMiddleware: rejects request bodies over `max_bytes` with 413 before the app parses them, for both
  Content-Length and chunked uploads.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

SECURITY_HEADERS: dict[str, str] = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "geolocation=(), camera=(), microphone=()",
    "cache-control": "no-store",
}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.decode("latin-1").lower() for k, _ in headers}
                for k, v in SECURITY_HEADERS.items():
                    if k not in present:  # the SSE route sets its own Cache-Control
                        headers.append((k.encode("latin-1"), v.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _reject(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    headers += [(k.encode("latin-1"), v.encode("latin-1")) for k, v in SECURITY_HEADERS.items()]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


DRAIN_LIMIT = 1024 * 1024  # bytes read and discarded before answering 413 (beyond this the client may see a reset)


async def _drain(receive: Receive, limit: int) -> None:
    seen = 0
    while seen <= limit:
        message = await receive()
        if message["type"] != "http.request":
            return
        seen += len(message.get("body", b""))
        if not message.get("more_body", False):
            return


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS", "DELETE"):
            await self.app(scope, receive, send)
            return
        declared = None
        for k, v in scope.get("headers", []):
            if k.lower() == b"content-length":
                declared = v
        if declared is not None:
            try:
                too_big = int(declared) > self.max_bytes
            except ValueError:
                await _reject(send, 400, "invalid Content-Length")
                return
            if too_big:
                await _drain(receive, limit=DRAIN_LIMIT)  # so ordinary clients read the 413, not a reset
                await _reject(send, 413, f"request body too large (max {self.max_bytes} bytes)")
                return
        # Chunked (or lying) bodies: buffer up to the limit, then replay to the app.
        chunks: list[bytes] = []
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await _reject(send, 413, f"request body too large (max {self.max_bytes} bytes)")
                return
            chunks.append(chunk)
            more = bool(message.get("more_body", False))
        body = b"".join(chunks)
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
