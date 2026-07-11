"""Offline LLM-flow stress harness — measure the call/token cost of a synthesis run
across configurations, with no network and no spend.

The dominant question the metering answers: *how does the LLM call count and token
volume scale* with days, projects, and the Phase-2 knobs (entries on/off, entry period,
summary cadence, skip-unchanged)? Real free-tier runs are rate-limited and slow, so we
replay a deterministic synthetic corpus through the REAL pipeline (`categorize_events`
+ `synthesize.run`) with a `FakeMeteredClient` standing in for the network. The fake
records the same `CallRecord` rows the real client does (labelled per call site, token
counts estimated from prompt/response length), so the numbers mirror the real topology
without any provider.

    python -m sim.llm_bench --matrix                     # the standard comparison table
    python -m sim.llm_bench --days 30 --projects 3       # bigger corpus, one default run
    python -m sim.llm_bench --matrix --json              # machine-readable

This is a diagnostic tool, not part of the pipeline: it never touches the bus, the gate,
or any real key. It exists so the flow can be optimized against numbers instead of guesses.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from throughlog.schema import (
    make_event, NormalizedEvent,
    FOCUS_SESSION, FILE_CHANGE, GIT_COMMIT, NARRATION,
)
from throughlog.llm.client import CallRecord
from throughlog.llm.prompts import DAILY_SEP
from throughlog.categorize import categorize_events
from throughlog import synthesize
from throughlog.synthesize import SynthesisOptions


# --------------------------------------------------------------------------- #
# Fake metered client — mirrors LLMClient's metering without any network
# --------------------------------------------------------------------------- #
class FakeMeteredClient:
    """Stand-in for ``LLMClient`` that returns pipeline-valid replies and records one
    ``CallRecord`` per ``chat()`` (tokens estimated at ~4 chars/token). Exposes ``.calls``
    and ``.metrics_summary()`` so the harness reads it exactly like the real client."""

    def __init__(self, *, latency_sec: float = 0.0) -> None:
        self.calls: list[CallRecord] = []
        self._latency = latency_sec

    @staticmethod
    def _toklen(s: str) -> int:
        return max(1, len(s) // 4)

    def _reply_for(self, label: str, user: str = "") -> str:
        if label == "categorize":
            # Decline everything (null) — the harness measures the CALL, not accuracy.
            return '{"assignments": []}'
        if label == "overview":
            # Must carry the daily separator so the pipeline extracts a daily paragraph
            # (the real fragility this models: no separator => lost daily line).
            return ("# Project\n**Status:** active\n\n## Current State\nProgress continued.\n\n"
                    "## Ongoing Threads\n- work — in progress\n\n## Chronological Narrative\n"
                    "Work advanced across the day.\n\n## Key Artifacts\n- files\n"
                    f"{DAILY_SEP}\nMade steady progress and committed fixes.")
        if label == "entry":
            # A batched (multi-day) entry prompt lists its dates; echo one '## YYYY-MM-DD'
            # section per date so the pipeline's per-day split succeeds (models reality).
            if "DAYS IN THIS BATCH" in user:
                import re
                m = re.search(r"DAYS IN THIS BATCH \(\d+\): (.+)", user)
                labels = ([d.strip() for d in m.group(1).split(",")] if m
                          else re.findall(r"\d{4}-\d{2}-\d{2}", user))
                return "\n\n".join(
                    f"## {d}\nWorked on {d}; value tried 0.5; result passing." for d in labels)
            return ("Worked through the day's changes; adjusted parameters and committed. "
                    "- value tried: 0.5\n- result: passing")
        # exec / period / chunk / init_enrich / ask
        return "Overall a productive span; steady progress across projects, nothing blocked."

    def chat(self, system: str, user: str, *, temperature: float = 0.0,
             max_tokens: int = 1500, label: str = "") -> str:
        reply = self._reply_for(label, user)
        pt = self._toklen(system) + self._toklen(user)
        ct = self._toklen(reply)
        self.calls.append(CallRecord(
            label=label, model="fake", prompt_tokens=pt, completion_tokens=ct,
            total_tokens=pt + ct, latency_sec=self._latency, attempts=1,
            fallback_used=False, ok=True))
        return reply

    def metrics_summary(self) -> dict[str, Any]:
        c = self.calls
        return {
            "calls": len(c),
            "prompt_tokens": sum(r.prompt_tokens for r in c),
            "completion_tokens": sum(r.completion_tokens for r in c),
            "total_tokens": sum(r.total_tokens for r in c),
        }

    def by_label(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.calls:
            out[r.label] = out.get(r.label, 0) + 1
        return out


# --------------------------------------------------------------------------- #
# Synthetic corpus — deterministic, path-attributed so Phase 1 resolves without
# the LLM (except one intentionally-ambiguous narration/day => exactly 1 batched
# categorize call, mirroring reality).
# --------------------------------------------------------------------------- #
def build_projects(n: int) -> list[dict[str, Any]]:
    return [{
        "id": f"proj{i}", "name": f"Project {i}", "status": "active",
        "description": f"Synthetic project {i} for the bench.",
        "signals": {"paths": [f"~/projects/proj{i}"], "keywords": [f"proj{i}kw"]},
    } for i in range(1, n + 1)]


def _ts(day_index: int, hour: int, minute: int = 0) -> str:
    # 2026-06-01 + day_index, so multi-day corpora span real calendar dates.
    d = 1 + day_index
    mm, dd = (6, d) if d <= 30 else (7, d - 30)
    return f"2026-{mm:02d}-{dd:02d}T{hour:02d}:{minute:02d}:00+03:00"


def build_corpus(days: int, projects: list[dict[str, Any]],
                 events_per_day: int) -> list[NormalizedEvent]:
    """One path-attributed working day per project per day, plus one ambiguous
    narration per day (drives a single batched categorize call for the whole run)."""
    evs: list[NormalizedEvent] = []
    for di in range(days):
        for pi, proj in enumerate(projects):
            pid = proj["id"]
            root = f"~/projects/{pid}"
            hour = 9 + (pi % 8)
            for k in range(max(1, events_per_day // len(projects))):
                evs.append(make_event(
                    FOCUS_SESSION, kind="os", adapter="os_focus", ts_wall=_ts(di, hour, k),
                    payload={"anchor": f"file{k}.py — {proj['name']}", "process": "Code.exe",
                             "duration_sec": 1200, "mode": "focused",
                             "active_file": f"{root}/src/file{k}.py",
                             "intent": {"label": f"work item {k}", "rung": "active_file"}}))
                evs.append(make_event(
                    FILE_CHANGE, kind="fs", adapter="fs_git", ts_wall=_ts(di, hour, k + 5),
                    payload={"path": f"{root}/src/file{k}.py", "action": "modified"}))
            evs.append(make_event(
                GIT_COMMIT, kind="git", adapter="fs_git", ts_wall=_ts(di, hour, 55),
                payload={"repo": f"github.com/acme/{pid}", "actor": "dev",
                         "message": f"progress on {pid} day {di}",
                         "files": [f"{root}/src/file0.py"]}))
        # one ambiguous, text-bearing event for the whole day (no keyword/path hit)
        evs.append(make_event(
            NARRATION, kind="intent", adapter="narration", ts_wall=_ts(di, 17, 30),
            payload={"note": "misc follow-ups and loose ends to revisit later"}))
    return evs


# --------------------------------------------------------------------------- #
# One measured run
# --------------------------------------------------------------------------- #
def run_once(events: list[NormalizedEvent], projects: list[dict[str, Any]],
             options: SynthesisOptions, *, journal_dir: Path,
             today: str) -> FakeMeteredClient:
    client = FakeMeteredClient()
    # Fresh attribution each run (categorize mutates in place).
    categorize_events(events, projects, client=client)
    synthesize.run(events, projects, journal_dir=journal_dir, client=client,
                   today=today, options=options)
    return client


# --------------------------------------------------------------------------- #
# The standard comparison matrix
# --------------------------------------------------------------------------- #
_MATRIX = [
    ("entries off",                    dict(write_entries=False, summary_cadence="off")),
    ("entries daily (per-day calls)",  dict(write_entries=True, entry_batch="day", summary_cadence="off")),
    ("entries weekly batch",           dict(write_entries=True, entry_batch="week",
                                            max_input_tokens=6000, summary_cadence="off")),
    ("entries adaptive batch",         dict(write_entries=True, entry_batch="adaptive",
                                            max_input_tokens=6000, summary_cadence="off")),
    ("adaptive + summary weekly",      dict(write_entries=True, entry_batch="adaptive",
                                            max_input_tokens=6000, summary_cadence="weekly")),
]

_LABELS = ("categorize", "entry", "overview", "chunk", "exec", "period")


def run_matrix(days: int, n_projects: int, events_per_day: int) -> list[dict[str, Any]]:
    projects = build_projects(n_projects)
    today = _ts(days - 1, 23)[:10]
    rows: list[dict[str, Any]] = []

    for name, kw in _MATRIX:
        events = build_corpus(days, projects, events_per_day)
        with tempfile.TemporaryDirectory(prefix="tl_bench_") as d:
            client = run_once(events, projects, SynthesisOptions(**kw),
                              journal_dir=Path(d), today=today)
        rows.append(_row(name, client))

    # skip_unchanged: re-run the product-default config over the SAME journal dir with
    # the SAME event objects — the fingerprint matches, so Phase 2 reuses its output and
    # only Phase 1 (the 1 batched categorize) is re-billed. (Rebuilding the corpus would
    # mint new event_ids and defeat the guard — the events must be byte-identical.)
    events = build_corpus(days, projects, events_per_day)
    default = SynthesisOptions(write_entries=True, entry_period="month",
                               summary_cadence="weekly", skip_unchanged=True)
    with tempfile.TemporaryDirectory(prefix="tl_bench_skip_") as d:
        dd = Path(d)
        run_once(events, projects, default, journal_dir=dd, today=today)      # 1st: full
        client2 = run_once(events, projects, default, journal_dir=dd, today=today)  # 2nd: unchanged
    rows.append(_row("↑ re-run, skip_unchanged", client2))

    # F3 staggered schedule: give each project a distinct weekday; bootstrap once, then a
    # second run on ONE weekday bills entries only for that day's project(s) — the same
    # total weekly work, spread out so peak calls/night drops ~N-fold.
    from datetime import date as _date, timedelta
    dows = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    sprojects = build_projects(n_projects)
    for i, p in enumerate(sprojects):
        p["synthesis"] = {"day": dows[i % 7]}
    stag_opts = SynthesisOptions(write_entries=True, entry_batch="adaptive",
                                 max_input_tokens=6000, summary_cadence="off")
    t0 = _date.fromisoformat(today)
    monday = t0 + timedelta(days=((7 - t0.weekday()) % 7) or 7)   # the next Monday
    with tempfile.TemporaryDirectory(prefix="tl_bench_stag_") as d:
        dd = Path(d)
        run_once(build_corpus(days, sprojects, events_per_day), sprojects,
                 stag_opts, journal_dir=dd, today=today)             # bootstrap all
        client3 = run_once(build_corpus(days + 1, sprojects, events_per_day), sprojects,
                           stag_opts, journal_dir=dd, today=monday.isoformat())
    rows.append(_row("↑ staggered, one weekday (Mon)", client3))
    return rows


def _row(name: str, client: FakeMeteredClient) -> dict[str, Any]:
    m = client.metrics_summary()
    bl = client.by_label()
    return {"config": name, "calls": m["calls"],
            **{lbl: bl.get(lbl, 0) for lbl in _LABELS},
            "in_tok": m["prompt_tokens"], "out_tok": m["completion_tokens"],
            "total_tok": m["total_tokens"]}


def print_table(rows: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    print(f"\nLLM call cost — {meta['days']}d × {meta['projects']} projects "
          f"× ~{meta['events_per_day']} events/day\n")
    cols = ["config", "calls", *(_LABELS), "in_tok", "out_tok", "total_tok"]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
    print("\nlegend: per-label columns are call counts. 'entries daily' bills one entry "
          "call per day×project; 'weekly'/'adaptive' batch WHOLE days into one call under "
          "the ~token budget (no condensing), cutting entry calls ~span-fold; skip_unchanged "
          "reuses unchanged projects; a staggered schedule spreads the same weekly work "
          "across weekdays so peak calls/night drops. overview is 1/project; categorize is "
          "1 batched call. entry_period (month|week) does not change call count.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline LLM-flow stress harness (no network).")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--projects", type=int, default=3)
    ap.add_argument("--events-per-day", type=int, default=12)
    ap.add_argument("--matrix", action="store_true",
                    help="run the standard configuration comparison table")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    meta = {"days": args.days, "projects": args.projects,
            "events_per_day": args.events_per_day}

    if args.matrix:
        rows = run_matrix(args.days, args.projects, args.events_per_day)
    else:
        projects = build_projects(args.projects)
        events = build_corpus(args.days, projects, args.events_per_day)
        today = _ts(args.days - 1, 23)[:10]
        opts = SynthesisOptions(write_entries=True, entry_period="month",
                                summary_cadence="weekly")
        with tempfile.TemporaryDirectory(prefix="tl_bench_") as d:
            client = run_once(events, projects, opts, journal_dir=Path(d), today=today)
        rows = [_row("product default (entries month, summary weekly)", client)]

    if args.json:
        print(json.dumps({"meta": meta, "rows": rows}, indent=2))
    else:
        print_table(rows, meta)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
