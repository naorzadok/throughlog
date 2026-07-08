"""Cloud relay — the agent endpoint, promoted to a self-hostable multi-account
service so cloud agents report in and devices sync, without weakening the moat.

This is *the same intake* as ``sources/agent_ingest`` (validate + trust-classify +
the privacy gate), scoped to an account by a per-user API token. It is the optional
hosted / self-hostable layer of the open-core model — run it yourself with
``tl relay`` and "your data never leaves infrastructure you control."

Endpoints (all but ``/healthz`` require ``Authorization: Bearer <token>``):

  POST /report   a cloud agent submits a raw report -> ingest_report -> gate ->
                 the account's store. 202 accepted / 422 rejected.
  POST /sync     a device pushes already-gated events {"events": [...]} ->
                 appended to the account's store. Each event MUST carry a privacy
                 stamp (proof it passed the device gate); ungated events are
                 refused, and every event is re-scrubbed on receipt.
  GET  /events   read the account's events (optional ?since=ISO) for cross-device
                 read / rollups.
  GET  /healthz  liveness, no auth.

The privacy stance is enforced structurally:
  * ``/report`` runs the full gate (redaction + content-strip + audit stamp).
  * ``/sync`` only accepts gate-passed events and re-scrubs them (defense in depth)
    — raw content can never enter the relay store.
  * accounts isolate streams: each token maps to one account directory; the account
    id is sanitized so a token can never escape its store path.

Pure pieces (``AccountRegistry``, ``AccountStore``, ``Relay``) are unit-tested; the
HTTP handler is thin and exercised in-process. No LLM. Standard library only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from throughlog.schema import NormalizedEvent
from throughlog.privacy.allowlist import Allowlist
from throughlog.privacy.gate import gate as default_gate, Dropped
from throughlog.sources.agent_ingest import ingest_report, AgentIngestConfig
from throughlog.bus import _date_key
from throughlog.sync import rescrub_event_dict


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
@dataclass
class AccountRegistry:
    """Maps API tokens to account ids. Lightweight by design — OAuth/SSO is the
    Team extension; this is the single-user / self-hosted default."""
    tokens: dict[str, str]

    def account_for(self, token: str | None) -> str | None:
        if not token:
            return None
        return self.tokens.get(token)

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "AccountRegistry":
        rc = (cfg or {}).get("relay", {}) or {}
        return cls(dict(rc.get("tokens", {}) or {}))


_SAFE_ACCOUNT = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_account(account: str) -> str:
    """Sanitize an account id into a single safe path segment (no traversal)."""
    cleaned = _SAFE_ACCOUNT.sub("_", account or "")
    cleaned = cleaned.strip("._") or "account"
    return cleaned[:64]


# --------------------------------------------------------------------------- #
# Per-account store (append-only JSONL, partitioned by day)
# --------------------------------------------------------------------------- #
class AccountStore:
    def __init__(self, root: str | Path, account: str) -> None:
        self.account = account
        self.dir = Path(root) / _safe_account(account) / "events"
        self.dir.mkdir(parents=True, exist_ok=True)

    def append(self, ev: NormalizedEvent) -> None:
        self._write(_date_key(ev.ts_wall), ev.to_json())

    def append_dict(self, d: dict[str, Any]) -> None:
        self._write(_date_key(d.get("ts_wall", "")), json.dumps(d, ensure_ascii=False))

    def _write(self, date_key: str, line: str) -> None:
        with open(self.dir / f"{date_key}.jsonl", "a", encoding="utf-8") as h:
            h.write(line + "\n")

    def read_since(self, since: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for f in sorted(self.dir.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                if since and str(d.get("ts_wall", "")) < since:
                    continue
                out.append(d)
        return out


# --------------------------------------------------------------------------- #
# Relay logic (transport-agnostic; returns (status_code, json_body))
# --------------------------------------------------------------------------- #
class Relay:
    def __init__(self, store_root: str | Path, registry: AccountRegistry, *,
                 ingest_cfg: AgentIngestConfig | None = None) -> None:
        self.store_root = Path(store_root)
        self.registry = registry
        self.ingest_cfg = ingest_cfg
        # AGENT_REPORTs are not path-gated, so an empty allowlist is correct here:
        # the gate still redacts, strips content, and stamps the audit trail.
        self._allow = Allowlist([])

    def handle_report(self, account: str, raw: dict) -> tuple[int, dict]:
        ev = ingest_report(raw, cfg=self.ingest_cfg)
        gated = default_gate(ev, self._allow)
        if isinstance(gated, Dropped):
            return 422, {"status": "dropped", "reason": gated.reason}
        AccountStore(self.store_root, account).append(gated)
        code = 202 if gated.trust != "rejected" else 422
        return code, {"status": "accepted", "trust": gated.trust,
                      "event_id": gated.event_id, "account": account}

    def handle_sync(self, account: str, events: list[dict]) -> tuple[int, dict]:
        store = AccountStore(self.store_root, account)
        accepted, blocked = 0, 0
        for d in events if isinstance(events, list) else []:
            if not isinstance(d, dict) or not d.get("privacy"):
                blocked += 1                  # never accept an ungated event
                continue
            store.append_dict(rescrub_event_dict(d))   # re-scrub on receipt
            accepted += 1
        return 200, {"accepted": accepted, "blocked": blocked, "account": account}

    def handle_events(self, account: str, since: str | None) -> tuple[int, dict]:
        return 200, {"events": AccountStore(self.store_root, account).read_since(since)}


# --------------------------------------------------------------------------- #
# HTTP server (thin)
# --------------------------------------------------------------------------- #
def build_relay_handler(relay: Relay):
    from http.server import BaseHTTPRequestHandler
    from urllib.parse import urlparse, parse_qs

    class _Handler(BaseHTTPRequestHandler):
        def _token(self) -> str | None:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
            return None

        def _account(self) -> str | None:
            return relay.registry.account_for(self._token())

        def _drain(self) -> None:
            """Read and discard any unread request body exactly once.

            Early responses (e.g. the 401 auth check) return *before* ``_body``
            consumes the request payload. ``BaseHTTPRequestHandler`` defaults to
            HTTP/1.0, so the connection is closed after each response; closing a
            socket that still has unread bytes in its receive buffer makes Windows
            answer with an RST instead of a FIN, and the client then sees
            ``ConnectionAbortedError (WinError 10053)`` from ``getresponse()``
            instead of cleanly reading the status. Draining first keeps the close
            clean. Idempotent: ``_body`` marks the payload consumed so this no-ops
            on request paths that already read it (re-reading would block waiting
            for bytes that never arrive)."""
            if getattr(self, "_drained", False):
                return
            self._drained = True
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length:
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass

        def _send(self, code: int, obj: dict) -> None:
            self._drain()   # never close a socket with an unread body (see _drain)
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Any:
            self._drained = True   # we consume the whole payload below
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return None

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                return self._send(200, {"ok": True})
            account = self._account()
            if not account:
                return self._send(401, {"error": "unauthorized"})
            if parsed.path == "/events":
                since = parse_qs(parsed.query).get("since", [None])[0]
                code, obj = relay.handle_events(account, since)
                return self._send(code, obj)
            return self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            account = self._account()
            if not account:
                return self._send(401, {"error": "unauthorized"})
            body = self._body()
            if body is None:
                return self._send(400, {"error": "invalid json"})
            if self.path == "/report":
                code, obj = relay.handle_report(account, body)
                return self._send(code, obj)
            if self.path == "/sync":
                events = body.get("events", []) if isinstance(body, dict) else []
                code, obj = relay.handle_sync(account, events)
                return self._send(code, obj)
            return self._send(404, {"error": "not found"})

        def log_message(self, *args):  # silence default stderr logging
            pass

    return _Handler


def make_relay(host: str, port: int, *, store_root: str | Path,
               registry: AccountRegistry, ingest_cfg: AgentIngestConfig | None = None):
    """Build (but do not start) the relay HTTPServer. ``port=0`` => ephemeral."""
    from http.server import HTTPServer

    relay = Relay(store_root, registry, ingest_cfg=ingest_cfg)
    return HTTPServer((host, port), build_relay_handler(relay))


def serve(host: str = "127.0.0.1", port: int = 8788, *,
          store_root: str | Path | None = None,
          registry: AccountRegistry | None = None) -> None:
    """Run the relay until Ctrl+C. Tokens come from the registry (config.relay.tokens)."""
    from throughlog import config as cfgmod

    cfg = cfgmod.load_config() if cfgmod.CONFIG_PATH.exists() else {}
    registry = registry or AccountRegistry.from_config(cfg)
    store_root = Path(store_root) if store_root else (cfgmod.data_dir(cfg) / "relay")
    if not registry.tokens:
        print("[tl] WARNING: no relay tokens configured (relay.tokens in config.json) "
              "— every request will be unauthorized.")
    httpd = make_relay(host, port, store_root=store_root, registry=registry)
    print(f"[tl] relay on http://{host}:{port}  store={store_root}  "
          f"accounts={len(set(registry.tokens.values()))}")
    print("[tl] Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        print("[tl] relay stopped.")
