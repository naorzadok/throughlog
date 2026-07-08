"""Process monitor — deterministic detection of LONG_RUN (AFK compute).

Some work happens while the OS focus layer is blind: a 6-hour solver, an
overnight render, a long compile — alive and burning CPU with no human present.
The focus adapter would see only idle. This source watches *processes* instead
of windows and emits a ``LONG_RUN`` event for any process that stays busy for a
sustained period while the human is away.

``LongRunTracker`` is the pure, clock-injected core: feed it process samples in
time order and it returns the LONG_RUN events as runs complete. No psutil, no
clock — fully deterministic and unit-testable. The live driver (``monitor_live``)
lazily imports psutil so the core and the test path stay stdlib-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from throughlog.schema import NormalizedEvent, make_event, LONG_RUN

DEFAULT_CPU_THRESHOLD = 50.0       # percent; at/above counts as "busy"
DEFAULT_LONG_RUN_MIN_SEC = 1800.0  # a busy+unattended run must last this long to matter


@dataclass(frozen=True)
class Proc:
    pid: int
    name: str
    cmdline: str = ""
    cpu_percent: float = 0.0


@dataclass
class ProcSample:
    """One scan tick: every process seen, plus whether a human is present."""
    ts: str
    procs: list[Proc]
    human_present: bool = False


def _elapsed(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


class LongRunTracker:
    def __init__(self, *,
                 cpu_threshold: float = DEFAULT_CPU_THRESHOLD,
                 long_run_min_sec: float = DEFAULT_LONG_RUN_MIN_SEC) -> None:
        self.cpu_threshold = float(cpu_threshold)
        self.long_run_min = float(long_run_min_sec)
        # pid -> running record while busy & unattended
        self._open: dict[int, dict] = {}

    def feed(self, sample: ProcSample) -> list[NormalizedEvent]:
        out: list[NormalizedEvent] = []
        busy_now: dict[int, Proc] = {}
        if not sample.human_present:
            busy_now = {p.pid: p for p in sample.procs
                        if p.cpu_percent >= self.cpu_threshold}

        # Extend or open runs for currently busy+unattended processes.
        for pid, proc in busy_now.items():
            rec = self._open.get(pid)
            if rec is None:
                self._open[pid] = {"proc": proc, "start": sample.ts,
                                   "last": sample.ts, "cpu_peak": proc.cpu_percent}
            else:
                rec["last"] = sample.ts
                rec["proc"] = proc
                rec["cpu_peak"] = max(rec["cpu_peak"], proc.cpu_percent)

        # Any open run no longer busy/unattended (CPU dropped, process gone, or
        # the human came back) is finalized now.
        for pid in [p for p in self._open if p not in busy_now]:
            ev = self._finalize(self._open.pop(pid))
            if ev is not None:
                out.append(ev)
        return out

    def close(self) -> list[NormalizedEvent]:
        out = [self._finalize(rec) for rec in self._open.values()]
        self._open.clear()
        return [e for e in out if e is not None]

    def _finalize(self, rec: dict) -> NormalizedEvent | None:
        duration = _elapsed(rec["start"], rec["last"])
        if duration < self.long_run_min:
            return None
        proc: Proc = rec["proc"]
        payload = {
            "pid": proc.pid,
            "process": proc.name,
            "cmdline": proc.cmdline,        # gate normalizes home paths / scrubs secrets
            "cpu_peak": round(rec["cpu_peak"], 1),
            "duration_sec": round(duration, 1),
            "started": rec["start"],
            "ended": rec["last"],
            "unattended": True,
        }
        return make_event(LONG_RUN, kind="os", adapter="proc_monitor",
                          payload=payload, ts_wall=rec["start"])


# --------------------------------------------------------------------------- #
# Live driver — lazy psutil; pairs with the focus adapter's idle signal.
# --------------------------------------------------------------------------- #
def monitor_live(emitter, idle_seconds_fn, *, stop=None, sample_interval_sec: float = 30.0,
                 idle_threshold_sec: float = 600.0, **cfg) -> None:
    """Scan processes on an interval, mark the human present/absent from the
    idle signal, drive the tracker, push LONG_RUN events through ``emitter``.
    Runs until ``stop`` (a threading.Event) is set or KeyboardInterrupt, then
    finalizes any in-flight run."""
    import threading
    import psutil
    from throughlog.schema import now_iso

    stop = stop or threading.Event()
    tracker = LongRunTracker(**cfg)
    psutil.cpu_percent(interval=None)  # prime per-process cpu_percent baselines
    try:
        while not stop.is_set():
            procs: list[Proc] = []
            for p in psutil.process_iter(["pid", "name", "cmdline", "cpu_percent"]):
                try:
                    info = p.info
                    procs.append(Proc(
                        pid=info["pid"],
                        name=info.get("name") or "unknown",
                        cmdline=" ".join(info.get("cmdline") or []),
                        cpu_percent=info.get("cpu_percent") or 0.0,
                    ))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            human = idle_seconds_fn() < idle_threshold_sec
            sample = ProcSample(ts=now_iso(), procs=procs, human_present=human)
            for ev in tracker.feed(sample):
                emitter.emit(ev)
            stop.wait(sample_interval_sec)
    except KeyboardInterrupt:
        pass
    finally:
        for ev in tracker.close():
            emitter.emit(ev)
