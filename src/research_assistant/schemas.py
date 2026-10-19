"""Typed hand-offs between agents. Every hand-off is a Pydantic model; no free-form persona text."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- LLM output schemas (strict)
class PlannedSubQuestion(BaseModel):
    question: str = Field(description="A specific, independently searchable sub-question.")
    search_queries: list[str] = Field(description="1-2 concise web search queries for this sub-question.")


class PlanOutput(BaseModel):
    sub_questions: list[PlannedSubQuestion]


class ExtractedFinding(BaseModel):
    page_id: str = Field(description="Id of the page block the snippet was copied from, e.g. P1.")
    claim: str = Field(description="One factual claim, in your own words, relevant to the sub-question.")
    snippet: str = Field(description="Exact sentence(s) copied verbatim from that page supporting the claim.")


class ExtractionOutput(BaseModel):
    findings: list[ExtractedFinding] = Field(default_factory=list)


class ReviewOutput(BaseModel):
    answered: bool = Field(description="True if the findings answer the sub-question.")
    missing: str = Field(default="", description="What is still missing; empty string if nothing.")
    follow_up_query: str = Field(
        default="", description="One better web search query if not answered, else empty string."
    )


class DraftParagraph(BaseModel):
    text: str
    citations: list[str] = Field(default_factory=list, description="Finding ids (e.g. F3) supporting this paragraph.")


class DraftSection(BaseModel):
    heading: str
    paragraphs: list[DraftParagraph]


class ReportDraft(BaseModel):
    # Defaults on non-critical fields: models occasionally omit a key even in strict JSON-schema mode.
    title: str = "Research report"
    summary: DraftParagraph
    sections: list[DraftSection]
    limitations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- domain records
class SubQuestion(BaseModel):
    id: str
    question: str
    search_queries: list[str]


class Plan(BaseModel):
    question: str
    sub_questions: list[SubQuestion]
    proposed_count: int = 0  # how many the planner proposed before the budget cap


class Finding(BaseModel):
    """A claim with provenance. Findings without provenance are never passed to the writer."""

    id: str
    sub_question_id: str
    claim: str
    url: str
    title: str = ""
    snippet: str
    retrieved_at: datetime


class SearchStatus(StrEnum):
    ok = "ok"
    no_results = "no_results"
    rate_limited = "rate_limited"
    paywalled = "paywalled"
    timeout = "timeout"
    blocked = "blocked"
    error = "error"


class ToolCall(BaseModel):
    tool: Literal["search", "fetch"]
    input: str
    status: str
    detail: str = ""
    latency_ms: float = 0.0
    results: int = 0


class LLMCall(BaseModel):
    role: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    usd: float
    cached: bool
    latency_ms: float
    ok: bool = True
    error: str = ""


ResearchStatus = Literal[
    "ok", "no_results", "rate_limited", "paywalled", "timeout", "blocked", "error", "budget_exhausted"
]


class ResearchResult(BaseModel):
    sub_question_id: str
    attempt: int
    status: ResearchStatus
    findings: list[Finding] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    notes: str = ""


class Review(BaseModel):
    sub_question_id: str
    attempt: int
    answered: bool
    missing: str
    follow_up_query: str
    by_llm: bool  # False when the supervisor decided deterministically from a failure status


SubQStatus = Literal["pending", "researched", "reviewed", "answered", "unanswered"]


class SubQState(BaseModel):
    status: SubQStatus = "pending"
    attempts: int = 0
    searches_used: int = 0
    send_backs: int = 0
    next_queries: list[str] = Field(default_factory=list)
    last_result: ResearchResult | None = None
    last_review: Review | None = None
    reason: str = ""


class Citation(BaseModel):
    number: int
    url: str
    title: str
    finding_ids: list[str]


class CitationCheck(BaseModel):
    total: int
    valid: int
    invalid_ids: list[str]
    uncited_paragraphs: int

    @property
    def rate(self) -> float:
        return 1.0 if self.total == 0 else self.valid / self.total


class Report(BaseModel):
    title: str
    markdown: str
    citations: list[Citation]
    first_draft_check: CitationCheck | None
    final_check: CitationCheck
    writer_attempts: int
    fallback: bool  # True when produced deterministically (no writer LLM): the run degraded, see fallback_reason
    fallback_reason: str = ""  # machine-readable cause (SLI label): no_findings | budget_<kind> | writer_* | ...


RunStatus = Literal["queued", "running", "completed", "failed"]


class RunState(BaseModel):
    run_id: str
    question: str
    owner: str = ""  # hashed API-key id; runs are only visible to the key that created them
    status: RunStatus = "queued"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    plan: Plan | None = None
    subqs: dict[str, SubQState] = Field(default_factory=dict)
    current_subq: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    seen_urls: list[str] = Field(default_factory=list)
    domain_counts: dict[str, int] = Field(default_factory=dict)
    tokens_used: int = 0
    usd_spent: float = 0.0
    searches_used: int = 0
    elapsed_s: float = 0.0
    budget_tripped: list[str] = Field(default_factory=list)
    next_node: str = "supervisor"
    report: Report | None = None
    error: str = ""
    step: int = 0
    completed_nodes: list[str] = Field(default_factory=list)
    send_backs_total: int = 0
