import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.intent.ladder import resolve_intent, IntentSignals


class Ladder(unittest.TestCase):
    def test_uia_wins_over_title(self):
        r = resolve_intent(IntentSignals(uia_value="Quarterly Plan.docx",
                                         title="Document1 - Word"))
        self.assertEqual(r.method, "uia")
        self.assertEqual(r.label, "Quarterly Plan.docx")

    def test_title_active_file(self):
        r = resolve_intent(IntentSignals(title="report.xlsx - Excel"))
        self.assertEqual(r.method, "title")
        self.assertEqual(r.label, "report.xlsx")

    def test_title_document_segment(self):
        r = resolve_intent(IntentSignals(title="Budget Q3 - Excel"))
        self.assertEqual(r.method, "title")
        self.assertEqual(r.label, "Budget Q3")

    def test_proc_cwd_attribution(self):
        # O3: generic title, no UIA -> attribute by working directory.
        r = resolve_intent(IntentSignals(title="PlainScan", process="plainscan.exe",
                                         cwd=r"C:\dev\plainscan",
                                         cmdline=r"C:\dev\plainscan\build\plainscan.exe --scan"))
        self.assertEqual(r.method, "proc_cmdline_cwd")
        self.assertEqual(r.label, "plainscan")

    def test_cmdline_path_when_no_cwd(self):
        r = resolve_intent(IntentSignals(title="App", process="app.exe",
                                         cmdline=r"C:\dev\myproj\build\app.exe --run"))
        self.assertEqual(r.method, "proc_cmdline_cwd")

    def test_bare_process_name_is_needs_review(self):
        # O3 fallback: tool name alone identifies the tool, not the work.
        r = resolve_intent(IntentSignals(title="MyTool", process="mytool.exe"))
        self.assertEqual(r.method, "needs_review")
        self.assertEqual(r.confidence, 0.0)

    def test_input_density_fallback(self):
        r = resolve_intent(IntentSignals(title="", keys=100, duration_sec=100))
        self.assertEqual(r.method, "input")
        self.assertEqual(r.label, "producing")

    def test_narration_floor(self):
        r = resolve_intent(IntentSignals(narration="debugging the privacy gate"))
        self.assertEqual(r.method, "narration")
        self.assertEqual(r.label, "debugging the privacy gate")

    def test_nothing_is_needs_review(self):
        self.assertEqual(resolve_intent(IntentSignals()).method, "needs_review")


if __name__ == "__main__":
    unittest.main()
