"""Citation validation and sanitized markdown rendering.

Model-written text is escaped so it cannot create links, images, HTML or headings; URLs inside
model text are removed. The only links in a report are Finding URLs, rendered by this code.
Every citation marker is checked against a real Finding record before the report is returned.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

from research_assistant.schemas import (
    Citation,
    CitationCheck,
    DraftParagraph,
    Finding,
    Plan,
    Report,
    ReportDraft,
    SubQState,
)
from research_assistant.search.providers import domain_of
from research_assistant.text import clean_line, strip_controls

_URLISH = re.compile(r"(?i)\b(?:https?|ftp|javascript|data|vbscript|file)\s*:\S*|\bwww\.\S+")
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+!|~^])")
_MARKER = re.compile(r"(?<!\\)\[\^(\d+)\](?!:)")
_DEF = re.compile(r"(?m)^\[\^(\d+)\]: ")
_ID = re.compile(r"^F\d+$")


def escape_md(text: str) -> str:
    t = strip_controls(text)
    t = _URLISH.sub("(link removed)", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # no raw HTML can survive
    return _MD_SPECIAL.sub(r"\\\1", t)


def safe_url(url: str) -> str | None:
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    return quote(url, safe=":/?&=%#@+,;~-._")


def _paragraphs(draft: ReportDraft) -> list[DraftParagraph]:
    return [draft.summary] + [p for s in draft.sections for p in s.paragraphs]


def check_citations(draft: ReportDraft, findings: list[Finding]) -> CitationCheck:
    known = {f.id for f in findings}
    total = valid = uncited = 0
    invalid: list[str] = []
    for p in _paragraphs(draft):
        if not p.citations:
            uncited += 1
        for c in p.citations:
            total += 1
            cid = c.strip()
            if _ID.match(cid) and cid in known:
                valid += 1
            else:
                invalid.append(cid[:20])
    return CitationCheck(total=total, valid=valid, invalid_ids=invalid, uncited_paragraphs=uncited)


class _Numbering:
    def __init__(self, findings: list[Finding]) -> None:
        self.by_id = {f.id: f for f in findings}
        self.url_num: dict[str, int] = {}
        self.cites: dict[int, Citation] = {}

    def num(self, fid: str) -> int | None:
        f = self.by_id.get(fid.strip())
        if f is None or safe_url(f.url) is None:
            return None
        if f.url not in self.url_num:
            n = len(self.url_num) + 1
            self.url_num[f.url] = n
            self.cites[n] = Citation(number=n, url=f.url, title=f.title, finding_ids=[])
        n = self.url_num[f.url]
        if f.id not in self.cites[n].finding_ids:
            self.cites[n].finding_ids.append(f.id)
        return n

    def markers(self, ids: list[str]) -> str:
        nums = sorted({n for n in (self.num(i) for i in ids) if n is not None})
        return "".join(f"[^{n}]" for n in nums)


def render(
    question: str,
    draft: ReportDraft,
    findings: list[Finding],
    plan: Plan | None,
    subqs: dict[str, SubQState],
    notes: list[str],
) -> tuple[str, list[Citation], CitationCheck]:
    """Render sanitized markdown. Paragraphs left without a valid citation are dropped, not shown."""
    nb = _Numbering(findings)
    out: list[str] = [
        f"# {escape_md(draft.title) or 'Research report'}",
        "",
        f"**Question:** {escape_md(question)}",
        "",
    ]
    dropped = 0
    summ = nb.markers(draft.summary.citations)
    out += ["## Summary", ""]
    if summ:
        out += [escape_md(draft.summary.text) + " " + summ, ""]
    else:
        dropped += 1
        out += ["_Summary omitted: it had no valid citation._", ""]
    for sec in draft.sections:
        body: list[str] = []
        for p in sec.paragraphs:
            m = nb.markers(p.citations)
            if not m:
                dropped += 1
                continue
            body += [escape_md(p.text) + " " + m, ""]
        if body:
            out += [f"## {escape_md(sec.heading)}", "", *body]
    lims = [escape_md(x) for x in draft.limitations if x.strip()]
    if plan is not None:
        for sq in plan.sub_questions:
            st = subqs.get(sq.id)
            if st is not None and st.status == "unanswered":
                lims.append(f"Unanswered: {escape_md(sq.question)} ({escape_md(st.reason)})")
    if dropped:
        lims.append(f"{dropped} paragraph(s) removed because they had no valid citation.")
    lims += [escape_md(n) for n in notes]
    if lims:
        out += ["## Limitations", "", *[f"- {x}" for x in lims], ""]
    out += ["## Sources", ""]
    for n in sorted(nb.cites):
        c = nb.cites[n]
        f = nb.by_id[c.finding_ids[0]]
        title = escape_md(c.title) or escape_md(domain_of(c.url))
        out.append(
            f"[^{n}]: [{title}]({safe_url(c.url)}) ({escape_md(domain_of(c.url))}, "
            f"retrieved {f.retrieved_at:%Y-%m-%d}). Findings {', '.join(c.finding_ids)}: "
            f"\u201c{escape_md(clean_line(f.snippet, 240))}\u201d"
        )
    md = "\n".join(out).rstrip() + "\n"
    cites = [nb.cites[n] for n in sorted(nb.cites)]
    return md, cites, validate_rendered(md, cites, findings)


def validate_rendered(md: str, cites: list[Citation], findings: list[Finding]) -> CitationCheck:
    """Final gate: every [^n] marker must resolve to a defined Source backed by real Findings."""
    by_num = {c.number: c for c in cites}
    defined = {int(m.group(1)) for m in _DEF.finditer(md)}
    known = {f.id: f for f in findings}
    total = valid = 0
    invalid: list[str] = []
    for m in _MARKER.finditer(md):
        total += 1
        n = int(m.group(1))
        c = by_num.get(n)
        ok = (
            c is not None
            and n in defined
            and bool(c.finding_ids)
            and all(fid in known and known[fid].url == c.url for fid in c.finding_ids)
        )
        if ok:
            valid += 1
        else:
            invalid.append(str(n))
    return CitationCheck(total=total, valid=valid, invalid_ids=invalid, uncited_paragraphs=0)


def fallback_report(
    question: str, findings: list[Finding], plan: Plan | None, subqs: dict[str, SubQState], reason: str
) -> Report:
    """Deterministic report (no LLM) used when the writer cannot run within budget."""
    sections = []
    if plan is not None:
        for sq in plan.sub_questions:
            paras = [DraftParagraph(text=f.claim, citations=[f.id]) for f in findings if f.sub_question_id == sq.id]
            if paras:
                sections.append({"heading": sq.question, "paragraphs": paras})
    draft = ReportDraft.model_validate(
        {
            "title": "Partial findings",
            "summary": {
                "text": f"Writer not run ({reason}); listing extracted findings per sub-question.",
                "citations": [findings[0].id] if findings else [],
            },
            "sections": sections,
            "limitations": [f"Report generated without the writer model because of: {reason}."],
        }
    )
    md, cites, final = render(question, draft, findings, plan, subqs, [])
    return Report(
        title=draft.title,
        markdown=md,
        citations=cites,
        first_draft_check=None,
        final_check=final,
        writer_attempts=0,
        fallback=True,
    )
