"""Live capture supervisor — runs every source adapter at once, into one bus.

    python -m throughlog.cli capture                      # start live capture (Ctrl+C to stop)
    python -m throughlog.cli capture --no-clipboard       # skip the clipboard source
    python -m throughlog.cli capture --no-agents          # skip the agent drop-folder

Each source adapter already owns a blocking live driver (``os_focus.capture_live``,
``proc_monitor.monitor_live``, ``fs_git.watch_live``,
``intent_bridge.watch_clipboard_live``, ``agent_ingest.watch_drop_folder_live``).
This module runs them concurrently — each in its own thread — all feeding a single
**thread-safe emitter** wrapped around the bus, so the privacy gate still runs on
every event before anything is persisted. Capture stays in the deterministic layer:
no LLM is ever touched here.

Lifecycle: ``start`` spawns one daemon thread per source → the main thread runs a
heartbeat loop writing ``data/daemon_status.json`` → on Ctrl+C / SIGTERM a shared
``stop`` Event is set, the workers are joined (each flushes its in-flight session),
a final status is written and the bus is closed. One source raising never takes the
supervisor down — the error is recorded and the other sources keep running.

Two optional hotkeys (best-effort, via the ``keyboard`` lib if present):
``ctrl+shift+m`` pops a whisper note, ``ctrl+shift+p`` toggles a privacy pause that
drops events at the emitter until resumed.
"""

from __future__ import annotations

import os
import signal
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from throughlog.schema import NormalizedEvent, now_iso
from throughlog.bus import EventBus
from throughlog.privacy.allowlist import Allowlist
from throughlog import config as cfgmod
from throughlog.sources import os_focus, proc_monitor, fs_git, intent_bridge, agent_ingest


# --------------------------------------------------------------------------- #
# Thread-safe emitter
# --------------------------------------------------------------------------- #
class ThreadSafeEmitter:
    """Serializes bus access across source threads and carries a pause switch.

    The bus owns file handles and counters that are not safe under concurrent
    writers, so every ``emit`` is taken under a lock. While ``paused`` is set,
    events are *dropped* (a privacy pause), never buffered — nothing observed
    during a pause is persisted."""

    def __init__(self, bus: Any, *, paused: threading.Event | None = None) -> None:
        self._bus = bus
        self._lock = threading.Lock()
        self.paused = paused or threading.Event()
        self.suppressed = 0

    def emit(self, event: NormalizedEvent) -> bool:
        # The lock guards BOTH the bus write and the suppressed counter: `+= 1` is
        # not atomic, so concurrent privacy-pause drops would otherwise lose-update
        # the count under real multi-source load (risk register #7).
        with self._lock:
            if self.paused.is_set():
                self.suppressed += 1
                return False
            return self._bus.emit(event)


# --------------------------------------------------------------------------- #
# Source registry
# --------------------------------------------------------------------------- #
@dataclass
class SourceSpec:
    """A named source runner. ``run(emitter, stop)`` blocks until ``stop`` is set."""
    name: str
    run: Callable[[Any, threading.Event], None]


def build_sources(cfg: dict[str, Any], roots: list[str], *,
                  enable_clipboard: bool = True, enable_agents: bool = True,
                  agent_drop_dir: str | Path | None = None,
                  agent_archive_dir: str | Path | None = None,
                  exclude_dirs: list[str | Path] | None = None,
                  idle_fn: Callable[[], float] | None = None,
                  diff_policy: Any = None) -> list[SourceSpec]:
    """Assemble the live source set from config flags. Pure wiring — returns
    specs whose ``run`` closes over the per-source args but receives the shared
    emitter + stop event at spawn time, so this is testable without any OS deps."""
    cap = (cfg or {}).get("capture", {})
    idle_fn = idle_fn or os_focus.idle_seconds
    idle_threshold = float(cap.get("idle_threshold_sec", 600.0))
    heartbeat = float(cap.get("heartbeat_sec", 2.0))
    clip_interval = float(cap.get("clipboard_check_interval_sec", 3.0))
    exclude = list(exclude_dirs or [])

    specs: list[SourceSpec] = [
        SourceSpec("os_focus", lambda e, stop: os_focus.capture_live(
            e, stop=stop, heartbeat_sec=heartbeat, idle_threshold_sec=idle_threshold)),
        SourceSpec("proc_monitor", lambda e, stop: proc_monitor.monitor_live(
            e, idle_fn, stop=stop, idle_threshold_sec=idle_threshold)),
    ]

    if roots:
        def _human_active() -> bool:
            return idle_fn() < 5.0          # input within the last 5s -> human at keyboard
        specs.append(SourceSpec("fs_git", lambda e, stop: fs_git.watch_live(
            e, roots, stop=stop, human_active_fn=_human_active, exclude=exclude,
            policy=diff_policy)))

    if enable_clipboard and cap.get("enable_clipboard", True):
        specs.append(SourceSpec("clipboard", lambda e, stop: intent_bridge.watch_clipboard_live(
            e, stop=stop, interval_sec=clip_interval)))

    if enable_agents and agent_drop_dir is not None:
        specs.append(SourceSpec("agent_ingest", lambda e, stop: agent_ingest.watch_drop_folder_live(
            e, agent_drop_dir, stop=stop, archive=agent_archive_dir)))

    return specs


# --------------------------------------------------------------------------- #
# Supervisor
# --------------------------------------------------------------------------- #
class Supervisor:
    def __init__(self, bus: Any, sources: list[SourceSpec], *,
                 status_path: str | Path | None = None, heartbeat_sec: float = 30.0,
                 paused: threading.Event | None = None) -> None:
        self.bus = bus
        self.paused = paused or threading.Event()
        self.emitter = ThreadSafeEmitter(bus, paused=self.paused)
        self.sources = list(sources)
        self.status_path = Path(status_path) if status_path else None
        self.heartbeat_sec = float(heartbeat_sec)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.started_at = ""
        self.errors: dict[str, str] = {}

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        self.started_at = now_iso()
        for spec in self.sources:
            t = threading.Thread(target=self._run_source, args=(spec,),
                                 name=f"tl-{spec.name}", daemon=True)
            t.start()
            self._threads.append(t)

    def _run_source(self, spec: SourceSpec) -> None:
        try:
            spec.run(self.emitter, self._stop)
        except Exception as exc:        # one bad source never kills the supervisor
            self.errors[spec.name] = f"{type(exc).__name__}: {exc}"

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = 10.0) -> None:
        for t in self._threads:
            t.join(timeout)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def toggle_pause(self) -> None:
        if self.paused.is_set():
            self.paused.clear()
        else:
            self.paused.set()

    # -- status ------------------------------------------------------------- #
    def status(self, *, alive: bool = True) -> dict[str, Any]:
        return {
            "alive": alive,
            "pid": os.getpid(),
            "started": self.started_at,
            "heartbeat": now_iso(),
            "paused": self.paused.is_set(),
            "sources": [s.name for s in self.sources],
            "threads_alive": sum(1 for t in self._threads if t.is_alive()),
            "errors": dict(self.errors),
            "stats": self.bus.stats(),
        }

    def write_status(self, *, alive: bool = True) -> None:
        if self.status_path is None:
            return
        import json
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.status(alive=alive), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.status_path)   # atomic swap so readers never see a half file

    def run_forever(self) -> None:
        """Blocking. Installs signal handlers (main thread only), runs the
        heartbeat loop, and shuts down cleanly on stop."""
        self._install_signal_handlers()
        self.start()
        try:
            while not self._stop.is_set():
                self.write_status()
                self._stop.wait(self.heartbeat_sec)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
            self.join()
            self.write_status(alive=False)

    def _install_signal_handlers(self) -> None:
        def _handler(signum: int, frame: Any) -> None:
            self._stop.set()
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass            # not in main thread / unsupported -> rely on KeyboardInterrupt


# --------------------------------------------------------------------------- #
# Hotkeys (optional, lazy)
# --------------------------------------------------------------------------- #
def _register_hotkeys(sup: Supervisor) -> bool:
    """Best-effort whisper + privacy-pause hotkeys. Returns True if registered."""
    try:
        import keyboard
    except Exception:
        return False

    def _whisper() -> None:
        try:
            intent_bridge.whisper_prompt(sup.emitter)
        except Exception:
            pass

    try:
        keyboard.add_hotkey("ctrl+shift+m", _whisper)
        keyboard.add_hotkey("ctrl+shift+p", sup.toggle_pause)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Runtime construction (shared by the CLI entry and the tray UI)
# --------------------------------------------------------------------------- #
@dataclass
class Runtime:
    """Everything the supervisor needs, already wired from config — so both the
    headless ``capture`` entry and the tray UI build it the same way."""
    sup: Supervisor
    bus: Any
    roots: list[Path]
    diaries_dir: Path
    data_dir: Path


def build_runtime(*, enable_clipboard: bool = True, enable_agents: bool = True,
                  heartbeat_sec: float = 30.0,
                  cfg: dict[str, Any] | None = None,
                  projects: list[dict[str, Any]] | None = None) -> Runtime:
    """Resolve config -> allowlist -> bus -> sources -> Supervisor (not started)."""
    cfg = cfg if cfg is not None else (
        cfgmod.load_config() if cfgmod.CONFIG_PATH.exists() else {})
    projects = projects if projects is not None else (
        cfgmod.load_projects() if cfgmod.PROJECTS_PATH.exists() else [])

    roots = cfgmod.allowlist_roots(cfg, projects)
    allow = Allowlist(roots)
    ddir = cfgmod.data_dir(cfg)
    diaries_dir = cfgmod.BASE_DIR / cfg.get("paths", {}).get("diaries_dir", "diaries")
    diff_policy = cfgmod.diff_policy_from(cfg, projects)
    bus = EventBus(ddir / "events", allow, diff_policy=diff_policy, diffs_dir=ddir / "diffs")

    agent_drop = ddir / "agent_drop"
    agent_archive = agent_drop / "_processed"

    sources = build_sources(
        cfg, [str(r) for r in roots],
        enable_clipboard=enable_clipboard, enable_agents=enable_agents,
        agent_drop_dir=agent_drop, agent_archive_dir=agent_archive,
        exclude_dirs=[ddir, diaries_dir], diff_policy=diff_policy)

    sup = Supervisor(bus, sources, status_path=ddir / "daemon_status.json",
                     heartbeat_sec=heartbeat_sec)
    return Runtime(sup, bus, roots, diaries_dir, ddir)


# --------------------------------------------------------------------------- #
# Top-level entry
# --------------------------------------------------------------------------- #
def run_capture(*, enable_clipboard: bool = True, enable_agents: bool = True,
                heartbeat_sec: float = 30.0, hotkeys: bool = True,
                cfg: dict[str, Any] | None = None,
                projects: list[dict[str, Any]] | None = None) -> Supervisor:
    """Build the bus + sources from config and run the supervisor until stopped."""
    rt = build_runtime(enable_clipboard=enable_clipboard, enable_agents=enable_agents,
                       heartbeat_sec=heartbeat_sec, cfg=cfg, projects=projects)
    sup, bus, roots = rt.sup, rt.bus, rt.roots

    print(f"[tl] capture starting — sources: {[s.name for s in sup.sources]}")
    print(f"[tl] allowlist roots: {len(roots)} | data: {rt.data_dir}")
    if not roots:
        print("[tl] WARNING: no allowlist roots — fs watching is off. "
              "Add project signal paths or privacy.allowlist_extra.")
    if hotkeys and _register_hotkeys(sup):
        print("[tl] hotkeys: ctrl+shift+m whisper · ctrl+shift+p pause")
    print("[tl] Ctrl+C to stop.")

    try:
        sup.run_forever()
    finally:
        bus.close()
        print(f"[tl] capture stopped. {bus.stats()}")
    return sup
