"""Shared plumbing for ThroughLog's drop-in agent hooks (Claude Code, Cursor, ...).

Every hook script is a thin adapter: read the host tool's own JSON payload off
stdin, translate it into a ``(summary, fields)`` pair, then call ``send(...)``
here to build and ship an ``AGENT_REPORT``. This module holds everything that
ISN'T tool-specific: the repo-root import bootstrap, stdin parsing, the
zero-config drop-folder default, and the never-block-the-host-tool guarantee.

Endpoint + auth come from the environment (same contract for every hook):
    TL_ENDPOINT   default http://127.0.0.1:8787/report
    TL_TOKEN      optional bearer token (relay)
    TL_DROP_DIR   optional fallback folder if the endpoint is down
                  (default: the data dir's agent_drop/ — the same folder
                  `tl capture`'s drop-folder watcher already reads, so a
                  report is never lost even if capture isn't running yet)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Make `throughlog` importable whether or not the package is installed: the repo
# root is one directory up from this file (integrations/_common.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def read_stdin_json() -> dict[str, Any]:
    """Best-effort JSON parse of stdin. Never raises — `{}` on any error."""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def default_drop_dir() -> str:
    """The drop folder `tl capture` already watches, so a report posted while
    capture is offline still lands somewhere it will be picked up later."""
    from throughlog import config as cfgmod
    return str(cfgmod.data_dir() / "agent_drop")


def send(identity: str, tool: str, summary: str, **fields: Any) -> None:
    """Build and send an agent report. Wrapped so a journal hiccup can NEVER
    block or crash the host tool (Claude Code, Cursor, ...) — any error here
    is swallowed and the caller should always exit 0 regardless."""
    try:
        from throughlog.agent_sdk import AgentReporter, DEFAULT_ENDPOINT

        reporter = AgentReporter(
            identity=identity,
            tool=tool,
            endpoint=os.environ.get("TL_ENDPOINT", DEFAULT_ENDPOINT),
            token=os.environ.get("TL_TOKEN") or None,
            drop_dir=os.environ.get("TL_DROP_DIR") or default_drop_dir(),
        )
        reporter.report(summary, **fields)
    except Exception:
        pass
