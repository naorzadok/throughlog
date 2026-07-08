"""Event bus — the ONLY path to persistence.

Every source adapter calls `bus.emit(event)`. The bus runs the privacy gate
(and, later, the timeline reconciler / thin-log filters) before writing a thin
JSONL line to data/events/YYYYMMDD.jsonl. Nothing reaches disk without passing
the gate.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, TextIO

from throughlog.schema import NormalizedEvent
from throughlog.privacy.allowlist import Allowlist
from throughlog.privacy.gate import gate as default_gate, Dropped
from throughlog.privacy.diff_policy import DiffPolicy, DEFAULT_POLICY

GateFn = Callable[[NormalizedEvent, Allowlist, DiffPolicy], "NormalizedEvent | Dropped"]


def _date_key(ts_wall: str) -> str:
    try:
        return datetime.fromisoformat(ts_wall).strftime("%Y%m%d")
    except (ValueError, TypeError):
        # Fallback: first 10 chars "YYYY-MM-DD" -> "YYYYMMDD".
        head = (ts_wall or "")[:10].replace("-", "")
        return head or "00000000"


class EventBus:
    def __init__(self, out_dir: str | Path, allowlist: Allowlist,
                 gate_fn: GateFn = default_gate, *,
                 diff_policy: DiffPolicy = DEFAULT_POLICY,
                 diffs_dir: str | Path | None = None):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.allowlist = allowlist
        self.gate_fn = gate_fn
        self.diff_policy = diff_policy
        # Sidecar location is explicit (never derived inside callers' temp dirs) so
        # the simulator can sandbox it; defaults to a `diffs/` sibling of events/.
        self._diffs_dir = Path(diffs_dir) if diffs_dir is not None else self.out_dir.parent / "diffs"

        self.written = 0
        self.dropped: Counter[str] = Counter()
        self.redactions: Counter[str] = Counter()
        self._handles: dict[str, TextIO] = {}

    def emit(self, event: NormalizedEvent) -> bool:
        """Gate, then persist. Returns True if persisted, False if dropped."""
        result = self.gate_fn(event, self.allowlist, self.diff_policy)
        if isinstance(result, Dropped):
            self.dropped[result.reason] += 1
            return False

        self._persist_diff(result)        # sidecar + diff_ref (no-op without a diff)

        if result.privacy is not None:
            for r in result.privacy.redactions:
                self.redactions[r] += 1

        self._write(_date_key(result.ts_wall), result.to_json())
        self.written += 1
        return True

    def _persist_diff(self, event: NormalizedEvent) -> None:
        """Move the gate's transient ``_diff_clean`` into a content-addressed sidecar
        ``<diffs_dir>/<sha256>.patch`` and replace it with a ``diff_ref``. Keyed by
        content hash (not event_id) so an attacker-supplied/reused id can't mis-point
        a ref. Any write failure degrades to "no diff" — the event is never dropped,
        and the transient is popped regardless (to_dict also strips ``_`` keys)."""
        clean = event.payload.pop("_diff_clean", None)
        if not clean:
            return
        try:
            sha = hashlib.sha256(clean.encode("utf-8", "replace")).hexdigest()
            self._diffs_dir.mkdir(parents=True, exist_ok=True)
            path = self._diffs_dir / f"{sha}.patch"
            if not path.exists():
                path.write_text(clean, encoding="utf-8")
            event.payload["diff_ref"] = sha
        except OSError:
            event.payload.pop("diff_ref", None)

    def _write(self, date_key: str, line: str) -> None:
        h = self._handles.get(date_key)
        if h is None:
            h = open(self.out_dir / f"{date_key}.jsonl", "a", encoding="utf-8")
            self._handles[date_key] = h
        h.write(line + "\n")

    def close(self) -> None:
        for h in self._handles.values():
            try:
                h.close()
            except OSError:
                pass
        self._handles.clear()

    def __enter__(self) -> "EventBus":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def stats(self) -> dict[str, object]:
        return {
            "written": self.written,
            "dropped_total": sum(self.dropped.values()),
            "dropped": dict(self.dropped),
            "redactions": dict(self.redactions),
        }
