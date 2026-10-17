"""Logging with PII/secret redaction. JSON lines by default (Cloud Logging parses `severity`/`message`)."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime

_PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
    (re.compile(r"\b(?:gsk|sk|tvly|hf)_[A-Za-z0-9-]{10,}\b"), "[secret]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{10,}"), "Bearer [secret]"),
    (re.compile(r"(?i)(rediss?://)[^@\s/]*@"), r"\1[secret]@"),
    (re.compile(r"\b(?:\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"), "[phone]"),
]


def redact(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Exception text is redacted too (it can echo request data)."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "pid": record.process,
            "thread": record.threadName,
        }
        if record.exc_info:
            entry["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: int = logging.INFO, fmt: str | None = None) -> None:
    fmt = (fmt or os.environ.get("LOG_FORMAT", "json")).lower()
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    if fmt == "text":
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s pid=%(process)d %(message)s"))
    else:
        handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "primp", "trafilatura", "ddgs"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):  # route uvicorn through the same JSON+redaction
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
