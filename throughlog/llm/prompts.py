"""Prompt construction for the two permitted LLM steps.

Phase 1 (categorization) only — Phase 2 prompts live with synthesize.py (M8).
Prompts are plain functions returning (system, user) text so they are trivially
unit-testable and so the exact bytes sent to a remote model are inspectable.
"""

from __future__ import annotations

from typing import Any

CATEGORIZE_SYSTEM = (
    "You are a deterministic work-activity categorizer. Assign each event to "
    "EXACTLY ONE project from the fixed list below, using the project id string, "
    "or null when no project genuinely fits. Do not invent ids. "
    "Respond with ONLY a single JSON object and nothing else — no markdown, no "
    "code fence, no commentary, no reasoning."
)

_CATEGORIZE_CONTRACT = (
    "Respond with ONLY this JSON object:\n"
    "{\n"
    '  "assignments": [\n'
    '    {"index": <int>, "project_id": "<id-or-null>", '
    '"confidence": <0.0-1.0>, "reason": "<short>"}\n'
    "  ]\n"
    "}\n"
    "Rules: one assignment per event index; project_id must be an exact id from "
    "the list or null; confidence is your certainty 0.0-1.0."
)


def projects_block(projects: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for p in projects:
        sig = p.get("signals", {}) or {}
        kws = ", ".join(sig.get("keywords", []) or [])
        lines.append(
            f"- id: {p['id']}\n"
            f"  name: {p.get('name', '')}\n"
            f"  description: {p.get('description', '')}\n"
            f"  keywords: {kws}"
        )
    return "\n".join(lines)


def build_categorize_prompt(event_summaries: list[str],
                            projects: list[dict[str, Any]]) -> tuple[str, str]:
    """Return (system, user) for a single batched categorization call."""
    user = (
        "ACTIVE PROJECTS:\n"
        f"{projects_block(projects)}\n\n"
        "EVENTS TO CATEGORIZE (one block each, [index] first):\n"
        f"{chr(10).join(event_summaries)}\n\n"
        f"{_CATEGORIZE_CONTRACT}"
    )
    return CATEGORIZE_SYSTEM, user


# --------------------------------------------------------------------------- #
# Phase 2 — synthesis prose
# --------------------------------------------------------------------------- #
DAILY_SEP = "---DAILY SUMMARY---"

OVERVIEW_SYSTEM = (
    "You maintain a living technical project overview for someone who was NOT present. "
    "Write accurate, legible prose grounded ONLY in the supplied events — never invent "
    "work, files, or decisions that the events do not support. Preserve important "
    "historical context from the current overview and connect new work to existing threads. "
    "Output the COMPLETE updated overview in the requested section structure, then the "
    "separator line, then a short plain-text daily summary — and nothing after it."
)

# Phase 2, tier 2 — the append-only, day-by-day DETAILED entries. These are the
# permanent fine-grained record (the layer the living overview deliberately rolls up),
# so their instructions are the inverse: keep every concrete specific, never round away.
ENTRY_SYSTEM = (
    "You write one day's entry in an append-only, detailed engineering journal for "
    "someone who was NOT present. This entry is the PERMANENT fine-grained record, so "
    "preserve concrete specifics exactly: parameters changed and the values tried, "
    "numeric and benchmark results, config keys, commands run, file and function names, "
    "decisions and their rationale, and any open questions or unresolved threads. Never "
    "round away or omit a number — if the events name a value, record it. Ground every "
    "statement ONLY in the supplied events; never invent work. Output plain markdown prose "
    "(short paragraphs, with bullets for parameter/result lists) and NO top-level heading — "
    "the date heading is added for you."
)

EXEC_SYSTEM = (
    "You write a concise cross-project executive summary of a single day's work for a "
    "busy reader. Be concrete and honest: where time actually went, the key outcomes and "
    "decisions, themes that span projects, and anything blocked or needing attention. "
    "Ground every statement in the supplied per-project notes; do not pad or invent. "
    "Output plain markdown with no preamble."
)


def build_overview_prompt(project: dict[str, Any], current_overview: str,
                       event_summaries: str, date_range: str, today: str,
                       *, high_level: bool = False) -> tuple[str, str]:
    """Living-doc rewrite prompt. When ``high_level`` is set the activity block is the
    already-summarized DETAILED entries (tier 2), and the model is told to roll
    them up — keep specifics OUT of the living doc, which holds them in the entries."""
    name = project.get("name", project["id"])
    desc = project.get("description", "")
    activity_label = ("NEW ACTIVITY — detailed entries to summarize" if high_level
                      else "NEW ACTIVITY")
    rollup = (
        "\nSTAY HIGH-LEVEL: do NOT enumerate specific parameter values, individual numbers, "
        "or config keys — refer to them in aggregate (e.g. \"several damping values were "
        "tried\"). The detailed entries already hold the specifics; this living document is "
        "the overview someone reads to grasp the current state fast.\n"
    ) if high_level else ""
    user = (
        f'CURRENT OVERVIEW for "{name}":\n{current_overview}\n\n'
        f"{activity_label} ({date_range}):\n{event_summaries}\n\n"
        f"Project description: {desc}\n"
        f"{rollup}\n"
        "=== TASK ===\n"
        f'Rewrite the overview for "{name}" to absorb the new activity. Output exactly '
        "these sections:\n"
        f"# {name}\n"
        f"**Status:** active | **Last Updated:** {today}\n\n"
        "## Current State\n[1-2 paragraphs: where the project stands now]\n\n"
        "## Ongoing Threads\n[workstreams spanning sessions, each as \"- name — status\"]\n\n"
        "## Chronological Narrative\n[connected prose — NO bullets; tie new work to past "
        "decisions; no artificial length limit]\n\n"
        "## Key Artifacts\n[bullets: files changed, tools/methods used, resources referenced]\n\n"
        f"After the complete overview, output this line alone:\n{DAILY_SEP}\n"
        f"Then 2-3 plain-text sentences (no markdown) summarizing ONLY {date_range}: what was "
        "worked on, what was accomplished, and any key decisions or findings."
    )
    return OVERVIEW_SYSTEM, user


def build_entry_prompt(project: dict[str, Any], event_summaries: str, date_label: str,
                         extract_hints: list[str] | None = None) -> tuple[str, str]:
    """Return (system, user) for one day's DETAILED entry. ``extract_hints`` are
    per-project things to be sure to capture (from ``signals.entry_extract``, else the
    project description + keywords)."""
    name = project.get("name", project["id"])
    hints = ""
    if extract_hints:
        hints = ("For THIS project, be especially sure to capture:\n"
                 + "\n".join(f"- {h}" for h in extract_hints) + "\n\n")
    user = (
        f'PROJECT: "{name}"\n'
        f"DATE: {date_label}\n\n"
        f"{hints}"
        f"ACTIVITY ON {date_label}:\n{event_summaries}\n\n"
        "=== TASK ===\n"
        f"Write the detailed entry for {date_label}. Capture the concrete "
        "specifics — exact parameters, the values tried, numeric results, decisions and "
        "why. Be thorough but legible; this is the record someone will later query for "
        "details. Plain markdown prose (bullets are fine for value/result lists), no "
        "heading line — the date heading is added for you."
    )
    return ENTRY_SYSTEM, user


# Phase 2, period rollup — the durable weekly/monthly retrospective. Reads the
# already-written (gated) per-project entries/archive sections for the period and
# distills ONE cross-project summary. Same posture as `tl ask`: read-only over output
# that has already passed the privacy gate, re-scrubbed again by client.chat.
PERIOD_SUMMARY_SYSTEM = (
    "You write a durable retrospective summarizing one PERIOD (a week or a month) of work "
    "across several projects, for someone reviewing what actually got done. Ground every "
    "statement ONLY in the supplied per-project notes — never invent work, files, or "
    "decisions. Surface the real arc: what advanced on each project, the concrete outcomes "
    "and decisions, themes that span projects, and anything still open or blocked. Keep "
    "specifics that matter (a shipped feature, a measured result) but stay a retrospective, "
    "not a transcript. Output plain markdown with no preamble."
)


def build_period_summary_prompt(period_label: str,
                                project_sections: dict[str, str]) -> tuple[str, str]:
    """Return (system, user) for one period (week/month) cross-project summary. ``project_sections``
    maps project id -> that project's already-gated entries/archive text for the period."""
    blocks = "\n\n".join(f"### {pid}\n{text}" for pid, text in project_sections.items())
    user = (
        f"PERIOD: {period_label}\n\n"
        f"PER-PROJECT ACTIVITY THIS PERIOD (already recorded entries/archive sections):\n{blocks}\n\n"
        "=== TASK ===\n"
        f"Write the retrospective for {period_label}. Open with one line on the overall shape of "
        "the period, then a short section per project (what advanced, key outcomes/decisions), then "
        "a closing line on cross-cutting themes or anything still open. Ground everything in the "
        "notes above; do not pad or invent."
    )
    return PERIOD_SUMMARY_SYSTEM, user


def build_chunk_summary_prompt(project: dict[str, Any], event_summaries: str,
                               range_label: str) -> tuple[str, str]:
    name = project.get("name", project["id"])
    user = (
        f'Summarize this chunk of work activity for project "{name}".\n\n'
        f"ACTIVITY ({range_label}):\n{event_summaries}\n\n"
        "Write a concise prose summary (3-6 sentences): what was worked on, what was "
        "produced or decided, and notable patterns. Plain text, no markdown headers."
    )
    return OVERVIEW_SYSTEM, user


def build_exec_summary_prompt(project_paragraphs: dict[str, str], day: str) -> tuple[str, str]:
    blocks = "\n\n".join(f"### {pid}\n{para}" for pid, para in project_paragraphs.items())
    user = (
        f"DAY: {day}\n\n"
        f"PER-PROJECT NOTES:\n{blocks}\n\n"
        "Write the executive summary now. Open with one line on the overall shape of the "
        "day, then a short bulleted list of per-project highlights, then a closing line on "
        "cross-cutting themes or anything blocked."
    )
    return EXEC_SYSTEM, user


# --------------------------------------------------------------------------- #
# Read-only journal Q&A (`tl ask`) — grounded strictly in retrieved passages
# --------------------------------------------------------------------------- #
ASK_SYSTEM = (
    "You answer questions about the user's OWN recorded work, grounded ONLY in the "
    "journal passages supplied below. Never invent work, dates, files, or decisions the "
    "passages do not support. If the passages do not contain the answer, say so plainly "
    "rather than guessing. Cite the source label(s) you used in square brackets. Be "
    "concise and concrete."
)


def build_ask_prompt(question: str,
                     passages: list[tuple[str, str]]) -> tuple[str, str]:
    """Return (system, user) for a single journal-Q&A call. ``passages`` is an ordered
    list of ``(source_label, text)`` — the *only* ground truth the model may use."""
    blocks = [f"[{i}] source: {src}\n{text}" for i, (src, text) in enumerate(passages, 1)]
    user = (
        f"QUESTION:\n{question}\n\n"
        "JOURNAL PASSAGES (the only ground truth — cite by source label):\n"
        f"{chr(10).join(blocks) if blocks else '(none)'}\n\n"
        "Answer the question using only the passages above. If they do not contain the "
        "answer, say what's missing. Cite sources in brackets, e.g. [checkout/overview › …]."
    )
    return ASK_SYSTEM, user


# --------------------------------------------------------------------------- #
# Opt-in `tl init --llm` enrichment — refine a deterministically-scanned project
# entry from a METADATA-ONLY digest (README excerpt + file tree + markers; never
# source bodies). The model only ever PROPOSES descriptive signals; the security-
# relevant fields (signals.paths, git_remotes) are owned by the deterministic
# scanner and are never sent for the model to set.
# --------------------------------------------------------------------------- #
INIT_SYSTEM = (
    "You help register a software project for an activity tracker by proposing the "
    "descriptive signals that identify it. You are given a project's NAME, its git remotes, "
    "and a small METADATA digest of the folder (README excerpt, file/dir tree, language "
    "markers) — never its source code. From those, infer signals that would match this "
    "project in window titles, file paths, and notes. Be specific and grounded in the "
    "digest; do not invent integrations or accounts the digest does not evidence. Respond "
    "with ONLY a single JSON object and nothing else — no markdown, no code fence, no prose."
)

_INIT_CONTRACT = (
    "Respond with ONLY this JSON object (omit a field or use [] when you have nothing "
    "grounded to propose):\n"
    "{\n"
    '  "description": "<one or two sentences on what the project is>",\n'
    '  "keywords": ["<distinctive term>", ...],\n'
    '  "window_patterns": ["<regex matching the project in a window title>", ...],\n'
    '  "entry_extract": ["<kind of specific to always capture in each journal entry>", ...],\n'
    '  "domains": ["<host the project owns, e.g. docs/issue tracker>", ...],\n'
    '  "jira_prefixes": ["<ABC>", ...]\n'
    "}\n"
    "Rules: keywords/window_patterns identify THIS project (avoid generic words like "
    '"app" or "test"); jira_prefixes only if the digest actually shows an issue-key '
    "pattern; never include file contents or secrets."
)


def build_init_enrich_prompt(base_entry: dict[str, Any], digest: str) -> tuple[str, str]:
    """Return (system, user) for one project-enrichment call. ``base_entry`` is the
    deterministically-scanned project; ``digest`` is the metadata-only folder digest."""
    sig = base_entry.get("signals", {}) or {}
    remotes = ", ".join(sig.get("git_remotes", []) or []) or "(none)"
    kws = ", ".join(sig.get("keywords", []) or []) or "(none yet)"
    user = (
        f"PROJECT NAME: {base_entry.get('name', base_entry.get('id', ''))}\n"
        f"GIT REMOTES: {remotes}\n"
        f"CURRENT KEYWORDS (from the name): {kws}\n\n"
        f"FOLDER METADATA DIGEST (no source code):\n{digest}\n\n"
        f"{_INIT_CONTRACT}"
    )
    return INIT_SYSTEM, user
