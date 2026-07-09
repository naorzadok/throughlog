"""``tl hook`` — safe, idempotent installer for AI-agent drop-in hooks.

Wires ``integrations/claude_code/tl_hook.py`` / ``integrations/cursor/tl_hook.py``
into the host tool's own settings file, without disturbing any hooks the user
already has configured for something else. Same pure-builder / thin-driver split
as :mod:`throughlog.deploy` (used there for autostart/schedule):

  * ``merge_*`` / ``strip_*`` are pure functions over a settings dict — install or
    remove *only* the entries whose ``command`` mentions ``tl_hook.py`` (our
    marker), leaving every other event/matcher/top-level key byte-for-byte alone.
    Re-running install after the repo moves replaces the stale command instead of
    duplicating it (idempotent).
  * ``install_hook`` / ``uninstall_hook`` / ``hook_status`` are the thin drivers:
    resolve the settings path for ``scope`` (``user`` -> ``~/.claude/settings.json``
    / ``~/.cursor/hooks.json``, ``project`` -> the repo's ``.claude/``/``.cursor/``),
    read-or-``{}``, call the pure function, write atomically (tmp-file + replace,
    same trick as ``appconfig._atomic_write_json``).

Claude Code's shape is nested (``{"hooks": {EVENT: [{"matcher"?, "hooks": [...]}]}}``);
Cursor's is flat (``{"version": 1, "hooks": {EVENT: [{"command", "matcher"?}]}}``) —
genuinely different enough that each tool gets its own merge/strip pair, dispatched
through a small registry.

    python -m throughlog.cli hook enable  claude-code|cursor [--scope user|project]
    python -m throughlog.cli hook disable claude-code|cursor [--scope user|project]
    python -m throughlog.cli hook status  claude-code|cursor [--scope user|project]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from throughlog import config as cfgmod
from throughlog import deploy

# Substring identifying "our" hook command entries, so re-running install replaces
# a stale prior path instead of duplicating, and strip only ever touches ours.
_MARKER = "tl_hook.py"

TOOLS = ("claude-code", "cursor")

_SCRIPT_REL = {
    "claude-code": Path("claude_code") / "tl_hook.py",
    "cursor": Path("cursor") / "tl_hook.py",
}

_SETTINGS_REL = {
    "claude-code": Path(".claude") / "settings.json",
    "cursor": Path(".cursor") / "hooks.json",
}


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def _quote(s: str) -> str:
    """Wrap in double quotes if it contains whitespace (Windows-safe enough for
    a JSON ``command`` string a shell will parse)."""
    return f'"{s}"' if any(c.isspace() for c in s) else s


def hook_command(tool: str, *, python: str | None = None) -> str:
    """The ``<python> <script>`` command line installed for ``tool``."""
    py = python or deploy.python_exe()
    script = cfgmod.BASE_DIR / "integrations" / _SCRIPT_REL[tool]
    return f"{_quote(py)} {_quote(str(script))}"


# --------------------------------------------------------------------------- #
# Claude Code — nested shape
# --------------------------------------------------------------------------- #
def _cc_is_ours(entry: dict[str, Any]) -> bool:
    return any(_MARKER in (h.get("command") or "") for h in entry.get("hooks", []))


def merge_claude_code(settings: dict[str, Any], command: str) -> dict[str, Any]:
    """Install PostToolUse (matcher ``Edit|Write|MultiEdit``) + Stop entries running
    ``command``, replacing any prior entry of ours; every other entry (any other
    event, matcher, or hook) is left untouched."""
    out = dict(settings)
    hooks: dict[str, Any] = dict(out.get("hooks") or {})

    for event, matcher in (("PostToolUse", "Edit|Write|MultiEdit"), ("Stop", None)):
        kept = [e for e in (hooks.get(event) or []) if not _cc_is_ours(e)]
        block: dict[str, Any] = {"hooks": [{"type": "command", "command": command}]}
        if matcher is not None:
            block["matcher"] = matcher
        kept.append(block)
        hooks[event] = kept

    out["hooks"] = hooks
    return out


def strip_claude_code(settings: dict[str, Any]) -> dict[str, Any]:
    out = dict(settings)
    hooks: dict[str, Any] = dict(out.get("hooks") or {})

    for event in ("PostToolUse", "Stop"):
        kept = [e for e in (hooks.get(event) or []) if not _cc_is_ours(e)]
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)

    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out


def _cc_installed(settings: dict[str, Any]) -> bool:
    hooks = settings.get("hooks") or {}
    return any(_cc_is_ours(e) for event in ("PostToolUse", "Stop")
              for e in (hooks.get(event) or []))


# --------------------------------------------------------------------------- #
# Cursor — flat shape
# --------------------------------------------------------------------------- #
def _cur_is_ours(entry: dict[str, Any]) -> bool:
    return _MARKER in (entry.get("command") or "")


def merge_cursor(settings: dict[str, Any], command: str) -> dict[str, Any]:
    """Install afterFileEdit + stop entries running ``command`` (no matcher
    needed — afterFileEdit already only fires for file edits)."""
    out = dict(settings)
    out.setdefault("version", 1)
    hooks: dict[str, Any] = dict(out.get("hooks") or {})

    for event in ("afterFileEdit", "stop"):
        kept = [e for e in (hooks.get(event) or []) if not _cur_is_ours(e)]
        kept.append({"command": command})
        hooks[event] = kept

    out["hooks"] = hooks
    return out


def strip_cursor(settings: dict[str, Any]) -> dict[str, Any]:
    out = dict(settings)
    hooks: dict[str, Any] = dict(out.get("hooks") or {})

    for event in ("afterFileEdit", "stop"):
        kept = [e for e in (hooks.get(event) or []) if not _cur_is_ours(e)]
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)

    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out


def _cur_installed(settings: dict[str, Any]) -> bool:
    hooks = settings.get("hooks") or {}
    return any(_cur_is_ours(e) for event in ("afterFileEdit", "stop")
              for e in (hooks.get(event) or []))


# --------------------------------------------------------------------------- #
# Registry + thin drivers
# --------------------------------------------------------------------------- #
_MERGE = {"claude-code": merge_claude_code, "cursor": merge_cursor}
_STRIP = {"claude-code": strip_claude_code, "cursor": strip_cursor}
_INSTALLED = {"claude-code": _cc_installed, "cursor": _cur_installed}


def settings_path(tool: str, *, scope: str = "user") -> Path:
    base = Path.home() if scope == "user" else cfgmod.BASE_DIR
    return base / _SETTINGS_REL[tool]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def install_hook(tool: str, *, scope: str = "user",
                 python: str | None = None) -> tuple[bool, str]:
    if tool not in TOOLS:
        return False, f"unknown tool {tool!r} — choose one of {', '.join(TOOLS)}"
    path = settings_path(tool, scope=scope)
    merged = _MERGE[tool](_read_json(path), hook_command(tool, python=python))
    _atomic_write_json(path, merged)
    return True, f"installed {tool} hook -> {path}"


def uninstall_hook(tool: str, *, scope: str = "user") -> tuple[bool, str]:
    if tool not in TOOLS:
        return False, f"unknown tool {tool!r} — choose one of {', '.join(TOOLS)}"
    path = settings_path(tool, scope=scope)
    if not path.exists():
        return True, f"{tool} hook was not installed ({path} not found)."
    _atomic_write_json(path, _STRIP[tool](_read_json(path)))
    return True, f"removed {tool} hook -> {path}"


def hook_status(tool: str, *, scope: str = "user") -> tuple[bool, str]:
    if tool not in TOOLS:
        return False, f"unknown tool {tool!r} — choose one of {', '.join(TOOLS)}"
    path = settings_path(tool, scope=scope)
    if not path.exists():
        return False, f"not installed ({path} not found)."
    installed = _INSTALLED[tool](_read_json(path))
    return installed, (f"installed at {path}" if installed
                       else f"not installed ({path} has no {tool} hook entry)")
