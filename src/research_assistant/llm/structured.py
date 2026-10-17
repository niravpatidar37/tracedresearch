"""Strict JSON-schema generation from Pydantic models + validated structured calls."""

from __future__ import annotations

import copy
import json
import re
from typing import Any


def strict_schema(model_schema: dict[str, Any]) -> dict[str, Any]:
    """Inline $refs, drop titles/defaults, require all keys, forbid extra keys (OpenAI/Groq strict mode)."""
    schema = copy.deepcopy(model_schema)
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return walk(copy.deepcopy(defs[name]))
            out = {k: walk(v) for k, v in node.items() if k not in ("title", "default", "examples")}
            if out.get("type") == "object" and "properties" in out:
                out["required"] = list(out["properties"].keys())
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    result: dict[str, Any] = walk(schema)
    return result


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> Any:
    """Parse a JSON object from model text (tolerates code fences / leading prose)."""
    t = _FENCE.sub("", text.strip())
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            return json.loads(t[start : end + 1])
        raise
