"""ThroughLog agent SDK — let any agent (local or cloud) write itself into the journal.

A tiny, dependency-free client that builds a schema-v2 ``AGENT_REPORT`` and sends
it to the tl report endpoint — the local capture HTTP endpoint
(``http://127.0.0.1:8787/report``, served by ``sources/agent_ingest.serve_http``)
or a remote relay (M16). One POST and the agent's work shows up, trust-classified
and attributed, in the diary and dashboard. This is the documented contract behind
every drop-in hook (Claude Code, Cursor, CI, n8n).

    from throughlog.agent_sdk import AgentReporter

    AgentReporter(identity="agent:claude-code").report(
        summary="refactored the parser; all tests green",
        project="throughlog",
        repo="github.com/naorzadok/throughlog",
        files=["throughlog/categorize.py"], tool="claude-code")

Design (mirrors ``throughlog.llm.client``):
  * ``build_report(...)`` is a PURE builder — produces the exact dict the
    ``/report`` endpoint validates (``throughlog.schema.validate``). Testable offline.
  * ``AgentReporter.send(report)`` is the transport: an ``urllib`` POST with an
    optional bearer token (for the relay), ``opener`` injectable so tests run with
    no network. If the endpoint is unreachable and a ``drop_dir`` is configured,
    the report is written there as a ``*.json`` file — which the capture
    supervisor's drop-folder watcher ingests — so a report is never silently lost.
  * Remote-capable from day one: the endpoint may be localhost or a cloud relay;
    the report contract is identical either way.

No LLM. Standard library only (``urllib``, ``json``).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from throughlog.schema import AGENT_REPORT, SCHEMA_VERSION, now_iso

DEFAULT_ENDPOINT = "http://127.0.0.1:8787/report"


# --------------------------------------------------------------------------- #
# Pure report builder
# --------------------------------------------------------------------------- #
def build_report(*, summary: str, identity: str, tool: str = "",
                 project: str | None = None, repo: str | None = None,
                 files: list[str] | None = None, status: str = "",
                 session_id: str = "", ts_wall: str | None = None,
                 event_type: str = AGENT_REPORT,
                 extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the report dict an agent submits. ``repo``/``files`` drive project
    attribution (path + git-remote signals); ``summary``/``project`` drive keyword
    signals; the prose is what lands in the diary. Always schema-valid."""
    payload: dict[str, Any] = {"summary": str(summary), "tool": tool}
    if files:
        payload["files"] = [str(f) for f in files]
    if repo:
        payload["repo"] = str(repo)
    if project:
        payload["project_hint"] = str(project)
    if status:
        payload["status"] = str(status)
    if extra:
        payload.update(extra)
    return {
        "schema_version": SCHEMA_VERSION,
        "type": event_type,
        "source": {
            "kind": "agent",
            "adapter": tool or "agent_sdk",
            "identity": identity,
            "session_id": session_id,
        },
        "ts_wall": ts_wall or now_iso(),
        "payload": payload,
    }


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
class SendResult:
    """Outcome of a send. ``ok`` is True if the report was accepted (HTTP 2xx) or
    written to the drop folder for later ingestion."""

    def __init__(self, *, ok: bool, transport: str, status: int | None = None,
                 path: str | None = None, error: str = "") -> None:
        self.ok = ok
        self.transport = transport          # "http" | "drop" | "failed"
        self.status = status
        self.path = path
        self.error = error

    def __repr__(self) -> str:
        bits = [f"ok={self.ok}", f"transport={self.transport!r}"]
        if self.status is not None:
            bits.append(f"status={self.status}")
        if self.error:
            bits.append(f"error={self.error!r}")
        return f"SendResult({', '.join(bits)})"


# --------------------------------------------------------------------------- #
# Reporter
# --------------------------------------------------------------------------- #
class AgentReporter:
    """Build + send agent reports to the ThroughLog endpoint."""

    def __init__(self, *, identity: str, endpoint: str = DEFAULT_ENDPOINT,
                 token: str | None = None, tool: str = "", session_id: str = "",
                 drop_dir: str | Path | None = None, timeout: float = 5.0,
                 opener: Callable[..., Any] | None = None) -> None:
        self.identity = identity
        self.endpoint = endpoint
        self.token = token
        self.tool = tool
        self.session_id = session_id
        self.drop_dir = Path(drop_dir) if drop_dir else None
        self.timeout = float(timeout)
        self._opener = opener or urllib.request.urlopen

    # -- convenience -------------------------------------------------------- #
    def report(self, summary: str, **kw: Any) -> SendResult:
        """Build a report from the given fields and send it."""
        report = build_report(
            summary=summary, identity=self.identity,
            tool=kw.pop("tool", self.tool),
            session_id=kw.pop("session_id", self.session_id), **kw)
        return self.send(report)

    # -- transport ---------------------------------------------------------- #
    def send(self, report: dict[str, Any]) -> SendResult:
        """POST ``report`` to the endpoint. On transport failure, fall back to the
        drop folder if configured; otherwise return a failed result (never raises
        — an agent's main job must not crash because the journal is down)."""
        body = json.dumps(report).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.endpoint, data=body, method="POST",
                                     headers=headers)
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
            return SendResult(ok=200 <= int(status) < 300, transport="http",
                              status=int(status))
        except urllib.error.HTTPError as exc:
            # The server answered, but not 2xx (e.g. 422 rejected). No retry/drop —
            # a rejection is a real answer, surfaced to the caller.
            return SendResult(ok=False, transport="http", status=exc.code,
                              error=f"HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return self._fallback(report, error=f"{type(exc).__name__}: {exc}")

    def _fallback(self, report: dict[str, Any], *, error: str) -> SendResult:
        if self.drop_dir is None:
            return SendResult(ok=False, transport="failed", error=error)
        try:
            self.drop_dir.mkdir(parents=True, exist_ok=True)
            name = f"{now_iso().replace(':', '').replace('+', 'p')}_{uuid.uuid4().hex[:8]}.json"
            path = self.drop_dir / name
            path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            return SendResult(ok=True, transport="drop", path=str(path), error=error)
        except OSError as exc:
            return SendResult(ok=False, transport="failed",
                              error=f"{error}; drop failed: {exc}")


# --------------------------------------------------------------------------- #
# CLI — used by the drop-in hooks (and handy for a manual report)
#   python -m throughlog.agent_sdk --summary "did X" --identity agent:ci --repo ... \
#       --endpoint http://127.0.0.1:8787/report --token $SAL_TOKEN
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(
        prog="tl-report", description="Send an agent report to the ThroughLog endpoint.")
    ap.add_argument("--summary", required=True, help="what the agent did (lands in the diary)")
    ap.add_argument("--identity", default="agent:unknown",
                    help="agent identity, e.g. agent:claude-code or agent:ci")
    ap.add_argument("--tool", default="", help="tool name, e.g. claude-code / cursor / ci")
    ap.add_argument("--project", default=None, help="project hint (keyword signal)")
    ap.add_argument("--repo", default=None, help="repo path or git remote (attribution)")
    ap.add_argument("--file", action="append", dest="files", default=None,
                    help="a file the agent touched (repeatable)")
    ap.add_argument("--status", default="", help="optional status, e.g. success/failed")
    ap.add_argument("--session", default="", help="session id to group a run")
    ap.add_argument("--endpoint", default=os.environ.get("SAL_ENDPOINT", DEFAULT_ENDPOINT))
    ap.add_argument("--token", default=os.environ.get("SAL_TOKEN") or None)
    ap.add_argument("--drop-dir", default=os.environ.get("SAL_DROP_DIR") or None,
                    help="fallback drop folder if the endpoint is unreachable")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    reporter = AgentReporter(
        identity=args.identity, endpoint=args.endpoint, token=args.token,
        tool=args.tool, session_id=args.session, drop_dir=args.drop_dir)
    res = reporter.report(
        args.summary, project=args.project, repo=args.repo,
        files=args.files, status=args.status)
    if not args.quiet:
        print(f"[tl-report] {res}", file=sys.stderr)
    return 0 if res.ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
