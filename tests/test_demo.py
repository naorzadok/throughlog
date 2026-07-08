"""The built-in demo day (`tl demo`) — the zero-config, keyless first run.

These lock the fresh-clone contract: a clone with no config.json, no API key,
and no captured corpus can still produce a populated, correctly-attributed,
privacy-clean dashboard. If any of this regresses, the launch front door breaks.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import demo
from throughlog import synthesize as syn
from throughlog.categorize import categorize_events
from throughlog.schema import AGENT_REPORT, validate, IDLE_START, IDLE_END
from throughlog.timeline import reconcile


class DemoEvents(unittest.TestCase):
    def test_events_are_valid_and_pre_gated(self):
        evs = demo.build_demo_events()
        self.assertGreater(len(evs), 10)
        for ev in evs:
            # schema-valid and carries a privacy stamp (i.e. "already gated")
            self.assertEqual(validate(ev.to_dict()), [], ev.type)
            self.assertIsNotNone(ev.privacy, f"{ev.type} not gate-stamped")
            self.assertTrue(ev.privacy.passed_at)

    def test_covers_both_projects_and_an_agent_thread(self):
        evs = demo.build_demo_events()
        types = {e.type for e in evs}
        self.assertIn(AGENT_REPORT, types)          # the wedge is present
        agent = next(e for e in evs if e.type == AGENT_REPORT)
        self.assertEqual(agent.source.kind, "agent")
        self.assertIn("claude", agent.source.identity)

    def test_every_meaningful_event_attributes_without_an_llm(self):
        # categorize with client=None — the deterministic stack must resolve
        # everything except idle (which carries no project by design).
        evs = [e for e in demo.build_demo_events()
               if e.type not in (IDLE_START, IDLE_END)]
        categorize_events(evs, demo.DEMO_PROJECTS, client=None)
        for ev in evs:
            self.assertIsNotNone(
                ev.attribution.project_id,
                f"{ev.type} ({ev.payload}) fell to needs_review with no LLM")
        pids = {e.attribution.project_id for e in evs}
        self.assertEqual(pids, {"acme-checkout", "pricing-model"})


class DemoPipeline(unittest.TestCase):
    def _run(self, out: Path):
        store = demo.write_demo_thinlog(out / f"{demo.DEMO_DAY}.jsonl")
        raw = [json.loads(l) for l in store.read_text(encoding="utf-8").splitlines()]
        from throughlog.schema import NormalizedEvent
        events = [NormalizedEvent.from_dict(d) for d in reconcile(raw)]
        categorize_events(events, demo.DEMO_PROJECTS, client=None)
        return events, syn.run(events, demo.DEMO_PROJECTS,
                               journal_dir=out / "journal", client=None,
                               today=demo.DEMO_TODAY)

    def test_synthesizes_populated_journal_offline(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            events, res = self._run(out)
            ids = {pd.project_id for pd in res.projects}
            self.assertEqual(ids, {"acme-checkout", "pricing-model"})
            for pd in res.projects:
                archive = out / "journal" / f"project_{pd.project_id}" / "archive.md"
                self.assertTrue(archive.exists())
                self.assertIn("## 2026-06-24", archive.read_text(encoding="utf-8"))
            acme = (out / "journal" / "project_acme-checkout" /
                    "archive.md").read_text(encoding="utf-8")
            self.assertIn("### Agent activity", acme)        # the wedge shows
            self.assertIn("claude-code", acme)

    def test_thinlog_write_is_deterministic(self):
        # Byte-stable output is what lets the demo be screenshot-stable.
        with tempfile.TemporaryDirectory() as d:
            a = demo.write_demo_thinlog(Path(d) / "a.jsonl").read_bytes()
            b = demo.write_demo_thinlog(Path(d) / "b.jsonl").read_bytes()
            self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
