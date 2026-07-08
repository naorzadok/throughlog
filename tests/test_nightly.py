import os
import sys
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import nightly


class ParseHHMM(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(nightly.parse_hhmm("22:30"), (22, 30))
        self.assertEqual(nightly.parse_hhmm("00:00"), (0, 0))
        self.assertEqual(nightly.parse_hhmm("7:05"), (7, 5))      # leading 0 optional

    def test_invalid(self):
        for bad in ("99:99", "24:00", "12:60", "noon", "", None, "12", "12:3:4"):
            self.assertIsNone(nightly.parse_hhmm(bad), bad)


class DueForSynthesis(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 6, 28)

    def test_before_target_not_due(self):
        now = datetime(2026, 6, 28, 21, 0)
        self.assertFalse(nightly.due_for_synthesis("22:30", now, None))

    def test_at_or_after_target_is_due(self):
        self.assertTrue(nightly.due_for_synthesis(
            "22:30", datetime(2026, 6, 28, 22, 30), None))
        self.assertTrue(nightly.due_for_synthesis(   # late launch catches up
            "22:30", datetime(2026, 6, 28, 23, 5), None))

    def test_not_due_again_same_day(self):
        now = datetime(2026, 6, 28, 23, 0)
        self.assertFalse(nightly.due_for_synthesis("22:30", now, self.today))

    def test_due_next_day(self):
        now = datetime(2026, 6, 29, 22, 30)
        self.assertTrue(nightly.due_for_synthesis("22:30", now, self.today))

    def test_no_schedule_never_due(self):
        self.assertFalse(nightly.due_for_synthesis(None, datetime.now(), None))
        self.assertFalse(nightly.due_for_synthesis("bad", datetime.now(), None))


class TimerCheck(unittest.TestCase):
    def test_runs_once_then_marks_the_day(self):
        clock = {"t": datetime(2026, 6, 28, 22, 30)}
        calls = []
        t = nightly.NightlyTimer("22:30", run=lambda: calls.append(1),
                                 clock=lambda: clock["t"])
        self.assertTrue(t.check())            # first tick at target -> runs
        self.assertEqual(len(calls), 1)
        clock["t"] = datetime(2026, 6, 28, 22, 31)
        self.assertFalse(t.check())           # same day -> does not run again
        self.assertEqual(len(calls), 1)
        clock["t"] = datetime(2026, 6, 29, 22, 30)
        self.assertTrue(t.check())            # next day -> runs again
        self.assertEqual(len(calls), 2)

    def test_start_is_noop_without_a_valid_target(self):
        calls = []
        t = nightly.NightlyTimer(None, run=lambda: calls.append(1))
        t.start()                              # must not spawn a thread or run
        t.stop()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
