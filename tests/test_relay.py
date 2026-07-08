import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import relay
from throughlog.relay import AccountRegistry, AccountStore, Relay, _safe_account
from throughlog.schema import make_event, AGENT_REPORT
from throughlog.privacy.gate import gate
from throughlog.privacy.allowlist import Allowlist


def _gated_dict(payload):
    ev = make_event(AGENT_REPORT, kind="agent", adapter="x", identity="agent:1",
                    payload=payload, ts_wall="2026-06-24T10:00:00+00:00")
    return gate(ev, Allowlist([])).to_dict()


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
class Accounts(unittest.TestCase):
    def test_account_for_token(self):
        reg = AccountRegistry({"tok-a": "alice", "tok-b": "bob"})
        self.assertEqual(reg.account_for("tok-a"), "alice")
        self.assertIsNone(reg.account_for("nope"))
        self.assertIsNone(reg.account_for(None))

    def test_from_config(self):
        reg = AccountRegistry.from_config({"relay": {"tokens": {"t": "u"}}})
        self.assertEqual(reg.account_for("t"), "u")

    def test_safe_account_blocks_traversal(self):
        self.assertNotIn("/", _safe_account("../../etc/passwd"))
        self.assertNotIn("\\", _safe_account("..\\..\\x"))
        self.assertNotIn("..", _safe_account(".."))


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class Store(unittest.TestCase):
    def test_append_and_read_since(self):
        with tempfile.TemporaryDirectory() as d:
            st = AccountStore(d, "alice")
            st.append(make_event(AGENT_REPORT, kind="agent", adapter="x",
                                 payload={"summary": "early"},
                                 ts_wall="2026-06-20T10:00:00+00:00"))
            st.append(make_event(AGENT_REPORT, kind="agent", adapter="x",
                                 payload={"summary": "late"},
                                 ts_wall="2026-06-24T10:00:00+00:00"))
            self.assertEqual(len(st.read_since()), 2)
            recent = st.read_since("2026-06-22T00:00:00+00:00")
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]["payload"]["summary"], "late")


# --------------------------------------------------------------------------- #
# Relay logic
# --------------------------------------------------------------------------- #
class Logic(unittest.TestCase):
    def test_report_accepted_and_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            r = Relay(d, AccountRegistry({"t": "alice"}))
            code, obj = r.handle_report("alice", {
                "type": "AGENT_REPORT",
                "source": {"kind": "agent", "adapter": "claude", "identity": "agent:c"},
                "ts_wall": "2026-06-24T10:00:00+00:00",
                "payload": {"summary": "did a thing"}})
            self.assertEqual(code, 202)
            self.assertEqual(obj["trust"], "validated")
            self.assertTrue(AccountStore(d, "alice").read_since())

    def test_report_with_raw_diff_and_body_is_stripped(self):
        # The relay moat: it always gates with DEFAULT_POLICY (capture off), so a
        # spoofed /report carrying a raw diff/body never retains either, and no
        # diff_ref is minted — regardless of any sender toggle.
        with tempfile.TemporaryDirectory() as d:
            r = Relay(d, AccountRegistry({"t": "alice"}))
            code, obj = r.handle_report("alice", {
                "type": "AGENT_REPORT",
                "source": {"kind": "agent", "adapter": "claude", "identity": "agent:c"},
                "ts_wall": "2026-06-24T10:00:00+00:00",
                "payload": {"summary": "ok", "body": "secret body text",
                            "diff": "diff --git a/.env b/.env\n+TOKEN=sk-ant-leakleak"}})
            self.assertEqual(code, 202)
            stored = AccountStore(d, "alice").read_since()[0]
            self.assertNotIn("diff", stored["payload"])
            self.assertNotIn("diff_ref", stored["payload"])
            self.assertNotIn("body", stored["payload"])
            self.assertNotIn("sk-ant-leakleak", json.dumps(stored))

    def test_malformed_report_is_rejected_but_audited(self):
        with tempfile.TemporaryDirectory() as d:
            r = Relay(d, AccountRegistry({"t": "alice"}))
            code, obj = r.handle_report("alice", {"garbage": True})
            self.assertEqual(code, 422)
            self.assertEqual(obj["trust"], "rejected")     # never dropped silently
            self.assertTrue(AccountStore(d, "alice").read_since())

    def test_sync_accepts_gated_blocks_ungated(self):
        with tempfile.TemporaryDirectory() as d:
            r = Relay(d, AccountRegistry({"t": "alice"}))
            gated = _gated_dict({"summary": "synced"})
            ungated = {"type": "AGENT_REPORT", "ts_wall": "2026-06-24T10:00:00+00:00",
                       "payload": {"summary": "ungated"}}   # no privacy stamp
            code, obj = r.handle_sync("alice", [gated, ungated])
            self.assertEqual(code, 200)
            self.assertEqual(obj["accepted"], 1)
            self.assertEqual(obj["blocked"], 1)
            self.assertEqual(len(AccountStore(d, "alice").read_since()), 1)


# --------------------------------------------------------------------------- #
# In-process HTTP smoke (ephemeral port, real auth)
# --------------------------------------------------------------------------- #
class HttpSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sal_relay_test_"))
        cls.httpd = relay.make_relay(
            "127.0.0.1", 0, store_root=cls.tmp,
            registry=AccountRegistry({"good-token": "alice"}))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls._await_ready()

    @classmethod
    def _await_ready(cls, timeout=2.0):
        """Block until the daemon thread is actually servicing requests.

        ``serve_forever`` may not be accepting connections by the time the
        first test fires a request, which otherwise surfaces as a transient
        connection ERROR under full-suite scheduler pressure. Poll the
        unauthenticated /healthz endpoint until it answers 200.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=1)
                conn.request("GET", "/healthz")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status == 200:
                    return
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("relay server did not become ready within "
                           f"{timeout}s")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _req(self, method, path, *, token=None, body=None):
        # No connection-scoped retry needed: the relay drains the request body
        # before every early 401/400, so the HTTP/1.0 close is a clean FIN even
        # on Windows (no RST -> no WinError 10053). See relay._Handler._drain.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        out = resp.read().decode("utf-8")
        conn.close()
        return resp.status, (json.loads(out) if out else {})

    def test_healthz_no_auth(self):
        status, body = self._req("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_report_requires_auth(self):
        status, _ = self._req("POST", "/report", body={"type": "AGENT_REPORT"})
        self.assertEqual(status, 401)

    def test_unauthed_post_with_body_drains_cleanly(self):
        # The 401 auth check returns before _body() reads the payload; the handler
        # must drain the request body first so the HTTP/1.0 connection close is a
        # clean FIN. Without the drain, Windows RSTs a socket with unread bytes and
        # the client sees WinError 10053 instead of this 401. A sizable body makes
        # the unread-buffer condition unmistakable.
        status, body = self._req("POST", "/report",
                                 body={"type": "AGENT_REPORT", "pad": "x" * 100_000})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_bad_json_with_token_returns_400(self):
        # Malformed JSON is consumed by _body() before the 400, so the connection
        # also closes cleanly. Send raw non-JSON bytes past the auth check.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/report", body=b"not json at all",
                     headers={"Authorization": "Bearer good-token",
                              "Content-Type": "application/json"})
        resp = conn.getresponse()
        out = resp.read().decode("utf-8")
        conn.close()
        self.assertEqual(resp.status, 400)
        self.assertEqual(json.loads(out)["error"], "invalid json")

    def test_report_with_token_then_read_back(self):
        status, body = self._req("POST", "/report", token="good-token", body={
            "type": "AGENT_REPORT",
            "source": {"kind": "agent", "adapter": "claude", "identity": "agent:c"},
            "ts_wall": "2026-06-24T11:00:00+00:00",
            "payload": {"summary": "cloud agent worked"}})
        self.assertEqual(status, 202)

        status, body = self._req("GET", "/events", token="good-token")
        self.assertEqual(status, 200)
        summaries = [e["payload"].get("summary") for e in body["events"]]
        self.assertIn("cloud agent worked", summaries)

    def test_sync_endpoint_accepts_gated(self):
        status, body = self._req("POST", "/sync", token="good-token",
                                 body={"events": [_gated_dict({"summary": "from device"})]})
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)


if __name__ == "__main__":
    unittest.main()
