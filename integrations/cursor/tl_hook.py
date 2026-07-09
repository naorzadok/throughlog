#!/usr/bin/env python3
"""Cursor -> ThroughLog hook: write what Cursor's agent did into your work journal.

Cursor runs this on a hook event and passes a JSON object on stdin. We turn that
into a ThroughLog AGENT_REPORT and POST it to the ThroughLog endpoint, so Cursor's
agent work shows up — trust-classified and attributed to the right project — in
your journal and dashboard.

Install it with:

    python -m throughlog.cli hook enable cursor

...or wire it up by hand in your Cursor hooks config (`~/.cursor/hooks.json` or a
project `.cursor/hooks.json`):

    {
      "version": 1,
      "hooks": {
        "afterFileEdit": [
          { "command": "python /ABS/PATH/throughlog/integrations/cursor/tl_hook.py" }
        ],
        "stop": [
          { "command": "python /ABS/PATH/throughlog/integrations/cursor/tl_hook.py" }
        ]
      }
    }

Endpoint + auth come from the environment (see `integrations/_common.py`):
    TL_ENDPOINT   default http://127.0.0.1:8787/report
    TL_TOKEN      optional bearer token (relay)
    TL_DROP_DIR   optional fallback folder if the endpoint is down

This hook is defensive by construction: it NEVER blocks Cursor. Any error is
swallowed and it always exits 0, so a journal outage can't interrupt your work.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _common


def _summarize(data: dict) -> tuple[str, dict]:
    """Build a (summary, fields) pair from a Cursor hook payload. ``fields`` are
    kwargs for AgentReporter.report (repo/files/project/session/status)."""
    event = data.get("hook_event_name", "")
    roots = data.get("workspace_roots") or []
    root = roots[0] if roots else ""
    session = data.get("conversation_id", "")
    fields: dict = {"session_id": session}
    if root:
        fields["repo"] = root                     # workspace root -> project attribution
        fields["project"] = Path(root).name

    if event == "afterFileEdit":
        path = data.get("file_path") or ""
        if path:
            fields["files"] = [path]
            summary = f"Cursor edited: {Path(path).name}"
        else:
            summary = "Cursor edited a file"
        return summary, fields

    if event == "stop":
        status = data.get("status", "")
        fields["status"] = status
        where = Path(root).name if root else "a session"
        suffix = f" ({status})" if status else ""
        return f"Cursor finished a session in {where}{suffix}", fields

    # Generic fallback for any other hook event.
    return f"Cursor event: {event or 'unknown'}", fields


def main() -> int:
    try:
        data = _common.read_stdin_json()
        summary, fields = _summarize(data)
        _common.send("agent:cursor", "cursor", summary, **fields)
    except Exception:
        pass  # never block Cursor — a journal hiccup must not fail the tool
    return 0


if __name__ == "__main__":
    sys.exit(main())
