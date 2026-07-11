"""The offline LLM stress harness (sim/llm_bench.py) — assert the call-cost scaling
laws it measures, so the numbers the harness reports stay honest and don't regress.

Pure/offline: uses the harness's own FakeMeteredClient; no network, no key, no bus.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.llm_bench import build_projects, build_corpus, run_once
from throughlog.synthesize import SynthesisOptions


def _labels(client):
    return client.by_label()


class HarnessScaling(unittest.TestCase):
    DAYS, PROJ = 3, 2

    def _run(self, options, journal_dir, events=None):
        projects = build_projects(self.PROJ)
        events = events if events is not None else build_corpus(self.DAYS, projects, 8)
        return run_once(events, projects, options, journal_dir=journal_dir,
                        today="2026-06-03"), events

    def test_entry_calls_scale_with_days_times_projects(self):
        with tempfile.TemporaryDirectory() as d:
            client, _ = self._run(
                SynthesisOptions(write_entries=True, summary_cadence="off"), Path(d))
        bl = _labels(client)
        self.assertEqual(bl.get("entry", 0), self.DAYS * self.PROJ)   # one entry / project / day
        self.assertEqual(bl.get("overview", 0), self.PROJ)            # one overview / project
        self.assertEqual(bl.get("categorize", 0), 1)                  # one batched Phase-1 call

    def test_entries_off_makes_no_entry_calls(self):
        with tempfile.TemporaryDirectory() as d:
            client, _ = self._run(
                SynthesisOptions(write_entries=False, summary_cadence="off"), Path(d))
        self.assertEqual(_labels(client).get("entry", 0), 0)

    def test_weekly_summary_bills_more_period_calls_than_monthly(self):
        # A span crossing multiple ISO weeks but one month: weekly > monthly period calls.
        projects = build_projects(self.PROJ)
        events = build_corpus(20, projects, 6)                        # 2026-06-01..20 -> 3 ISO weeks
        with tempfile.TemporaryDirectory() as dw, tempfile.TemporaryDirectory() as dm:
            wk = run_once(events, projects, SynthesisOptions(
                write_entries=True, summary_cadence="weekly"), journal_dir=Path(dw),
                today="2026-06-20")
            mo = run_once(build_corpus(20, projects, 6), projects, SynthesisOptions(
                write_entries=True, summary_cadence="monthly"), journal_dir=Path(dm),
                today="2026-06-20")
        self.assertGreater(_labels(wk).get("period", 0), _labels(mo).get("period", 0))
        self.assertEqual(_labels(mo).get("period", 0), 1)             # one month key

    def test_batching_cuts_entry_calls_below_per_day(self):
        # Over a multi-week span, weekly/adaptive batching bills far fewer entry calls than
        # the per-day path, while still covering every day (WHOLE-day units).
        projects = build_projects(self.PROJ)
        events = build_corpus(14, projects, 6)                        # 2 ISO weeks
        with tempfile.TemporaryDirectory() as dd, tempfile.TemporaryDirectory() as dw, \
                tempfile.TemporaryDirectory() as da:
            daily = run_once(events, projects, SynthesisOptions(
                write_entries=True, entry_batch="day"), journal_dir=Path(dd),
                today="2026-06-14")
            weekly = run_once(build_corpus(14, projects, 6), projects, SynthesisOptions(
                write_entries=True, entry_batch="week", max_input_tokens=6000),
                journal_dir=Path(dw), today="2026-06-14")
            adaptive = run_once(build_corpus(14, projects, 6), projects, SynthesisOptions(
                write_entries=True, entry_batch="adaptive", max_input_tokens=6000),
                journal_dir=Path(da), today="2026-06-14")
        self.assertEqual(_labels(daily).get("entry", 0), 14 * self.PROJ)   # per-day baseline
        self.assertEqual(_labels(weekly).get("entry", 0), 2 * self.PROJ)   # one call / week
        self.assertLessEqual(_labels(adaptive).get("entry", 0),
                             _labels(daily).get("entry", 0))
        self.assertEqual(_labels(adaptive).get("chunk", 0), 0)             # never condenses

    def test_skip_unchanged_rerun_reuses_and_bills_only_phase1(self):
        projects = build_projects(self.PROJ)
        events = build_corpus(self.DAYS, projects, 8)
        opts = SynthesisOptions(write_entries=True, summary_cadence="weekly",
                                skip_unchanged=True)
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d)
            run_once(events, projects, opts, journal_dir=dd, today="2026-06-03")   # full
            client2 = run_once(events, projects, opts, journal_dir=dd,             # unchanged
                               today="2026-06-03")
        bl = _labels(client2)
        # Phase 2 fully reused; only the batched Phase-1 categorize is re-billed.
        self.assertEqual(client2.metrics_summary()["calls"], 1)
        self.assertEqual(bl.get("entry", 0), 0)
        self.assertEqual(bl.get("overview", 0), 0)


if __name__ == "__main__":
    unittest.main()
