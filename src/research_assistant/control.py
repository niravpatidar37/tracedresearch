"""Operational kill switch.

Two independent ways to engage it (either one is enough):
- AGENT_KILL_SWITCH=1 in the environment (needs a new Cloud Run revision; survives Redis loss), or
- the Redis key `ra:control:kill_switch` (`research-assistant kill-switch on`; takes effect within
  CACHE_S on every instance, no redeploy).

While engaged: POST /research returns 503, workers stop consuming the queue and do not resume orphans,
and any run in flight is preempted before its next LLM / search / fetch call (its last checkpoint is
kept and it resumes when the switch is released). Read-only endpoints keep working.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from research_assistant.config import Settings
from research_assistant.store import RunStore

log = logging.getLogger(__name__)


class KillSwitch:
    CACHE_S = 2.0

    def __init__(self, settings: Settings, store: RunStore, clock: Callable[[], float] = time.monotonic) -> None:
        self.env_flag = settings.kill_switch
        self.store = store
        self.clock = clock
        self._lock = threading.Lock()
        self._cached = False
        self._checked_at = float("-inf")

    def __call__(self) -> bool:
        return self.active()

    def active(self) -> bool:
        if self.env_flag:
            return True
        with self._lock:
            now = self.clock()
            if now - self._checked_at >= self.CACHE_S:
                try:
                    self._cached = self.store.kill_switch_set()
                except Exception:  # noqa: BLE001 - Redis down: keep the last known value
                    log.warning("kill switch: Redis unreachable, keeping last value %s", self._cached)
                self._checked_at = now
            return self._cached
