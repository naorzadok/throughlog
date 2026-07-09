#!/usr/bin/env python3
"""Claude Code -> ThroughLog hook: write what Claude Code did into your work journal.

Claude Code runs this on a hook event and passes a JSON object on stdin. We turn
that into a ThroughLog AGENT_REPORT and POST it to the ThroughLog endpoint, so Claude Code's
work shows up — trust-classified and attributed to the right project — in your
journal and dashboard. "Add one hook and Claude Code writes itself into your journal."

Install it with:

    python -m throughlog.cli hook enable claude-code

...or wire it up by hand in your Claude Code settings (`~/.claude/settings.json`
or a project `.claude/settings.json`):

    {
      "hooks": {
        "PostToolUse": [
          { "matcher": "Edit|Write|MultiEdit",
            "hooks": [ { "type": "command",
              "command": "python /ABS/PATH/throughlog/integrations/claude_code/tl_hook.py" } ] }
        ],
        "Stop": [
          { "hooks": [ { "type": "command",
              "command": "python /ABS/PATH/throughlog/integrations/claude_code/tl_hook.py" } ] }
        ]
      }
    }

Endpoint + auth come from the environment (see `integrations/_common.py`):
    TL_ENDPOINT   default http://127.0.0.1:8787/report
    TL_TOKEN      optional bearer token (relay)
    TL_DROP_DIR   optional fallback folder if the endpoint is down

This hook is defensive by construction: it NEVER blocks Claude Code. Any error is
swallowed and it always exits 0, so a journal outage can't interrupt your work.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _common


def _summarize(data: dict) -> tuple[str, dict]:
    """Build a (summary, fields) pair from a Claude Code hook payload. ``fields``
    are kwargs for AgentReporter.report (repo/files/project/session/status)."""
    event = data.get("hook_event_name", "")
    cwd = data.get("cwd") or ""
    session = data.get("session_id", "")
    fields: dict = {"session_id": session}
    if cwd:
        fields["repo"] = cwd                      # cwd path -> project attribution
        fields["project"] = Path(cwd).name

    if event == "PostToolUse":
        tool = data.get("tool_name", "tool")
        tin = data.get("tool_input", {}) or {}
        path = tin.get("file_path") or tin.get("path") or ""
        if path:
            fields["files"] = [path]
            summary = f"Claude Code {tool}: {Path(path).name}"
        else:
            summary = f"Claude Code used {tool}"
        return summary, fields

    if event == "Stop":
        where = Path(cwd).name if cwd else "a session"
        return f"Claude Code finished a session in {where}", fields

    # Generic fallback for any other hook event.
    return f"Claude Code event: {event or 'unknown'}", fields


def main() -> int:
    try:
        data = _common.read_stdin_json()
        summary, fields = _summarize(data)
        _common.send("agent:claude-code", "claude-code", summary, **fields)
    except Exception:
        pass  # never block Claude Code — a journal hiccup must not fail the tool
    return 0


if __name__ == "__main__":
    sys.exit(main())
