"""Normalized event schema (v2) — one shape for every source.

This is the single contract that all source adapters emit and the whole
pipeline consumes. It is deterministic, dependency-free (stdlib only), and
round-trips losslessly to/from JSON. Validation is rule-based and is reused by
the agent-ingestion adapter (M5) to reject malformed/spoofed reports.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 2

# --- Controlled vocabularies -------------------------------------------------

# Normalized event types. Uppercase by convention.
FOCUS_SESSION = "FOCUS_SESSION"   # a window-focus work session (os_focus adapter)
FILE_CHANGE = "FILE_CHANGE"       # a meaningful save under an allowed dir
GIT_COMMIT = "GIT_COMMIT"         # a commit (carries author for human/agent split)
NARRATION = "NARRATION"           # tolerant human narration (whisper floor)
CLIPBOARD = "CLIPBOARD"           # typed clipboard summary (never raw content)
IDLE_START = "IDLE_START"
IDLE_END = "IDLE_END"
DEEP_WORK = "DEEP_WORK"           # opaque-app deep work (mouse+saves, ~0 keys)
LONG_RUN = "LONG_RUN"             # long-running compute, no human present
AGENT_REPORT = "AGENT_REPORT"     # ingested agent/remote report

EVENT_TYPES: frozenset[str] = frozenset({
    FOCUS_SESSION, FILE_CHANGE, GIT_COMMIT, NARRATION, CLIPBOARD,
    IDLE_START, IDLE_END, DEEP_WORK, LONG_RUN, AGENT_REPORT,
})

SOURCE_KINDS: frozenset[str] = frozenset({"os", "fs", "git", "intent", "agent", "remote"})
TRUST_LEVELS: frozenset[str] = frozenset({"validated", "low_trust", "rejected"})


def now_iso() -> str:
    """Timezone-aware ISO-8601 wall-clock timestamp (local tz)."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _coerce_offset(v: Any) -> float:
    """clock_offset_sec arrives from untrusted JSON; coerce to float (junk -> 0.0)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# --- Nested records ----------------------------------------------------------

@dataclass
class Source:
    kind: str                      # one of SOURCE_KINDS
    adapter: str                   # e.g. "os_focus", "fs_git", "agent_ingest"
    identity: str = ""             # e.g. "host:DEV-PC" or "agent:claude-1"
    session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "adapter": self.adapter,
                "identity": self.identity, "session_id": self.session_id}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Source":
        return cls(kind=d.get("kind", ""), adapter=d.get("adapter", ""),
                   identity=d.get("identity", ""), session_id=d.get("session_id", ""))


@dataclass
class Attribution:
    project_id: str | None = None
    confidence: float = 0.0
    method: str | None = None      # which signal/rung resolved it, or "llm"/"needs_review"

    def to_dict(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "confidence": self.confidence,
                "method": self.method}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Attribution":
        return cls(project_id=d.get("project_id"), confidence=d.get("confidence", 0.0),
                   method=d.get("method"))


@dataclass
class Privacy:
    """Audit trail stamped by the gate. Absent => event has NOT passed the gate."""
    gate_version: str = ""
    redactions: list[str] = field(default_factory=list)
    passed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"gate_version": self.gate_version,
                "redactions": list(self.redactions), "passed_at": self.passed_at}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Privacy":
        return cls(gate_version=d.get("gate_version", ""),
                   redactions=list(d.get("redactions", [])),
                   passed_at=d.get("passed_at", ""))


# --- The event ---------------------------------------------------------------

@dataclass
class NormalizedEvent:
    type: str
    source: Source
    ts_wall: str                                   # source wall clock (tz-aware ISO)
    payload: dict[str, Any] = field(default_factory=dict)
    recv_ts: str = ""                              # aggregator receipt time
    clock_offset_sec: float = 0.0                  # filled by timeline reconciler
    attribution: Attribution = field(default_factory=Attribution)
    privacy: Privacy | None = None                 # stamped by the gate
    trust: str = "validated"
    schema_version: int = SCHEMA_VERSION
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "schema_version": self.schema_version,
            "source": self.source.to_dict(),
            "ts_wall": self.ts_wall,
            "recv_ts": self.recv_ts,
            "clock_offset_sec": self.clock_offset_sec,
            "type": self.type,
            # Payload keys beginning with "_" are TRANSIENTS that must never be
            # serialized to disk or egress (e.g. the gate's `_diff_clean`, which the
            # bus writes to a sidecar instead). Dropping them here is a structural
            # barrier: no writer or crash window can leak a transient, regardless of
            # whether the producer remembered to pop it.
            "payload": {k: v for k, v in self.payload.items() if not k.startswith("_")},
            "attribution": self.attribution.to_dict(),
            "privacy": self.privacy.to_dict() if self.privacy is not None else None,
            "trust": self.trust,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NormalizedEvent":
        priv = d.get("privacy")
        return cls(
            type=d.get("type", ""),
            source=Source.from_dict(d.get("source", {})),
            ts_wall=d.get("ts_wall", ""),
            payload=dict(d.get("payload", {})),
            recv_ts=d.get("recv_ts", ""),
            clock_offset_sec=_coerce_offset(d.get("clock_offset_sec")),
            attribution=Attribution.from_dict(d.get("attribution", {})),
            privacy=Privacy.from_dict(priv) if priv else None,
            trust=d.get("trust", "validated"),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            event_id=d.get("event_id", uuid.uuid4().hex),
        )

    def to_json(self) -> str:
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "NormalizedEvent":
        import json
        return cls.from_dict(json.loads(line))


def make_event(type: str, *, kind: str, adapter: str, payload: dict[str, Any] | None = None,
               ts_wall: str | None = None, identity: str = "", session_id: str = "",
               trust: str = "validated") -> NormalizedEvent:
    """Convenience constructor for source adapters."""
    ts = ts_wall or now_iso()
    return NormalizedEvent(
        type=type,
        source=Source(kind=kind, adapter=adapter, identity=identity, session_id=session_id),
        ts_wall=ts,
        recv_ts=now_iso(),
        payload=dict(payload or {}),
        trust=trust,
    )


def validate(d: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation errors (empty => valid).

    Deterministic, used by agent ingestion to reject malformed/spoofed reports
    and by tests. Does NOT mutate.
    """
    errors: list[str] = []

    def req(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    req(isinstance(d, dict), "event must be an object")
    if not isinstance(d, dict):
        return errors

    req(d.get("schema_version") == SCHEMA_VERSION,
        f"schema_version must be {SCHEMA_VERSION}")
    req(d.get("type") in EVENT_TYPES, f"type must be one of {sorted(EVENT_TYPES)}")

    src = d.get("source")
    req(isinstance(src, dict), "source must be an object")
    if isinstance(src, dict):
        req(src.get("kind") in SOURCE_KINDS, f"source.kind must be one of {sorted(SOURCE_KINDS)}")
        req(bool(src.get("adapter")), "source.adapter is required")

    ts = d.get("ts_wall")
    req(isinstance(ts, str) and bool(ts), "ts_wall is required (ISO-8601 string)")
    if isinstance(ts, str) and ts:
        try:
            datetime.fromisoformat(ts)
        except ValueError:
            errors.append("ts_wall is not a valid ISO-8601 timestamp")

    req(isinstance(d.get("payload", {}), dict), "payload must be an object")
    req(d.get("trust", "validated") in TRUST_LEVELS,
        f"trust must be one of {sorted(TRUST_LEVELS)}")

    return errors
