"""USD spend ledgers with a hard cap (global + per project), checked before every paid call.

SpendLedger (local default): append-only JSONL shared by the six portfolio projects, one line per paid call
{ts, project, provider, model, input_tokens, output_tokens, usd}; a file lock serialises concurrent writers.

RedisSpendLedger (deployed): Cloud Run instances have no shared disk, so a per-instance file would reset
the cap on every new instance. The deployed service keeps monthly (UTC calendar month) counters in Redis
plus an audit stream with the same record shape.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

import portalocker

if TYPE_CHECKING:
    import redis


class BudgetExceeded(RuntimeError):
    """A hard budget would be exceeded. Raised *before* the expensive action happens."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def load_pricing() -> dict[str, dict[str, Any]]:
    text = resources.files("research_assistant").joinpath("pricing.json").read_text(encoding="utf-8")
    data: dict[str, dict[str, Any]] = json.loads(text)
    return data


_PRICING = load_pricing()


def price_for(provider: str, model: str) -> tuple[float, float]:
    """USD per 1M tokens (input, output). Unknown models fail closed with a high price."""
    table = _PRICING.get(provider, {})
    entry = table.get(model) or table.get("*")
    if not isinstance(entry, dict):
        return 15.0, 75.0
    return float(entry["input"]), float(entry["output"])


def cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = price_for(provider, model)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


def estimate_tokens(text: str) -> int:
    """Cheap, conservative token estimate (~3.5 chars/token, rounded up)."""
    return math.ceil(len(text) / 3.5) + 8


def enforce_caps(
    global_total: float,
    project_total: float,
    estimate_usd: float,
    global_cap: float,
    project_cap: float,
    window: str = "",
) -> None:
    """Refuse (raise BudgetExceeded) if the worst-case estimate would breach either cap. Shared by both ledgers."""
    g, p = global_total, project_total
    if g + estimate_usd > global_cap:
        raise BudgetExceeded(
            "global_usd", f"global spend {g:.4f} + estimate {estimate_usd:.4f} > cap {global_cap:.2f} USD{window}"
        )
    if p + estimate_usd > project_cap:
        raise BudgetExceeded(
            "project_usd", f"project spend {p:.4f} + estimate {estimate_usd:.4f} > cap {project_cap:.2f} USD{window}"
        )


@dataclass
class SpendLedger:
    path: Path
    project: str
    global_cap_usd: float
    project_cap_usd: float

    def _lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def totals(self) -> tuple[float, float]:
        """(global_total, project_total) in USD."""
        if not self.path.exists():
            return 0.0, 0.0
        g = p = 0.0
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                usd = float(rec.get("usd", 0.0))
                g += usd
                if rec.get("project") == self.project:
                    p += usd
        return g, p

    def check(self, estimate_usd: float) -> None:
        """Refuse (raise BudgetExceeded) if the worst-case estimate would breach either cap."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self._lock_path()), timeout=30):
            g, p = self.totals()
        enforce_caps(g, p, estimate_usd, self.global_cap_usd, self.project_cap_usd)

    def record(self, provider: str, model: str, input_tokens: int, output_tokens: int, usd: float) -> None:
        rec = {
            "ts": time.time(),
            "project": self.project,
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "usd": round(usd, 8),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (
            portalocker.Lock(str(self._lock_path()), timeout=30),
            self.path.open("a", encoding="utf-8") as fh,
        ):
            fh.write(json.dumps(rec) + "\n")


class Ledger(Protocol):
    def check(self, estimate_usd: float) -> None: ...

    def record(self, provider: str, model: str, input_tokens: int, output_tokens: int, usd: float) -> None: ...


@dataclass
class RedisSpendLedger:
    """Per-deployment ledger: caps apply per UTC calendar month (keys expire after ~2 months)."""

    client: redis.Redis
    project: str
    global_cap_usd: float
    project_cap_usd: float
    clock: Any = time.time

    def period(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=UTC).strftime("%Y-%m")

    def _keys(self) -> tuple[str, str]:
        per = self.period()
        return f"ra:spend:{per}:global", f"ra:spend:{per}:project:{self.project}"

    def totals(self) -> tuple[float, float]:
        g, p = cast(list[str | None], self.client.mget(list(self._keys())))
        return float(g or 0.0), float(p or 0.0)

    def check(self, estimate_usd: float) -> None:
        g, p = self.totals()
        enforce_caps(g, p, estimate_usd, self.global_cap_usd, self.project_cap_usd, window=f" in {self.period()}")

    def record(self, provider: str, model: str, input_tokens: int, output_tokens: int, usd: float) -> None:
        gk, pk = self._keys()
        rec = {
            "ts": self.clock(),
            "project": self.project,
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "usd": round(usd, 8),
        }
        pipe = self.client.pipeline(transaction=True)
        pipe.incrbyfloat(gk, usd)
        pipe.incrbyfloat(pk, usd)
        pipe.expire(gk, 62 * 86_400)
        pipe.expire(pk, 62 * 86_400)
        pipe.xadd("ra:spend:ledger", {"data": json.dumps(rec)}, maxlen=100_000, approximate=True)
        pipe.execute()
