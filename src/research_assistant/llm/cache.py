"""Content-hash disk cache for LLM responses so reruns are free."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    import redis


class Cache(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...

    def set(self, key: str, value: dict[str, Any]) -> None: ...


class DiskCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @staticmethod
    def key(payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        p = self._path(key)
        if not p.is_file():
            return None
        try:
            data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data

    def set(self, key: str, value: dict[str, Any]) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)


class RedisCache:
    """Shared LLM cache for the deployed service.

    Cloud Run's filesystem is in-memory and per instance, so a disk cache would both eat the memory limit and
    be lost when a preempted run resumes on another instance. Entries expire after `ttl_s`.
    """

    def __init__(self, client: redis.Redis, ttl_s: int = 86_400) -> None:
        self.r = client
        self.ttl_s = ttl_s

    @staticmethod
    def _k(key: str) -> str:
        return f"ra:llmcache:{key}"

    def get(self, key: str) -> dict[str, Any] | None:
        raw = self.r.get(self._k(key))
        if not raw:
            return None
        try:
            data: dict[str, Any] = json.loads(cast(str, raw))
        except json.JSONDecodeError:
            return None
        return data

    def set(self, key: str, value: dict[str, Any]) -> None:
        self.r.set(self._k(key), json.dumps(value, ensure_ascii=False), ex=self.ttl_s)
