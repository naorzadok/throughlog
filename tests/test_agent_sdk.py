import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import agent_sdk
from throughlog.agent_sdk import AgentReporter, build_report
from throughlog.schema import validate, AGENT_REPORT
from throughlog.sources.agent_ingest import ingest_report, ingest_drop_folder
from throughlog.categorize import categorize_events


# --------------------------------------------------------------------------- #
# Fake HTTP openers (no network)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status): self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def getcode(self): return self.status
    def read(self): return b""


def _ok_opener(status=202):
    def _open(req, timeout=None):
        return _Resp(status)
    return _open


def _http_error_opener(code=422):
    def _open(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "rejected", {}, None)
    return _open


def _net_fail_opener():
    def _open(req, timeout=None):
        raise urllib.error.URLError("connection refused")
    return _open


# --------------------------------------------------------------------------- #
# build_report — pure, always schema-valid
# --------------------------------------------------------------------------- #
class BuildReport(unittest.TestCase):
    def test_report_is_schema_valid(self):
        rep = build_report(summary="did a thing", identity="agent:ci",
                           tool="ci", repo="github.com/me/repo",
                           files=["a.py"], project="repo")
        # validate() expects a trust field default; ingest sets it, but the raw
        # report omits it (defaults to "validated"), which validate accepts.
        self.assertEqual(validate({**rep, "trust": "validated"}), [])
        self.assertEqual(rep["type"], AGENT_REPORT)
        self.assertEqual(rep["source"]["kind"], "agent")
        self.assertEqual(rep["source"]["adapter"], "ci")
        self.assertEqual(rep["payload"]["summary"], "did a thing")
        self.assertEqual(rep["payload"]["files"], ["a.py"])
        self.assertEqual(rep["payload"]["repo"], "github.com/me/repo")
        self.assertEqual(rep["payload"]["project_hint"], "repo")

    def test_minimal_report_valid(self):
        rep = build_report(summary="s", identity="agent:x")
        self.assertEqual(validate({**rep, "trust": "validated"}), [])
        self.assertEqual(rep["source"]["adapter"], "agent_sdk")   # default


# --------------------------------------------------------------------------- #
# The wedge end-to-end: build -> ingest -> categorize -> a project
# --------------------------------------------------------------------------- #
class WedgeRoundTrip(unittest.TestCase):
    def test_built_report_ingests_validated_and_attributes(self):
        rep = build_report(summary="opened a PR refactoring the parser",
                           identity="agent:claude-code", tool="claude-code",
                           repo="github.com/acme/acme-api")
        ev = ingest_report(rep)
        self.assertEqual(ev.trust, "validated")
        self.assertEqual(ev.type, AGENT_REPORT)

        projects = [{"id": "acme-api", "status": "active",
                     "signals": {"git_remotes": ["github.com/acme/acme-api"]}}]
        categorize_events([ev], projects)
        self.assertEqual(ev.attribution.project_id, "acme-api")
        self.assertEqual(ev.attribution.method, "signal_git")


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
class Transport(unittest.TestCase):
    def test_http_success(self):
        r = AgentReporter(identity="agent:x", opener=_ok_opener(202))
        res = r.report("hello", repo="github.com/me/r")
        self.assertTrue(res.ok)
        self.assertEqual(res.transport, "http")
        self.assertEqual(res.status, 202)

    def test_http_rejection_surfaces(self):
        r = AgentReporter(identity="agent:x", opener=_http_error_opener(422))
        res = r.report("hello")
        self.assertFalse(res.ok)
        self.assertEqual(res.transport, "http")
        self.assertEqual(res.status, 422)

    def test_token_header_is_set(self):
        seen = {}

        def _open(req, timeout=None):
            seen["auth"] = req.headers.get("Authorization")
            return _Resp(202)

        AgentReporter(identity="agent:x", token="secret", opener=_open).report("hi")
        self.assertEqual(seen["auth"], "Bearer secret")

    def test_network_failure_without_drop_is_failed(self):
        r = AgentReporter(identity="agent:x", opener=_net_fail_opener())
        res = r.report("hi")
        self.assertFalse(res.ok)
        self.assertEqual(res.transport, "failed")

    def test_network_failure_falls_back_to_drop_and_is_ingestable(self):
        with tempfile.TemporaryDirectory() as d:
            drop = Path(d) / "agent_drop"
            r = AgentReporter(identity="agent:x", opener=_net_fail_opener(),
                              drop_dir=drop)
            res = r.report("dropped work", repo="github.com/me/r")
            self.assertTrue(res.ok)
            self.assertEqual(res.transport, "drop")
            self.assertTrue(Path(res.path).exists())

            # The dropped file must be valid and ingestable through the bus path.
            files = list(drop.glob("*.json"))
            self.assertEqual(len(files), 1)
            raw = json.loads(files[0].read_text(encoding="utf-8"))
            ev = ingest_report(raw)
            self.assertEqual(ev.trust, "validated")
            self.assertEqual(ev.payload["summary"], "dropped work")

            # And ingest_drop_folder drains it through a collecting emitter.
            collected = []
            ingest_drop_folder(type("E", (), {"emit": lambda s, e: collected.append(e)})(),
                               drop)
            self.assertEqual(len(collected), 1)


if __name__ == "__main__":
    unittest.main()
