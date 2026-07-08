import json
import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import cli
from throughlog.schema import make_event, FILE_CHANGE, IDLE_START

PROJECTS = [
    {"id": "logger", "name": "ThroughLog", "status": "active",
     "description": "the activity logger pipeline",
     "signals": {"paths": [r"C:\Users\dev\Desktop\projects\throughlog"],
                 "git_remotes": [], "jira_prefixes": [], "keywords": [],
                 "apps": [], "domains": [], "window_patterns": []}},
]

TS = "2026-05-06T10:00:00+03:00"


def _write_log(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


class GatherEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sal_cli_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gather_reconciles_and_dedups(self):
        a = make_event(FILE_CHANGE, kind="fs", adapter="fs_git", ts_wall=TS,
                       payload={"path": "~/proj/a.py"}).to_dict()
        b = make_event(FILE_CHANGE, kind="fs", adapter="fs_git",
                       ts_wall="2026-05-06T09:00:00+03:00",
                       payload={"path": "~/proj/b.py"}).to_dict()
        log = self.tmp / "20260506.jsonl"
        _write_log(log, [a, b])
        events = cli.gather_events([log])
        self.assertEqual(len(events), 2)
        # reconcile orders by effective wall time: b (09:00) before a (10:00)
        self.assertEqual(events[0].payload["path"], "~/proj/b.py")

    def test_gather_skips_missing_files(self):
        self.assertEqual(cli.gather_events([self.tmp / "nope.jsonl"]), [])


class RunPipelineOffline(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="sal_cli_out_"))

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def test_pipeline_categorizes_and_writes_archive(self):
        # A path under the logger project -> deterministic attribution, no LLM.
        ev = make_event(FILE_CHANGE, kind="fs", adapter="fs_git", ts_wall=TS,
                        payload={"path": "~/Desktop/projects/throughlog/throughlog/x.py"})
        idle = make_event(IDLE_START, kind="os", adapter="os_focus", ts_wall=TS,
                          payload={"idle_after_sec": 700})
        res = cli.run_pipeline([ev, idle], PROJECTS, diaries_dir=self.out,
                               client=None, today="2026-05-06")
        self.assertEqual(ev.attribution.project_id, "logger")
        self.assertEqual(len(res.projects), 1)
        archive = (self.out / "project_logger" / "archive.md").read_text(encoding="utf-8")
        self.assertIn("x.py", archive)

    def test_attribution_counts(self):
        ev = make_event(FILE_CHANGE, kind="fs", adapter="fs_git", ts_wall=TS,
                        payload={"path": "~/Desktop/projects/throughlog/throughlog/x.py"})
        cli.run_pipeline([ev], PROJECTS, diaries_dir=self.out, client=None, today="2026-05-06")
        counts = cli._attribution_counts([ev])
        self.assertEqual(counts.get("signal_path"), 1)


class Parser(unittest.TestCase):
    def test_synthesize_subcommand_parses(self):
        args = cli.build_parser().parse_args(["synthesize", "--replay", "--no-llm"])
        self.assertTrue(args.replay)
        self.assertTrue(args.no_llm)
        self.assertEqual(args.command, "synthesize")

    def test_source_flags_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["synthesize", "--replay", "--date", "20260506"])


if __name__ == "__main__":
    unittest.main()
