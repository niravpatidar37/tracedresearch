"""Text utilities: untrusted-data delimiting, passage selection, verbatim snippet matching."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_DELIM = re.compile(r"(<<<|>>>)")
_WS = re.compile(r"\s+")
_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-"})


def strip_controls(text: str) -> str:
    """Remove control chars and bidi/zero-width characters (common in obfuscated injections)."""
    return _CTRL.sub("", text)


def clean_line(text: str, limit: int) -> str:
    return _WS.sub(" ", strip_controls(text)).strip()[:limit]


def neutralize(text: str) -> str:
    """Untrusted text must not be able to close or forge our data delimiters."""
    return _DELIM.sub(lambda m: "‹‹‹" if m.group(1) == "<<<" else "›››", strip_controls(text))


def as_data_block(label: str, content: str, attrs: str = "") -> str:
    head = f"<<<{label}{(' ' + attrs) if attrs else ''}>>>"
    return f"{head}\n{neutralize(content)}\n<<<END {label}>>>"


def _collapse(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return _WS.sub(" ", t).strip()


def norm(text: str) -> str:
    return _collapse(text).lower()


def select_passages(text: str, keywords: list[str], limit_chars: int) -> str:
    """Keep the paragraphs most relevant to the keywords, in original order, within a char budget."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if len(p.strip()) > 40]
    if not paras:
        return text[:limit_chars]
    kws = {k for k in (w.lower() for w in keywords) if len(k) > 2}
    scored = [(sum(1 for k in kws if k in p.lower()), i, p) for i, p in enumerate(paras)]
    chosen: list[tuple[int, str]] = []
    used = 0
    for _score, i, p in sorted(scored, key=lambda t: (-t[0], t[1])):
        if used + len(p) > limit_chars:
            if not chosen:
                chosen.append((i, p[:limit_chars]))
            continue
        chosen.append((i, p))
        used += len(p) + 1
    return "\n".join(p for _, p in sorted(chosen))


def find_verbatim(snippet: str, page: str, min_ratio: float = 0.85) -> str | None:
    """Return the page's own text matching the model's snippet, or None if it is not really on the page.

    Exact (normalised) containment wins; otherwise accept a contiguous match covering >= min_ratio of
    the snippet. The returned string always comes from the page, never from the model.
    """
    ns = norm(snippet)
    cpage = _collapse(page)
    npage = cpage.lower()
    same_len = len(npage) == len(cpage)
    if len(ns) < 20:
        return None
    idx = npage.find(ns)
    if idx != -1:
        return (cpage if same_len else npage)[idx : idx + len(ns)]
    m = SequenceMatcher(None, npage, ns, autojunk=False).find_longest_match(0, len(npage), 0, len(ns))
    if m.size >= min_ratio * len(ns):
        return (cpage if same_len else npage)[m.a : m.a + m.size]
    return None


def keywords(text: str) -> list[str]:
    stop = {
        "what",
        "which",
        "when",
        "where",
        "does",
        "with",
        "from",
        "that",
        "this",
        "have",
        "between",
        "their",
        "about",
        "into",
        "than",
        "they",
        "were",
        "will",
        "would",
        "there",
        "how",
        "the",
        "and",
    }
    return [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-\.]{2,}", text) if w.lower() not in stop]
