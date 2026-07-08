"""Intent bridges — the human's own voice + the clipboard, fed deterministically.

Two low-effort, high-value intent signals that the OS can't infer:

  * narration (the "whisper" floor) — a short typed note of what the human is
    doing, optionally retroactive ("since 2pm" relabels an earlier window). When
    the note is meaningful it becomes the session's intent; when it's terse or
    absent we record that and do NOT invent intent (case C6).

  * clipboard — captured then handed to the gate, which types/summarizes it and
    drops credential-shaped content. Raw clipboard text is never persisted
    (case C7). This adapter only de-dups and forwards; the gate does the redaction.

Deterministic and stdlib-only in the core. The live driver lazily uses a clipboard
library + a tiny prompt dialog.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from throughlog.schema import NormalizedEvent, make_event, NARRATION, CLIPBOARD
from throughlog.intent.ladder import is_meaningful_narration

# "since 2pm" / "since 14:30" — retroactively frame an earlier window.
_SINCE_RE = re.compile(r"\bsince\s+([0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm)?)\b", re.IGNORECASE)


def parse_retroactive(note: str, ref_dt: datetime) -> tuple[str, str | None]:
    """Split a narration note into (cleaned_note, retroactive_since_iso | None)."""
    m = _SINCE_RE.search(note)
    if not m:
        return note.strip(), None
    raw = m.group(1).strip().lower().replace(" ", "")
    since = None
    for fmt in ("%I:%M%p", "%H:%M", "%I%p", "%H"):
        try:
            p = datetime.strptime(raw, fmt)
            since = ref_dt.replace(hour=p.hour, minute=p.minute, second=0, microsecond=0)
            if since > ref_dt:                 # a time later than now means yesterday
                since -= timedelta(days=1)
            break
        except ValueError:
            continue
    cleaned = _SINCE_RE.sub("", note).strip(" -,;")
    return cleaned, (since.isoformat() if since else None)


def make_narration(text: str, ts: str) -> NormalizedEvent:
    """Build a NARRATION event, parsing retroactive framing and flagging whether
    the note actually carries intent (so the ladder never fabricates from filler)."""
    try:
        ref = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        ref = datetime.now().astimezone()
    cleaned, since = parse_retroactive(text.strip(), ref)
    payload: dict = {"note": cleaned, "meaningful": is_meaningful_narration(cleaned)}
    if since:
        payload["retroactive_since"] = since
    return make_event(NARRATION, kind="intent", adapter="intent_bridge",
                      payload=payload, ts_wall=ts)


class ClipboardCapture:
    """De-dups consecutive identical clipboard contents; emits CLIPBOARD events
    with RAW content for the gate to type/redact. Never persists raw itself."""

    def __init__(self) -> None:
        self._last = ""

    def observe(self, text: str, ts: str) -> NormalizedEvent | None:
        t = (text or "").strip()
        if not t or t == self._last:
            return None
        self._last = t
        return make_event(CLIPBOARD, kind="intent", adapter="intent_bridge",
                          payload={"content": t}, ts_wall=ts)


# --------------------------------------------------------------------------- #
# Live drivers — lazy optional deps (clipboard lib + a prompt dialog).
# --------------------------------------------------------------------------- #
def watch_clipboard_live(emitter, *, stop=None, interval_sec: float = 3.0) -> None:
    """Poll the clipboard, de-dup, and forward CLIPBOARD events for the gate to
    type/redact. Runs until ``stop`` (a threading.Event) is set or
    KeyboardInterrupt. Raw clipboard content is never persisted here."""
    import threading
    import pyperclip
    from throughlog.schema import now_iso

    stop = stop or threading.Event()
    cap = ClipboardCapture()
    try:
        while not stop.is_set():
            try:
                ev = cap.observe(pyperclip.paste(), now_iso())
                if ev is not None:
                    emitter.emit(ev)
            except Exception:
                pass
            stop.wait(interval_sec)
    except KeyboardInterrupt:
        return


def whisper_prompt(emitter) -> None:
    """Pop a one-line dialog asking what the human is doing; emit a NARRATION."""
    import tkinter as tk
    from tkinter import simpledialog
    from throughlog.schema import now_iso

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    raw = simpledialog.askstring(
        "Whisper — Intent Note",
        'What are you working on?\n\nTip: add "since 2pm" to relabel an earlier window.',
        parent=root)
    root.destroy()
    if raw and raw.strip():
        emitter.emit(make_narration(raw.strip(), now_iso()))
