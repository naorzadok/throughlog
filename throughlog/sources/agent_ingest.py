"""Agent ingestion — a remote emitter speaking the same v2 schema.

Autonomous agents (cloud or local) are first-class sources. They submit reports
that flow through the *same* gate and bus as everything else, so privacy and the
thin-log discipline apply uniformly. This adapter is the deterministic intake:

  * schema-validate every report; malformed/garbage is REJECTED (recorded as an
    audit stub with trust=rejected, content never trusted) — case A5;
  * anomaly checks (unknown identity, far-future timestamp, wrong source kind)
    demote a well-formed but suspicious report to LOW_TRUST — case A5;
  * per-agent ``source.identity`` + ``session_id`` are preserved so concurrent
    agents stay distinct streams — case A3;
  * ``recv_ts`` is stamped at intake so the timeline can place a late report by
    its real ``ts_wall``, not its arrival — cases A2/A4.

Nothing here calls an LLM. The live driver (drop folder + a stdlib HTTP endpoint)
lazily uses only the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from throughlog.schema import (
    NormalizedEvent, Source, validate, now_iso,
    AGENT_REPORT, SCHEMA_VERSION,
)


@dataclass
class AgentIngestConfig:
    trusted_identities: tuple[str, ...] = ()      # empty => identity is not checked
    future_tolerance_sec: float = 120.0           # ts_wall beyond now+this => suspicious


def _anomalies(ev: NormalizedEvent, now: str, cfg: AgentIngestConfig) -> list[str]:
    reasons: list[str] = []
    if ev.source.kind != "agent":
        reasons.append("source_kind_not_agent")
    if cfg.trusted_identities and ev.source.identity not in cfg.trusted_identities:
        reasons.append("unknown_identity")
    try:
        ts = datetime.fromisoformat(ev.ts_wall)
        ref = datetime.fromisoformat(now)
        # The store mixes tz-naive ts_wall (demo/replay corpora) with tz-aware
        # now_iso(); align them so a valid naive timestamp isn't mislabeled
        # unparseable and the far-future check still runs.
        if ts.tzinfo is None and ref.tzinfo is not None:
            ts = ts.replace(tzinfo=ref.tzinfo)
        elif ref.tzinfo is None and ts.tzinfo is not None:
            ref = ref.replace(tzinfo=ts.tzinfo)
        if ts > ref + timedelta(seconds=cfg.future_tolerance_sec):
            reasons.append("future_timestamp")
    except (ValueError, TypeError):
        reasons.append("unparseable_ts_wall")
    return reasons


def _rejected_stub(raw: dict, errors: list[str], now: str) -> NormalizedEvent:
    """An audit record that a report was rejected — without trusting its content."""
    ident = ""
    src = raw.get("source")
    if isinstance(src, dict):
        ident = str(src.get("identity", ""))[:80]
    ev = NormalizedEvent(
        type=AGENT_REPORT,
        source=Source(kind="agent", adapter="agent_ingest", identity=ident),
        ts_wall=now,
        payload={"rejected": True, "reasons": errors,
                 "claimed_type": str(raw.get("type", ""))[:40]},
        recv_ts=now,
        trust="rejected",
    )
    return ev


def ingest_report(raw: dict, *, now: str | None = None,
                  cfg: AgentIngestConfig | None = None) -> NormalizedEvent:
    """Validate + trust-classify one agent report. Always returns an event
    (never drops): rejected reports become audit stubs."""
    now = now or now_iso()
    cfg = cfg or AgentIngestConfig()

    rec = dict(raw)
    rec.setdefault("schema_version", SCHEMA_VERSION)
    rec.pop("privacy", None)          # the gate stamps this; an agent cannot self-certify
    rec["trust"] = "validated"        # provisional; we decide trust here, not the sender

    errors = validate(rec)
    if errors:
        return _rejected_stub(raw, errors, now)

    ev = NormalizedEvent.from_dict(rec)
    ev.recv_ts = now
    reasons = _anomalies(ev, now, cfg)
    if reasons:
        ev.trust = "low_trust"
        ev.payload = {**ev.payload, "trust_reasons": reasons}
    else:
        ev.trust = "validated"
    return ev


# --------------------------------------------------------------------------- #
# Live driver — drop folder + stdlib HTTP endpoint (no third-party deps).
# --------------------------------------------------------------------------- #
def ingest_drop_folder(emitter, folder, *, cfg: AgentIngestConfig | None = None,
                       archive=None) -> int:
    """Ingest every *.json report file in ``folder`` through the gate/bus. Each
    file may hold one report object or a list of them. Returns the count emitted."""
    import json
    from pathlib import Path

    folder = Path(folder)
    count = 0
    for f in sorted(folder.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            emitter.emit(_rejected_stub({"source": {"identity": f.name}},
                                        ["unreadable_report_file"], now_iso()))
            count += 1
            continue
        reports = data if isinstance(data, list) else [data]
        for raw in reports:
            emitter.emit(ingest_report(raw, cfg=cfg))
            count += 1
        if archive is not None:
            from pathlib import Path as _P
            f.replace(_P(archive) / f.name)
    return count


def watch_drop_folder_live(emitter, folder, *, stop=None, interval_sec: float = 5.0,
                           cfg: AgentIngestConfig | None = None, archive=None) -> None:
    """Poll a drop folder for agent reports until ``stop`` (a threading.Event)
    is set or KeyboardInterrupt. Each pass ingests and (if ``archive`` is given)
    moves processed report files. A single bad pass never kills the loop."""
    import threading
    from pathlib import Path as _P

    stop = stop or threading.Event()
    folder = _P(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if archive is not None:
        _P(archive).mkdir(parents=True, exist_ok=True)
    try:
        while not stop.is_set():
            try:
                ingest_drop_folder(emitter, folder, cfg=cfg, archive=archive)
            except Exception:
                pass
            stop.wait(interval_sec)
    except KeyboardInterrupt:
        return


def serve_http(emitter, *, host: str = "127.0.0.1", port: int = 8787,
               cfg: AgentIngestConfig | None = None):
    """A tiny stdlib HTTP endpoint: agents POST a JSON report to /report."""
    import json
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                raw = json.loads(body)
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            ev = ingest_report(raw, cfg=cfg)
            emitter.emit(ev)
            self.send_response(202 if ev.trust != "rejected" else 422)
            self.end_headers()

        def log_message(self, *args):  # silence default stderr logging
            pass

    return HTTPServer((host, port), _Handler)
