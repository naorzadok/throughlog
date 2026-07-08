import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.bus import EventBus
from throughlog.schema import make_event, FILE_CHANGE
from throughlog.privacy.allowlist import Allowlist
from throughlog.privacy.diff_policy import DiffPolicy

REPO = "C:/Users/dev/Desktop/projects/throughlog"
ALLOW = Allowlist([REPO])
ON = DiffPolicy(capture_diffs=True)


def _fc(diff: str):
    return make_event(FILE_CHANGE, kind="fs", adapter="fs_git",
                      payload={"path": f"{REPO}/throughlog/app.py", "diff": diff})


def _diff(*body: str) -> str:
    head = ("diff --git a/throughlog/app.py b/throughlog/app.py\n--- a/throughlog/app.py\n"
            "+++ b/throughlog/app.py\n@@ -1 +1,2 @@\n import os\n")
    return head + "".join(line + "\n" for line in body)


def _lines(out: Path):
    return [json.loads(l) for f in sorted(out.glob("*.jsonl"))
            for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


class Sidecar(unittest.TestCase):
    def test_diff_written_to_sidecar_keyed_by_hash(self):
        with tempfile.TemporaryDirectory() as d:
            out, diffs = Path(d) / "events", Path(d) / "diffs"
            bus = EventBus(out, ALLOW, diff_policy=ON, diffs_dir=diffs)
            self.assertTrue(bus.emit(_fc(_diff('+print("hi")'))))
            bus.close()

            ev = _lines(out)[0]
            ref = ev["payload"]["diff_ref"]
            # the thin-log carries only a ref, never the transient nor raw diff
            self.assertNotIn("_diff_clean", ev["payload"])
            self.assertNotIn("diff", ev["payload"])
            # the sidecar file is named for the content hash (V-08)
            patch = diffs / f"{ref}.patch"
            self.assertTrue(patch.exists())
            body = patch.read_text(encoding="utf-8")
            self.assertEqual(hashlib.sha256(body.encode("utf-8")).hexdigest(), ref)
            self.assertIn('print("hi")', body)

    def test_identical_diffs_dedupe_by_hash(self):
        with tempfile.TemporaryDirectory() as d:
            out, diffs = Path(d) / "events", Path(d) / "diffs"
            bus = EventBus(out, ALLOW, diff_policy=ON, diffs_dir=diffs)
            bus.emit(_fc(_diff("+x = 1")))
            bus.emit(_fc(_diff("+x = 1")))
            bus.close()
            self.assertEqual(len(list(diffs.glob("*.patch"))), 1)   # content-addressed

    def test_off_writes_no_sidecar(self):
        with tempfile.TemporaryDirectory() as d:
            out, diffs = Path(d) / "events", Path(d) / "diffs"
            bus = EventBus(out, ALLOW, diffs_dir=diffs)            # default policy = off
            bus.emit(_fc(_diff("+x = 1")))
            bus.close()
            self.assertFalse(diffs.exists() and list(diffs.glob("*.patch")))
            self.assertNotIn("diff", _lines(out)[0]["payload"])

    def test_sidecar_write_failure_never_persists_transient(self):
        # V-02 — even if the sidecar write fails, the event is still persisted and the
        # transient _diff_clean never reaches disk (structural drop in to_dict).
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "events"
            blocker = Path(d) / "blocker"
            blocker.write_text("i am a file, not a dir", encoding="utf-8")
            bus = EventBus(out, ALLOW, diff_policy=ON, diffs_dir=blocker / "sub")
            self.assertTrue(bus.emit(_fc(_diff('+secret_marker = 1'))))   # not dropped
            bus.close()
            ev = _lines(out)[0]
            self.assertNotIn("_diff_clean", ev["payload"])
            raw = sorted(out.glob("*.jsonl"))[0].read_text(encoding="utf-8")
            self.assertNotIn("_diff_clean", raw)
            self.assertNotIn("secret_marker", raw)        # diff body not in the thin-log


class Auditor(unittest.TestCase):
    """`--audit [--diffs]` is idempotency-based: redaction placeholders and the
    diff_ref content-hash are NOT miscounted as leaks, but a genuine residual secret
    still fails the audit."""

    def _store(self, d):
        from throughlog.privacy.gate import gate
        out, diffs = Path(d) / "events", Path(d) / "diffs"
        bus = EventBus(out, ALLOW, diff_policy=ON, diffs_dir=diffs)
        bus.emit(_fc(_diff('+API_KEY = "sk-ant-api03-LEAK1234567890abcdefGHIJ"')))
        bus.close()
        return out, diffs

    def test_clean_store_audits_clean(self):
        from throughlog.privacy.gate import _audit_main
        with tempfile.TemporaryDirectory() as d:
            out, diffs = self._store(d)
            evfile = sorted(out.glob("*.jsonl"))[0]
            rc = _audit_main(["--audit", str(evfile), "--diffs", str(diffs)])
            self.assertEqual(rc, 0)          # placeholders + sha ref are not leaks

    def test_planted_raw_secret_fails_audit(self):
        from throughlog.privacy.gate import _audit_main
        with tempfile.TemporaryDirectory() as d:
            out, diffs = self._store(d)
            # tamper: drop a RAW aws key into a sidecar (simulating a gate bug)
            (diffs / "deadbeef.patch").write_text(
                "+key AKIA1234567890ABCDEF\n", encoding="utf-8")
            evfile = sorted(out.glob("*.jsonl"))[0]
            rc = _audit_main(["--audit", str(evfile), "--diffs", str(diffs)])
            self.assertEqual(rc, 1)          # the real leak is still caught


if __name__ == "__main__":
    unittest.main()
