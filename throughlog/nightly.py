"""In-process nightly synthesis — the no-admin way to "synthesize every night".

Task Scheduler / launchd / cron can run synthesis while nothing is open, but
registering a Task Scheduler job needs elevation on Windows (``schtasks /Create``
writes under the protected root task folder, so a normal user hits "Access is
denied"). When the app is already running all day — which it is, once capture
autostarts at logon — it can simply synthesize *itself* at a chosen time. No
elevated task, no extra moving parts.

This module is that timer, split the repo's usual way:

  * a **pure decision** (:func:`due_for_synthesis` / :func:`parse_hhmm`) that is
    unit-tested with an injected clock, and
  * a thin :class:`NightlyTimer` driver thread that, when the target time passes,
    shells out to ``tl synthesize`` exactly like the tray's "Synthesize now"
    (so it inherits the deterministic-without-a-key behavior and never blocks).

It runs synthesis **at most once per calendar day**: the first tick at/after the
target time fires it and records the date; a late launch (logging in after the
target, having missed it) catches up on the first tick. No LLM is touched here —
the spawned ``synthesize`` process owns that boundary.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable


def parse_hhmm(s: Any) -> tuple[int, int] | None:
    """``"22:30"`` -> ``(22, 30)``; ``None`` for anything not a valid 24h time."""
    try:
        hh, mm = str(s).split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    return (h, m) if (0 <= h < 24 and 0 <= m < 60) else None


def due_for_synthesis(target_hhmm: Any, now: datetime,
                      last_run: date | None) -> bool:
    """Whether synthesis should run on this tick: ``now`` is at/after today's target
    time **and** it hasn't already run today. Pure — the timer injects the clock."""
    hm = parse_hhmm(target_hhmm)
    if hm is None:
        return False
    if last_run == now.date():
        return False
    return (now.hour, now.minute) >= hm


class NightlyTimer:
    """Background thread that runs synthesis once a day at ``target_hhmm``.

    ``run`` (defaults to spawning ``tl synthesize``) and ``clock`` are injectable so
    the decision can be tested without real time or a subprocess. ``start`` is a
    no-op when ``target_hhmm`` is unset/invalid, so a missing schedule costs nothing."""

    def __init__(self, target_hhmm: str | None, *,
                 run: Callable[[], None] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 tick_sec: float = 30.0,
                 base_dir: str | Path | None = None) -> None:
        self.target = target_hhmm
        self._run = run or self._default_run
        self._clock = clock or datetime.now
        self.tick = float(tick_sec)
        self._base = base_dir
        self._stop = threading.Event()
        self._last: date | None = None
        self._thread: threading.Thread | None = None

    def _default_run(self) -> None:
        try:                               # detached; never blocks the timer thread
            subprocess.Popen([sys.executable, "-m", "throughlog.cli", "synthesize"],
                             cwd=str(self._base) if self._base else None)
        except Exception:
            pass

    def check(self) -> bool:
        """One tick. Runs synthesis if due (recording the date so it fires once a
        day). Returns whether it ran — handy for tests."""
        now = self._clock()
        if due_for_synthesis(self.target, now, self._last):
            self._last = now.date()
            self._run()
            return True
        return False

    def start(self) -> None:
        if parse_hhmm(self.target) is None:
            return                         # no schedule configured -> nothing to run

        def _loop() -> None:
            while not self._stop.is_set():
                try:
                    self.check()
                except Exception:
                    pass
                self._stop.wait(self.tick)

        self._thread = threading.Thread(target=_loop, name="tl-nightly", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
