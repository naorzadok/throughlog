"""Phase 2 — synthesis. The second (and last) permitted LLM place.

Consumes categorized v2 NormalizedEvents (attribution filled by Phase 1) and,
per project, produces:

  journal/project_<id>/archive.md  — append-across-days, idempotent-within-a-day,
                                     DETERMINISTIC (no LLM). Always written, even
                                     when the model is unreachable; re-synthesizing
                                     a day replaces that day's section (no dupes).
  journal/project_<id>/overview.md    — a living document rewritten by the LLM.
  journal/daily.md                 — newest-at-top per-project daily paragraphs.
  journal/executive_summary.md     — cross-project executive summary (LLM).

Resilience (C5): the archive is built from event data alone and is written first.
Any LLM failure leaves the overview unchanged and the daily/exec text falls back to a
deterministic concatenation — events are never lost and the run never crashes.
The egress gate is re-run inside `client.chat` before every send (see llm/client).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from throughlog.schema import (
    NormalizedEvent,
    FOCUS_SESSION, DEEP_WORK, LONG_RUN, FILE_CHANGE, GIT_COMMIT,
    NARRATION, CLIPBOARD, IDLE_START, IDLE_END, AGENT_REPORT,
)
from throughlog.llm.client import LLMError
from throughlog.llm.prompts import (
    DAILY_SEP, build_overview_prompt, build_chunk_summary_prompt,
    build_exec_summary_prompt, build_entry_prompt, build_batched_entry_prompt,
    build_period_summary_prompt, est_tokens,
)

# A single project's event batch is condensed via the LLM before the overview
# rewrite once it exceeds this, so a huge day never blows the context window.
CHUNK_SIZE = 150


@dataclass(frozen=True)
class SynthesisOptions:
    """Opt-in Phase-2 knobs. The default is OFF/identical-to-before so library callers
    and tests are byte-identical; the shipped config.example.json turns entry-writing ON
    (the product default). See config.py::synthesis_options_from."""
    write_entries: bool = False         # produce the tier-2 detailed entries/<period>.md
    entry_max_tokens: int = 1500
    # How entries (still one per DAY) are grouped into files: "month" ->
    # entries/<YYYY-MM>.md, "week" -> entries/<YYYY-Www>.md. Entry granularity is
    # unchanged; only the file partition changes.
    entry_period: str = "month"
    # The durable cross-project retrospective tier: "off" | "weekly" | "monthly" ->
    # journal/summaries/<period>.md, distilled from the gated entries/archive sections.
    summary_cadence: str = "off"
    # Opt-in re-run economy (default OFF): when a project's event batch is byte-for-byte
    # unchanged since its last synthesis, reuse the already-written overview/entries
    # instead of re-billing the LLM. The deterministic archive is still rewritten. Never
    # skips a project that changed, so it can't drop work the pipeline needs.
    skip_unchanged: bool = False
    # F1/F2 — batched entry calls + chunk-not-condense input budget. All three default to
    # the current per-day, condense-on-overflow behavior so library callers/tests stay
    # byte-identical; config.example.json opts into the product defaults (adaptive/auto/7).
    entry_batch: str = "day"        # "day" | "week" | "adaptive"
    # Per-call INPUT budget in ~4-char tokens (est_tokens). 0 => legacy _activity_block
    # condense path (byte-identical). >0 => chunk-not-condense: whole days are packed into
    # units under this budget and each unit is one call with RAW event lines (no summary).
    max_input_tokens: int = 0
    max_batch_days: int = 7         # hard span cap (calendar days) per unit for week/adaptive


DEFAULT_SYNTHESIS = SynthesisOptions()

# Attribution ids that mean "no project" — never synthesized. needs_review events
# carry project_id=None and are likewise skipped (they live in the review queue).
SKIP_PROJECT_IDS = frozenset({None, "__unrelated__"})

# Event types that carry no journal-worthy narrative on their own.
_QUIET_TYPES = frozenset({IDLE_START, IDLE_END})


def _as_int(v: Any, default: int = 0) -> int:
    """Coerce a persisted-payload value to int; junk (str/None/list) -> default.
    Synthesis is built from payloads that may carry attacker-influenced numerics
    (the same type-discipline gap as F-01's clock_offset_sec). The deterministic
    archive must never crash on a bad duration_sec — the module's C5 'never
    crashes' contract. Accepts int-ish floats and numeric strings ("12", "12.5")."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Result records
# --------------------------------------------------------------------------- #
@dataclass
class ProjectJournal:
    project_id: str
    event_count: int
    archive_section: str           # deterministic markdown appended to archive.md
    overview_md: str                  # full rewritten overview (or unchanged on failure)
    daily_paragraph: str           # "" when the LLM did not produce one
    llm_calls: int = 0
    error: str | None = None
    # tier-2 detailed entries: {"YYYY-MM": merged dated sections} routed to month files.
    entries_by_period: dict[str, str] = field(default_factory=dict)
    entry_calls: int = 0
    entry_error: str | None = None


@dataclass
class SynthesisRun:
    today: str
    projects: list[ProjectJournal] = field(default_factory=list)
    exec_summary: str = ""
    exec_error: str | None = None
    # Period keys (e.g. "2026-W26" / "2026-06") whose summaries/<key>.md this run wrote.
    summaries: list[str] = field(default_factory=list)
    summary_error: str | None = None


# --------------------------------------------------------------------------- #
# Small deterministic helpers
# --------------------------------------------------------------------------- #
def _hhmm(ts: str) -> str:
    return ts[11:16] if len(ts) >= 16 else ts


def _date_key(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%Y%m%d")
    except (ValueError, TypeError):
        return (ts or "")[:10].replace("-", "") or "00000000"


def _date_label(key: str) -> str:
    return f"{key[:4]}-{key[4:6]}-{key[6:]}" if len(key) == 8 and key.isdigit() else key


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _as_event(ev: Any) -> NormalizedEvent:
    return ev if isinstance(ev, NormalizedEvent) else NormalizedEvent.from_dict(ev)


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #
def group_by_project(events: list[NormalizedEvent]) -> dict[str, list[NormalizedEvent]]:
    """Bucket categorized events by project id, skipping unattributed/needs_review."""
    groups: dict[str, list[NormalizedEvent]] = {}
    for ev in events:
        pid = ev.attribution.project_id if ev.attribution else None
        if pid in SKIP_PROJECT_IDS:
            continue
        groups.setdefault(pid, []).append(ev)
    return groups


# --------------------------------------------------------------------------- #
# Compact per-event summary (fed to the overview LLM)
# --------------------------------------------------------------------------- #
def summarize_event(ev: NormalizedEvent) -> str:
    """A compact, gated-only, human-readable line (or block) for one event.
    Returns "" for events with nothing to narrate (idle)."""
    p = ev.payload or {}
    tm = _hhmm(ev.ts_wall)
    t = ev.type

    if t in (FOCUS_SESSION, DEEP_WORK, LONG_RUN):
        dur = _as_int(p.get("duration_sec"))
        mode = p.get("mode", "")
        head = f"[{tm}] {t} {dur}s" + (f" ({mode})" if mode else "")
        parts = [head]
        anchor = p.get("anchor") or p.get("process") or ""
        if anchor:
            proc = p.get("process", "")
            parts.append(f"  focus: {anchor}" + (f" [{proc}]" if proc else ""))
        if p.get("active_file"):
            parts.append(f"  file: {p['active_file']}")
        intent = p.get("intent")
        if isinstance(intent, dict) and intent.get("label"):
            parts.append(f'  intent: "{intent["label"]}"')
        sats = [s.get("title", "") for s in p.get("satellites", []) or []
                if isinstance(s, dict) and s.get("title")]
        if sats:
            parts.append(f"  also: {', '.join(sats[:5])}")
        cmd = p.get("cmdline") or p.get("command")
        if cmd:
            parts.append(f"  cmd: {cmd}")
        return "\n".join(parts)

    # A diff lives only in the sidecar (referenced by hash); the marker hints it
    # exists without ever putting diff text into the line (so it never reaches an LLM).
    diff_mark = " (+diff)" if p.get("diff_ref") else ""

    if t == FILE_CHANGE:
        return f"[{tm}] file {p.get('action', 'changed')}: {p.get('path', '')}{diff_mark}"

    if t == GIT_COMMIT:
        actor = p.get("actor")
        who = f" by {actor}" if actor else ""
        files = p.get("files") or []
        fstr = f" ({len(files)} file{'s' if len(files) != 1 else ''})" if files else ""
        return f"[{tm}] commit{who}: {p.get('message', '')}{fstr} [{p.get('repo', '')}]{diff_mark}"

    if t == NARRATION:
        return f'[{tm}] narration: "{p.get("note", "")}"'

    if t == CLIPBOARD:
        return f"[{tm}] clipboard {p.get('kind', '')} {p.get('host', '')}".rstrip()

    if t == AGENT_REPORT:
        import json
        summary = p.get("summary") or p.get("note") or json.dumps(p, ensure_ascii=False)[:160]
        return f"[{tm}] agent report ({ev.source.identity}): {summary}"

    if t in _QUIET_TYPES:
        return ""

    import json
    return f"[{tm}] {t}: {json.dumps(p, ensure_ascii=False)[:160]}"


# --------------------------------------------------------------------------- #
# Deterministic archive section (NO LLM)
# --------------------------------------------------------------------------- #
def build_archive_section(date_label: str, events: list[NormalizedEvent]) -> str:
    """The permanent, append-only record for one date — derived purely from event
    data, so it survives any LLM outage."""
    lines: list[str] = ["---", f"## {date_label}", ""]

    sessions = [e for e in events if e.type in (FOCUS_SESSION, DEEP_WORK, LONG_RUN)]
    if sessions:
        lines.append("### Sessions")
        for e in sessions:
            p = e.payload or {}
            dur = _as_int(p.get("duration_sec"))
            mode = p.get("mode", "")
            anchor = p.get("anchor") or p.get("process") or "session"
            intent = p.get("intent")
            intent_str = ""
            if isinstance(intent, dict) and intent.get("label"):
                intent_str = f" — {intent['label']}"
            sats = [s.get("title", "") for s in p.get("satellites", []) or []
                    if isinstance(s, dict) and s.get("title")]
            sat_str = f" — also: {', '.join(sats[:4])}" if sats else ""
            meta = f"{dur}s, {mode}" if mode else f"{dur}s"
            lines.append(f"- {_hhmm(e.ts_wall)} — {anchor} ({meta}){intent_str}{sat_str}")
        lines.append("")

    commits = [e for e in events if e.type == GIT_COMMIT]
    if commits:
        lines.append("### Commits")
        for e in commits:
            p = e.payload or {}
            actor = p.get("actor")
            who = f" [{actor}]" if actor else ""
            lines.append(f"- {_hhmm(e.ts_wall)} — {p.get('message', '')}{who}")
        lines.append("")

    # Agent activity — autonomous/remote reports are first-class in the record:
    # "what my agents did" is the wedge, so it must survive in the LLM-free archive.
    agents = [e for e in events if e.type == AGENT_REPORT]
    if agents:
        lines.append("### Agent activity")
        for e in agents:
            p = e.payload or {}
            who = e.source.identity or p.get("tool", "agent")
            summary = p.get("summary") or p.get("message") or p.get("note", "")
            lines.append(f"- {_hhmm(e.ts_wall)} — {who}: {summary}")
        lines.append("")

    files: list[str] = []
    for e in events:
        if e.type == FILE_CHANGE:
            name = _basename((e.payload or {}).get("path", ""))
            if name and name not in files:
                files.append(name)
    for e in commits + agents:
        for f in (e.payload or {}).get("files", []) or []:
            name = _basename(str(f))
            if name and name not in files:
                files.append(name)
    if files:
        lines.append("### Files changed")
        lines.extend(f"- {n}" for n in files)
        lines.append("")

    notes = [e for e in events if e.type == NARRATION]
    if notes:
        lines.append("### Narration")
        for e in notes:
            lines.append(f'- [{_hhmm(e.ts_wall)}] "{(e.payload or {}).get("note", "")}"')
        lines.append("")

    clips = [e for e in events if e.type == CLIPBOARD]
    if clips:
        lines.append(f"### Clipboard captures: {len(clips)}")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _iter_dated_sections(text: str):
    """Yield ``(label, chunk)`` for each dated section in a framed markdown document.

    A section starts at a '---' line immediately followed by '## label'; the zero-width
    lookahead splits before each, keeping the frame attached. Shared by the idempotent
    merge and the period-summary collector so both parse archive/entries files identically."""
    import re
    for chunk in re.split(r"(?m)(?=^---\n## )", text or ""):
        if not chunk.strip():
            continue
        m = re.match(r"^---\n## (.+)", chunk)
        label = m.group(1).strip() if m else chunk[:60]
        yield label, chunk


def _merge_dated_sections(existing: str, incoming: str) -> str:
    """Idempotent append: every date section ('## <label>') appears exactly once.

    Shared by the deterministic archive.md and the LLM entries/<period>.md — both are
    append-only ACROSS days, but re-running synthesis for the SAME day must not duplicate
    that day's section: it REPLACES it with the freshly built one (which may now cover
    more events). Sections keep their order of first appearance; a brand-new date is
    appended at the end (risk register #5)."""
    from collections import OrderedDict

    merged: "OrderedDict[str, str]" = OrderedDict()
    for label, chunk in _iter_dated_sections(existing):
        merged[label] = chunk
    for label, chunk in _iter_dated_sections(incoming):
        merged[label] = chunk           # later same-label wins (replace, not append)
    return "".join(merged.values())


def overview_stub(project_name: str, today: str) -> str:
    return (
        f"# {project_name}\n"
        f"**Status:** active | **Last Updated:** {today}\n\n"
        "## Current State\n(No history yet)\n\n"
        "## Ongoing Threads\n\n"
        "## Chronological Narrative\n\n"
        "## Key Artifacts\n"
    )


# --------------------------------------------------------------------------- #
# Activity block (chunk-condensed when very large)
# --------------------------------------------------------------------------- #
def _activity_block(project: dict[str, Any], events: list[NormalizedEvent],
                    range_label: str, client: Any) -> tuple[str, int]:
    """Render events to the text block fed to the overview prompt. Under CHUNK_SIZE
    (or with no client) returns raw compact summaries; above it, condenses each
    chunk via one LLM call (falling back to raw summaries if a chunk call fails)."""
    summaries = [s for s in (summarize_event(e) for e in events) if s]
    if len(events) <= CHUNK_SIZE or client is None:
        return "\n\n".join(summaries), 0

    chunks = [events[i:i + CHUNK_SIZE] for i in range(0, len(events), CHUNK_SIZE)]
    narratives: list[str] = []
    calls = 0
    for idx, chunk in enumerate(chunks):
        block = "\n\n".join(s for s in (summarize_event(e) for e in chunk) if s)
        system, user = build_chunk_summary_prompt(
            project, block, f"chunk {idx + 1}/{len(chunks)} of {range_label}")
        try:
            narratives.append(client.chat(system, user, max_tokens=600, label="chunk").strip())
            calls += 1
        except LLMError:
            narratives.append(block)            # resilient: keep the raw detail
    return "\n\n".join(narratives), calls


# --------------------------------------------------------------------------- #
# Tier-2 detailed entries (append-only, day-by-day, LLM)
# --------------------------------------------------------------------------- #
def _period_key(date_key: str, period: str = "month") -> str:
    """Map '20260621' to the file/period stem: '2026-06' (month) or ISO '2026-W26' (week).

    Junk -> '0000-00' / '0000-W00'. The ISO week-year (not the calendar year) is used so a
    date in early January correctly lands in the adjacent year's last/first week."""
    if len(date_key) == 8 and date_key.isdigit():
        if period == "week":
            try:
                y, w, _ = date(int(date_key[:4]), int(date_key[4:6]),
                               int(date_key[6:])).isocalendar()
                return f"{y:04d}-W{w:02d}"
            except ValueError:
                pass
        else:
            return f"{date_key[:4]}-{date_key[4:6]}"
    return "0000-W00" if period == "week" else "0000-00"


def _entry_section_wrap(date_label: str, entry: str) -> str:
    """Frame one day's narrated entry the same way archive sections are framed, so the
    idempotent-by-date merge (_merge_dated_sections) treats entries and archive alike."""
    return f"---\n## {date_label}\n\n{entry.strip()}\n\n---\n"


def _entry_hints(project: dict[str, Any]) -> list[str]:
    """Per-project 'be sure to capture' directives: explicit signals.entry_extract,
    else fall back to the project description + keywords so extraction is still steered."""
    sig = project.get("signals", {}) or {}
    explicit = [str(h) for h in (sig.get("entry_extract") or []) if str(h).strip()]
    if explicit:
        return explicit
    out: list[str] = []
    if project.get("description"):
        out.append(str(project["description"]))
    kws = sig.get("keywords") or []
    if kws:
        out.append("keywords: " + ", ".join(str(k) for k in kws))
    return out


def _entry_for_day(project: dict[str, Any], day_events: list[NormalizedEvent],
                     date_label: str, client: Any, options: SynthesisOptions,
                     ) -> tuple[str, str, int, str | None]:
    """One detailed entry for one day. Returns (section_for_file,
    feed_text_for_living_doc, llm_calls, error). On LLMError the day is NEVER lost — it
    falls back to the deterministic archive section (raw but complete)."""
    block, calls = _activity_block(project, day_events, date_label, client)
    system, user = build_entry_prompt(project, block, date_label, _entry_hints(project))
    try:
        raw = client.chat(system, user, max_tokens=options.entry_max_tokens, label="entry")
        entry = raw.strip()
        if not entry:                                   # empty reply -> deterministic fallback
            raise LLMError("empty entry reply")
        return (_entry_section_wrap(date_label, entry),
                f"## {date_label}\n{entry}", calls + 1, None)
    except LLMError as exc:
        fallback = build_archive_section(date_label, day_events)   # already a framed section
        return (fallback, fallback, calls, f"entry LLM failed ({date_label}): {exc}")


# --------------------------------------------------------------------------- #
# F1/F2 — whole-day budget packer + batched entry (chunk-not-condense)
# --------------------------------------------------------------------------- #
def _raw_day_block(events: list[NormalizedEvent]) -> str:
    """Raw compact event summaries for a day — NO condensing. This is the chunk-not-condense
    input: detail is preserved and, if too large for one call, split across calls at day
    boundaries by the packer (never within a day)."""
    return "\n\n".join(s for s in (summarize_event(e) for e in events) if s)


def _span_days(first_key: str, last_key: str) -> int:
    """Inclusive calendar span in days between two 'YYYYMMDD' keys (junk -> 1)."""
    try:
        a = date(int(first_key[:4]), int(first_key[4:6]), int(first_key[6:8]))
        b = date(int(last_key[:4]), int(last_key[4:6]), int(last_key[6:8]))
        return (b - a).days + 1
    except (ValueError, TypeError):
        return 1


def pack_days_by_budget(dates: list[str], by_date: dict[str, list[NormalizedEvent]],
                        options: SynthesisOptions) -> list[list[str]]:
    """Group WHOLE days (date keys, ascending) into batched-call "units". A day is atomic —
    the packer only ever cuts BETWEEN days, never within one:

    * ``entry_batch='day'``      -> one unit per day.
    * ``entry_batch='week'``     -> break at ISO-week boundaries, and also on budget/span.
    * ``entry_batch='adaptive'`` -> greedy: flush the current unit when the NEXT day would
      push it over ``max_input_tokens`` OR it already spans ``max_batch_days`` calendar
      days — whichever first. A single day whose own rendered input exceeds the budget
      still forms its own (over-budget) unit rather than being split mid-day.
    """
    if options.entry_batch == "day":
        return [[d] for d in dates]

    budget = options.max_input_tokens
    span_cap = max(1, options.max_batch_days)
    tok = {d: est_tokens(_raw_day_block(by_date[d])) for d in dates}

    units: list[list[str]] = []
    unit: list[str] = []
    unit_tok = 0
    for d in dates:
        if unit:
            over_budget = budget > 0 and (unit_tok + tok[d]) > budget
            over_span = _span_days(unit[0], d) > span_cap
            new_week = (options.entry_batch == "week"
                        and _period_key(d, "week") != _period_key(unit[0], "week"))
            if over_budget or over_span or new_week:
                units.append(unit)
                unit, unit_tok = [], 0
        unit.append(d)
        unit_tok += tok[d]
    if unit:
        units.append(unit)
    return units


def _split_batched_reply(text: str) -> dict[str, str]:
    """Split a batched entry reply into ``{date_label: body}`` on its ``## YYYY-MM-DD``
    headers. Anything before the first header (preamble) is dropped; a day the model
    omitted simply won't appear (the caller falls back to that day's archive section)."""
    import re
    out: dict[str, str] = {}
    parts = re.split(r"(?m)^\s{0,3}#{1,3}\s*(\d{4}-\d{2}-\d{2})\s*$", text or "")
    for i in range(1, len(parts) - 1, 2):          # [preamble, date, body, date, body, ...]
        label = parts[i].strip()
        body = parts[i + 1].strip()
        if label and body:
            out[label] = body
    return out


def _entry_for_unit(project: dict[str, Any], unit: list[str],
                    by_date: dict[str, list[NormalizedEvent]], client: Any,
                    options: SynthesisOptions,
                    ) -> tuple[dict[str, str], list[str], int, list[str]]:
    """Detailed entries for one packer unit (one-or-more WHOLE days) in a single LLM call,
    RAW (chunk-not-condense). Returns ``(sections_by_datekey, feeds, llm_calls, errors)``.
    On LLMError/empty (or a day the model drops) that day falls back to its deterministic
    archive section — never lost (C5)."""
    labels = {d: _date_label(d) for d in unit}

    if len(unit) == 1:                                     # single day — raw, no condense
        d = unit[0]
        block = _raw_day_block(by_date[d])
        system, user = build_entry_prompt(project, block, labels[d], _entry_hints(project))
        try:
            entry = client.chat(system, user, max_tokens=options.entry_max_tokens,
                                 label="entry").strip()
            if not entry:
                raise LLMError("empty entry reply")
            return ({d: _entry_section_wrap(labels[d], entry)},
                    [f"## {labels[d]}\n{entry}"], 1, [])
        except LLMError as exc:
            fb = build_archive_section(labels[d], by_date[d])
            return ({d: fb}, [fb], 0, [f"entry LLM failed ({labels[d]}): {exc}"])

    # Multi-day batch — one call emitting per-day sections.
    days_block = "\n\n".join(f"## {labels[d]}\n{_raw_day_block(by_date[d])}" for d in unit)
    date_labels = [labels[d] for d in unit]
    system, user = build_batched_entry_prompt(
        project, days_block, date_labels, _entry_hints(project))
    max_tokens = min(8000, max(options.entry_max_tokens,
                               options.entry_max_tokens * len(unit)))
    try:
        bodies = _split_batched_reply(
            client.chat(system, user, max_tokens=max_tokens, label="entry"))
        calls = 1
    except LLMError as exc:
        bodies, calls = {}, 0
        prefix = f"batched entry LLM failed ({date_labels[0]}..{date_labels[-1]}): {exc}"
    else:
        prefix = ""

    sections: dict[str, str] = {}
    feeds: list[str] = []
    errs: list[str] = [prefix] if prefix else []
    for d in unit:
        body = (bodies.get(labels[d]) or "").strip()
        if body:
            sections[d] = _entry_section_wrap(labels[d], body)
            feeds.append(f"## {labels[d]}\n{body}")
        else:                                              # dropped/failed -> archive fallback
            fb = build_archive_section(labels[d], by_date[d])
            sections[d] = fb
            feeds.append(fb)
            if not prefix:
                errs.append(f"batched entry missing day {labels[d]}")
    return sections, feeds, calls, errs


# --------------------------------------------------------------------------- #
# Per-project synthesis
# --------------------------------------------------------------------------- #
def synthesize_project(project: dict[str, Any], events: list[NormalizedEvent],
                       current_overview: str, *, today: str, client: Any,
                       options: SynthesisOptions | None = None,
                       entry_only: set[str] | None = None) -> ProjectJournal:
    """Build the deterministic archive (always), the optional tier-2 detailed entries,
    and the LLM living overview (best-effort). On any LLM failure the overview is left
    unchanged and the archive is preserved; an entry-call failure degrades that day to
    its deterministic archive section. ``options`` defaults to entry-writing OFF, so the
    behavior is byte-identical to before the entries feature when not enabled."""
    options = options or DEFAULT_SYNTHESIS
    pid = project["id"]
    events = sorted(events, key=lambda e: e.ts_wall)

    by_date: dict[str, list[NormalizedEvent]] = {}
    for e in events:
        by_date.setdefault(_date_key(e.ts_wall), []).append(e)
    dates = sorted(by_date)
    archive_section = "".join(build_archive_section(_date_label(d), by_date[d]) for d in dates)
    date_range = _date_label(dates[0]) if len(dates) == 1 else \
        f"{_date_label(dates[0])} to {_date_label(dates[-1])}"

    if client is None:
        return ProjectJournal(pid, len(events), archive_section, current_overview, "", 0, None)

    # Tier 2 — detailed entries, one focused call per day, routed to period files
    # (month or week per options.entry_period; the entry stays per-day either way).
    entries_by_period: dict[str, list[str]] = {}
    entry_feed: list[str] = []
    entry_calls = 0
    entry_errs: list[str] = []
    if options.write_entries:
        entry_dates = [d for d in dates if entry_only is None or d in entry_only]
        # Legacy per-day path (byte-identical): day-batching AND no input budget set.
        if options.entry_batch == "day" and options.max_input_tokens <= 0:
            for d in entry_dates:
                section, feed, calls, err = _entry_for_day(
                    project, by_date[d], _date_label(d), client, options)
                entry_calls += calls
                entries_by_period.setdefault(
                    _period_key(d, options.entry_period), []).append(section)
                entry_feed.append(feed)
                if err:
                    entry_errs.append(err)
        else:                                              # F1/F2 — budget-packed batches
            for unit in pack_days_by_budget(entry_dates, by_date, options):
                sections, feeds, calls, errs = _entry_for_unit(
                    project, unit, by_date, client, options)
                entry_calls += calls
                for d in unit:
                    entries_by_period.setdefault(
                        _period_key(d, options.entry_period), []).append(sections[d])
                entry_feed.extend(feeds)
                entry_errs.extend(errs)

    # Tier 3 — living overview. When entry-writing is on it ROLLS UP the (already-summarized)
    # entries and is told to stay high-level; otherwise it summarizes raw events.
    if options.write_entries:
        block, llm_calls = "\n\n".join(entry_feed), 0
        system, user = build_overview_prompt(project, current_overview, block, date_range,
                                          today, high_level=True)
    else:
        block, llm_calls = _activity_block(project, events, date_range, client)
        system, user = build_overview_prompt(project, current_overview, block, date_range, today)

    overview_md, daily_paragraph, error = current_overview, "", None
    try:
        raw = client.chat(system, user, max_tokens=2000, label="overview")
        llm_calls += 1
        if DAILY_SEP in raw:
            head, tail = raw.split(DAILY_SEP, 1)
            overview_md = head.strip() + "\n"
            daily_paragraph = tail.strip()
        else:
            overview_md = raw.strip() + "\n"
    except LLMError as exc:
        error = f"overview LLM failed: {exc}"

    return ProjectJournal(
        pid, len(events), archive_section, overview_md, daily_paragraph, llm_calls, error,
        entries_by_period={m: "".join(parts) for m, parts in entries_by_period.items()},
        entry_calls=entry_calls,
        entry_error="; ".join(entry_errs) if entry_errs else None)


# --------------------------------------------------------------------------- #
# Cross-project executive summary
# --------------------------------------------------------------------------- #
def _fallback_exec_body(paragraphs: dict[str, str]) -> str:
    out: list[str] = []
    for pid, para in paragraphs.items():
        out.append(f"## {pid}")
        out.append(para or "(no daily summary produced)")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def synthesize_exec_summary(paragraphs: dict[str, str], day: str,
                            *, client: Any) -> tuple[str, str | None]:
    """Return (body_markdown, error). Falls back to a deterministic concatenation
    of the per-project paragraphs if the LLM is unavailable or fails (C5)."""
    if not paragraphs:
        return "", None
    if client is None:
        return _fallback_exec_body(paragraphs), None
    system, user = build_exec_summary_prompt(paragraphs, day)
    try:
        body = client.chat(system, user, max_tokens=1200, label="exec")
    except LLMError as exc:
        return _fallback_exec_body(paragraphs), f"exec summary LLM failed: {exc}"
    return body.strip() + "\n", None


# --------------------------------------------------------------------------- #
# Output writers
# --------------------------------------------------------------------------- #
def _prepend_daily(journal_dir: Path, today: str, paragraphs: dict[str, str]) -> None:
    if not paragraphs:
        return
    lines = [f"## {today}", ""]
    for pid, para in paragraphs.items():
        lines.append(f"**{pid}** — {para}")
        lines.append("")
    lines.append("---")
    lines.append("")
    section = "\n".join(lines)

    daily_path = journal_dir / "daily.md"
    existing = daily_path.read_text(encoding="utf-8") if daily_path.exists() else ""
    daily_path.write_text(section + existing, encoding="utf-8")


def _write_exec_summary(journal_dir: Path, today: str, body: str) -> None:
    doc = f"# Executive Summary — {today}\n\n{body.rstrip()}\n"
    (journal_dir / "executive_summary.md").write_text(doc, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Period summary — the durable weekly/monthly cross-project retrospective.
# Read-only over the already-gated entries/archive sections (same posture as
# `tl ask`); writes journal/summaries/<period>.md. Idempotent (overwrites).
# --------------------------------------------------------------------------- #
def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return ""


def _sections_in_period(text: str, period_key: str, period: str) -> str:
    """Keep only the dated sections of ``text`` (archive/entries markdown) whose date falls
    in ``period_key`` (the section's '## YYYY-MM-DD' label mapped through _period_key)."""
    keep = [chunk for label, chunk in _iter_dated_sections(text)
            if _period_key(label.replace("-", ""), period) == period_key]
    return "".join(keep)


def _collect_period_sections(journal_dir: Path, period_key: str,
                             period: str) -> dict[str, str]:
    """Per project, the already-recorded text for one period: the richer entries
    when present, else the deterministic archive sections (so a summary works even with
    entry-writing off). Returns ``{project_id: text}`` for projects with activity that period."""
    out: dict[str, str] = {}
    for pdir in sorted(Path(journal_dir).glob("project_*")):
        pid = pdir.name[len("project_"):]
        text = ""
        edir = pdir / "entries"
        if edir.is_dir():
            for jf in sorted(edir.glob("*.md")):
                text += _sections_in_period(_read_text(jf), period_key, period)
        if not text.strip():                       # fall back to the deterministic archive
            text = _sections_in_period(_read_text(pdir / "archive.md"), period_key, period)
        if text.strip():
            out[pid] = text.strip()
    return out


def _period_label(period_key: str, period: str) -> str:
    return f"week {period_key}" if period == "week" else f"month {period_key}"


def _fallback_period_body(project_sections: dict[str, str]) -> str:
    out: list[str] = []
    for pid, text in project_sections.items():
        out.append(f"## {pid}")
        out.append(text)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def summarize_period(journal_dir: str | Path, period_key: str, period: str,
                     *, client: Any) -> tuple[str, str | None]:
    """Distil one period (week/month) of work across all projects into a retrospective.
    Returns ``(body_markdown, error)`` — ``("", None)`` when the period has no activity.
    Falls back to a deterministic concatenation of the per-project sections when the LLM
    is unavailable or fails (C5). Reads only already-gated journal output; never the bus."""
    sections = _collect_period_sections(Path(journal_dir), period_key, period)
    if not sections:
        return "", None
    if client is None:
        return _fallback_period_body(sections), None
    system, user = build_period_summary_prompt(_period_label(period_key, period), sections)
    try:
        body = client.chat(system, user, max_tokens=1500, label="period")
    except LLMError as exc:
        return (_fallback_period_body(sections),
                f"period summary LLM failed ({period_key}): {exc}")
    return body.strip() + "\n", None


def _write_period_summary(journal_dir: Path, period_key: str, period: str, body: str) -> None:
    sdir = journal_dir / "summaries"
    sdir.mkdir(parents=True, exist_ok=True)
    heading = "Weekly summary" if period == "week" else "Monthly summary"
    doc = f"# {heading} — {period_key}\n\n{body.rstrip()}\n"
    (sdir / f"{period_key}.md").write_text(doc, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Skip-unchanged guard (opt-in, default OFF) — reuse a project's already-written
# LLM output when its event batch is unchanged since the last synthesis, so a
# re-run does not re-bill the model. State lives in journal/.synth_state.json
# (under gitignored journal/); the deterministic archive is still rewritten.
# --------------------------------------------------------------------------- #
SYNTH_STATE_FILE = ".synth_state.json"


def _project_fingerprint(events: list[NormalizedEvent]) -> str:
    """A stable, order-independent content hash of a project's event batch. Two runs
    over the same (gated) events produce the same digest; any added/changed/removed
    event changes it — so the guard reuses old output only when nothing moved."""
    digests = sorted(
        hashlib.sha256(
            json.dumps(e.to_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        for e in events)
    h = hashlib.sha256()
    for d in digests:
        h.update(d.encode("ascii"))
    return h.hexdigest()


def _load_synth_state(journal_dir: Path) -> dict[str, dict[str, Any]]:
    p = journal_dir / SYNTH_STATE_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_synth_state(journal_dir: Path, state: dict[str, dict[str, Any]]) -> None:
    p = journal_dir / SYNTH_STATE_FILE
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


def _project_archive(events: list[NormalizedEvent]) -> str:
    """The deterministic archive markdown for a project's whole batch (all its dates).
    Extracted so the skip path can rebuild the archive with no LLM work."""
    ordered = sorted(events, key=lambda e: e.ts_wall)
    by_date: dict[str, list[NormalizedEvent]] = {}
    for e in ordered:
        by_date.setdefault(_date_key(e.ts_wall), []).append(e)
    return "".join(build_archive_section(_date_label(d), by_date[d])
                   for d in sorted(by_date))


# --------------------------------------------------------------------------- #
# F3 — per-project spread scheduling (which weekday a project's LLM tiers run)
# --------------------------------------------------------------------------- #
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def project_synthesis_day(project: dict[str, Any]) -> str:
    """The weekday a project's expensive LLM tiers run on: ``"daily"`` (default — every
    run) or ``"mon".."sun"``. Read from per-project ``synthesis.day``; anything
    unrecognized -> ``"daily"`` (so a typo never silently mutes a project)."""
    day = str(((project or {}).get("synthesis") or {}).get("day", "daily")).strip().lower()
    return day if day in _WEEKDAYS else "daily"


def is_due(project: dict[str, Any], today: date) -> bool:
    """Whether a project's LLM tiers should run today: ``"daily"`` -> always; a weekday ->
    only when it matches. Pure. The deterministic archive is written regardless of this —
    only the entry/overview calls are gated, so nothing is captured late."""
    day = project_synthesis_day(project)
    return day == "daily" or today.weekday() == _WEEKDAYS[day]


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def run(events: list[Any], projects: list[dict[str, Any]], *, journal_dir: str | Path,
        client: Any = None, today: str | None = None,
        options: SynthesisOptions | None = None) -> SynthesisRun:
    """Synthesize a batch of CATEGORIZED events into overview/archive/entries/daily/exec
    files.

    Events must already carry attribution (run Phase 1 first). The archive is
    written for every project with activity regardless of LLM availability; the
    overview, daily paragraph, executive summary, and (opt-in) detailed monthly entries
    use the LLM when `client` is given and degrade gracefully otherwise. ``options``
    defaults to entry-writing OFF — byte-identical to before the feature.
    """
    options = options or DEFAULT_SYNTHESIS
    today = today or date.today().isoformat()
    try:
        today_date = date.fromisoformat(today[:10])
    except ValueError:
        today_date = date.today()
    journal_dir = Path(journal_dir)
    journal_dir.mkdir(parents=True, exist_ok=True)

    evs = [_as_event(e) for e in events]
    groups = group_by_project(evs)
    by_id = {p["id"]: p for p in projects}

    result = SynthesisRun(today=today)
    paragraphs: dict[str, str] = {}

    # Persistent per-project state (journal/.synth_state.json) drives two opt-ins:
    #   * skip_unchanged — reuse LLM output when a project's events are byte-identical;
    #   * F3 scheduling  — a per-project weekday + a synth-days watermark so an assigned
    #                      project only re-bills its NEW days, and only on its day.
    # State is loaded/saved whenever either is active; both default off -> byte-identical.
    skip = options.skip_unchanged
    scheduling_on = any(project_synthesis_day(by_id.get(pid, {})) != "daily"
                        for pid in groups)
    use_state = skip or scheduling_on
    prior_state = _load_synth_state(journal_dir) if use_state else {}
    new_state: dict[str, dict[str, Any]] = dict(prior_state)
    any_changed = False

    for pid in sorted(groups):
        project = by_id.get(pid, {"id": pid, "name": pid})
        name = project.get("name", pid)
        project_dir = journal_dir / f"project_{pid}"
        project_dir.mkdir(parents=True, exist_ok=True)
        overview_path = project_dir / "overview.md"
        archive_path = project_dir / "archive.md"

        fp = _project_fingerprint(groups[pid]) if use_state else ""
        prior = prior_state.get(pid) or {}
        scheduled = project_synthesis_day(project) != "daily"

        # Reuse (no LLM): refresh the deterministic archive but keep the existing
        # overview/entries when EITHER this project isn't due today (F3 scheduling) OR its
        # events are unchanged (skip_unchanged). A staggered project's last output still
        # feeds the global daily/exec, so it appears every day. Bootstraps on first
        # sighting (no overview yet -> synthesize even if it's not the project's day).
        not_due = scheduled and not is_due(project, today_date) and overview_path.exists()
        unchanged = skip and prior.get("fingerprint") == fp and overview_path.exists()
        if not_due or unchanged:
            archive_section = _project_archive(groups[pid])
            existing_archive = archive_path.read_text(encoding="utf-8") \
                if archive_path.exists() else ""
            archive_path.write_text(
                _merge_dated_sections(existing_archive, archive_section), encoding="utf-8")
            daily = prior.get("daily", "")
            if daily:
                paragraphs[pid] = daily
            if use_state:
                new_state[pid] = prior          # keep the fingerprint/synth_days watermark
            result.projects.append(ProjectJournal(
                pid, len(groups[pid]), archive_section,
                overview_path.read_text(encoding="utf-8"), daily, 0, None))
            continue
        any_changed = True

        current = overview_path.read_text(encoding="utf-8") if overview_path.exists() \
            else overview_stub(name, today)

        # A scheduled project entries only its NEW days (past the watermark), so its due-day
        # run doesn't re-bill its whole history; an unscheduled 'daily' project entries all
        # days (entry_only=None) -> byte-identical to before F3.
        entry_only: set[str] | None = None
        if scheduled:
            done_days = set(prior.get("synth_days", []) or [])
            entry_only = {_date_key(e.ts_wall) for e in groups[pid]} - done_days

        pd = synthesize_project(project, groups[pid], current, today=today,
                                client=client, options=options, entry_only=entry_only)

        # Archive first (deterministic) — never lost to an LLM outage. Merged
        # idempotently so re-synthesizing a day replaces its section, not appends.
        existing_archive = archive_path.read_text(encoding="utf-8") \
            if archive_path.exists() else ""
        archive_path.write_text(
            _merge_dated_sections(existing_archive, pd.archive_section), encoding="utf-8")
        overview_path.write_text(pd.overview_md, encoding="utf-8")

        # Tier-2 detailed entries: one append-only file per month, same idempotent
        # by-date merge as the archive. Only written when entry-writing is enabled.
        if pd.entries_by_period:
            entries_dir = project_dir / "entries"
            entries_dir.mkdir(parents=True, exist_ok=True)
            for month, section in pd.entries_by_period.items():
                epath = entries_dir / f"{month}.md"
                existing_entries = epath.read_text(encoding="utf-8") \
                    if epath.exists() else ""
                epath.write_text(
                    _merge_dated_sections(existing_entries, section), encoding="utf-8")

        if pd.daily_paragraph:
            paragraphs[pid] = pd.daily_paragraph
        if use_state:
            entry: dict[str, Any] = {"fingerprint": fp, "daily": pd.daily_paragraph}
            if scheduled:                       # advance the synth-days watermark
                done_days = set(prior.get("synth_days", []) or [])
                if entry_only:
                    done_days |= entry_only
                entry["synth_days"] = sorted(done_days)
            elif prior.get("synth_days"):       # preserve a watermark if day was un-set
                entry["synth_days"] = prior["synth_days"]
            new_state[pid] = entry
        result.projects.append(pd)

    if use_state:
        _save_synth_state(journal_dir, new_state)

    # Nothing changed this run (all projects reused/not-due): the daily/exec/period files
    # on disk are still correct, so don't re-bill the model rebuilding them (and don't
    # prepend a duplicate daily).
    if use_state and not any_changed:
        return result

    _prepend_daily(journal_dir, today, paragraphs)
    body, exec_error = synthesize_exec_summary(paragraphs, today, client=client)
    if body:
        _write_exec_summary(journal_dir, today, body)
    result.exec_summary = body
    result.exec_error = exec_error

    # Period summary tier — (re)build the weekly/monthly retrospective(s) the run's dates
    # fall into, distilled from the just-written entries/archive sections. Idempotent;
    # off by default (function-level), enabled via config synthesis.summary_cadence.
    if options.summary_cadence in ("weekly", "monthly"):
        gran = "week" if options.summary_cadence == "weekly" else "month"
        run_dates = {_date_key(e.ts_wall) for evlist in groups.values() for e in evlist}
        errs: list[str] = []
        for pk in sorted({_period_key(d, gran) for d in run_dates}):
            sbody, serr = summarize_period(journal_dir, pk, gran, client=client)
            if sbody:
                _write_period_summary(journal_dir, pk, gran, sbody)
                result.summaries.append(pk)
            if serr:
                errs.append(serr)
        result.summary_error = "; ".join(errs) if errs else None
    return result


def load_events(path: str | Path) -> list[NormalizedEvent]:
    """Read a persisted thin-log JSONL, reconcile to real order, return events."""
    from throughlog.timeline import load_jsonl, reconcile
    return [NormalizedEvent.from_dict(d) for d in reconcile(load_jsonl(path))]


# --------------------------------------------------------------------------- #
# Live smoke — `python -m throughlog.synthesize --smoke`
# --------------------------------------------------------------------------- #
def _smoke(argv: list[str] | None = None) -> int:
    import argparse
    import shutil
    import sys
    import tempfile
    from throughlog.config import load_config, load_projects
    from throughlog.llm.client import LLMConfig, LLMClient
    from throughlog.schema import make_event

    ap = argparse.ArgumentParser(description="Live Phase-2 smoke: synthesize a tiny day.")
    ap.add_argument("--smoke", action="store_true")
    ap.parse_args(argv)

    # The LLM may emit unicode the local console codepage can't encode (e.g. a
    # non-breaking hyphen on a Windows cp125x terminal). Don't let printing crash
    # the smoke — the files on disk are UTF-8 regardless.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    projects = load_projects()
    cfg = LLMConfig.from_config(load_config())
    client = LLMClient(cfg)
    print(f"model={cfg.model}  key={'present' if cfg.resolve_key() else 'MISSING'}")

    pid = projects[0]["id"] if projects else "demo"

    # Pre-attributed (we are testing Phase 2 in isolation, not Phase 1).
    def ev(t, payload, ts):
        e = make_event(t, kind="os", adapter="os_focus", payload=payload, ts_wall=ts)
        e.attribution.project_id = pid
        e.attribution.confidence = 0.95
        e.attribution.method = "signal_path"
        return e

    events = [
        ev(FOCUS_SESSION, {"anchor": "overview design - editor", "process": "Code.exe",
                           "active_file": "throughlog/synthesize.py", "duration_sec": 1800,
                           "mode": "producing",
                           "intent": {"label": "writing the Phase 2 synthesizer",
                                      "method": "title", "confidence": 0.8}},
           "2026-06-21T10:00:00+03:00"),
        ev(GIT_COMMIT, {"repo": "throughlog", "actor": "human",
                        "message": "feat: M8 phase 2 synthesis",
                        "files": ["throughlog/synthesize.py"]},
           "2026-06-21T11:30:00+03:00"),
        ev(NARRATION, {"note": "wired the deterministic archive so it survives an LLM outage",
                       "meaningful": True}, "2026-06-21T11:45:00+03:00"),
    ]

    out = Path(tempfile.mkdtemp(prefix="sal_synth_smoke_"))
    try:
        res = run(events, projects, journal_dir=out, client=client, today="2026-06-21")
        overview_file = out / f"project_{pid}" / "overview.md"
        archive_file = out / f"project_{pid}" / "archive.md"
        overview = overview_file.read_text(encoding="utf-8") if overview_file.exists() else ""
        archive = archive_file.read_text(encoding="utf-8") if archive_file.exists() else ""
        print(f"\narchive.md ({len(archive)} chars) — deterministic, head:")
        print("\n".join(archive.splitlines()[:8]))
        pd = res.projects[0] if res.projects else None
        if pd and pd.error:
            print(f"\n[!] overview LLM error: {pd.error}")
        print(f"\noverview.md ({len(overview)} chars) — head:")
        print("\n".join(overview.splitlines()[:6]))
        print(f"\nexecutive_summary.md ({len(res.exec_summary)} chars), "
              f"exec_error={res.exec_error}")
        ok_archive = "### Sessions" in archive and "M8" in archive
        ok_overview = bool(pd) and pd.error is None and "## " in overview and len(overview) > 200
        if ok_archive and ok_overview:
            print("\nRESULT: live Phase-2 synthesis works (archive + LLM overview).")
            return 0
        if ok_archive and pd and pd.error:
            print("\nRESULT: archive written; LLM overview failed but pipeline stayed safe "
                  "(consider the fallback model).")
            return 0
        print("\nRESULT: unexpected synthesis state.")
        return 1
    finally:
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    import sys
    sys.exit(_smoke())
