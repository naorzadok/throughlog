"""Sync client — push gated events to the relay, pull them back, privacy-first.

The local store stays the source of truth; the relay holds a synced copy for
cross-device read and team rollups. The guarantee that makes cloud sync safe is
enforced *here*, structurally:

  * **Gated-only egress.** Only events that already carry a ``privacy`` stamp
    (proof they passed ``privacy/gate.py``) are eligible to leave the machine.
    An ungated event is never sent.
  * **Re-scrub before send.** Every outbound event's payload is run through the
    independent egress scrubber again, so a gate bug still cannot leak — the same
    defense-in-depth the LLM client applies before any model call.
  * **Opt-in + scoped.** Sync is off until configured; a bearer token scopes every
    push/pull to one account.

Pure pieces (``prepare_for_egress``, ``rescrub_event_dict``) are unit-tested; the
transports are thin ``urllib`` POST/GET with an injectable ``opener``. No LLM.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

from throughlog.privacy import egress


# --------------------------------------------------------------------------- #
# Egress guard (the privacy boundary for sync)
# --------------------------------------------------------------------------- #
def _scrub_obj(obj: Any) -> Any:
    """Recursively scrub every string value through the egress scrubber."""
    if isinstance(obj, str):
        clean, _ = egress.egress_check(obj)
        return clean
    if isinstance(obj, dict):
        return {k: _scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_obj(v) for v in obj]
    return obj


def rescrub_event_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an event dict with its payload egress-re-scrubbed."""
    return {**d, "payload": _scrub_obj(d.get("payload", {}))}


def prepare_for_egress(events: list[Any]) -> tuple[list[dict], list[str]]:
    """Split events into (sendable, blocked_ids). An event is sendable only if it
    carries a ``privacy`` stamp (gate-passed); each sendable event is re-scrubbed.
    ``events`` may be NormalizedEvents or plain dicts."""
    sendable: list[dict] = []
    blocked: list[str] = []
    for ev in events:
        d = ev.to_dict() if hasattr(ev, "to_dict") else dict(ev)
        if not d.get("privacy"):
            blocked.append(str(d.get("event_id", "?")))
            continue
        sendable.append(rescrub_event_dict(d))
    return sendable, blocked


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
@dataclass
class SyncResult:
    ok: bool
    sent: int = 0
    blocked: int = 0
    status: int | None = None
    body: dict | None = None
    error: str = ""


def _request(url: str, *, method: str, token: str, body: bytes | None,
             opener: Callable[..., Any] | None, timeout: float = 15.0
             ) -> tuple[int | None, Any, str]:
    opener = opener or urllib.request.urlopen
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with opener(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read().decode("utf-8", "replace")
        try:
            return int(status), json.loads(raw) if raw else {}, ""
        except json.JSONDecodeError:
            return int(status), {}, ""
    except urllib.error.HTTPError as exc:
        return exc.code, None, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def push(endpoint: str, token: str, events: list[Any], *,
         opener: Callable[..., Any] | None = None) -> SyncResult:
    """Push gated events to ``{endpoint}/sync``. Ungated events are dropped before
    they ever reach the wire (see ``prepare_for_egress``)."""
    sendable, blocked = prepare_for_egress(events)
    body = json.dumps({"events": sendable}).encode("utf-8")
    status, payload, err = _request(f"{endpoint.rstrip('/')}/sync", method="POST",
                                    token=token, body=body, opener=opener)
    ok = status is not None and 200 <= status < 300
    return SyncResult(ok=ok, sent=len(sendable), blocked=len(blocked),
                      status=status, body=payload, error=err)


def pull(endpoint: str, token: str, *, since: str | None = None,
         opener: Callable[..., Any] | None = None) -> SyncResult:
    """Pull the account's events from ``{endpoint}/events`` (optional ``since``)."""
    url = f"{endpoint.rstrip('/')}/events"
    if since:
        url += f"?since={quote(since)}"
    status, payload, err = _request(url, method="GET", token=token, body=None,
                                    opener=opener)
    ok = status is not None and 200 <= status < 300
    events = (payload or {}).get("events", []) if isinstance(payload, dict) else []
    return SyncResult(ok=ok, sent=0, status=status,
                      body={"events": events} if ok else payload, error=err)
