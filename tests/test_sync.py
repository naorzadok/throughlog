import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import sync
from throughlog.schema import make_event, AGENT_REPORT, FILE_CHANGE
from throughlog.privacy.gate import gate
from throughlog.privacy.allowlist import Allowlist
from throughlog.privacy.diff_policy import DiffPolicy


def _gated(payload):
    """A real gate-passed event (AGENT_REPORT isn't path-gated, so it carries a
    privacy stamp after the gate)."""
    ev = make_event(AGENT_REPORT, kind="agent", adapter="x", identity="agent:1",
                    payload=payload, ts_wall="2026-06-24T10:00:00+00:00")
    return gate(ev, Allowlist([]))


# --------------------------------------------------------------------------- #
# Egress guard — the privacy boundary for sync
# --------------------------------------------------------------------------- #
class EgressGuard(unittest.TestCase):
    def test_gated_event_is_sendable(self):
        sendable, blocked = sync.prepare_for_egress([_gated({"summary": "ok"})])
        self.assertEqual(len(sendable), 1)
        self.assertEqual(blocked, [])
        self.assertIn("privacy", sendable[0])

    def test_ungated_event_is_blocked(self):
        # No privacy stamp => never leaves the machine.
        raw = make_event(AGENT_REPORT, kind="agent", adapter="x",
                         payload={"summary": "ungated"})
        sendable, blocked = sync.prepare_for_egress([raw])
        self.assertEqual(sendable, [])
        self.assertEqual(len(blocked), 1)

    def test_captured_diff_never_leaves_the_machine(self):
        # A capture-on FILE_CHANGE with a real diff: only the hash ref + metadata are
        # eligible to sync — never the diff body, never the transient.
        repo = "C:/Users/dev/Desktop/projects/throughlog"
        diff = ("diff --git a/throughlog/app.py b/throughlog/app.py\n--- a/throughlog/app.py\n"
                "+++ b/throughlog/app.py\n@@ -1 +1,2 @@\n import os\n+x = 1\n")
        ev = make_event(FILE_CHANGE, kind="fs", adapter="fs_git",
                        payload={"path": f"{repo}/throughlog/app.py", "diff": diff},
                        ts_wall="2026-06-24T10:00:00+00:00")
        gated = gate(ev, Allowlist([repo]), DiffPolicy(capture_diffs=True))
        sendable, _ = sync.prepare_for_egress([gated])
        out = sendable[0]["payload"]
        self.assertNotIn("_diff_clean", out)
        self.assertNotIn("diff", out)
        self.assertNotIn("x = 1", json.dumps(sendable[0]))   # body stays in the sidecar

    def test_payload_is_rescrubbed_before_send(self):
        # A gated dict that (hypothetically) still carried a secret must be scrubbed
        # again on the way out — defense in depth against a gate bug.
        secret = "sk-ant-" + "a" * 24
        d = {"event_id": "e1", "ts_wall": "2026-06-24T10:00:00+00:00",
             "privacy": {"gate_version": "1"},
             "payload": {"summary": f"leaked {secret}"}}
        sendable, _ = sync.prepare_for_egress([d])
        out = sendable[0]["payload"]["summary"]
        self.assertNotIn(secret, out)
        self.assertIn("[REDACTED:anthropic_key]", out)


# --------------------------------------------------------------------------- #
# Transports (injected opener; no network)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status, body=b"{}"):
        self.status = status
        self._body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def getcode(self): return self.status
    def read(self): return self._body


class Transports(unittest.TestCase):
    def test_push_sends_only_gated_and_counts_blocked(self):
        seen = {}

        def _open(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.headers.get("Authorization")
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return _Resp(200, b'{"accepted":1,"blocked":0}')

        ungated = make_event(AGENT_REPORT, kind="agent", adapter="x",
                             payload={"summary": "nope"})
        res = sync.push("https://relay.test", "tok",
                        [_gated({"summary": "ship it"}), ungated], opener=_open)
        self.assertTrue(res.ok)
        self.assertEqual(res.sent, 1)       # only the gated event went
        self.assertEqual(res.blocked, 1)
        self.assertTrue(seen["url"].endswith("/sync"))
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertEqual(len(seen["body"]["events"]), 1)

    def test_pull_returns_events(self):
        def _open(req, timeout=None):
            return _Resp(200, json.dumps({"events": [{"event_id": "a"}]}).encode())
        res = sync.pull("https://relay.test", "tok", since="2026-06-01", opener=_open)
        self.assertTrue(res.ok)
        self.assertEqual(res.body["events"], [{"event_id": "a"}])

    def test_push_network_failure_not_ok(self):
        def _open(req, timeout=None):
            raise urllib.error.URLError("refused")
        res = sync.push("https://relay.test", "tok", [_gated({"summary": "x"})],
                        opener=_open)
        self.assertFalse(res.ok)
        self.assertIn("URLError", res.error)


if __name__ == "__main__":
    unittest.main()
