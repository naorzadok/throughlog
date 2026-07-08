import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.schema import FOCUS_SESSION, IDLE_START, IDLE_END, DEEP_WORK
from throughlog.sources.os_focus import (
    FocusSessionizer, FocusSample, Window, extract_active_file,
)

T0 = "2026-06-21T09:00:00+03:00"


def _ts(sec: int) -> str:
    # Build an ISO timestamp `sec` seconds after T0 (same minute math, +03:00).
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(T0) + timedelta(seconds=sec)).isoformat()


def _run(sessionizer: FocusSessionizer, samples: list[FocusSample]):
    events = []
    for s in samples:
        events += sessionizer.feed(s)
    events += sessionizer.close()
    return events


EDITOR = Window("main.py - proj - Visual Studio Code", "Code.exe")
CHAT = Window("general - Slack", "slack.exe")
BROWSER = Window("Docs - Google Chrome", "chrome.exe")


class Debounce(unittest.TestCase):
    def test_rapid_switches_become_satellites_not_sessions(self):
        s = FocusSessionizer(anchor_timeout_sec=60, idle_threshold_sec=600)
        evs = _run(s, [
            FocusSample(_ts(0), EDITOR, keys=20),
            FocusSample(_ts(10), CHAT),
            FocusSample(_ts(20), BROWSER),
            FocusSample(_ts(30), EDITOR, keys=20),
            FocusSample(_ts(50), EDITOR, keys=20),
        ])
        sessions = [e for e in evs if e.type == FOCUS_SESSION]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].payload["anchor"], EDITOR.title)
        sats = {(x["title"], x["process"]) for x in sessions[0].payload["satellites"]}
        self.assertEqual(sats, {(CHAT.title, CHAT.process), (BROWSER.title, BROWSER.process)})

    def test_window_held_past_timer_promotes(self):
        s = FocusSessionizer(anchor_timeout_sec=60, idle_threshold_sec=600)
        evs = _run(s, [
            FocusSample(_ts(0), EDITOR, keys=20),
            FocusSample(_ts(10), CHAT),       # candidate starts settling at t=10
            FocusSample(_ts(40), CHAT),       # 30s held, not yet
            FocusSample(_ts(75), CHAT),       # 65s held -> promote, flush editor
            FocusSample(_ts(120), CHAT),
        ])
        sessions = [e for e in evs if e.type == FOCUS_SESSION]
        self.assertEqual([x.payload["anchor"] for x in sessions], [EDITOR.title, CHAT.title])


class ReadingVsAfk(unittest.TestCase):
    def test_low_keystroke_reading_is_kept_as_reading(self):
        s = FocusSessionizer(anchor_timeout_sec=60, idle_threshold_sec=600, kps_threshold=0.3)
        pdf = Window("paper.pdf - SumatraPDF", "SumatraPDF.exe")
        evs = _run(s, [
            FocusSample(_ts(0), pdf, idle_seconds=0, keys=0),
            FocusSample(_ts(120), pdf, idle_seconds=15, keys=1),
            FocusSample(_ts(280), pdf, idle_seconds=20, keys=0),
        ])
        sessions = [e for e in evs if e.type == FOCUS_SESSION]
        self.assertEqual(len(sessions), 1)                 # NOT dropped despite ~0 keys
        self.assertEqual(sessions[0].payload["mode"], "READING")
        self.assertFalse([e for e in evs if e.type == IDLE_START])  # never went idle

    def test_afk_trips_idle_and_ends_session(self):
        s = FocusSessionizer(anchor_timeout_sec=60, idle_threshold_sec=600)
        pdf = Window("paper.pdf - SumatraPDF", "SumatraPDF.exe")
        evs = _run(s, [
            FocusSample(_ts(0), pdf, idle_seconds=0, keys=0),
            FocusSample(_ts(900), pdf, idle_seconds=620, keys=0),  # walked away
        ])
        types = [e.type for e in evs]
        self.assertEqual(types.count(FOCUS_SESSION), 1)
        self.assertEqual(types.count(IDLE_START), 1)
        idle = next(e for e in evs if e.type == IDLE_START)
        self.assertEqual(idle.payload["idle_after_sec"], 620)
        # Session ends when input actually stopped (900 - 620 = 280s in), not at 900s.
        sess = next(e for e in evs if e.type == FOCUS_SESSION)
        self.assertEqual(sess.payload["duration_sec"], 280.0)

    def test_return_from_idle_emits_idle_end(self):
        s = FocusSessionizer(anchor_timeout_sec=60, idle_threshold_sec=600)
        pdf = Window("paper.pdf - SumatraPDF", "SumatraPDF.exe")
        evs = _run(s, [
            FocusSample(_ts(0), pdf, idle_seconds=0),
            FocusSample(_ts(900), pdf, idle_seconds=620),   # idle
            FocusSample(_ts(960), pdf, idle_seconds=0, keys=5),  # back
        ])
        self.assertEqual(len([e for e in evs if e.type == IDLE_END]), 1)


class DeepWork(unittest.TestCase):
    def test_opaque_app_with_saves_is_deep_work(self):
        # O1: CAD-style — long focus, ~0 keys, mouse + saves => DEEP_WORK, not READING.
        s = FocusSessionizer(anchor_timeout_sec=60, idle_threshold_sec=600,
                             kps_threshold=0.3, deep_work_min_sec=300,
                             mouse_active_min=30, periodic_flush_sec=86400)
        cad = Window("Robot Arm v3 - Autodesk Fusion 360", "Fusion360.exe")
        evs = _run(s, [
            FocusSample(_ts(0), cad, idle_seconds=0, keys=0, mouse=40),
            FocusSample(_ts(1800), cad, idle_seconds=20, keys=1, mouse=55, saves=1),
            FocusSample(_ts(3600), cad, idle_seconds=15, keys=0, mouse=60, saves=1),
        ])
        self.assertEqual([e.type for e in evs], [DEEP_WORK])
        self.assertEqual(evs[0].payload["mode"], "DEEP_WORK")
        self.assertEqual(evs[0].payload["saves"], 2)

    def test_mouse_only_long_session_is_deep_work(self):
        s = FocusSessionizer(kps_threshold=0.3, deep_work_min_sec=300,
                             mouse_active_min=30, idle_threshold_sec=600,
                             periodic_flush_sec=86400)
        cad = Window("Part1 - SolidWorks", "SLDWORKS.exe")
        evs = _run(s, [
            FocusSample(_ts(0), cad, mouse=40, keys=0),
            FocusSample(_ts(600), cad, mouse=60, keys=0),
        ])
        self.assertEqual([e.type for e in evs], [DEEP_WORK])


class Mode(unittest.TestCase):
    def test_high_density_is_producing(self):
        s = FocusSessionizer(anchor_timeout_sec=60, idle_threshold_sec=600, kps_threshold=0.3)
        evs = _run(s, [
            FocusSample(_ts(0), EDITOR, keys=0),
            FocusSample(_ts(100), EDITOR, keys=100),  # 100 keys / 100s = 1.0 kps
        ])
        sess = next(e for e in evs if e.type == FOCUS_SESSION)
        self.assertEqual(sess.payload["mode"], "PRODUCING")


class TitleParse(unittest.TestCase):
    def test_bare_filename(self):
        self.assertEqual(extract_active_file("report.xlsx - Excel"), "report.xlsx")

    def test_absolute_path(self):
        self.assertEqual(extract_active_file(r"C:\Users\me\doc.docx - Word"),
                         r"C:\Users\me\doc.docx")

    def test_no_file(self):
        self.assertIsNone(extract_active_file("Google Chrome"))


if __name__ == "__main__":
    unittest.main()
