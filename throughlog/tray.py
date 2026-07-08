"""System-tray UI for live capture — a visible, controllable front-end.

Runs the same capture supervisor as ``throughlog.cli capture``, but in-process behind a
tray icon so the day-long recording is visible and controllable without a console:

  * the icon is **green** while recording, **amber** while paused;
  * the menu shows a live one-line status (event count, sources alive);
  * *Pause / Resume* flips the privacy pause (events are dropped while paused) —
    also bound to the global ``Ctrl+Shift+P`` hotkey;
  * *Whisper note…* pops the intent dialog — also bound to ``Ctrl+Shift+M``;
  * *Synthesize now* runs the analysis pipeline over what's captured so far;
  * *Open journal folder* / *Quit* do the obvious thing — Quit shuts the
    supervisor down cleanly (each source flushes) and writes a final status.

The two global hotkeys are registered here (best-effort, via the ``keyboard`` lib
if present) so they work the same whether capture runs headless or behind the tray;
when they register, their accelerators are shown right-aligned on the menu lines.

``pystray`` + ``Pillow`` are imported lazily (the ``capture`` extra), so importing
this module — and the pure :func:`status_line` / :func:`menu_label` helpers — needs
no GUI deps. No LLM is touched here; *Synthesize now* shells out to ``synthesize``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import Any

from throughlog import config as cfgmod
from throughlog.capture import build_runtime
from throughlog.sources import intent_bridge

_GREEN = (46, 160, 67, 255)     # recording
_AMBER = (219, 154, 4, 255)     # paused


def status_line(status: dict[str, Any]) -> str:
    """One-line human summary from a supervisor ``status()`` dict (pure)."""
    state = "paused" if status.get("paused") else "recording"
    stats = status.get("stats") or {}
    written = stats.get("written", 0)
    alive = status.get("threads_alive", 0)
    n_src = len(status.get("sources") or [])
    return f"ThroughLog — {state} · {written} events · {alive}/{n_src} sources"


def menu_label(base: str, shortcut: str | None) -> str:
    """Tray menu label with an optional shortcut hint shown inline (pure).

    pystray's Win32 backend builds menu text with ``MIIM_STRING`` and does NOT
    honor the ``\\t`` accelerator-column convention, so the hint is appended as
    plain parenthetical text that always renders. Only attached when the global
    hotkey actually registered, so the menu never advertises a dead shortcut."""
    return f"{base}  ({shortcut})" if shortcut else base


def _icon_image(color: tuple[int, int, int, int]):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=color)
    d.ellipse((26, 26, 38, 38), fill=(255, 255, 255, 235))   # a small "lens" dot
    return img


def _run_synthesis_async() -> None:
    """Synthesize today's captured events in a subprocess (never blocks the UI)."""
    try:
        subprocess.run([sys.executable, "-m", "throughlog.cli", "synthesize"],
                       cwd=str(cfgmod.BASE_DIR))
    except Exception:
        pass


def run_tray(*, enable_clipboard: bool = True, enable_agents: bool = True,
             heartbeat_sec: float = 30.0) -> None:
    """Build the runtime, start capture, and block on the tray icon until Quit."""
    import pystray

    rt = build_runtime(enable_clipboard=enable_clipboard, enable_agents=enable_agents,
                       heartbeat_sec=heartbeat_sec)
    sup, bus = rt.sup, rt.bus
    sup.start()

    hb_stop = threading.Event()

    def _heartbeat() -> None:
        while not hb_stop.is_set():
            try:
                sup.write_status()
            except Exception:
                pass
            hb_stop.wait(heartbeat_sec)

    threading.Thread(target=_heartbeat, name="tl-tray-heartbeat", daemon=True).start()

    icon = pystray.Icon("tl", _icon_image(_GREEN), "ThroughLog")

    def _refresh() -> None:
        icon.icon = _icon_image(_AMBER if sup.paused.is_set() else _GREEN)
        icon.update_menu()

    def _do_pause() -> None:
        sup.toggle_pause()
        _refresh()

    def _do_whisper() -> None:
        threading.Thread(target=lambda: intent_bridge.whisper_prompt(sup.emitter),
                         daemon=True).start()

    def on_pause(_icon: Any, _item: Any) -> None:
        _do_pause()

    def on_whisper(_icon: Any, _item: Any) -> None:
        _do_whisper()

    # Global hotkeys — same bindings as headless capture, so muscle memory carries
    # over and the menu hints below are truthful. Best-effort; the menu only shows
    # a shortcut when its registration actually succeeded.
    pause_hk = whisper_hk = None
    try:
        import keyboard

        keyboard.add_hotkey("ctrl+shift+m", _do_whisper)
        keyboard.add_hotkey("ctrl+shift+p", _do_pause)
        pause_hk, whisper_hk = "Ctrl+Shift+P", "Ctrl+Shift+M"
    except Exception as exc:                      # no keyboard lib / no permission
        print(f"[tl] hotkeys unavailable ({exc}); menu items still work by click.")

    def on_synthesize(_icon: Any, _item: Any) -> None:
        threading.Thread(target=_run_synthesis_async, daemon=True).start()

    def on_open(_icon: Any, _item: Any) -> None:
        try:
            rt.journal_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(rt.journal_dir))   # noqa: this is Windows-only by design
        except Exception:
            pass

    def on_quit(_icon: Any, _item: Any) -> None:
        hb_stop.set()
        sup.stop()
        icon.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem(lambda _i: status_line(sup.status()), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda _i: menu_label(
            "Resume capture" if sup.paused.is_set() else "Pause capture", pause_hk),
            on_pause),
        pystray.MenuItem(menu_label("Whisper note…", whisper_hk), on_whisper),
        pystray.MenuItem("Synthesize now", on_synthesize),
        pystray.MenuItem("Open journal folder", on_open),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    print(f"[tl] tray running — {len(rt.roots)} allowlist root(s), data: {rt.data_dir}")
    if not rt.roots:
        print("[tl] WARNING: no allowlist roots — fs watching is off.")
    if pause_hk:
        print("[tl] hotkeys: ctrl+shift+m whisper · ctrl+shift+p pause")
    print("[tl] right-click the tray icon to pause / whisper / synthesize / quit.")

    try:
        icon.run()
    finally:
        hb_stop.set()
        if pause_hk:
            try:
                import keyboard
                keyboard.remove_all_hotkeys()
            except Exception:
                pass
        sup.stop()
        sup.join()
        sup.write_status(alive=False)
        bus.close()
        print(f"[tl] tray stopped. {bus.stats()}")
