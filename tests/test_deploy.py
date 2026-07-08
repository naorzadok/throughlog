import os
import sys
import unittest
from datetime import date
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import deploy
from throughlog.tray import status_line, menu_label


class TaskXml(unittest.TestCase):
    def test_xml_is_well_formed(self):
        xml = deploy.capture_task_xml()
        ET.fromstring(xml)   # raises on malformed XML

    def test_capture_xml_runs_capture(self):
        xml = deploy.capture_task_xml()
        self.assertIn("-m throughlog.cli capture", xml)
        self.assertIn("<LogonTrigger>", xml)
        self.assertIn("PT0S", xml)              # no execution time limit

    def test_capture_tray_variant(self):
        xml = deploy.capture_task_xml(tray=True)
        self.assertIn("-m throughlog.cli tray", xml)
        self.assertNotIn("-m throughlog.cli capture", xml)

    def test_capture_flags_appended(self):
        xml = deploy.capture_task_xml(no_clipboard=True, no_agents=True)
        self.assertIn("-m throughlog.cli capture --no-clipboard --no-agents", xml)

    def test_synthesis_daily_trigger(self):
        xml = deploy.synthesis_task_xml(time_hhmm="22:30", start_day=date(2026, 1, 2))
        self.assertIn("-m throughlog.cli synthesize", xml)
        self.assertIn("<CalendarTrigger>", xml)
        self.assertIn("2026-01-02T22:30:00", xml)
        self.assertIn("<DaysInterval>1</DaysInterval>", xml)

    def test_synthesis_no_llm_flag(self):
        xml = deploy.synthesis_task_xml(time_hhmm="07:15", no_llm=True,
                                        start_day=date(2026, 1, 2))
        self.assertIn("-m throughlog.cli synthesize --no-llm", xml)

    def test_description_is_escaped(self):
        xml = deploy.build_task_xml(
            command="py.exe", arguments="-m x", workdir="C:/r",
            description="run A & B <fast>", trigger_xml=deploy._logon_trigger())
        self.assertIn("run A &amp; B &lt;fast&gt;", xml)

    def test_command_points_at_a_python(self):
        cmd = deploy.python_exe()
        self.assertTrue(cmd.lower().endswith("python.exe") or "python" in cmd.lower())


class StatusLine(unittest.TestCase):
    def test_recording(self):
        s = status_line({"paused": False, "threads_alive": 3,
                         "sources": ["a", "b", "c"], "stats": {"written": 42}})
        self.assertIn("recording", s)
        self.assertIn("42 events", s)
        self.assertIn("3/3 sources", s)

    def test_paused(self):
        s = status_line({"paused": True, "threads_alive": 0,
                         "sources": ["a"], "stats": {"written": 0}})
        self.assertIn("paused", s)
        self.assertIn("0 events", s)


class MenuLabel(unittest.TestCase):
    def test_appends_visible_shortcut_hint(self):
        # Inline parenthetical, not a \t accelerator — pystray's Win32 menu ignores
        # the tab convention, so the hint must be plain text that always renders.
        out = menu_label("Pause capture", "Ctrl+Shift+P")
        self.assertEqual(out, "Pause capture  (Ctrl+Shift+P)")
        self.assertNotIn("\t", out)

    def test_no_shortcut_leaves_label_bare(self):
        # When the hotkey didn't register we must NOT advertise a shortcut.
        self.assertEqual(menu_label("Whisper note…", None), "Whisper note…")
        self.assertEqual(menu_label("Whisper note…", ""), "Whisper note…")


if __name__ == "__main__":
    unittest.main()
