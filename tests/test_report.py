import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import report


_DAILY = """\
## 2026-06-24

**throughlog** — Shipped the dashboard and the agent SDK.

**foldio** — Fixed the edge-detection crash on rotated scans.

---

## 2026-06-23

**foldio** — Drafted the Play Store listing.

---
"""

_EXEC = "# Executive Summary — 2026-06-24\n\nStrong day: dashboard + agent wedge landed.\n"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
class Parse(unittest.TestCase):
    def test_parse_daily_sections_and_order(self):
        secs = report.parse_daily(_DAILY)
        self.assertEqual([s.date for s in secs], ["2026-06-24", "2026-06-23"])
        self.assertEqual(secs[0].projects["throughlog"],
                         "Shipped the dashboard and the agent SDK.")
        self.assertEqual(secs[0].projects["foldio"],
                         "Fixed the edge-detection crash on rotated scans.")
        self.assertEqual(list(secs[1].projects), ["foldio"])

    def test_exec_body_strips_title(self):
        self.assertEqual(report.exec_summary_body(_EXEC),
                         "Strong day: dashboard + agent wedge landed.")

    def test_select_newest_specific_and_weekly(self):
        secs = report.parse_daily(_DAILY)
        self.assertEqual([s.date for s in report.select_sections(secs, date=None, weekly=False)],
                         ["2026-06-24"])
        self.assertEqual([s.date for s in report.select_sections(secs, date="2026-06-23", weekly=False)],
                         ["2026-06-23"])
        self.assertEqual(len(report.select_sections(secs, date=None, weekly=True)), 2)


# --------------------------------------------------------------------------- #
# Formatters
# --------------------------------------------------------------------------- #
class Formatters(unittest.TestCase):
    def _inputs(self, **kw):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "daily.md").write_text(_DAILY, encoding="utf-8")
            (Path(d) / "executive_summary.md").write_text(_EXEC, encoding="utf-8")
            return report.load_inputs(d, **kw)

    def test_stdout_markdown_has_projects_and_exec(self):
        out = report.standup_markdown(self._inputs())
        self.assertIn("Daily standup — 2026-06-24", out)
        self.assertIn("**throughlog** — Shipped", out)
        self.assertIn("Executive summary", out)
        self.assertIn("dashboard + agent wedge", out)

    def test_weekly_lists_each_date_no_exec(self):
        out = report.standup_markdown(self._inputs(weekly=True), weekly=True)
        self.assertIn("### 2026-06-24", out)
        self.assertIn("### 2026-06-23", out)
        self.assertNotIn("Executive summary", out)

    def test_slack_payload_converts_bold_and_bullets(self):
        payload = report.slack_payload(self._inputs())
        text = payload["text"]
        self.assertIn("*throughlog*", text)   # **x** -> *x*
        self.assertNotIn("**", text)
        self.assertIn("• ", text)                         # "- " -> "• "

    def test_github_markdown_has_footer(self):
        out = report.github_markdown(self._inputs())
        self.assertIn("ThroughLog", out)
        self.assertIn("**foldio**", out)                  # GFM bold preserved

    def test_empty_inputs_message(self):
        with tempfile.TemporaryDirectory() as d:
            out = report.standup_markdown(report.load_inputs(d))
            self.assertIn("No journal entries", out)


# --------------------------------------------------------------------------- #
# Transports (injected opener; no network)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status): self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def getcode(self): return self.status


class Transports(unittest.TestCase):
    def test_slack_post_success_and_body(self):
        seen = {}

        def _open(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return _Resp(200)

        res = report.post_slack("https://hooks.slack.test/x", {"text": "hi"}, opener=_open)
        self.assertTrue(res.ok)
        self.assertEqual(seen["body"], {"text": "hi"})

    def test_parse_github_target(self):
        self.assertEqual(report.parse_github_target("me/repo#42"), ("me/repo", "42"))
        with self.assertRaises(ValueError):
            report.parse_github_target("not-a-target")

    def test_github_comment_url_and_auth(self):
        seen = {}

        def _open(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.headers.get("Authorization")
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return _Resp(201)

        res = report.post_github_comment("me/repo#7", "the body", "tok", opener=_open)
        self.assertTrue(res.ok)
        self.assertEqual(seen["url"],
                         "https://api.github.com/repos/me/repo/issues/7/comments")
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertEqual(seen["body"], {"body": "the body"})

    def test_transport_network_failure_is_not_ok(self):
        def _open(req, timeout=None):
            raise urllib.error.URLError("refused")
        res = report.post_slack("https://x", {"text": "y"}, opener=_open)
        self.assertFalse(res.ok)
        self.assertIn("URLError", res.error)


class PeriodSummaryPreference(unittest.TestCase):
    def _journal_inputs(self, *, with_summary=True):
        d = Path(tempfile.mkdtemp(prefix="sal_report_test_"))
        (d / "daily.md").write_text(_DAILY, encoding="utf-8")
        (d / "executive_summary.md").write_text(_EXEC, encoding="utf-8")
        if with_summary:
            sdir = d / "summaries"
            sdir.mkdir()
            (sdir / "2026-W26.md").write_text(
                "# Weekly summary — 2026-W26\n\nThe week's synthesized retrospective.\n",
                encoding="utf-8")
            (sdir / "2026-06.md").write_text(
                "# Monthly summary — 2026-06\n\nThe month's synthesized retrospective.\n",
                encoding="utf-8")
        return d

    def test_weekly_prefers_synthesized_summary(self):
        d = self._journal_inputs()
        inp = report.load_inputs(d, weekly=True)
        md = report.standup_markdown(inp, weekly=True)
        self.assertIn("Work summary — 2026-W26", md)
        self.assertIn("synthesized retrospective", md)
        self.assertNotIn("Shipped the dashboard", md)        # not the daily reglue

    def test_monthly_picks_the_month_file(self):
        d = self._journal_inputs()
        inp = report.load_inputs(d, monthly=True)
        self.assertEqual(inp.period_label, "2026-06")
        md = report.standup_markdown(inp, weekly=True)
        self.assertIn("Work summary — 2026-06", md)

    def test_falls_back_to_daily_reglue_without_summary(self):
        d = self._journal_inputs(with_summary=False)
        inp = report.load_inputs(d, weekly=True)
        self.assertEqual(inp.period_summary, "")
        md = report.standup_markdown(inp, weekly=True)
        self.assertIn("Shipped the dashboard", md)           # original behavior preserved


if __name__ == "__main__":
    unittest.main()
