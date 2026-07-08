import os
import plistlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import deploy
from throughlog.sources import os_focus


# --------------------------------------------------------------------------- #
# macOS — launchd plist builder
# --------------------------------------------------------------------------- #
class LaunchdPlist(unittest.TestCase):
    def test_capture_plist_is_valid_and_runs_at_load(self):
        xml = deploy.capture_plist()
        data = plistlib.loads(xml.encode("utf-8"))   # raises on malformed plist
        self.assertEqual(data["Label"], deploy.CAPTURE_LABEL)
        self.assertTrue(data.get("RunAtLoad"))
        self.assertTrue(data.get("KeepAlive"))
        self.assertIn("capture", data["ProgramArguments"])
        self.assertIn("-m", data["ProgramArguments"])
        self.assertIn("throughlog.cli", data["ProgramArguments"])
        self.assertNotIn("StartCalendarInterval", data)   # capture isn't scheduled

    def test_capture_tray_and_flags(self):
        data = plistlib.loads(
            deploy.capture_plist(tray=True, no_clipboard=True).encode("utf-8"))
        args = data["ProgramArguments"]
        self.assertIn("tray", args)
        self.assertNotIn("capture", args)
        self.assertIn("--no-clipboard", args)

    def test_synthesis_plist_has_calendar_interval(self):
        data = plistlib.loads(
            deploy.synthesis_plist(time_hhmm="22:30").encode("utf-8"))
        self.assertEqual(data["Label"], deploy.SYNTHESIS_LABEL)
        self.assertEqual(data["StartCalendarInterval"]["Hour"], 22)
        self.assertEqual(data["StartCalendarInterval"]["Minute"], 30)
        self.assertIn("synthesize", data["ProgramArguments"])

    def test_synthesis_no_llm(self):
        data = plistlib.loads(
            deploy.synthesis_plist(time_hhmm="07:05", no_llm=True).encode("utf-8"))
        self.assertEqual(data["StartCalendarInterval"]["Minute"], 5)
        self.assertIn("--no-llm", data["ProgramArguments"])


# --------------------------------------------------------------------------- #
# Linux — cron line builders + crontab merge/strip (pure)
# --------------------------------------------------------------------------- #
class Cron(unittest.TestCase):
    def test_capture_line_reboot_and_marker(self):
        line = deploy.cron_capture_line()
        self.assertTrue(line.startswith("@reboot "))
        self.assertIn("-m throughlog.cli capture", line)
        self.assertTrue(line.rstrip().endswith(f"# {deploy.CAPTURE_TASK}"))

    def test_synthesis_line_schedule(self):
        line = deploy.cron_synthesis_line(time_hhmm="22:30")
        # "M H * * *" -> "30 22 * * *"
        self.assertTrue(line.startswith("30 22 * * * "))
        self.assertIn("-m throughlog.cli synthesize", line)

    def test_merge_replaces_prior_marker_line(self):
        existing = ("0 0 * * * /usr/bin/backup\n"
                    f"@reboot OLD  # {deploy.CAPTURE_TASK}\n")
        new = deploy.merge_crontab(existing, deploy.cron_capture_line(),
                                   deploy.CAPTURE_TASK)
        # user's unrelated line survives; our old line is replaced, not duplicated
        self.assertIn("/usr/bin/backup", new)
        self.assertEqual(new.count(f"# {deploy.CAPTURE_TASK}"), 1)
        self.assertNotIn("@reboot OLD", new)

    def test_strip_removes_only_our_marker(self):
        existing = ("0 0 * * * /usr/bin/backup\n"
                    f"@reboot X  # {deploy.CAPTURE_TASK}\n"
                    f"30 22 * * * Y  # {deploy.SYNTHESIS_TASK}\n")
        out = deploy.strip_crontab(existing, deploy.CAPTURE_TASK)
        self.assertIn("/usr/bin/backup", out)
        self.assertIn(f"# {deploy.SYNTHESIS_TASK}", out)
        self.assertNotIn(f"# {deploy.CAPTURE_TASK}", out)

    def test_strip_to_empty(self):
        existing = f"@reboot X  # {deploy.CAPTURE_TASK}\n"
        self.assertEqual(deploy.strip_crontab(existing, deploy.CAPTURE_TASK), "")


# --------------------------------------------------------------------------- #
# Focus probes dispatch safely on any host (no crash, safe fallback)
# --------------------------------------------------------------------------- #
class ProbeDispatch(unittest.TestCase):
    def test_idle_seconds_never_raises_and_is_nonneg(self):
        val = os_focus._idle_seconds()
        self.assertIsInstance(val, float)
        self.assertGreaterEqual(val, 0.0)

    def test_focused_window_returns_window_or_none(self):
        win = os_focus._focused_window()
        self.assertTrue(win is None or isinstance(win, os_focus.Window))

    def test_each_platform_probe_is_callable(self):
        # Calling the off-platform probes must degrade, not explode.
        for fn in (os_focus._idle_seconds_macos, os_focus._idle_seconds_x11,
                   os_focus._idle_seconds_windows):
            self.assertGreaterEqual(fn(), 0.0)
        for fn in (os_focus._focused_window_macos, os_focus._focused_window_x11):
            self.assertTrue(fn() is None or isinstance(fn(), os_focus.Window))


if __name__ == "__main__":
    unittest.main()
