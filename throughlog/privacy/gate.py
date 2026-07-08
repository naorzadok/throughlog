"""The privacy gate — the mandatory chokepoint before persistence/egress.

gate(event, allowlist, policy=DEFAULT_POLICY) -> NormalizedEvent (stamped) | Dropped

Deterministic. Runs:
  1. allowlist check on path-bearing event types,
  2. clipboard typing (never store raw; drop credential-shaped) + optional preview,
  3a. (opt-in, default OFF) diff/body/diffstat capture — per-file scrub for
      FILE_CHANGE/GIT_COMMIT only; the scrubbed diff is parked on the transient
      ``_diff_clean`` payload key for the bus to sidecar,
  3. content-field stripping (metadata-by-default) for everything not kept above,
  4. recursive redaction of every remaining string value (secrets + home paths),
  5. privacy audit stamp.

With the DEFAULT (capture-off) policy, step 3a is a no-op and behavior is
byte-identical to before this feature existed: any ``diff``/``body`` field is
stripped exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from throughlog.schema import NormalizedEvent, Privacy, now_iso, CLIPBOARD, FILE_CHANGE, GIT_COMMIT
from throughlog.privacy.allowlist import Allowlist
from throughlog.privacy import redactors
from throughlog.privacy import diff_policy as dp
from throughlog.privacy.diff_policy import DiffPolicy, DEFAULT_POLICY

GATE_VERSION = "1"

# Event types whose primary path MUST be inside the allowlist to be persisted.
_PATH_GATED: dict[str, str] = {FILE_CHANGE: "path", GIT_COMMIT: "repo"}

# Only first-party local file/commit events may ever retain a diff/body. An
# AGENT_REPORT is the spoof surface and must NEVER get the retention upgrade
# (V-07) — it falls through to the content strip regardless of the toggle.
_DIFF_AWARE_TYPES = frozenset({FILE_CHANGE, GIT_COMMIT})

# Raw content fields that are stripped entirely (metadata-by-default).
_CONTENT_DROP_KEYS = ("content", "file_contents", "diff", "body", "text_full", "snippet_full")


@dataclass
class Dropped:
    reason: str
    event: NormalizedEvent


def _redact_walk(obj: Any, found: set[str]) -> Any:
    """Recursively scrub every string value in a JSON-like structure."""
    if isinstance(obj, str):
        cleaned, reds = redactors.scrub(obj)
        found.update(reds)
        return cleaned
    if isinstance(obj, dict):
        return {k: _redact_walk(v, found) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_walk(v, found) for v in obj]
    return obj


def _capture_diff(event: NormalizedEvent, policy: DiffPolicy,
                  redactions: set[str]) -> None:
    """Step 3a (opt-in). Per-file scrub the ``diff`` field and park the clean text
    on the transient ``_diff_clean`` key for the bus to write to a sidecar; the raw
    ``diff`` is always removed from the payload here. A committed secret-file hunk
    inside a multi-file diff is dropped by the secrets denylist / ignore globs."""
    raw = event.payload.get("diff")
    if not isinstance(raw, str) or not raw:
        return
    del event.payload["diff"]
    kept: list[str] = []
    total_lines = 0
    truncated = False
    for file_rel, hunk in dp.split_diff_by_file(raw):
        if dp.is_secret_file(file_rel) or dp.path_ignored(file_rel, policy.ignore_globs):
            redactions.add(dp.DIFF_SUPPRESSED_IGNORED)
            continue
        clean, codes = dp.scrub_diff(hunk, policy)
        redactions.update(codes)
        if dp.DIFF_TRUNCATED in codes:
            truncated = True
        if clean is not None:
            kept.append(clean)
            total_lines += clean.count("\n") + 1
    if kept:
        event.payload["_diff_clean"] = "\n".join(kept)   # transient -> bus sidecar
        event.payload["diff_lines"] = total_lines
        event.payload["diff_truncated"] = truncated


def gate(event: NormalizedEvent, allowlist: Allowlist,
         policy: DiffPolicy = DEFAULT_POLICY) -> NormalizedEvent | Dropped:
    redactions: set[str] = set()

    # 1. Allowlist — path-gated event types must resolve to an allowed path.
    path_key = _PATH_GATED.get(event.type)
    if path_key is not None:
        path = event.payload.get(path_key)
        if not path:
            return Dropped("missing_path", event)
        if not allowlist.allows(str(path)):
            return Dropped("not_in_allowlist", event)

    # 2. Clipboard — replace raw content with a typed summary; drop credentials.
    if event.type == CLIPBOARD:
        raw = event.payload.get("content", "")
        summary = redactors.classify_clipboard(raw)
        if summary.get("kind") == "credential":
            return Dropped("clipboard_credential", event)
        # Preserve any non-content metadata the adapter attached, then overlay summary.
        meta = {k: v for k, v in event.payload.items() if k != "content"}
        event.payload = {**meta, **summary}
        redactions.add("clipboard_typed")
        if policy.clipboard_preview:
            preview, preds = dp.make_clipboard_preview(raw, policy)
            if preview:
                event.payload["preview"] = preview
                redactions.update(preds)

    # 3a. Opt-in diff/body/diffstat capture (FIRST-PARTY file/commit events only).
    #     With DEFAULT_POLICY this whole block is skipped -> identical to before.
    kept_keys: set[str] = set()
    if policy.capture_diffs and event.type in _DIFF_AWARE_TYPES:
        body = event.payload.get("body")
        if isinstance(body, str) and body:
            cleaned, reds = redactors.scrub(body)
            event.payload["body"] = cleaned
            redactions.add(dp.COMMIT_BODY_CAPTURED)
            redactions.update(reds)
            kept_keys.add("body")
        stat = event.payload.get("diffstat")
        if isinstance(stat, str) and stat:
            cleaned, reds = redactors.scrub(stat)
            event.payload["diffstat"] = cleaned
            redactions.add(dp.DIFFSTAT_CAPTURED)
            redactions.update(reds)
        _capture_diff(event, policy, redactions)

    # 3. Strip raw content fields entirely (except any kept above).
    for k in _CONTENT_DROP_KEYS:
        if k in event.payload and k not in kept_keys:
            del event.payload[k]
            redactions.add("content_stripped")

    # 4. Redact every remaining string value (secrets + home paths). The already-
    #    scrubbed transient diff is held out (it's large and clean) and restored.
    clean_diff = event.payload.pop("_diff_clean", None)
    event.payload = _redact_walk(event.payload, redactions)
    if clean_diff is not None:
        event.payload["_diff_clean"] = clean_diff

    # 5. Stamp the audit trail.
    event.privacy = Privacy(gate_version=GATE_VERSION,
                            redactions=sorted(redactions),
                            passed_at=now_iso())
    return event


def _audit_main(argv: list[str] | None = None) -> int:
    """Audit a persisted v2 events file: re-run the egress check over every
    event and prove nothing in the store would leak if sent to a model.

        python -m throughlog.privacy.gate --audit data/events_replay/20260506.jsonl
    """
    import argparse
    import json
    from throughlog.privacy import redactors

    from pathlib import Path

    # A *residual* leak: scrubbing actually changes the text, i.e. a real secret
    # survived. This is idempotent-safe — re-scrubbing an already-redacted
    # `KEY="[REDACTED:...]"` line leaves it unchanged, so a redaction placeholder is
    # not miscounted as a leak (unlike a raw `assert_clean`, which re-matches the
    # retained key prefix). Content-addressing hashes (`diff_ref`) are excluded by
    # the caller, since a sha256 is high-entropy but reveals nothing.
    def _residual(text: str) -> list[str]:
        clean, names = redactors.scrub(text)
        return names if clean != text else []

    ap = argparse.ArgumentParser(description=_audit_main.__doc__)
    ap.add_argument("--audit", required=True, metavar="FILE",
                    help="persisted v2 JSONL events file to audit")
    ap.add_argument("--show", type=int, default=5, help="sample N leaks if any")
    ap.add_argument("--diffs", metavar="DIR", default=None,
                    help="also audit every diff sidecar (*.patch) under DIR")
    args = ap.parse_args(argv)

    total = leaks = shown = 0
    leak_types: dict[str, int] = {}
    red_hist: dict[str, int] = {}
    with open(args.audit, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            total += 1
            for r in (ev.get("privacy") or {}).get("redactions", []):
                red_hist[r] = red_hist.get(r, 0) + 1
            # diff_ref is a content-address (sha256), not content — exclude it.
            payload = {k: v for k, v in (ev.get("payload") or {}).items() if k != "diff_ref"}
            blob = json.dumps(payload, ensure_ascii=False)
            found = _residual(blob)
            if found:
                leaks += 1
                for t in found:
                    leak_types[t] = leak_types.get(t, 0) + 1
                if shown < args.show:
                    print(f"LEAK {ev.get('event_id')}: {found} :: {blob[:160]}")
                    shown += 1

    diff_files = 0
    if args.diffs:
        for patch in sorted(Path(args.diffs).glob("*.patch")):
            diff_files += 1
            body = patch.read_text(encoding="utf-8")
            found = _residual(body)
            if found:
                leaks += 1
                for t in found:
                    leak_types[t] = leak_types.get(t, 0) + 1
                if shown < args.show:
                    print(f"LEAK {patch.name}: {found} :: {body[:160]}")
                    shown += 1

    print(f"audited events     : {total}")
    if args.diffs:
        print(f"audited diff files : {diff_files}")
    print(f"redaction histogram: {red_hist}")
    print(f"egress leaks found : {leaks}  {leak_types}")
    print("RESULT: " + ("CLEAN - nothing in the store would leave the machine"
                         if leaks == 0 else "LEAKS PRESENT - gate bug"))
    return 0 if leaks == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_audit_main())
