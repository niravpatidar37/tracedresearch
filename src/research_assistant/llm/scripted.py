"""Deterministic scripted LLM (LLM_PROVIDER=fake) for CI, the compose smoke stack and unit tests.

It answers each structured-output schema with a canned, well-formed response derived only from the prompt
(e.g. one finding per page block, quoting the block's first sentence verbatim). It makes no network calls
and costs $0. It is NOT a model: reports it produces are only meant to exercise the pipeline.

Gate: Settings refuses LLM_PROVIDER=fake unless ENV is dev/ci/test (config.FAKE_LLM_ENVS), and make_llm
re-checks it, so a prod or staging deployment can never run on canned answers.
"""

from __future__ import annotations

import re
from typing import Any

from research_assistant.llm.base import LLMRequest
from research_assistant.llm.fake import FakeLLM

PAGE_RE = re.compile(r"<<<PAGE id=(P\d+)[^>]*>>>\n(.*?)\n<<<END PAGE>>>", re.S)
FID_RE = re.compile(r"^(F\d+) \|", re.M)

_PLAN = [
    {
        "question": "What is the record efficiency of silicon solar cells?",
        "search_queries": ["silicon solar cell record efficiency"],
    },
    {
        "question": "How efficient are perovskite tandem solar cells?",
        "search_queries": ["perovskite tandem efficiency record"],
    },
    {
        "question": "How much do solar modules cost per watt?",
        "search_queries": ["solar module prices per watt"],
    },
]


def plan_responder(n: int = 2) -> Any:
    def r(req: LLMRequest) -> dict[str, Any]:
        extra = [
            {
                "question": f"Extra sub-question number {i} about solar panels?",
                "search_queries": [f"solar panel topic {i}"],
            }
            for i in range(max(0, n - len(_PLAN)))
        ]
        return {"sub_questions": (_PLAN + extra)[:n]}

    return r


def extraction_responder(req: LLMRequest) -> dict[str, Any]:
    """Behaves like a well-formed model: one finding per page, snippet = first sentence of the page block."""
    findings = []
    for pid, text in PAGE_RE.findall(req.user)[:3]:
        sentence = re.split(r"(?<=\.)\s", text.strip())[0][:250]
        findings.append({"page_id": pid, "claim": f"Claim from {pid}: {sentence[:80]}", "snippet": sentence})
    return {"findings": findings}


def review_accept(req: LLMRequest) -> dict[str, Any]:
    return {"answered": True, "missing": "", "follow_up_query": ""}


def writer_responder(req: LLMRequest) -> dict[str, Any]:
    ids = FID_RE.findall(req.user) or ["F1"]
    return {
        "title": "Solar report",
        "summary": {"text": "Solar efficiency keeps improving.", "citations": ids[:2]},
        "sections": [
            {
                "heading": "Efficiency",
                "paragraphs": [{"text": f"Finding {i} says something.", "citations": [i]} for i in ids[:4]],
            }
        ],
        "limitations": ["Fixture corpus only."],
    }


def scripted_llm(n_sub_questions: int = 2, **overrides: Any) -> FakeLLM:
    responses: dict[str, Any] = {
        "PlanOutput": plan_responder(n_sub_questions),
        "ExtractionOutput": extraction_responder,
        "ReviewOutput": review_accept,
        "ReportDraft": writer_responder,
    }
    responses.update(overrides)
    return FakeLLM(responses)
