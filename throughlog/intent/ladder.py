"""The intent-resolver ladder (deterministic).

Given the signals available for a focus session or process, resolve the best
*intent descriptor* and record which rung produced it. Rungs are tried in
priority order; the first that yields a usable signal wins:

    1. uia              — UIA tree document/value text (richest, when exposed)
    2. title            — window title: parsed active file, else a document-ish title
    3. proc_cmdline_cwd — working dir / command-line path (opaque & self-built exes)
    4. saved_artifact   — the file most recently saved in the session
    5. narration        — the human narration floor (explicit intent beats a
                          mechanical keystroke guess; only when it says something)
    6. input            — keystroke density (producing vs reading) — weak, last
    -> needs_review     — nothing usable; never fabricate intent

Everything here is pure and rule-based. The LLM (Phase 1) may later refine a
needs_review or low-confidence result, but it is never required to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PureWindowsPath

from throughlog.sources.os_focus import extract_active_file

# Titles that carry no intent on their own.
_GENERIC = {"", "unknown", "new tab", "untitled", "program manager",
            "desktop", "start", "task switching"}

# Narration tokens that carry no intent (so terse notes don't fabricate intent).
_FILLER = {"hmm", "stuff", "things", "idk", "meh", "na", "ok", "okay", "work",
           "wip", "todo", "note", "test", "yeah", "nope", "stuff", "etc"}

_KPS_PRODUCING = 0.3


def is_meaningful_narration(note: str) -> bool:
    """True if a narration note carries real intent (>= 2 tokens, at least one
    substantive non-filler word). Terse/filler notes return False so the ladder
    falls through rather than inventing intent (case C6)."""
    toks = re.findall(r"[A-Za-z0-9']+", (note or "").lower())
    if len(toks) < 2:
        return False
    return any(t not in _FILLER and len(t) >= 3 for t in toks)


@dataclass
class IntentSignals:
    uia_value: str = ""        # UIA document/value text for the focused control
    title: str = ""            # window title
    process: str = ""          # process image name (e.g. plainscan.exe)
    cmdline: str = ""          # full command line
    cwd: str = ""              # process working directory
    saved_artifact: str = ""   # most-recent file saved in this session
    narration: str = ""        # human note / whisper narration
    keys: int = 0              # keystrokes in the session
    duration_sec: float = 0.0


@dataclass
class IntentResult:
    label: str                 # the resolved descriptor ("" when needs_review)
    method: str                # which rung produced it
    confidence: float
    artifact: str | None = None  # a file/doc/dir path, when one was identified


def _looks_like_path(text: str) -> bool:
    return ("\\" in text or "/" in text) and len(text) > 3


def _doc_title(title: str) -> str | None:
    """A 'document - app' style title yields the left (document) segment."""
    for sep in (" - ", " — ", " | ", " – "):
        if sep in title:
            left = title.split(sep, 1)[0].strip()
            if len(left) >= 2 and left.lower() not in _GENERIC:
                return left
    return None


def _from_cwd_or_cmdline(cwd: str, cmdline: str) -> str | None:
    """Project-identifying label from the working dir or a path in the cmdline.
    Bare process names are intentionally NOT used here — they identify the tool,
    not the work, so they fall through to needs_review (case O3)."""
    if cwd:
        name = PureWindowsPath(cwd).name
        if name:
            return name
    if cmdline:
        for tok in cmdline.replace('"', " ").split():
            p = PureWindowsPath(tok)
            if len(p.parts) >= 2 and (p.parent.name or ""):
                return p.parent.name
    return None


def resolve_intent(s: IntentSignals) -> IntentResult:
    # 1 — UIA document/value text
    uia = s.uia_value.strip()
    if uia and uia.lower() not in _GENERIC:
        return IntentResult(uia, "uia", 0.9, artifact=uia if _looks_like_path(uia) else None)

    # 2 — window title
    f = extract_active_file(s.title)
    if f:
        return IntentResult(f, "title", 0.8, artifact=f)
    dt = _doc_title(s.title)
    if dt:
        return IntentResult(dt, "title", 0.55)

    # 3 — process working dir / command line (opaque & self-compiled exes)
    label = _from_cwd_or_cmdline(s.cwd, s.cmdline)
    if label:
        return IntentResult(label, "proc_cmdline_cwd", 0.6, artifact=(s.cwd or None))

    # 4 — most recent saved artifact
    if s.saved_artifact:
        return IntentResult(s.saved_artifact, "saved_artifact", 0.7, artifact=s.saved_artifact)

    # 5 — human narration (explicit intent beats a mechanical keystroke guess),
    #     but only when it actually says something (C6: no fabrication from filler).
    note = s.narration.strip()
    if note and is_meaningful_narration(note):
        return IntentResult(note, "narration", 0.5)

    # 6 — input density (weak)
    if s.duration_sec > 0 and s.keys > 0:
        kps = s.keys / s.duration_sec
        return IntentResult("producing" if kps >= _KPS_PRODUCING else "reading", "input", 0.3)

    return IntentResult("", "needs_review", 0.0)
