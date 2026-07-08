import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.schema import (
    make_event,
    FOCUS_SESSION, GIT_COMMIT, FILE_CHANGE, NARRATION, CLIPBOARD, IDLE_START,
    AGENT_REPORT,
)
from throughlog.llm.client import LLMError
from throughlog.llm.prompts import DAILY_SEP, ENTRY_SYSTEM, PERIOD_SUMMARY_SYSTEM
from throughlog import synthesize as syn
from throughlog.synthesize import SynthesisOptions

PROJECTS = [
    {"id": "logger", "name": "ThroughLog",
     "description": "the activity logger pipeline"},
    {"id": "shoes", "name": "Training Shoes Research",
     "description": "training shoe research"},
]

TS = "2026-06-21T10:00:00+03:00"


def attributed(ev, pid, method="signal_path", conf=0.95):
    ev.attribution.project_id = pid
    ev.attribution.confidence = conf
    ev.attribution.method = method
    return ev


def focus(anchor, *, pid, process="Code.exe", active_file=None, intent_label="",
          duration=1800, mode="producing", ts=TS):
    payload = {"anchor": anchor, "process": process, "active_file": active_file,
               "satellites": [], "duration_sec": duration, "mode": mode}
    if intent_label:
        payload["intent"] = {"label": intent_label, "method": "title", "confidence": 0.8}
    return attributed(make_event(FOCUS_SESSION, kind="os", adapter="os_focus",
                                 payload=payload, ts_wall=ts), pid)


# --------------------------------------------------------------------------- #
class FakeClient:
    """Returns a canned reply (or raises) for every chat() call; records count."""
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0
        self.sent = []

    def chat(self, system, user, **kw):
        self.calls += 1
        self.sent.append((system, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _overview_reply(body="Updated body.", daily="Did the thing today."):
    return (
        "# ThroughLog\n"
        "**Status:** active | **Last Updated:** 2026-06-21\n\n"
        "## Current State\n" + body + "\n\n"
        "## Ongoing Threads\n- M8 — in progress\n\n"
        "## Chronological Narrative\nWork happened and connected to prior work.\n\n"
        "## Key Artifacts\n- throughlog/synthesize.py\n"
        f"{DAILY_SEP}\n{daily}"
    )


class TieredFake:
    """Routes by system message: the ENTRY_SYSTEM call returns a detailed entry, any
    other call (overview/exec/chunk) returns a canned overview reply. Records all prompts."""
    def __init__(self, entry_reply, overview_reply=None, fail_entry=False):
        self.entry_reply = entry_reply
        self.overview_reply = overview_reply or _overview_reply()
        self.fail_entry = fail_entry
        self.entry_calls = 0
        self.overview_calls = 0
        self.sent = []

    def chat(self, system, user, **kw):
        self.sent.append((system, user))
        if system == ENTRY_SYSTEM:
            self.entry_calls += 1
            if self.fail_entry:
                raise LLMError("entry model down")
            return self.entry_reply
        self.overview_calls += 1
        return self.overview_reply

    def overview_user(self):
        """The user prompt of the first non-entry (living-doc) call."""
        for system, user in self.sent:
            if system != ENTRY_SYSTEM:
                return user
        return ""


# --------------------------------------------------------------------------- #
class Grouping(unittest.TestCase):
    def test_groups_by_project_and_skips_unattributed(self):
        evs = [
            focus("a", pid="logger"),
            focus("b", pid="shoes"),
        ]
        review = make_event(FOCUS_SESSION, kind="os", adapter="os_focus",
                            payload={"anchor": "mystery"}, ts_wall=TS)  # project_id None
        groups = syn.group_by_project(evs + [review])
        self.assertEqual(set(groups), {"logger", "shoes"})
        self.assertEqual(len(groups["logger"]), 1)

    def test_unrelated_sentinel_skipped(self):
        ev = focus("x", pid="logger")
        ev.attribution.project_id = "__unrelated__"
        self.assertEqual(syn.group_by_project([ev]), {})


# --------------------------------------------------------------------------- #
class DeterministicArchive(unittest.TestCase):
    def test_archive_has_sections_commits_files_narration(self):
        events = [
            focus("editor", pid="logger", active_file="throughlog/x.py",
                  intent_label="writing the synthesizer"),
            attributed(make_event(GIT_COMMIT, kind="git", adapter="fs_git", ts_wall=TS,
                                  payload={"repo": "throughlog", "actor": "human",
                                           "message": "feat: M8", "files": ["throughlog/synthesize.py"]}),
                       "logger"),
            attributed(make_event(FILE_CHANGE, kind="fs", adapter="fs_git", ts_wall=TS,
                                  payload={"path": "~/proj/throughlog/bus.py"}), "logger"),
            attributed(make_event(NARRATION, kind="intent", adapter="intent_bridge", ts_wall=TS,
                                  payload={"note": "deterministic archive first"}), "logger"),
        ]
        section = syn.build_archive_section("2026-06-21", events)
        self.assertIn("## 2026-06-21", section)
        self.assertIn("### Sessions", section)
        self.assertIn("### Commits", section)
        self.assertIn("feat: M8", section)
        self.assertIn("### Files changed", section)
        self.assertIn("synthesize.py", section)        # from commit files
        self.assertIn("bus.py", section)                # from FILE_CHANGE basename
        self.assertIn("### Narration", section)
        self.assertIn("deterministic archive first", section)

    def test_archive_renders_agent_activity(self):
        # "what my agents did" must survive in the deterministic (LLM-free) record.
        agent = attributed(make_event(
            AGENT_REPORT, kind="agent", adapter="agent_ingest", ts_wall=TS,
            identity="agent:claude-code",
            payload={"repo": "github.com/acme/checkout", "tool": "claude-code",
                     "summary": "opened PR #482 with rounding fix",
                     "files": ["src/checkout/currency.ts"]}), "logger")
        section = syn.build_archive_section("2026-06-21", [agent])
        self.assertIn("### Agent activity", section)
        self.assertIn("agent:claude-code", section)
        self.assertIn("opened PR #482", section)
        self.assertIn("currency.ts", section)            # agent files folded in

    def test_clipboard_counted_not_dumped(self):
        clip = attributed(make_event(CLIPBOARD, kind="intent", adapter="intent_bridge",
                                    ts_wall=TS, payload={"kind": "url", "host": "github.com"}),
                          "logger")
        section = syn.build_archive_section("2026-06-21", [clip])
        self.assertIn("### Clipboard captures: 1", section)

    def test_summarize_event_idle_is_empty(self):
        idle = make_event(IDLE_START, kind="os", adapter="os_focus", ts_wall=TS,
                          payload={"idle_after_sec": 700})
        self.assertEqual(syn.summarize_event(idle), "")


# --------------------------------------------------------------------------- #
class ProjectSynthesis(unittest.TestCase):
    def test_overview_split_on_separator(self):
        ev = focus("editor", pid="logger")
        client = FakeClient(_overview_reply(daily="Built the overview writer."))
        pd = syn.synthesize_project(PROJECTS[0], [ev], "old overview",
                                    today="2026-06-21", client=client)
        self.assertEqual(client.calls, 1)
        self.assertIn("## Current State", pd.overview_md)
        self.assertEqual(pd.daily_paragraph, "Built the overview writer.")
        self.assertIsNone(pd.error)
        self.assertTrue(pd.archive_section.startswith("---"))

    def test_no_separator_still_writes_overview(self):
        ev = focus("editor", pid="logger")
        client = FakeClient("# Logger\n## Current State\nJust the overview, no separator.\n")
        pd = syn.synthesize_project(PROJECTS[0], [ev], "old", today="2026-06-21", client=client)
        self.assertIn("no separator", pd.overview_md)
        self.assertEqual(pd.daily_paragraph, "")

    def test_llm_failure_preserves_overview_and_archive(self):
        ev = focus("editor", pid="logger")
        client = FakeClient(LLMError("openrouter down"))
        pd = syn.synthesize_project(PROJECTS[0], [ev], "PRIOR OVERVIEW TEXT",
                                    today="2026-06-21", client=client)
        self.assertEqual(pd.overview_md, "PRIOR OVERVIEW TEXT")       # unchanged
        self.assertIn("### Sessions", pd.archive_section)        # archive still built
        self.assertIsNotNone(pd.error)

    def test_no_client_builds_archive_only(self):
        ev = focus("editor", pid="logger")
        pd = syn.synthesize_project(PROJECTS[0], [ev], "stub", today="2026-06-21", client=None)
        self.assertEqual(pd.llm_calls, 0)
        self.assertIn("### Sessions", pd.archive_section)


# --------------------------------------------------------------------------- #
class ExecSummary(unittest.TestCase):
    def test_llm_exec_summary(self):
        client = FakeClient("Solid day across two projects.\n- logger: shipped M8\n")
        body, err = syn.synthesize_exec_summary(
            {"logger": "Shipped M8.", "shoes": "Read reviews."}, "2026-06-21", client=client)
        self.assertIn("Solid day", body)
        self.assertIsNone(err)
        self.assertEqual(client.calls, 1)

    def test_exec_summary_falls_back_on_failure(self):
        client = FakeClient(LLMError("down"))
        body, err = syn.synthesize_exec_summary(
            {"logger": "Shipped M8."}, "2026-06-21", client=client)
        self.assertIn("Shipped M8.", body)      # deterministic fallback content
        self.assertIsNotNone(err)

    def test_empty_paragraphs_no_summary(self):
        body, err = syn.synthesize_exec_summary({}, "2026-06-21", client=None)
        self.assertEqual(body, "")
        self.assertIsNone(err)


# --------------------------------------------------------------------------- #
class RunDriver(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="sal_synth_test_"))

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def test_run_writes_all_artifacts(self):
        events = [
            focus("logger editor", pid="logger", active_file="throughlog/synthesize.py"),
            focus("shoe review", pid="shoes", process="EXCEL.EXE"),
        ]
        client = FakeClient(_overview_reply(daily="Daily paragraph here."))
        res = syn.run(events, PROJECTS, journal_dir=self.out, client=client, today="2026-06-21")

        self.assertEqual(len(res.projects), 2)
        for pid in ("logger", "shoes"):
            self.assertTrue((self.out / f"project_{pid}" / "overview.md").exists())
            self.assertTrue((self.out / f"project_{pid}" / "archive.md").exists())
        daily = (self.out / "daily.md").read_text(encoding="utf-8")
        self.assertIn("## 2026-06-21", daily)
        self.assertIn("**logger**", daily)
        exec_doc = (self.out / "executive_summary.md").read_text(encoding="utf-8")
        self.assertIn("Executive Summary", exec_doc)

    def test_run_archive_appends_across_calls(self):
        ev1 = focus("day one", pid="logger", ts="2026-06-21T10:00:00+03:00")
        ev2 = focus("day two", pid="logger", ts="2026-06-22T10:00:00+03:00")
        client = FakeClient(_overview_reply())
        syn.run([ev1], PROJECTS, journal_dir=self.out, client=client, today="2026-06-21")
        syn.run([ev2], PROJECTS, journal_dir=self.out, client=client, today="2026-06-22")
        archive = (self.out / "project_logger" / "archive.md").read_text(encoding="utf-8")
        self.assertIn("## 2026-06-21", archive)
        self.assertIn("## 2026-06-22", archive)

    def test_run_offline_archive_and_deterministic_summary(self):
        # No client: archive + no overview rewrite (stub stays), no daily -> no exec doc.
        events = [focus("logger editor", pid="logger")]
        res = syn.run(events, PROJECTS, journal_dir=self.out, client=None, today="2026-06-21")
        archive = (self.out / "project_logger" / "archive.md").read_text(encoding="utf-8")
        self.assertIn("### Sessions", archive)
        self.assertEqual(res.exec_summary, "")          # no daily paragraphs -> no exec
        overview = (self.out / "project_logger" / "overview.md").read_text(encoding="utf-8")
        self.assertIn("ThroughLog", overview)


# --------------------------------------------------------------------------- #
class DetailedEntries(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="tl_entries_test_"))
        self.proj = {"id": "logger", "name": "ThroughLog",
                     "description": "the pipeline",
                     "signals": {"entry_extract": ["params and values tried"]}}

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def _entries_dir(self, pid="logger"):
        return self.out / f"project_{pid}" / "entries"

    def test_off_by_default_writes_no_entries(self):
        # The library default (no options) is OFF -> byte-identical to before the feature.
        ev = focus("editor", pid="logger")
        client = FakeClient(_overview_reply())
        pd = syn.synthesize_project(PROJECTS[0], [ev], "old", today="2026-06-21",
                                    client=client)
        self.assertEqual(client.calls, 1)               # only the overview call, no entries
        self.assertEqual(pd.entries_by_period, {})
        syn.run([ev], PROJECTS, journal_dir=self.out, client=client, today="2026-06-21")
        self.assertFalse(self._entries_dir().exists())

    def test_entries_on_writes_month_file_with_specifics(self):
        ev = focus("tuning", pid="logger", ts="2026-06-21T10:00:00+03:00")
        client = TieredFake("Tried damping=0.1, 0.2; best RMSE 0.044 at 0.2.")
        syn.run([ev], [self.proj], journal_dir=self.out, client=client,
                today="2026-06-21", options=SynthesisOptions(write_entries=True))
        jfile = self._entries_dir() / "2026-06.md"
        self.assertTrue(jfile.exists())
        text = jfile.read_text(encoding="utf-8")
        self.assertIn("## 2026-06-21", text)
        self.assertIn("damping=0.1", text)              # specifics preserved
        self.assertIn("RMSE 0.044", text)
        self.assertEqual(client.entry_calls, 1)

    def test_living_doc_is_fed_entries_and_told_high_level(self):
        ev = focus("tuning", pid="logger")
        client = TieredFake("Tried damping=0.1, 0.2.")
        syn.synthesize_project(self.proj, [ev], "old", today="2026-06-21",
                               client=client, options=SynthesisOptions(write_entries=True))
        overview_user = client.overview_user()
        self.assertIn("damping=0.1", overview_user)         # fed the entry text
        self.assertIn("STAY HIGH-LEVEL", overview_user)     # rollup directive present

    def test_entries_idempotent_per_day(self):
        opts = SynthesisOptions(write_entries=True)
        c1 = TieredFake("First entry for the day.")
        syn.run([focus("a", pid="logger", ts="2026-06-21T10:00:00+03:00")],
                [self.proj], journal_dir=self.out, client=c1, today="2026-06-21", options=opts)
        c2 = TieredFake("Revised entry for the same day.")
        syn.run([focus("a redux", pid="logger", ts="2026-06-21T11:00:00+03:00")],
                [self.proj], journal_dir=self.out, client=c2, today="2026-06-21", options=opts)
        text = (self._entries_dir() / "2026-06.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("## 2026-06-21"), 1)     # replaced, not duplicated
        self.assertIn("Revised entry", text)
        self.assertNotIn("First entry", text)

    def test_entries_routes_to_month_files(self):
        opts = SynthesisOptions(write_entries=True)
        events = [focus("june", pid="logger", ts="2026-06-30T10:00:00+03:00"),
                  focus("july", pid="logger", ts="2026-07-01T10:00:00+03:00")]
        syn.run(events, [self.proj], journal_dir=self.out,
                client=TieredFake("entry"), today="2026-07-01", options=opts)
        months = sorted(p.name for p in self._entries_dir().glob("*.md"))
        self.assertEqual(months, ["2026-06.md", "2026-07.md"])

    def test_entries_failure_falls_back_to_archive_section(self):
        ev = focus("editor", pid="logger", active_file="throughlog/x.py")
        client = TieredFake("unused", fail_entry=True)
        pd = syn.synthesize_project(self.proj, [ev], "PRIOR", today="2026-06-21",
                                    client=client, options=SynthesisOptions(write_entries=True))
        entry = pd.entries_by_period["2026-06"]
        self.assertIn("### Sessions", entry)             # deterministic archive fallback
        self.assertIsNotNone(pd.entry_error)
        self.assertIn("### Sessions", pd.archive_section)  # archive still built
        self.assertNotEqual(pd.overview_md, "PRIOR")          # overview call still ran


class PeriodFake:
    """Routes by system message: entries, period-summary, or overview replies. Records
    the period-summary user prompt and can be told to fail the period call."""
    def __init__(self, entry_reply="entry", period_reply="WEEKLY ROLLUP",
                 fail_period=False):
        self.entry_reply = entry_reply
        self.period_reply = period_reply
        self.fail_period = fail_period
        self.overview_reply = _overview_reply()
        self.period_calls = 0
        self.period_user = ""

    def chat(self, system, user, **kw):
        if system == ENTRY_SYSTEM:
            return self.entry_reply
        if system == PERIOD_SUMMARY_SYSTEM:
            self.period_calls += 1
            self.period_user = user
            if self.fail_period:
                raise LLMError("period model down")
            return self.period_reply
        return self.overview_reply


class PeriodKey(unittest.TestCase):
    def test_month_and_week(self):
        self.assertEqual(syn._period_key("20260621", "month"), "2026-06")
        self.assertEqual(syn._period_key("20260621", "week"), "2026-W25")
        self.assertEqual(syn._period_key("20260622", "week"), "2026-W26")  # Monday -> next week

    def test_junk(self):
        self.assertEqual(syn._period_key("nope", "month"), "0000-00")
        self.assertEqual(syn._period_key("nope", "week"), "0000-W00")


class PeriodSummary(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="sal_period_test_"))
        self.proj = {"id": "logger", "name": "ThroughLog", "description": "the pipeline"}

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def _summaries(self):
        sdir = self.out / "summaries"
        return sorted(p.name for p in sdir.glob("*.md")) if sdir.is_dir() else []

    def test_off_by_default_writes_no_summaries(self):
        # Function-level default summary_cadence='off' -> byte-identical (no summaries dir).
        ev = focus("a", pid="logger")
        syn.run([ev], [self.proj], journal_dir=self.out,
                client=PeriodFake(), today="2026-06-21",
                options=SynthesisOptions(write_entries=True))
        self.assertFalse((self.out / "summaries").exists())

    def test_entries_routes_to_week_files(self):
        opts = SynthesisOptions(write_entries=True, entry_period="week")
        events = [focus("sun", pid="logger", ts="2026-06-21T10:00:00+03:00"),   # W25
                  focus("mon", pid="logger", ts="2026-06-22T10:00:00+03:00")]   # W26
        syn.run(events, [self.proj], journal_dir=self.out,
                client=PeriodFake(), today="2026-06-22", options=opts)
        files = sorted(p.name for p in (self.out / "project_logger" / "entries").glob("*.md"))
        self.assertEqual(files, ["2026-W25.md", "2026-W26.md"])

    def test_weekly_summary_written_and_idempotent(self):
        opts = SynthesisOptions(write_entries=True, summary_cadence="weekly")
        c1 = PeriodFake(period_reply="First rollup")
        syn.run([focus("a", pid="logger", ts="2026-06-21T10:00:00+03:00")],
                [self.proj], journal_dir=self.out, client=c1, today="2026-06-21", options=opts)
        self.assertEqual(self._summaries(), ["2026-W25.md"])
        text = (self.out / "summaries" / "2026-W25.md").read_text(encoding="utf-8")
        self.assertIn("Weekly summary — 2026-W25", text)
        self.assertIn("First rollup", text)
        # Re-running the same week replaces (overwrites) the summary, never appends.
        c2 = PeriodFake(period_reply="Second rollup")
        syn.run([focus("a redux", pid="logger", ts="2026-06-21T11:00:00+03:00")],
                [self.proj], journal_dir=self.out, client=c2, today="2026-06-21", options=opts)
        text2 = (self.out / "summaries" / "2026-W25.md").read_text(encoding="utf-8")
        self.assertIn("Second rollup", text2)
        self.assertNotIn("First rollup", text2)

    def test_summary_fed_already_gated_entries_sections(self):
        opts = SynthesisOptions(write_entries=True, summary_cadence="weekly")
        c = PeriodFake(entry_reply="Tried damping=0.2; RMSE 0.044.")
        syn.run([focus("tuning", pid="logger", ts="2026-06-21T10:00:00+03:00")],
                [self.proj], journal_dir=self.out, client=c, today="2026-06-21", options=opts)
        self.assertEqual(c.period_calls, 1)
        self.assertIn("damping=0.2", c.period_user)        # the entry feeds the summary
        self.assertIn("logger", c.period_user)

    def test_summary_llm_failure_falls_back_to_concat(self):
        opts = SynthesisOptions(write_entries=True, summary_cadence="weekly")
        c = PeriodFake(entry_reply="Detailed entries body here.", fail_period=True)
        res = syn.run([focus("a", pid="logger", ts="2026-06-21T10:00:00+03:00")],
                      [self.proj], journal_dir=self.out, client=c, today="2026-06-21",
                      options=opts)
        text = (self.out / "summaries" / "2026-W25.md").read_text(encoding="utf-8")
        self.assertIn("## logger", text)                   # deterministic concat fallback
        self.assertIn("Detailed entries body here.", text)
        self.assertIsNotNone(res.summary_error)

    def test_summarize_period_falls_back_to_archive_without_entries(self):
        # No entries: the summary still works off the deterministic archive sections.
        syn.run([focus("editor", pid="logger", active_file="x.py",
                        ts="2026-06-21T10:00:00+03:00")],
                [self.proj], journal_dir=self.out, client=None, today="2026-06-21")
        body, err = syn.summarize_period(self.out, "2026-W25", "week", client=None)
        self.assertIn("## logger", body)
        self.assertIn("### Sessions", body)                # archive section content
        self.assertIsNone(err)

    def test_summarize_period_empty_when_no_activity(self):
        body, err = syn.summarize_period(self.out, "2026-W01", "week", client=None)
        self.assertEqual(body, "")
        self.assertIsNone(err)


class Helpers(unittest.TestCase):
    def test_basename_handles_both_separators(self):
        self.assertEqual(syn._basename(r"C:\a\b\c.py"), "c.py")
        self.assertEqual(syn._basename("~/a/b/c.py"), "c.py")

    def test_date_label(self):
        self.assertEqual(syn._date_label("20260621"), "2026-06-21")
        self.assertEqual(syn._date_label("weird"), "weird")


if __name__ == "__main__":
    unittest.main()
