"""F1/F2/F3 — batched entry calls, chunk-not-condense budget, per-project scheduling.

These lock the NEW cost knobs added on top of the per-day synthesis path:
  * pack_days_by_budget groups WHOLE days into call units (never splits a day);
  * a budget>0 entry path chunks raw detail instead of condensing it (no `chunk` calls);
  * a batched reply is split back into per-day sections, with a C5 archive fallback;
  * per-project synthesis.day + a synth-days watermark spread & bound the LLM calls.
All offline (fake client), deterministic, and decoupled from the live registry.
"""
import os
import sys
import re
import json
import tempfile
import shutil
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.schema import make_event, FOCUS_SESSION
from throughlog.llm.client import LLMError
from throughlog.llm.prompts import DAILY_SEP
from throughlog import synthesize as syn
from throughlog.synthesize import (
    SynthesisOptions, pack_days_by_budget, is_due, project_synthesis_day,
    _split_batched_reply, _date_key,
)


def _ev(day: int, k: int, pid="p1"):
    e = make_event(FOCUS_SESSION, kind="os", adapter="os_focus",
                   ts_wall=f"2026-06-{day:02d}T09:{k % 60:02d}:00+03:00",
                   payload={"anchor": f"file{k}.py", "process": "Code.exe",
                            "duration_sec": 1200, "mode": "producing",
                            "active_file": f"~/p/{pid}/file{k}.py"})
    e.attribution.project_id = pid
    e.attribution.confidence = 0.95
    e.attribution.method = "signal_path"
    return e


def _overview_reply():
    return ("# P\n**Status:** active | **Last Updated:** 2026-06-30\n\n"
            "## Current State\nx\n\n## Ongoing Threads\n- t — wip\n\n"
            "## Chronological Narrative\ny\n\n## Key Artifacts\n- f\n"
            f"{DAILY_SEP}\nDaily line.")


class LabeledFake:
    """Records (label, system, user) per chat(); emits per-day sections for a batched
    entry so the pipeline's split succeeds, mirroring the real client contract."""

    def __init__(self, fail_entry=False, drop_day=None):
        self.calls = []
        self.fail_entry = fail_entry
        self.drop_day = drop_day          # a date label the model "forgets" to emit

    def chat(self, system, user, *, temperature=0.0, max_tokens=1500, label=""):
        self.calls.append((label, system, user))
        if label == "overview":
            return _overview_reply()
        if label == "entry":
            if self.fail_entry:
                raise LLMError("entry model down")
            if "DAYS IN THIS BATCH" in user:
                m = re.search(r"DAYS IN THIS BATCH \(\d+\): (.+)", user)
                labels = [d.strip() for d in m.group(1).split(",")]
                return "\n\n".join(f"## {d}\nDetail for {d}: value 0.5."
                                   for d in labels if d != self.drop_day)
            return "Single-day detail: value 0.5."
        return "generic"

    def n(self, label):
        return sum(1 for lbl, _, _ in self.calls if lbl == label)


PROJECT = {"id": "p1", "name": "P1", "description": "d"}


# --------------------------------------------------------------------------- #
class Packer(unittest.TestCase):
    def _by_date(self, days, per_day=3):
        by = {}
        for d in range(1, days + 1):
            by[f"202606{d:02d}"] = [_ev(d, k) for k in range(per_day)]
        return sorted(by), by

    def test_day_mode_one_unit_per_day(self):
        dates, by = self._by_date(5)
        units = pack_days_by_budget(dates, by, SynthesisOptions(entry_batch="day"))
        self.assertEqual(units, [[d] for d in dates])

    def test_adaptive_flushes_on_budget(self):
        dates, by = self._by_date(6, per_day=3)
        # each day ~ a few dozen tokens; a tiny budget forces ~1 day per unit
        units = pack_days_by_budget(
            dates, by, SynthesisOptions(entry_batch="adaptive",
                                        max_input_tokens=40, max_batch_days=7))
        self.assertTrue(all(len(u) == 1 for u in units))
        self.assertEqual(sum(len(u) for u in units), 6)   # every day present, none dropped

    def test_adaptive_flushes_on_span_cap(self):
        dates, by = self._by_date(10, per_day=1)
        units = pack_days_by_budget(
            dates, by, SynthesisOptions(entry_batch="adaptive",
                                        max_input_tokens=10 ** 9, max_batch_days=3))
        self.assertEqual([len(u) for u in units], [3, 3, 3, 1])

    def test_week_mode_breaks_on_iso_week(self):
        # 2026-06-01 is Monday (W23); 06-08 starts W24.
        dates, by = self._by_date(10, per_day=1)
        units = pack_days_by_budget(
            dates, by, SynthesisOptions(entry_batch="week",
                                        max_input_tokens=10 ** 9, max_batch_days=7))
        self.assertEqual([len(u) for u in units], [7, 3])

    def test_single_over_budget_day_is_its_own_unit_not_split(self):
        by = {"20260601": [_ev(1, k) for k in range(200)],   # huge day
              "20260602": [_ev(2, 0)]}
        dates = sorted(by)
        units = pack_days_by_budget(
            dates, by, SynthesisOptions(entry_batch="adaptive",
                                        max_input_tokens=100, max_batch_days=7))
        self.assertEqual(units, [["20260601"], ["20260602"]])   # day never split mid-day


class SplitReply(unittest.TestCase):
    def test_splits_on_date_headers(self):
        out = _split_batched_reply("intro\n## 2026-06-01\nA\n\n## 2026-06-02\nB\n")
        self.assertEqual(out, {"2026-06-01": "A", "2026-06-02": "B"})

    def test_missing_day_absent(self):
        out = _split_batched_reply("## 2026-06-01\nonly one")
        self.assertEqual(set(out), {"2026-06-01"})


# --------------------------------------------------------------------------- #
class BatchedRun(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="tl_batch_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _events(self, days):
        return [_ev(d, k) for d in range(1, days + 1) for k in range(4)]

    def _entries_text(self):
        edir = self.dir / "project_p1" / "entries"
        return "".join(p.read_text(encoding="utf-8") for p in sorted(edir.glob("*.md")))

    def test_weekly_batch_cuts_calls_but_keeps_per_day_sections(self):
        c = LabeledFake()
        opts = SynthesisOptions(write_entries=True, entry_batch="week",
                                max_input_tokens=6000, max_batch_days=7)
        syn.run(self._events(10), [PROJECT], journal_dir=self.dir, client=c,
                today="2026-06-30", options=opts)
        self.assertEqual(c.n("entry"), 2)              # 2 ISO weeks, not 10 days
        text = self._entries_text()
        for d in range(1, 11):                          # every day still has its section
            self.assertIn(f"## 2026-06-{d:02d}", text)

    def test_batched_entry_failure_falls_back_to_archive(self):
        c = LabeledFake(fail_entry=True)
        opts = SynthesisOptions(write_entries=True, entry_batch="adaptive",
                                max_input_tokens=6000)
        res = syn.run(self._events(4), [PROJECT], journal_dir=self.dir, client=c,
                      today="2026-06-30", options=opts)
        text = self._entries_text()
        for d in range(1, 5):                           # no day lost — archive sections kept
            self.assertIn(f"## 2026-06-0{d}", text)
        self.assertIn("### Sessions", text)             # archive fallback shape
        self.assertTrue(res.projects[0].entry_error)

    def test_dropped_day_falls_back_to_archive(self):
        c = LabeledFake(drop_day="2026-06-02")
        opts = SynthesisOptions(write_entries=True, entry_batch="week",
                                max_input_tokens=6000)
        syn.run(self._events(3), [PROJECT], journal_dir=self.dir, client=c,
                today="2026-06-30", options=opts)
        text = self._entries_text()
        self.assertIn("Detail for 2026-06-01", text)    # model-written day
        self.assertIn("## 2026-06-02", text)            # dropped day present via archive
        self.assertIn("### Sessions", text)


class ChunkNotCondense(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="tl_chunk_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _big_day(self):
        return [_ev(1, k) for k in range(200)]          # > CHUNK_SIZE (150)

    def test_legacy_budget0_condenses_big_day(self):
        c = LabeledFake()
        opts = SynthesisOptions(write_entries=True, entry_batch="day",
                                max_input_tokens=0)     # legacy path
        syn.run(self._big_day(), [PROJECT], journal_dir=self.dir, client=c,
                today="2026-06-30", options=opts)
        self.assertGreater(c.n("chunk"), 0)             # condensed via chunk calls

    def test_budget_chunks_raw_without_condensing(self):
        c = LabeledFake()
        opts = SynthesisOptions(write_entries=True, entry_batch="day",
                                max_input_tokens=6000)  # budget>0 => chunk-not-condense
        syn.run(self._big_day(), [PROJECT], journal_dir=self.dir, client=c,
                today="2026-06-30", options=opts)
        self.assertEqual(c.n("chunk"), 0)               # NEVER condenses
        self.assertEqual(c.n("entry"), 1)               # still one entry for the day


# --------------------------------------------------------------------------- #
class Scheduling(unittest.TestCase):
    def test_is_due_weekday_and_daily(self):
        mon = {"id": "a", "synthesis": {"day": "mon"}}
        self.assertTrue(is_due(mon, date(2026, 6, 15)))     # Monday
        self.assertFalse(is_due(mon, date(2026, 6, 16)))    # Tuesday
        self.assertTrue(is_due({"id": "b"}, date(2026, 6, 16)))   # daily default
        self.assertEqual(project_synthesis_day({"synthesis": {"day": "FRI"}}), "fri")
        self.assertEqual(project_synthesis_day({"synthesis": {"day": "junk"}}), "daily")

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="tl_sched_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_not_due_reuses_and_bounds_to_new_days(self):
        proj = {"id": "p1", "name": "P1", "synthesis": {"day": "mon"}}
        opts = SynthesisOptions(write_entries=True, entry_batch="adaptive",
                                max_input_tokens=6000)
        ev3 = [_ev(d, k) for d in range(1, 4) for k in range(3)]   # 06-01..03

        # Tuesday, first sight -> bootstrap (overview absent), entries all 3 days.
        c1 = LabeledFake()
        syn.run(ev3, [proj], journal_dir=self.dir, client=c1, today="2026-06-16", options=opts)
        self.assertGreaterEqual(c1.n("entry"), 1)
        state = json.loads((self.dir / ".synth_state.json").read_text())
        self.assertEqual(set(state["p1"]["synth_days"]), {"20260601", "20260602", "20260603"})

        # Wednesday, not due -> reuse, zero LLM calls; archive still refreshed.
        c2 = LabeledFake()
        syn.run(ev3, [proj], journal_dir=self.dir, client=c2, today="2026-06-17", options=opts)
        self.assertEqual(c2.n("entry"), 0)
        self.assertEqual(c2.n("overview"), 0)

        # Monday, due, one new day -> entries ONLY the new day (watermark bounds it).
        ev4 = ev3 + [_ev(4, k) for k in range(3)]        # + 06-04
        c3 = LabeledFake()
        syn.run(ev4, [proj], journal_dir=self.dir, client=c3, today="2026-06-22", options=opts)
        entry_users = [u for lbl, _, u in c3.calls if lbl == "entry"]
        joined = "\n".join(entry_users)
        self.assertIn("2026-06-04", joined)
        self.assertNotIn("2026-06-01", joined)           # old days not re-billed
        state3 = json.loads((self.dir / ".synth_state.json").read_text())
        self.assertIn("20260604", state3["p1"]["synth_days"])


if __name__ == "__main__":
    unittest.main()
