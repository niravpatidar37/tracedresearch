"""Versioned keyspace migrations for the Redis run store.

Redis has no DDL, but the stored JSON and key layout are a schema. Each change ships as a numbered module
`mNNNN_<name>.py` exposing `VERSION`, `DESCRIPTION` and `up(r)`. `research-assistant migrate` applies
pending ones in order under a lock and records each in the `ra:schema:migrations` hash (the "migrations
table"). Migrations never run at import or app startup: in Cloud Run they run as a Cloud Run Job before
the new revision is deployed. /readyz reports not-ready until the store is at REQUIRED_VERSION.
"""

from __future__ import annotations

import importlib
import pkgutil
import time
from dataclasses import dataclass
from types import ModuleType
from typing import cast

import redis

SCHEMA_VERSION_KEY = "ra:schema:version"
MIGRATIONS_KEY = "ra:schema:migrations"
LOCK_KEY = "ra:schema:lock"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    description: str
    module: ModuleType

    def up(self, r: redis.Redis) -> None:
        self.module.up(r)


def discover() -> list[Migration]:
    out: list[Migration] = []
    for info in pkgutil.iter_modules(__path__):
        if not info.name.startswith("m") or not info.name[1:5].isdigit():
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        out.append(Migration(int(mod.VERSION), info.name, str(mod.DESCRIPTION), mod))
    out.sort(key=lambda m: m.version)
    versions = [m.version for m in out]
    if versions != list(range(1, len(out) + 1)):
        raise RuntimeError(f"migration versions must be contiguous from 1, got {versions}")
    return out


REQUIRED_VERSION = len(discover())


def current_version(r: redis.Redis) -> int:
    raw = r.get(SCHEMA_VERSION_KEY)
    return int(cast(str, raw)) if raw else 0


def pending(r: redis.Redis) -> list[Migration]:
    cur = current_version(r)
    return [m for m in discover() if m.version > cur]


def migrate(r: redis.Redis, lock_ttl_s: int = 300) -> list[str]:
    """Apply pending migrations in order. Safe to run concurrently (lock) and repeatedly (idempotent)."""
    if not r.set(LOCK_KEY, str(time.time()), nx=True, ex=lock_ttl_s):
        raise RuntimeError("another migration is running (ra:schema:lock held)")
    applied: list[str] = []
    try:
        for m in pending(r):
            m.up(r)
            pipe = r.pipeline(transaction=True)
            pipe.hset(
                MIGRATIONS_KEY, f"{m.version:04d}", f"{m.name} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
            )
            pipe.set(SCHEMA_VERSION_KEY, m.version)
            pipe.execute()
            applied.append(m.name)
    finally:
        r.delete(LOCK_KEY)
    return applied
