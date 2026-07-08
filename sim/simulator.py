"""Scenario simulator — replays declarative case-matrix scenarios through the
REAL bus (gate + persistence) and asserts the expected outcome.

Each scenario file in sim/scenarios/*.json is one case-matrix row. Run:

    python -m sim.simulator --all
    python -m sim.simulator --scenario sim/scenarios/m1_allowlist_drop.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.schema import NormalizedEvent          # noqa: E402
from throughlog.privacy.allowlist import Allowlist      # noqa: E402
from throughlog.bus import EventBus                      # noqa: E402
from throughlog.sources.os_focus import (                # noqa: E402
    FocusSessionizer, FocusSample, Window,
)
from throughlog.sources.proc_monitor import (             # noqa: E402
    LongRunTracker, ProcSample, Proc,
)
from throughlog.sources.fs_git import (                    # noqa: E402
    FileChurnFilter, RawFsEvent, ActorConfig, make_git_commit,
)
from throughlog.sources.agent_ingest import (              # noqa: E402
    ingest_report, AgentIngestConfig,
)
from throughlog.sources.intent_bridge import (             # noqa: E402
    make_narration, ClipboardCapture,
)
from throughlog.timeline import reconcile                  # noqa: E402

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"

# Keys under scenario["focus"] that configure the sessionizer (the rest is "ticks").
_FOCUS_CFG_KEYS = ("anchor_timeout_sec", "idle_threshold_sec", "kps_threshold",
                   "periodic_flush_sec", "deep_work_min_sec", "mouse_active_min")
# Keys under scenario["processes"] that configure the LONG_RUN tracker.
_PROC_CFG_KEYS = ("cpu_threshold", "long_run_min_sec")
# Keys under scenario["filesystem"] that configure the churn filter.
_FS_CFG_KEYS = ("coalesce_sec", "burst_window_sec", "burst_threshold")


def _run_focus(focus: dict, bus: EventBus) -> None:
    """Drive declarative focus ticks through the real sessionizer into the bus,
    exactly as the live capture loop would."""
    cfg = {k: focus[k] for k in _FOCUS_CFG_KEYS if k in focus}
    sessionizer = FocusSessionizer(**cfg)
    for tick in focus.get("ticks", []):
        win = tick.get("window")
        sample = FocusSample(
            ts=tick["ts"],
            window=Window(**win) if win else None,
            idle_seconds=tick.get("idle_seconds", 0.0),
            keys=tick.get("keys", 0),
            mouse=tick.get("mouse", 0),
            saves=tick.get("saves", 0),
            uia_value=tick.get("uia_value", ""),
            cmdline=tick.get("cmdline", ""),
            cwd=tick.get("cwd", ""),
            saved_artifact=tick.get("saved_artifact", ""),
            narration=tick.get("narration", ""),
        )
        for ev in sessionizer.feed(sample):
            bus.emit(ev)
    for ev in sessionizer.close():
        bus.emit(ev)


def _run_processes(spec: dict, bus: EventBus) -> None:
    """Drive declarative process scans through the real LongRunTracker into the bus."""
    cfg = {k: spec[k] for k in _PROC_CFG_KEYS if k in spec}
    tracker = LongRunTracker(**cfg)
    for tick in spec.get("ticks", []):
        sample = ProcSample(
            ts=tick["ts"],
            procs=[Proc(**p) for p in tick.get("procs", [])],
            human_present=tick.get("human_present", False),
        )
        for ev in tracker.feed(sample):
            bus.emit(ev)
    for ev in tracker.close():
        bus.emit(ev)


def _run_filesystem(spec: dict, bus: EventBus) -> None:
    """Drive raw fs events through the churn filter and git commits through the
    actor classifier into the bus. A scenario may attach an inline ``diff`` (and a
    commit ``body``/``diffstat``) so the real gate exercises diff capture end-to-end
    — no git binary needed."""
    cfg = {k: spec[k] for k in _FS_CFG_KEYS if k in spec}
    actor_cfg = ActorConfig(**{k: tuple(v) for k, v in spec.get("actor", {}).items()})
    churn = FileChurnFilter(actor_config=actor_cfg, **cfg)
    for ev in spec.get("events", []):
        if ev.get("kind") == "git_commit":
            commit = make_git_commit(
                repo=ev["repo"], author=ev.get("author", ""),
                message=ev.get("message", ""), ts=ev["ts"],
                files=ev.get("files"), actor_config=actor_cfg)
            for k in ("diff", "body", "diffstat"):
                if k in ev:
                    commit.payload[k] = ev[k]
            bus.emit(commit)
        else:
            raw = RawFsEvent(ts=ev["ts"], path=ev["path"],
                             action=ev.get("action", "modified"),
                             author=ev.get("author", ""),
                             human_active=ev.get("human_active", False))
            for out in churn.feed(raw):
                if "diff" in ev:
                    out.payload["diff"] = ev["diff"]
                bus.emit(out)


def _run_agents(spec: dict, bus: EventBus) -> None:
    """Ingest declarative agent reports (validate -> trust) into the bus."""
    cfg = AgentIngestConfig(
        trusted_identities=tuple(spec.get("trusted_identities", [])),
        future_tolerance_sec=spec.get("future_tolerance_sec", 120.0),
    )
    now = spec.get("now")
    for raw in spec.get("reports", []):
        bus.emit(ingest_report(raw, now=now, cfg=cfg))


def _run_github(spec: dict, bus: EventBus) -> None:
    """Replay captured GitHub API payloads through the real transformers + bus."""
    from throughlog.sources.github_pull import (
        commit_to_event, pull_request_to_event, workflow_run_to_event,
    )
    remote = spec.get("repo_remote", "")
    for c in spec.get("commits", []):
        bus.emit(commit_to_event(c, remote))
    for pr in spec.get("pulls", []):
        bus.emit(pull_request_to_event(pr, remote))
    for wf in spec.get("workflow_runs", []):
        bus.emit(workflow_run_to_event(wf, remote))


def _run_narration(spec: dict, bus: EventBus) -> None:
    """Emit human narration notes (whisper floor) through the gate into the bus."""
    for note in spec.get("notes", []):
        bus.emit(make_narration(note["text"], note["ts"]))


def _run_clipboard(spec: dict, bus: EventBus) -> None:
    """De-dup clipboard captures and let the gate type/redact them."""
    cap = ClipboardCapture()
    for clip in spec.get("captures", []):
        ev = cap.observe(clip["text"], clip["ts"])
        if ev is not None:
            bus.emit(ev)


def run_scenario(scn: dict) -> tuple[bool, list[str]]:
    """Run one scenario dict. Returns (passed, failure_messages)."""
    from throughlog.privacy.diff_policy import DiffPolicy

    msgs: list[str] = []
    allow = Allowlist(scn.get("allowlist", []))
    out = Path(tempfile.mkdtemp(prefix="sal_sim_"))
    try:
        # Sandbox the diff sidecar INSIDE the scenario temp dir (never the shared
        # system-temp root) so sidecars are isolated, cleaned, and visible to the
        # sidecar leak-scan below.
        diff_policy = DiffPolicy(**scn.get("diff_policy", {}))
        diffs_dir = out / "diffs"
        bus = EventBus(out, allow, diff_policy=diff_policy, diffs_dir=diffs_dir)
        for raw in scn.get("events", []):
            rec = dict(raw)
            rec.setdefault("schema_version", 2)
            bus.emit(NormalizedEvent.from_dict(rec))
        if "focus" in scn:
            _run_focus(scn["focus"], bus)
        if "processes" in scn:
            _run_processes(scn["processes"], bus)
        if "filesystem" in scn:
            _run_filesystem(scn["filesystem"], bus)
        if "agents" in scn:
            _run_agents(scn["agents"], bus)
        if "github" in scn:
            _run_github(scn["github"], bus)
        if "narration" in scn:
            _run_narration(scn["narration"], bus)
        if "clipboard" in scn:
            _run_clipboard(scn["clipboard"], bus)
        bus.close()

        persisted: list[dict] = []
        text = ""
        for f in sorted(out.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                text += line + "\n"
                persisted.append(json.loads(line))

        sidecar_text = ""
        if diffs_dir.is_dir():
            for f in sorted(diffs_dir.glob("*.patch")):
                sidecar_text += f.read_text(encoding="utf-8") + "\n"

        return _check(scn.get("expect", {}), bus, persisted, text, sidecar_text, msgs), msgs
    finally:
        shutil.rmtree(out, ignore_errors=True)


def _check(expect: dict, bus: EventBus, persisted: list[dict], text: str,
           sidecar_text: str, msgs: list[str]) -> bool:
    ok = True

    if "written" in expect and bus.written != expect["written"]:
        ok = False
        msgs.append(f"written={bus.written}, expected {expect['written']}")

    for reason, count in expect.get("dropped", {}).items():
        if bus.dropped.get(reason, 0) != count:
            ok = False
            msgs.append(f"dropped[{reason}]={bus.dropped.get(reason, 0)}, expected {count}")

    for sub in expect.get("persisted_present", []):
        if sub not in text:
            ok = False
            msgs.append(f"expected substring missing from output: {sub!r}")

    for sub in expect.get("persisted_absent", []):
        if sub in text:
            ok = False
            msgs.append(f"LEAK — substring present in output: {sub!r}")

    # Diff sidecars (data/diffs/*.patch) — the leak-scan MUST cover these, or a diff
    # secret would be invisible to persisted_absent (it lives in the sidecar, not the
    # thin-log).
    for sub in expect.get("sidecar_present", []):
        if sub not in sidecar_text:
            ok = False
            msgs.append(f"expected substring missing from diff sidecar: {sub!r}")

    for sub in expect.get("sidecar_absent", []):
        if sub in sidecar_text:
            ok = False
            msgs.append(f"LEAK — substring present in diff sidecar: {sub!r}")

    if "redactions_any" in expect:
        seen: set[str] = set()
        for ev in persisted:
            seen.update((ev.get("privacy") or {}).get("redactions", []))
        for r in expect["redactions_any"]:
            if r not in seen:
                ok = False
                msgs.append(f"redaction {r!r} not recorded (saw {sorted(seen)})")

    if "event_types" in expect:
        counts: dict[str, int] = {}
        for ev in persisted:
            t = ev.get("type", "?")
            counts[t] = counts.get(t, 0) + 1
        for t, n in expect["event_types"].items():
            if counts.get(t, 0) != n:
                ok = False
                msgs.append(f"event_types[{t}]={counts.get(t, 0)}, expected {n} (saw {counts})")

    if "actor_counts" in expect:
        actors: dict[str, int] = {}
        for ev in persisted:
            a = (ev.get("payload") or {}).get("actor")
            if a is not None:
                actors[a] = actors.get(a, 0) + 1
        for a, n in expect["actor_counts"].items():
            if actors.get(a, 0) != n:
                ok = False
                msgs.append(f"actor_counts[{a}]={actors.get(a, 0)}, expected {n} (saw {actors})")

    if "trust_counts" in expect:
        trust: dict[str, int] = {}
        for ev in persisted:
            t = ev.get("trust", "validated")
            trust[t] = trust.get(t, 0) + 1
        for t, n in expect["trust_counts"].items():
            if trust.get(t, 0) != n:
                ok = False
                msgs.append(f"trust_counts[{t}]={trust.get(t, 0)}, expected {n} (saw {trust})")

    if "timeline_order" in expect:
        # Reconcile the persisted log and check the real order by a payload tag.
        spec = expect["timeline_order"]
        key, want = spec["key"], spec["values"]
        got = [(ev.get("payload") or {}).get(key) for ev in reconcile(persisted)]
        got = [g for g in got if g is not None]
        if got != want:
            ok = False
            msgs.append(f"timeline_order by {key!r}: got {got}, expected {want}")

    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="run every scenario")
    ap.add_argument("--scenario", help="run a single scenario file")
    args = ap.parse_args(argv)

    if args.scenario:
        files = [Path(args.scenario)]
    else:
        files = sorted(SCENARIO_DIR.glob("*.json"))

    passed = 0
    for f in files:
        scn = json.loads(Path(f).read_text(encoding="utf-8"))
        ok, msgs = run_scenario(scn)
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {scn.get('name', f.stem)}")
        for m in msgs:
            print(f"         - {m}")

    print(f"\n{passed}/{len(files)} scenarios passed")
    return 0 if passed == len(files) else 1


if __name__ == "__main__":
    sys.exit(main())
