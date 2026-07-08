"""OS focus adapter — deterministic sessionization of window focus.

The pure ``FocusSessionizer`` state machine consumes a stream of focus *samples*
(window + idle-seconds + keystroke count per tick) and emits NormalizedEvents:
``FOCUS_SESSION``, ``IDLE_START``, ``IDLE_END``. It contains no Windows API, no
threads and no wall clock — the caller supplies every timestamp — which makes it
fully deterministic and unit-testable.

The same core is driven two ways:
  * ``capture_live`` — the live loop, sampling real UIA focus + GetLastInputInfo,
  * the scenario simulator — replaying declarative focus ticks from sim/.

Anchor/satellite grouping with a settle timer (debounce), input-density mode
(PRODUCING vs READING), and idle detection. A focused, non-idle session is
recorded even with ~0 keystrokes (reading a paper), instead of being dropped as
"nothing happened". That is what makes case C3 (reading vs AFK) pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import PureWindowsPath

from throughlog.schema import (
    NormalizedEvent, make_event,
    FOCUS_SESSION, IDLE_START, IDLE_END, DEEP_WORK,
)

# Default capture timers; overridable per-construction.
DEFAULT_ANCHOR_TIMEOUT_SEC = 60.0     # settle timer before a candidate is promoted
DEFAULT_IDLE_THRESHOLD_SEC = 600.0    # no input this long -> idle
DEFAULT_KPS_THRESHOLD = 0.3           # keys/sec at/above -> PRODUCING, else READING
DEFAULT_PERIODIC_FLUSH_SEC = 900.0    # cap on how long one session runs before a cut
# DEEP_WORK = opaque-app production: mouse-driven, ~0 keys, but saving / long active.
DEFAULT_DEEP_WORK_MIN_SEC = 300.0     # session this long can qualify on mouse alone
DEFAULT_MOUSE_ACTIVE_MIN = 30         # accumulated mouse-activity ticks for mouse-only path


# --------------------------------------------------------------------------- #
# Sample / window value types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Window:
    title: str
    process: str = ""


@dataclass
class FocusSample:
    """One observation tick. ``keys``/``mouse``/``saves`` are counts since the
    previous tick; the remaining fields are intent signals for the focused app."""
    ts: str                          # ISO-8601 wall time (tz-aware)
    window: Window | None = None
    idle_seconds: float = 0.0
    keys: int = 0
    mouse: int = 0                   # mouse-activity ticks (movement/clicks) since last
    saves: int = 0                   # file saves observed in this app since last
    uia_value: str = ""              # UIA document/value text (intent ladder rung 1)
    cmdline: str = ""                # process command line (rung 3)
    cwd: str = ""                    # process working directory (rung 3)
    saved_artifact: str = ""         # path of the most recent save (rung 4)
    narration: str = ""              # human note (rung 6)


# --------------------------------------------------------------------------- #
# Time helpers (caller-supplied ISO timestamps -> arithmetic)
# --------------------------------------------------------------------------- #
def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _elapsed(start: str, end: str) -> float:
    return (_parse(end) - _parse(start)).total_seconds()


def _minus(ts: str, seconds: float) -> str:
    return (_parse(ts) - timedelta(seconds=max(0.0, seconds))).isoformat()


# --------------------------------------------------------------------------- #
# Window title -> active file (ported from capture_daemon._extract_file_from_title)
# --------------------------------------------------------------------------- #
_TITLE_SEP_RE = re.compile(r" [-—|–] ")          # " - ", " | ", en/em dash
_FILE_TOKEN_RE = re.compile(r"\.[A-Za-z0-9]{1,6}$")


def extract_active_file(title: str) -> str | None:
    """Best-effort filename/path parsed from a window title, else None."""
    if not title:
        return None
    for part in _TITLE_SEP_RE.split(title):
        part = part.strip()
        if not part:
            continue
        p = PureWindowsPath(part)
        if (len(p.drive) == 2 and p.drive[1] == ":") and p.suffix:
            return str(p)                                  # absolute path, high confidence
        if _FILE_TOKEN_RE.search(part) and "\\" not in part and "/" not in part:
            return part                                    # bare filename with extension
    return None


# --------------------------------------------------------------------------- #
# The deterministic core
# --------------------------------------------------------------------------- #
class FocusSessionizer:
    """Anchor/satellite focus state machine. Feed it samples in time order;
    each ``feed`` returns the NormalizedEvents produced by that tick."""

    def __init__(self, *,
                 anchor_timeout_sec: float = DEFAULT_ANCHOR_TIMEOUT_SEC,
                 idle_threshold_sec: float = DEFAULT_IDLE_THRESHOLD_SEC,
                 kps_threshold: float = DEFAULT_KPS_THRESHOLD,
                 periodic_flush_sec: float = DEFAULT_PERIODIC_FLUSH_SEC,
                 deep_work_min_sec: float = DEFAULT_DEEP_WORK_MIN_SEC,
                 mouse_active_min: int = DEFAULT_MOUSE_ACTIVE_MIN) -> None:
        self.anchor_timeout = float(anchor_timeout_sec)
        self.idle_threshold = float(idle_threshold_sec)
        self.kps_threshold = float(kps_threshold)
        self.periodic_flush = float(periodic_flush_sec)
        self.deep_work_min = float(deep_work_min_sec)
        self.mouse_active_min = int(mouse_active_min)

        self.anchor: Window | None = None
        self.session_start: str | None = None
        self.pending: Window | None = None
        self.pending_since: str | None = None
        self.satellites: set[tuple[str, str]] = set()
        self.key_count = 0
        self.mouse_count = 0
        self.save_count = 0
        self._sig: dict[str, str] = {}     # latest intent signals for the anchor
        self.idle = False
        self.idle_start_ts: str | None = None
        self._last_ts: str | None = None

    # -- public ------------------------------------------------------------- #
    def feed(self, sample: FocusSample) -> list[NormalizedEvent]:
        out: list[NormalizedEvent] = []
        ts = sample.ts
        self._last_ts = ts
        self.key_count += max(0, int(sample.keys))
        self.mouse_count += max(0, int(sample.mouse))
        self.save_count += max(0, int(sample.saves))

        is_idle = sample.idle_seconds >= self.idle_threshold

        # Enter idle: end the active session at the moment input actually stopped.
        if is_idle and not self.idle:
            idle_start = _minus(ts, sample.idle_seconds)
            out += self._flush(idle_start)
            self._clear_anchor()
            self.idle = True
            self.idle_start_ts = idle_start
            out.append(make_event(IDLE_START, kind="os", adapter="os_focus",
                                  payload={"idle_after_sec": round(sample.idle_seconds)},
                                  ts_wall=idle_start))
            return out

        # Leave idle: report how long the user was away, then handle this tick.
        if (not is_idle) and self.idle:
            away = _elapsed(self.idle_start_ts, ts) if self.idle_start_ts else 0.0
            out.append(make_event(IDLE_END, kind="os", adapter="os_focus",
                                  payload={"away_sec": round(away)}, ts_wall=ts))
            self.idle = False
            self.idle_start_ts = None

        if is_idle:                       # still idle, nothing more to do
            return out

        if sample.window is not None:
            out += self._observe_window(ts, sample.window)
            # Capture the latest intent signals for whatever is now the anchor.
            if self.anchor is not None and sample.window.title == self.anchor.title:
                for key in ("uia_value", "cmdline", "cwd", "saved_artifact", "narration"):
                    val = getattr(sample, key)
                    if val:
                        self._sig[key] = val

        # Cap session length so long-lived focus still produces periodic records.
        if self.anchor is not None and self.session_start is not None:
            if _elapsed(self.session_start, ts) >= self.periodic_flush:
                out += self._flush(ts)
                self.session_start = ts   # same anchor, fresh session window
        return out

    def close(self) -> list[NormalizedEvent]:
        """Flush the final in-flight session (call at shutdown / scenario end)."""
        if self._last_ts is None:
            return []
        return self._flush(self._last_ts)

    # -- internals ---------------------------------------------------------- #
    def _observe_window(self, ts: str, win: Window) -> list[NormalizedEvent]:
        out: list[NormalizedEvent] = []
        if self.anchor is not None and win.title == self.anchor.title:
            self.pending = None                      # returned to anchor -> cancel
            self.pending_since = None
        elif self.pending is not None and win.title == self.pending.title:
            if _elapsed(self.pending_since, ts) >= self.anchor_timeout:
                out += self._flush(ts)               # candidate settled -> promote
                self.anchor = win
                self.session_start = ts
                self.pending = None
                self.pending_since = None
        else:
            if self.anchor is None:
                self.anchor = win                    # first window -> anchor now
                self.session_start = ts
            else:
                self.satellites.add((win.title, win.process))   # transient -> satellite
                self.pending = win
                self.pending_since = ts
        return out

    def _flush(self, end_ts: str) -> list[NormalizedEvent]:
        if self.anchor is None or self.session_start is None:
            self._reset_accumulators()
            return []
        duration = max(0.0, _elapsed(self.session_start, end_ts))
        # Drop only genuinely empty flushes (zero time, no input, no satellites).
        # A non-idle focused stretch with ~0 keys is real reading and is kept.
        if (duration <= 0.0 and self.key_count == 0
                and self.mouse_count == 0 and self.save_count == 0 and not self.satellites):
            self._reset_accumulators()
            return []
        kps = self.key_count / duration if duration > 0 else 0.0
        etype, mode = self._classify(kps, duration)
        intent = self._resolve_intent(duration)
        payload = {
            "anchor": self.anchor.title,
            "process": self.anchor.process,
            "active_file": extract_active_file(self.anchor.title),
            "satellites": [{"title": t, "process": p} for t, p in sorted(self.satellites)],
            "duration_sec": round(duration, 1),
            "activity_score": self.key_count,
            "mouse_score": self.mouse_count,
            "saves": self.save_count,
            "kps": round(kps, 3),
            "mode": mode,
            "intent": {"label": intent.label, "method": intent.method,
                       "confidence": intent.confidence},
            "ended": end_ts,
        }
        ev = make_event(etype, kind="os", adapter="os_focus",
                        payload=payload, ts_wall=self.session_start)
        ev.attribution.method = intent.method
        ev.attribution.confidence = intent.confidence
        self._reset_accumulators()
        return [ev]

    def _classify(self, kps: float, duration: float) -> tuple[str, str]:
        """(event_type, mode). Keyboard density -> PRODUCING; otherwise opaque-app
        production evidence (saves, or sustained mouse over a long session) ->
        DEEP_WORK; otherwise READING."""
        if kps >= self.kps_threshold:
            return FOCUS_SESSION, "PRODUCING"
        if self.save_count > 0 or (self.mouse_count >= self.mouse_active_min
                                   and duration >= self.deep_work_min):
            return DEEP_WORK, "DEEP_WORK"
        return FOCUS_SESSION, "READING"

    def _resolve_intent(self, duration: float):
        from throughlog.intent.ladder import resolve_intent, IntentSignals
        return resolve_intent(IntentSignals(
            uia_value=self._sig.get("uia_value", ""),
            title=self.anchor.title if self.anchor else "",
            process=self.anchor.process if self.anchor else "",
            cmdline=self._sig.get("cmdline", ""),
            cwd=self._sig.get("cwd", ""),
            saved_artifact=self._sig.get("saved_artifact", ""),
            narration=self._sig.get("narration", ""),
            keys=self.key_count,
            duration_sec=duration,
        ))

    def _reset_accumulators(self) -> None:
        self.satellites = set()
        self.key_count = 0
        self.mouse_count = 0
        self.save_count = 0
        self._sig = {}

    def _clear_anchor(self) -> None:
        self.anchor = None
        self.session_start = None
        self.pending = None
        self.pending_since = None


# --------------------------------------------------------------------------- #
# Live driver — optional per-OS deps imported lazily so the pure core (and the
# test suite / simulator) never require uiautomation/psutil/keyboard/pyobjc.
#
# The deterministic FocusSessionizer above is OS-agnostic, so the *analysis*
# pipeline is identical on every platform. Only these two probes — "what window
# is focused" and "how long since the last input" — are platform-specific, and
# both are dispatched by platform with a graceful (0.0 / None) fallback so an
# unsupported host degrades to idle-blind capture instead of crashing.
# --------------------------------------------------------------------------- #
import sys


def _idle_seconds() -> float:
    """Seconds since the last keyboard/mouse/touch input. 0.0 if unknown."""
    if sys.platform == "darwin":
        return _idle_seconds_macos()
    if sys.platform.startswith("win"):
        return _idle_seconds_windows()
    return _idle_seconds_x11()


def _focused_window() -> Window | None:
    """The currently focused top-level window, or None if it can't be read."""
    if sys.platform == "darwin":
        return _focused_window_macos()
    if sys.platform.startswith("win"):
        return _focused_window_windows()
    return _focused_window_x11()


# -- Windows ---------------------------------------------------------------- #
def _idle_seconds_windows() -> float:
    try:
        import ctypes

        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
        elapsed_ms = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        return max(elapsed_ms / 1000.0, 0.0)
    except Exception:
        return 0.0


def _focused_window_windows() -> Window | None:
    try:
        import uiautomation as auto
        import psutil

        focused = auto.GetFocusedControl()
        if not focused:
            return None
        top = focused.GetTopLevelControl()
        if not top:
            return None
        try:
            proc = psutil.Process(top.ProcessId).name()
        except Exception:
            proc = "unknown"
        return Window(title=top.Name or "Unknown", process=proc)
    except Exception:
        return None


# -- macOS ------------------------------------------------------------------ #
def _idle_seconds_macos() -> float:
    """Seconds since last HID input via Quartz CGEventSource (no special perms)."""
    try:
        import Quartz

        # kCGAnyInputEventType (0xFFFFFFFF) against the HID system event source.
        return max(float(Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateHIDSystemState,
            Quartz.kCGAnyInputEventType)), 0.0)
    except Exception:
        return 0.0


def _focused_window_macos() -> Window | None:
    """Frontmost app (NSWorkspace) + its window title (CGWindowList). The window
    title needs Screen-Recording permission; without it we fall back to the app
    name so capture still produces a process/title-bearing session."""
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        proc = str(app.localizedName() or "unknown")
        title = _macos_window_title(int(app.processIdentifier())) or proc
        return Window(title=title, process=proc)
    except Exception:
        return None


def _macos_window_title(pid: int) -> str | None:
    try:
        import Quartz

        opts = (Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements)
        for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
            if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer", 1) == 0:
                name = w.get("kCGWindowName")
                if name:
                    return str(name)
        return None
    except Exception:
        return None


# -- Linux / X11 ------------------------------------------------------------ #
def _idle_seconds_x11() -> float:
    """Best-effort idle via the `xprintidle` helper (ms). 0.0 if unavailable."""
    try:
        import subprocess

        out = subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=1)
        if out.returncode == 0 and out.stdout.strip():
            return max(float(out.stdout.strip()) / 1000.0, 0.0)
    except Exception:
        pass
    return 0.0


def _focused_window_x11() -> Window | None:
    """Active window title/class via `xdotool` if present. None otherwise."""
    try:
        import subprocess

        wid = subprocess.run(["xdotool", "getactivewindow"],
                             capture_output=True, text=True, timeout=1)
        if wid.returncode != 0 or not wid.stdout.strip():
            return None
        win_id = wid.stdout.strip()
        name = subprocess.run(["xdotool", "getwindowname", win_id],
                              capture_output=True, text=True, timeout=1)
        cls = subprocess.run(["xdotool", "getwindowclassname", win_id],
                             capture_output=True, text=True, timeout=1)
        title = name.stdout.strip() if name.returncode == 0 else ""
        proc = cls.stdout.strip() if cls.returncode == 0 else ""
        if not title and not proc:
            return None
        return Window(title=title or proc, process=proc)
    except Exception:
        return None


@dataclass
class _KeyCounter:
    count: int = 0
    _hooked: bool = field(default=False, repr=False)

    def start(self) -> None:
        import keyboard

        def _on_key(event: object) -> None:
            if getattr(event, "event_type", None) == "down":
                self.count += 1

        keyboard.hook(_on_key)
        self._hooked = True

    def take(self) -> int:
        n, self.count = self.count, 0
        return n


def idle_seconds() -> float:
    """Public idle probe (seconds since last input). Shared by the proc monitor
    and fs watcher so every adapter reads input-presence from one place."""
    return _idle_seconds()


def capture_live(emitter, *, stop=None, heartbeat_sec: float = 2.0, **cfg) -> None:
    """Live capture loop: sample real focus + idle + keystrokes, drive the
    sessionizer, push every event through ``emitter`` (the bus). Runs until
    ``stop`` (a threading.Event) is set or KeyboardInterrupt, then flushes the
    final session. ``stop`` lets the supervisor shut the loop down from another
    thread; with no ``stop`` it behaves like a standalone Ctrl+C loop."""
    import threading
    from throughlog.schema import now_iso

    stop = stop or threading.Event()
    sessionizer = FocusSessionizer(**cfg)
    keys = _KeyCounter()
    try:
        keys.start()
    except Exception:
        pass  # no keyboard hook available -> keystroke density degrades to 0
    try:
        while not stop.is_set():
            sample = FocusSample(ts=now_iso(), window=_focused_window(),
                                 idle_seconds=_idle_seconds(), keys=keys.take())
            for ev in sessionizer.feed(sample):
                emitter.emit(ev)
            stop.wait(heartbeat_sec)
    except KeyboardInterrupt:
        pass
    finally:
        for ev in sessionizer.close():
            emitter.emit(ev)
