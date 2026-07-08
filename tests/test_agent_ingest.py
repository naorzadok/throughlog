import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.schema import AGENT_REPORT
from throughlog.sources.agent_ingest import ingest_report, AgentIngestConfig

NOW = "2026-06-21T12:00:00+03:00"
CFG = AgentIngestConfig(trusted_identities=("agent:claude-1",), future_tolerance_sec=120)


def _report(**over):
    base = {
        "type": AGENT_REPORT,
        "source": {"kind": "agent", "adapter": "agent_ingest",
                   "identity": "agent:claude-1", "session_id": "s1"},
        "ts_wall": "2026-06-21T11:30:00+03:00",
        "payload": {"summary": "ran tests"},
    }
    base.update(over)
    return base


class Ingest(unittest.TestCase):
    def test_valid_is_validated_and_stamped(self):
        ev = ingest_report(_report(), now=NOW, cfg=CFG)
        self.assertEqual(ev.trust, "validated")
        self.assertEqual(ev.recv_ts, NOW)
        self.assertEqual(ev.source.identity, "agent:claude-1")

    def test_unknown_identity_is_low_trust(self):
        ev = ingest_report(_report(source={"kind": "agent", "adapter": "agent_ingest",
                                            "identity": "agent:evil"}), now=NOW, cfg=CFG)
        self.assertEqual(ev.trust, "low_trust")
        self.assertIn("unknown_identity", ev.payload["trust_reasons"])

    def test_future_timestamp_is_low_trust(self):
        ev = ingest_report(_report(ts_wall="2027-01-01T00:00:00+03:00"), now=NOW, cfg=CFG)
        self.assertEqual(ev.trust, "low_trust")
        self.assertIn("future_timestamp", ev.payload["trust_reasons"])

    def test_non_agent_source_is_low_trust(self):
        ev = ingest_report(_report(source={"kind": "os", "adapter": "agent_ingest",
                                            "identity": "agent:claude-1"}), now=NOW, cfg=CFG)
        self.assertEqual(ev.trust, "low_trust")
        self.assertIn("source_kind_not_agent", ev.payload["trust_reasons"])

    def test_malformed_is_rejected_stub(self):
        ev = ingest_report({"source": {"kind": "banana"}, "payload": "nope"}, now=NOW, cfg=CFG)
        self.assertEqual(ev.trust, "rejected")
        self.assertEqual(ev.type, AGENT_REPORT)
        self.assertTrue(ev.payload["rejected"])
        self.assertTrue(ev.payload["reasons"])

    def test_sender_cannot_self_certify_trust(self):
        # A report claiming trust=validated is still judged here.
        ev = ingest_report(_report(trust="validated",
                                   source={"kind": "agent", "adapter": "agent_ingest",
                                           "identity": "agent:evil"}), now=NOW, cfg=CFG)
        self.assertEqual(ev.trust, "low_trust")


if __name__ == "__main__":
    unittest.main()
