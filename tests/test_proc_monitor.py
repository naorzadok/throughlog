import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.schema import LONG_RUN
from throughlog.sources.proc_monitor import LongRunTracker, ProcSample, Proc

T0 = "2026-06-21T08:00:00+03:00"


def _ts(sec: int) -> str:
    return (datetime.fromisoformat(T0) + timedelta(seconds=sec)).isoformat()


SOLVER = Proc(pid=4242, name="solver.exe", cmdline="solve.py --hard", cpu_percent=95)


class LongRun(unittest.TestCase):
    def test_unattended_busy_run_is_captured(self):
        t = LongRunTracker(cpu_threshold=50, long_run_min_sec=1800)
        evs = []
        evs += t.feed(ProcSample(_ts(0), [SOLVER], human_present=False))
        evs += t.feed(ProcSample(_ts(3600), [SOLVER], human_present=False))
        evs += t.feed(ProcSample(_ts(3700), [SOLVER], human_present=True))   # human back
        evs += t.close()
        runs = [e for e in evs if e.type == LONG_RUN]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].payload["process"], "solver.exe")
        self.assertEqual(runs[0].payload["duration_sec"], 3600.0)

    def test_short_run_is_filtered(self):
        t = LongRunTracker(cpu_threshold=50, long_run_min_sec=1800)
        evs = []
        evs += t.feed(ProcSample(_ts(0), [SOLVER], human_present=False))
        evs += t.feed(ProcSample(_ts(60), [], human_present=False))          # gone after 60s
        evs += t.close()
        self.assertEqual([e for e in evs if e.type == LONG_RUN], [])

    def test_human_present_suppresses(self):
        t = LongRunTracker(cpu_threshold=50, long_run_min_sec=1800)
        evs = []
        evs += t.feed(ProcSample(_ts(0), [SOLVER], human_present=True))
        evs += t.feed(ProcSample(_ts(7200), [SOLVER], human_present=True))
        evs += t.close()
        self.assertEqual(evs, [])

    def test_idle_cpu_not_counted(self):
        t = LongRunTracker(cpu_threshold=50, long_run_min_sec=1800)
        idle_proc = Proc(pid=1, name="idle.exe", cpu_percent=5)
        evs = t.feed(ProcSample(_ts(0), [idle_proc], human_present=False))
        evs += t.feed(ProcSample(_ts(7200), [idle_proc], human_present=False))
        evs += t.close()
        self.assertEqual(evs, [])


if __name__ == "__main__":
    unittest.main()
