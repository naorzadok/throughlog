"""Ask your journal a question (`tl ask "…"`).

A read-only natural-language query surface over the already-synthesized journal —
*not* part of the capture→synthesize pipeline. The flow mirrors retrieval-augmented
generation, but every piece that can be deterministic is:

  1. DETERMINISTIC RETRIEVAL — split the per-project ``overview.md`` / ``archive.md``
     (+ ``daily.md`` / ``executive_summary.md``) into heading-delimited passages and
     rank them against the question by keyword overlap. Pure, offline, testable.
  2. ONE LLM CALL — stuff the top passages in as the *only* ground truth and ask the
     question. This is a third, optional, **read-only** LLM touchpoint that lives
     outside the determinism-bounded pipeline; it only ever reads journal markdown that
     already passed the privacy gate, and ``client.chat`` re-runs the egress scrub on
     the outbound prompt, so nothing un-gated can leave the machine.
  3. GRACEFUL DEGRADE — no key, ``--no-llm``, or any ``LLMError`` falls back to
     printing the ranked passages verbatim. The question is always answered with
     something useful; it never crashes and never reaches for un-retrieved data.

    python -m throughlog.cli ask "what did I ship on the checkout project this week?"
    python -m throughlog.cli ask --project checkout --no-llm "open threads?"

The retrieval functions are pure (unit-tested with inline fixtures); the LLM step
takes an injected client so tests run with no network.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from throughlog.llm.client import LLMError
from throughlog.llm import prompts


# --------------------------------------------------------------------------- #
# Corpus model + loading (pure)
# --------------------------------------------------------------------------- #
@dataclass
class Passage:
    """One retrievable chunk of journal text, with a human-readable source label."""
    source: str
    text: str
    score: float = 0.0


def _label_base(rel: Path) -> str:
    """``project_checkout/overview.md`` -> ``checkout/overview``; ``daily.md`` -> ``daily``."""
    parts = list(rel.with_suffix("").parts)
    parts = [p[len("project_"):] if p.startswith("project_") else p for p in parts]
    return "/".join(parts)


def split_markdown(text: str) -> list[tuple[str, str]]:
    """Split markdown into ``(heading, body)`` passages on ``#`` headings (pure).

    Content before the first heading becomes a passage with an empty heading."""
    passages: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if heading or body:
            passages.append((heading, body))

    for line in (text or "").splitlines():
        if line.lstrip().startswith("#"):
            flush()
            heading = line.lstrip().lstrip("#").strip()
            buf = []
        else:
            buf.append(line)
    flush()
    return [(h, b) for (h, b) in passages if (h or b)]


def _matches_project(rel: Path, project: str) -> bool:
    base = _label_base(rel).split("/", 1)[0].lower()
    return base == project.lower() or project.lower() in str(rel).lower()


def load_corpus(journal_dir: str | Path, *, project: str | None = None) -> list[Passage]:
    """Read every ``*.md`` under ``journal_dir`` into ranked-ready passages (pure I/O).

    Optionally restrict to a single ``project`` id. Reads only already-synthesized,
    already-gated markdown — never raw events."""
    root = Path(journal_dir)
    if not root.exists():
        return []
    out: list[Passage] = []
    for md in sorted(root.rglob("*.md")):
        rel = md.relative_to(root)
        if project and not _matches_project(rel, project):
            continue
        base = _label_base(rel)
        text = md.read_text(encoding="utf-8", errors="replace")
        for heading, body in split_markdown(text):
            label = f"{base} › {heading}" if heading else base
            content = (f"{heading}\n{body}" if heading else body).strip()
            if content:
                out.append(Passage(source=label, text=content))
    return out


# --------------------------------------------------------------------------- #
# Deterministic retrieval / ranking (pure)
# --------------------------------------------------------------------------- #
_STOP = frozenset(
    "a an the of to in on for and or but with at by from as is are was were be been "
    "being it its this that these those i you he she we they my your our do does did "
    "what when where which who how why did done what's whats about into".split()
)
_TOK = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> list[str]:
    return [t for t in _TOK.findall((s or "").lower()) if t not in _STOP and len(t) > 1]


def score_passage(q_terms: set[str], text: str) -> float:
    """Coverage-weighted keyword overlap (distinct query terms dominate ties)."""
    if not q_terms:
        return 0.0
    tf = Counter(_tokens(text))
    if not tf:
        return 0.0
    covered = sum(1 for t in q_terms if tf[t] > 0)
    hits = sum(min(tf[t], 5) for t in q_terms)
    return covered * 10.0 + min(hits, 50)


def rank_passages(question: str, passages: list[Passage], *, top_k: int = 6) -> list[Passage]:
    """Return the top-``k`` passages scoring > 0, deterministically ordered."""
    q_terms = set(_tokens(question))
    scored = [
        Passage(source=p.source, text=p.text, score=s)
        for p in passages
        if (s := score_passage(q_terms, p.text)) > 0
    ]
    scored.sort(key=lambda p: (-p.score, p.source))
    return scored[:top_k]


def _budget(passages: list[Passage], max_chars: int) -> list[Passage]:
    """Keep passages in rank order until the char budget is hit (always >= 1)."""
    out: list[Passage] = []
    total = 0
    for p in passages:
        if out and total + len(p.text) > max_chars:
            break
        out.append(p)
        total += len(p.text)
    return out


# --------------------------------------------------------------------------- #
# Answer (thin LLM driver; degrades to deterministic retrieval)
# --------------------------------------------------------------------------- #
@dataclass
class Answer:
    text: str
    sources: list[str]
    used_llm: bool
    error: str = ""


_NO_MATCH = "I couldn't find anything in your journal about that."


def _deterministic_answer(passages: list[Passage]) -> str:
    lines = ["(No model — here are the most relevant journal sections.)", ""]
    for p in passages:
        lines.append(f"### {p.source}")
        lines.append(p.text)
        lines.append("")
    return "\n".join(lines).strip()


def answer(question: str, passages: list[Passage], client: Any, *,
           top_k: int = 6, max_chars: int = 6000) -> Answer:
    """Retrieve deterministically, then answer with one LLM call if a client is
    given; degrade to the ranked passages on no client / any ``LLMError``."""
    ranked = rank_passages(question, passages, top_k=top_k)
    if not ranked:
        return Answer(_NO_MATCH, [], used_llm=False)

    selected = _budget(ranked, max_chars)
    sources = [p.source for p in selected]
    if client is None:
        return Answer(_deterministic_answer(selected), sources, used_llm=False)

    system, user = prompts.build_ask_prompt(
        question, [(p.source, p.text) for p in selected])
    try:
        text = client.chat(system, user, max_tokens=800).strip()
        return Answer(text or _deterministic_answer(selected), sources, used_llm=True)
    except LLMError as exc:
        return Answer(_deterministic_answer(selected), sources,
                      used_llm=False, error=str(exc))
