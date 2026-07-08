"""Tests for the guarded settings writers (throughlog/appconfig.py).

All writes go to temp paths (the functions take explicit ``config_path`` /
``projects_path`` overrides), so these never touch the real config.json /
projects.json. Pure + offline.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import appconfig


class UpdateConfig(unittest.TestCase):
    def test_writes_only_allowed_keys_and_ignores_none(self):
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "config.json"
            cfgp.write_text(json.dumps({"llm": {"api_key": "OLD", "model": "m0"},
                                        "paths": {"data_dir": "data"}}),
                            encoding="utf-8")
            appconfig.update_llm({"model": "new/model", "api_key": None,
                                  "not_allowed": "x"}, config_path=cfgp)
            out = json.loads(cfgp.read_text(encoding="utf-8"))
            self.assertEqual(out["llm"]["model"], "new/model")
            self.assertEqual(out["llm"]["api_key"], "OLD")      # None ignored -> kept
            self.assertNotIn("not_allowed", out["llm"])         # filtered
            self.assertEqual(out["paths"]["data_dir"], "data")  # unknown section kept

    def test_seeds_from_example_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "config.json"
            self.assertFalse(cfgp.exists())
            out = appconfig.update_llm({"model": "z"}, config_path=cfgp)
            self.assertTrue(cfgp.exists())
            # the example seeds a full structure (paths/privacy), not just llm
            self.assertIn("paths", out)
            self.assertEqual(out["llm"]["model"], "z")

    def test_privacy_booleans_and_ints(self):
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "config.json"
            appconfig.update_privacy({"capture_diffs": True, "diff_max_lines": 200},
                                     config_path=cfgp)
            out = json.loads(cfgp.read_text(encoding="utf-8"))
            self.assertIs(out["privacy"]["capture_diffs"], True)
            self.assertEqual(out["privacy"]["diff_max_lines"], 200)

    def test_atomic_write_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "config.json"
            appconfig.update_llm({"model": "m"}, config_path=cfgp)
            leftovers = list(Path(d).glob("*.tmp"))
            self.assertEqual(leftovers, [])

    def test_key_is_set(self):
        self.assertTrue(appconfig.key_is_set({"llm": {"api_key": "sk-x"}}))
        self.assertFalse(appconfig.key_is_set({"llm": {"api_key": ""}}))
        # env-var fallback
        os.environ["SAL_TEST_KEY"] = "v"
        try:
            self.assertTrue(appconfig.key_is_set({"llm": {"api_key_env": "SAL_TEST_KEY"}}))
        finally:
            del os.environ["SAL_TEST_KEY"]


class AddProject(unittest.TestCase):
    def _projects(self, path):
        return json.loads(path.read_text(encoding="utf-8")).get("projects", [])

    def test_adds_inferred_entry_merge_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = root / "my-repo"
            repo.mkdir()
            (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            pjson = root / "projects.json"
            pjson.write_text(json.dumps({"projects": [
                {"id": "existing", "name": "Existing",
                 "signals": {"paths": [str(root / "other")]}}]}), encoding="utf-8")

            entry = appconfig.add_project(repo, projects_path=pjson)
            self.assertEqual(entry["id"], "my-repo")
            self.assertEqual(entry["signals"]["paths"], [str(repo)])

            after = self._projects(pjson)
            self.assertEqual(len(after), 2)                 # merge-only, kept existing
            self.assertEqual(after[0]["id"], "existing")    # existing preserved first

    def test_rejects_non_directory(self):
        with tempfile.TemporaryDirectory() as d:
            pjson = Path(d) / "projects.json"
            with self.assertRaises(ValueError):
                appconfig.add_project(Path(d) / "nope", projects_path=pjson)

    def test_rejects_already_tracked(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            pjson = Path(d) / "projects.json"
            appconfig.add_project(repo, projects_path=pjson)
            with self.assertRaises(ValueError):
                appconfig.add_project(repo, projects_path=pjson)


class UpdateSynthesisAndInit(unittest.TestCase):
    def test_synthesis_writes_only_allowed_keys(self):
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "config.json"
            cfgp.write_text(json.dumps({"relay": {"tokens": {"k": "v"}}}), encoding="utf-8")
            appconfig.update_synthesis(
                {"daily_journal": False, "journal_period": "week",
                 "summary_cadence": "monthly", "BOGUS": "x"}, config_path=cfgp)
            out = json.loads(cfgp.read_text(encoding="utf-8"))
            self.assertIs(out["synthesis"]["daily_journal"], False)
            self.assertEqual(out["synthesis"]["journal_period"], "week")
            self.assertEqual(out["synthesis"]["summary_cadence"], "monthly")
            self.assertNotIn("BOGUS", out["synthesis"])
            self.assertEqual(out["relay"], {"tokens": {"k": "v"}})   # unrelated keys kept

    def test_init_enrich_toggle_roundtrips(self):
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "config.json"
            cfgp.write_text(json.dumps({"llm": {"api_key": "K"}}), encoding="utf-8")
            appconfig.update_init({"llm_enrich": True}, config_path=cfgp)
            out = json.loads(cfgp.read_text(encoding="utf-8"))
            self.assertIs(out["init"]["llm_enrich"], True)
            self.assertEqual(out["llm"]["api_key"], "K")             # preserved
            self.assertTrue(appconfig.init_enrich_enabled(out))
            self.assertFalse(appconfig.init_enrich_enabled({}))


class AddProjectEnrich(unittest.TestCase):
    class _Client:
        def chat(self, system, user, **kw):
            return '{"description":"Enriched.","keywords":["alpha"],"jira_prefixes":["ABC"]}'

    def test_client_enriches_but_never_touches_paths(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Repo\nhello", encoding="utf-8")
            pjson = Path(d) / "projects.json"
            entry = appconfig.add_project(repo, projects_path=pjson, client=self._Client())
            self.assertEqual(entry["description"], "Enriched.")
            self.assertIn("alpha", entry["signals"]["keywords"])
            self.assertEqual(entry["signals"]["jira_prefixes"], ["ABC"])
            self.assertEqual(entry["signals"]["paths"], [str(repo)])  # path stays deterministic


class UpdateSchedule(unittest.TestCase):
    def test_set_and_clear_nightly_time(self):
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "config.json"
            cfgp.write_text(json.dumps({"llm": {"api_key": "K"}}), encoding="utf-8")
            appconfig.update_schedule("22:30", config_path=cfgp)
            out = json.loads(cfgp.read_text(encoding="utf-8"))
            self.assertEqual(out["schedule"]["synthesize_at"], "22:30")
            self.assertEqual(out["llm"]["api_key"], "K")        # other keys preserved
            self.assertEqual(appconfig.nightly_time(out), "22:30")

            appconfig.update_schedule(None, config_path=cfgp)   # clear
            out2 = json.loads(cfgp.read_text(encoding="utf-8"))
            self.assertNotIn("synthesize_at", out2.get("schedule", {}))
            self.assertIsNone(appconfig.nightly_time(out2))

    def test_nightly_time_handles_missing(self):
        self.assertIsNone(appconfig.nightly_time({}))
        self.assertIsNone(appconfig.nightly_time({"schedule": {"synthesize_at": ""}}))


class ScanProjects(unittest.TestCase):
    def _make_repo(self, parent: Path, name: str) -> Path:
        repo = parent / name
        (repo / ".git").mkdir(parents=True)        # find_git_repos keys on .git
        return repo

    def test_scan_previews_new_repos_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "code"
            self._make_repo(root, "alpha")
            self._make_repo(root, "beta")
            pjson = Path(d) / "projects.json"
            pjson.write_text(json.dumps({"projects": []}), encoding="utf-8")
            found = appconfig.scan_projects(root, projects_path=pjson)
            self.assertEqual(len(found), 2)
            # preview only: nothing written
            self.assertEqual(json.loads(pjson.read_text())["projects"], [])

    def test_scan_skips_already_tracked(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "code"
            alpha = self._make_repo(root, "alpha")
            self._make_repo(root, "beta")
            pjson = Path(d) / "projects.json"
            pjson.write_text(json.dumps({"projects": [
                {"id": "alpha", "signals": {"paths": [str(alpha.resolve())]}}]}),
                encoding="utf-8")
            found = appconfig.scan_projects(root, projects_path=pjson)
            self.assertEqual([e["id"] for e in found], ["beta"])

    def test_add_scanned_merges_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "code"
            self._make_repo(root, "alpha")
            self._make_repo(root, "beta")
            pjson = Path(d) / "projects.json"
            pjson.write_text(json.dumps({"projects": [
                {"id": "keep", "signals": {"paths": [str(Path(d) / "elsewhere")]}}]}),
                encoding="utf-8")
            added = appconfig.add_scanned_projects(root, projects_path=pjson)
            self.assertEqual(len(added), 2)
            after = json.loads(pjson.read_text())["projects"]
            self.assertEqual(after[0]["id"], "keep")           # existing preserved first
            self.assertEqual(len(after), 3)

    def test_scan_rejects_non_directory(self):
        with self.assertRaises(ValueError):
            appconfig.scan_projects(Path(tempfile.gettempdir()) / "sal_nope_xyz")


class AllowlistDelta(unittest.TestCase):
    def test_new_folder_is_widening(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            pjson = Path(d) / "projects.json"
            pjson.write_text(json.dumps({"projects": []}), encoding="utf-8")
            delta = appconfig.allowlist_delta(repo, projects_path=pjson)
            self.assertEqual(len(delta), 1)

    def test_already_tracked_folder_no_widening(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            pjson = Path(d) / "projects.json"
            pjson.write_text(json.dumps({"projects": [
                {"id": "r", "signals": {"paths": [str(repo)]}}]}), encoding="utf-8")
            self.assertEqual(appconfig.allowlist_delta(repo, projects_path=pjson), [])


if __name__ == "__main__":
    unittest.main()
