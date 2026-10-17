"""Shared, append-only USD spend ledger with a hard cap (global across projects + per project).

One JSONL line per paid call: {ts, project, provider, model, input_tokens, output_tokens, usd}.
A file lock serialises concurrent writers from the six portfolio projects.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import portalocker


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
        if g + estimate_usd > self.global_cap_usd:
            raise BudgetExceeded(
                "global_usd",
                f"global spend {g:.4f} + estimate {estimate_usd:.4f} > cap {self.global_cap_usd:.2f} USD",
            )
        if p + estimate_usd > self.project_cap_usd:
            raise BudgetExceeded(
                "project_usd",
                f"project spend {p:.4f} + estimate {estimate_usd:.4f} > cap {self.project_cap_usd:.2f} USD",
            )

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
