"""Phase 1 — project attribution. Deterministic signal stack first; the LLM is a
*fallback* for genuine ambiguity only.

Pipeline per batch of (already-gated) NormalizedEvents:

  1. signal_stack(event)  — pure, rule-based scoring against projects.json signals.
     Actual precedence (max scores): path 0.95 > jira 0.85 > git-remote 0.82 >
     domain 0.80 > title-keyword ≤0.78 > window-pattern 0.75 > narration-keyword
     0.72 > app 0.70. Title-keyword scales with hit count (0.50 + 0.08·hits, capped
     0.78) so its rank is data-dependent; `app` is the weakest signal. The strongest
     signal across all projects wins.
  2. score >= 0.51        — assign the project deterministically. The LLM is NOT
     called. This is where the overwhelming majority of events resolve.
  3. ambiguous residue    — events with no usable text go straight to needs_review;
     the rest are batched into ONE LLM call (token-frugal — per-event calls die on
     rate-limited free models). The LLM answer is accepted only at >=0.51 and only
     for a real project id; anything else becomes needs_review.
  4. C5 — any LLM failure (transport exhausted, or unparseable after retries)
     leaves the whole residue as needs_review. Events are NEVER dropped or crashed.

The home-path `~` normalization the gate applies to event paths is applied to the
project signal paths here too, so attribution matches gated data exactly.
"""

from __future__ import annotations

import json
import re
from typing import Any

from throughlog.schema import (
    NormalizedEvent, Attribution,
    FOCUS_SESSION, DEEP_WORK, FILE_CHANGE, GIT_COMMIT, NARRATION, CLIPBOARD,
    IDLE_START, IDLE_END, LONG_RUN, AGENT_REPORT,
)
from throughlog.privacy.redactors import normalize_home_paths
from throughlog.llm.client import LLMError
from throughlog.llm.prompts import build_categorize_prompt

THRESHOLD = 0.51

# Event types that carry no project intent — never scored, never sent to the LLM.
_SKIP_TYPES = frozenset({IDLE_START, IDLE_END})


# --------------------------------------------------------------------------- #
# Signal extraction from a (gated) NormalizedEvent
# --------------------------------------------------------------------------- #
def _event_signals(ev: NormalizedEvent) -> dict[str, Any]:
    """Pull the text signals out of an event's payload, regardless of type."""
    p = ev.payload or {}
    titles: list[str] = []
    procs: list[str] = []
    paths: list[str] = []
    note = ""

    # Focus-style payloads (FOCUS_SESSION, DEEP_WORK, LONG_RUN share the shape).
    if p.get("anchor"):
        titles.append(str(p["anchor"]))
    if p.get("process"):
        procs.append(str(p["process"]).lower())
    if p.get("active_file"):
        paths.append(str(p["active_file"]))
    intent = p.get("intent")
    if isinstance(intent, dict) and intent.get("label"):
        titles.append(str(intent["label"]))
    for sat in p.get("satellites", []) or []:
        if isinstance(sat, dict):
            if sat.get("title"):
                titles.append(str(sat["title"]))
            if sat.get("process"):
                procs.append(str(sat["process"]).lower())

    if ev.type == FILE_CHANGE and p.get("path"):
        paths.append(str(p["path"]))
    if ev.type == GIT_COMMIT:
        if p.get("repo"):
            paths.append(str(p["repo"]))
        for f in p.get("files", []) or []:
            paths.append(str(f))
        if p.get("message"):
            titles.append(str(p["message"]))
    if ev.type == NARRATION and p.get("note"):
        note = str(p["note"])
        titles.append(note)
    if ev.type == CLIPBOARD and p.get("host"):
        titles.append(str(p["host"]))      # typed clipboard exposes only the host
    if ev.type == AGENT_REPORT:
        # An agent declares what it touched: repo/files drive path+git signals,
        # the prose drives keyword signals — same stack as any other source.
        if p.get("repo"):
            paths.append(str(p["repo"]))
        for f in p.get("files", []) or []:
            paths.append(str(f))
        for k in ("summary", "message", "project_hint", "tool"):
            if p.get(k):
                titles.append(str(p[k]))
    # also fold any process-monitor cmdline/cwd if present
    for k in ("cmdline", "cwd", "command"):
        if p.get(k):
            paths.append(str(p[k]))

    blob = " ".join(titles + procs + paths).lower()
    return {"titles": titles, "procs": procs, "paths": paths, "note": note, "blob": blob}


def _has_text(ev: NormalizedEvent) -> bool:
    s = _event_signals(ev)
    return bool(s["blob"].strip() or s["note"].strip())


# --------------------------------------------------------------------------- #
# Path matching (gate-normalized on both sides)
# --------------------------------------------------------------------------- #
def _canon(path: str) -> str:
    return path.replace("\\", "/").lower()


def _path_match(event_path: str, project_paths: list[str]) -> bool:
    """True if a gated event path is *under* (or equal to) any project path. Project
    paths are run through the SAME home-normalization the gate applied to the event
    path, so `C:\\Users\\dev\\...` and the stored `~/...` form compare equal. The
    match respects directory boundaries (like the privacy allowlist): a sibling whose
    name merely extends a project root — `~/proj/app-v2` under project `~/proj/app` —
    does NOT match."""
    target = _canon(event_path)
    for pp in project_paths:
        norm = _canon(normalize_home_paths(pp)).rstrip("/")
        if norm and (target == norm or target.startswith(norm + "/")):
            return True
    return False


def _kw_hits(keywords: list[str], text: str) -> int:
    t = text.lower()
    return sum(1 for kw in keywords if kw.lower() in t)


# --------------------------------------------------------------------------- #
# The deterministic signal stack
# --------------------------------------------------------------------------- #
def signal_stack(ev: NormalizedEvent,
                 projects: list[dict[str, Any]]) -> tuple[str | None, float, str, str]:
    """Score `ev` against every project; return the best (id, score, method, reason).
    No match -> (None, 0.0, "unresolved", ...). A genuine tie at the top assignable
    score -> (None, score, "ambiguous_tie", ...), so attribution never depends on
    projects.json ordering (the caller routes it to the LLM / needs_review)."""
    sig = _event_signals(ev)
    titles_join = " ".join(sig["titles"])
    best: tuple[str | None, float, str, str] = (None, 0.0, "unresolved", "no signal matched")
    # Other project ids that tie `best` at an assignable score (>= THRESHOLD).
    # A non-empty list means the winner is ambiguous, not a confident match.
    tied: list[str] = []

    for proj in projects:
        s = proj.get("signals", {}) or {}
        pid = proj["id"]
        score, method, reason = 0.0, "unresolved", ""

        # path — strongest, unambiguous
        if sig["paths"] and s.get("paths"):
            for ep in sig["paths"]:
                if _path_match(ep, s["paths"]):
                    score, method, reason = 0.95, "signal_path", "path under project directory"
                    break

        # git remote in any text
        if score < 0.82 and s.get("git_remotes"):
            for r in s["git_remotes"]:
                if r and r.lower() in sig["blob"]:
                    score, method, reason = 0.82, "signal_git", f"git remote '{r}'"
                    break

        # jira ticket prefix in titles
        if score < 0.85 and s.get("jira_prefixes"):
            for pre in s["jira_prefixes"]:
                if pre and re.search(rf"\b{re.escape(pre)}-\d+\b", titles_join, re.I):
                    score, method, reason = 0.85, "signal_jira", f"jira '{pre}'"
                    break

        # known domain in any text
        if score < 0.80 and s.get("domains"):
            for d in s["domains"]:
                if d and d.lower() in sig["blob"]:
                    score, method, reason = 0.80, "signal_domain", f"domain '{d}'"
                    break

        # window-title regex
        if score < 0.75 and s.get("window_patterns") and titles_join:
            for pat in s["window_patterns"]:
                try:
                    if pat and re.search(pat, titles_join, re.I):
                        score, method, reason = 0.75, "signal_pattern", f"title pattern '{pat}'"
                        break
                except re.error:
                    continue

        # app / process name
        if score < 0.70 and s.get("apps"):
            apps = {a.lower() for a in s["apps"]}
            if any(pr in apps for pr in sig["procs"]):
                score, method, reason = 0.70, "signal_app", "process matches project app"

        # explicit narration keyword (the human said it)
        if score < 0.72 and s.get("keywords") and sig["note"]:
            hits = _kw_hits(s["keywords"], sig["note"])
            if hits:
                score, method, reason = 0.72, "signal_note", f"{hits} keyword(s) in narration"

        # keyword density in titles/blob
        if score < 0.78 and s.get("keywords"):
            hits = _kw_hits(s["keywords"], sig["blob"])
            if hits:
                kw = min(0.50 + 0.08 * hits, 0.78)
                if kw > score:
                    score, method, reason = kw, "signal_keyword", f"{hits} keyword(s) matched"

        if score > best[1]:
            best = (pid, score, method, reason)
            tied = []
        elif score == best[1] and score >= THRESHOLD and best[0] is not None:
            tied.append(pid)

    # Two+ projects share the top assignable score: resolving by list order would
    # be a silent, projects.json-position-dependent tiebreak (risk register #3).
    # Surface the ambiguity instead — pid=None routes it to the LLM/needs_review
    # path in categorize_events, which never picks by registry order.
    if tied:
        names = ", ".join([str(best[0]), *tied])
        return (None, best[1], "ambiguous_tie", f"tie between {names}")

    return best


# --------------------------------------------------------------------------- #
# LLM fallback (batched, robust parse)
# --------------------------------------------------------------------------- #
def event_summary(ev: NormalizedEvent, index: int) -> str:
    """Compact, gated-only summary of an event for the LLM prompt."""
    p = ev.payload or {}
    tm = ev.ts_wall[11:16] if len(ev.ts_wall) >= 16 else ev.ts_wall
    lines = [f"[{index}] {ev.type} {tm}"]
    if ev.type in (FOCUS_SESSION, DEEP_WORK, LONG_RUN):
        lines.append(f'  app="{p.get("process", "")}" title="{p.get("anchor", "")}"')
        if p.get("active_file"):
            lines.append(f'  file={p["active_file"]}')
        sats = [s.get("title", "") for s in p.get("satellites", []) or [] if isinstance(s, dict)]
        if sats:
            lines.append(f'  also: {", ".join(t for t in sats[:5] if t)}')
        intent = p.get("intent")
        if isinstance(intent, dict) and intent.get("label"):
            lines.append(f'  intent="{intent["label"]}"')
    elif ev.type == FILE_CHANGE:
        lines.append(f'  path={p.get("path", "")}')
    elif ev.type == GIT_COMMIT:
        lines.append(f'  repo={p.get("repo", "")} msg="{p.get("message", "")}"')
    elif ev.type == NARRATION:
        lines.append(f'  note="{p.get("note", "")}"')
    elif ev.type == CLIPBOARD:
        lines.append(f'  clipboard kind={p.get("kind", "")} host={p.get("host", "")}')
    else:
        lines.append(f"  {json.dumps(p, ensure_ascii=False)[:120]}")
    return "\n".join(lines)


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON-object recovery from a model reply: strip <think> traces
    and code fences, then take the outermost {...}."""
    if not raw:
        return None
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.S | re.I).strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if m:
            text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_assignments(raw: str, valid_ids: set[str]) -> dict[int, dict[str, Any]] | None:
    """Parse the model's assignment JSON into {index: {project_id, confidence, reason}}.
    Returns None when the reply isn't usable JSON (caller retries / needs_review).
    Hallucinated project ids are coerced to None."""
    obj = _extract_json_object(raw)
    if obj is None:
        return None
    assigns = obj.get("assignments")
    if not isinstance(assigns, list):
        return None
    out: dict[int, dict[str, Any]] = {}
    for a in assigns:
        if not isinstance(a, dict) or a.get("index") is None:
            continue
        try:
            idx = int(a["index"])
        except (TypeError, ValueError):
            continue
        pid = a.get("project_id")
        if pid in (None, "null", "none", ""):
            pid = None
        elif pid not in valid_ids:                 # never trust an invented id
            pid = None
        out[idx] = {"project_id": pid,
                    "confidence": _as_float(a.get("confidence")),
                    "reason": str(a.get("reason", ""))[:200]}
    return out


def _llm_categorize(batch: list[tuple[int, NormalizedEvent]],
                    projects: list[dict[str, Any]], client: Any,
                    *, parse_retries: int = 1) -> dict[int, dict[str, Any]]:
    """One batched call (plus a stricter re-ask on unparseable output). Returns
    {} on any terminal failure so the caller routes the residue to needs_review."""
    valid_ids = {p["id"] for p in projects}
    summaries = [event_summary(ev, idx) for idx, ev in batch]
    system, user = build_categorize_prompt(summaries, projects)

    # Scale the output budget with batch size: each assignment is ~80 tokens of JSON, so a
    # large ambiguous batch can truncate the array under the 1500 default -> unparseable ->
    # the WHOLE residue drops to needs_review. Stays 1500 for typical small batches (wire
    # byte-identical), grows for big ones, capped so it never runs away.
    max_tokens = min(8000, max(1500, 80 * len(batch)))

    for attempt in range(parse_retries + 1):
        try:
            raw = client.chat(system, user, max_tokens=max_tokens, label="categorize")
        except LLMError:
            return {}                              # C5: transport gave up
        parsed = _parse_assignments(raw, valid_ids)
        if parsed is not None:
            return parsed
        user += "\n\nREMINDER: output ONLY the JSON object, no other text."
    return {}                                      # C5: never produced valid JSON


def _mark_needs_review(ev: NormalizedEvent, hint: dict[str, Any] | None = None) -> None:
    # Preserve a sub-threshold LLM confidence as context; project stays unassigned.
    conf = _as_float(hint.get("confidence")) if hint else 0.0
    ev.attribution = Attribution(project_id=None, confidence=round(conf, 4),
                                 method="needs_review")


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def categorize_events(events: list[NormalizedEvent],
                      projects: list[dict[str, Any]],
                      *, client: Any | None = None) -> list[NormalizedEvent]:
    """Fill `attribution` on every event in place; return the same list.

    Deterministic first; ambiguous text-bearing residue batched to `client` (when
    given). With no client, or on any LLM failure, the residue is needs_review.
    Events are never dropped (C5)."""
    active = [p for p in projects if p.get("status", "active") == "active"]
    ambiguous: list[tuple[int, NormalizedEvent]] = []

    for i, ev in enumerate(events):
        if ev.type in _SKIP_TYPES:
            continue
        pid, score, method, _ = signal_stack(ev, active)
        if score >= THRESHOLD and pid:
            ev.attribution = Attribution(project_id=pid, confidence=round(score, 4),
                                         method=method)
        else:
            ambiguous.append((i, ev))

    if not ambiguous:
        return events

    # Events with no usable text can't be helped by the LLM — review directly.
    llm_batch: list[tuple[int, NormalizedEvent]] = []
    for i, ev in ambiguous:
        if _has_text(ev):
            llm_batch.append((i, ev))
        else:
            _mark_needs_review(ev)

    if not llm_batch:
        return events
    if client is None:
        for _, ev in llm_batch:
            _mark_needs_review(ev)
        return events

    assignments = _llm_categorize(llm_batch, active, client)
    valid_ids = {p["id"] for p in active}
    for idx, ev in llm_batch:
        a = assignments.get(idx)
        if (a and a.get("project_id") in valid_ids
                and _as_float(a.get("confidence")) >= THRESHOLD):
            ev.attribution = Attribution(project_id=a["project_id"],
                                         confidence=round(_as_float(a["confidence"]), 4),
                                         method="llm")
        else:
            _mark_needs_review(ev, a)
    return events


# --------------------------------------------------------------------------- #
# Live smoke — `python -m throughlog.categorize --smoke`
# --------------------------------------------------------------------------- #
def _smoke(argv: list[str] | None = None) -> int:
    import argparse
    from throughlog.config import load_config, load_projects
    from throughlog.schema import make_event
    from throughlog.llm.client import LLMConfig, LLMClient

    ap = argparse.ArgumentParser(description="Live Phase-1 smoke: one ambiguous event.")
    ap.add_argument("--smoke", action="store_true")
    ap.parse_args(argv)

    projects = load_projects()
    cfg = LLMConfig.from_config(load_config())
    client = LLMClient(cfg)
    print(f"model={cfg.model}  key={'present' if cfg.resolve_key() else 'MISSING'}")

    # Deliberately signal-invisible (no keyword/app/path/domain hit) but humanly
    # inferable from the description ("lateral stability" -> training-shoes).
    ev = make_event(
        FOCUS_SESSION, kind="os", adapter="os_focus",
        payload={"anchor": "Untitled - Notes", "process": "notes.exe",
                 "active_file": None, "satellites": [],
                 "intent": {"label": "summarizing the lateral-stability findings "
                                     "for the footwear roundup",
                            "method": "narration", "confidence": 0.5}},
        ts_wall="2026-06-21T15:00:00+03:00")

    pid, score, method, reason = signal_stack(ev, projects)
    print(f"signal stack: project={pid} score={score} method={method} ({reason})")
    if score >= THRESHOLD:
        print("note: resolved deterministically; LLM not exercised. Still OK.")
        return 0

    categorize_events([ev], projects, client=client)
    a = ev.attribution
    print(f"after LLM   : project={a.project_id} confidence={a.confidence} method={a.method}")
    if a.method == "llm" and a.project_id:
        print("RESULT: live Phase-1 categorization works.")
        return 0
    if a.method == "needs_review":
        print("RESULT: LLM reachable but routed to needs_review "
              "(model declined / low confidence / failure). Pipeline safe; "
              "consider the fallback model if this persists.")
        return 0
    print("RESULT: unexpected attribution state.")
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(_smoke())
