"""Timeline reconciliation — deterministic ordering of a multi-source thin log.

Events are persisted in *arrival* order (the bus appends as it gets them), but a
journal needs them in *real* order. Reconciliation reads the thin JSONL log and
produces a single ordered, de-duplicated timeline:

  * order by effective wall time = ts_wall corrected by clock_offset_sec, so a
    cloud-agent report that lands hours late (A2) or a skewed remote clock (C4)
    is placed where it actually belongs, not where it arrived;
  * de-duplicate by event_id, keeping the more-trusted / earlier-received copy
    (A2/A4 retries);
  * drop rejected events from the trusted timeline (A5) while leaving them in the
    log for audit.

Pure functions over plain dicts — JSONL is enough; no database required. (The
plan's deferred SQLite index would only become worthwhile for incremental
cross-day reconciliation at scale; the correctness here does not need it.)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_TRUST_RANK = {"validated": 2, "low_trust": 1, "rejected": 0}


def _coerce_float(v: object) -> float:
    """Best-effort float; any non-numeric (str/list/None) becomes 0.0 — a bad
    clock_offset_sec must never crash reconciliation (events are never dropped)."""
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def effective_dt(ev: dict) -> datetime | None:
    """ts_wall shifted by the estimated clock offset, as a tz-aware datetime.

    The thin log mixes tz-naive ts_wall (demo/replay corpora) with tz-aware
    ts_wall (live capture's now_iso()). A tz-naive value is interpreted as UTC
    so the result is ALWAYS tz-aware and the sort key below is deterministic and
    machine-independent — otherwise `datetime.timestamp()` would read a naive
    value in the host's local zone and interleave naive/aware events differently
    on every machine (risk register #2: mixed naive/aware ordering)."""
    ts = ev.get("ts_wall") or ""
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    offset = _coerce_float(ev.get("clock_offset_sec"))
    return dt - timedelta(seconds=offset)


def _sort_key(ev: dict):
    dt = effective_dt(ev)
    # Unparseable timestamps sort last but stay in the timeline (never dropped).
    when = (0, dt.timestamp()) if dt is not None else (1, 0.0)
    return when, ev.get("recv_ts") or "", ev.get("event_id") or ""


def reconcile(events, *, drop_rejected: bool = True, dedup: bool = True) -> list[dict]:
    """Return events as one ordered, de-duplicated timeline."""
    chosen: dict[str, dict] = {}
    passthrough: list[dict] = []

    for ev in events:
        if drop_rejected and ev.get("trust") == "rejected":
            continue
        eid = ev.get("event_id")
        if not dedup or not eid:
            passthrough.append(ev)
            continue
        prev = chosen.get(eid)
        if prev is None or _prefer(ev, prev):
            chosen[eid] = ev

    merged = list(chosen.values()) + passthrough
    merged.sort(key=_sort_key)
    return merged


def _prefer(candidate: dict, incumbent: dict) -> bool:
    """Prefer higher trust; tie-break on earlier receipt (the original, not a retry)."""
    ct = _TRUST_RANK.get(candidate.get("trust", "validated"), 1)
    it = _TRUST_RANK.get(incumbent.get("trust", "validated"), 1)
    if ct != it:
        return ct > it
    cr = candidate.get("recv_ts") or ""
    ir = incumbent.get("recv_ts") or ""
    return bool(cr) and (not ir or cr < ir)


def load_jsonl(path) -> list[dict]:
    """Read a persisted thin-log file into a list of event dicts."""
    import json
    from pathlib import Path

    out: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out
