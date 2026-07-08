import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.schema import FILE_CHANGE, GIT_COMMIT
from throughlog.sources.fs_git import (
    is_noise, FileChurnFilter, RawFsEvent, ActorConfig,
    classify_author, attribute_actor, make_git_commit, should_capture_diff,
    _git_diff_worktree,
)
from throughlog.privacy.diff_policy import DiffPolicy

ROOT = "C:/proj/repo"
T0 = "2026-06-21T16:00:00+03:00"


def _ts(sec: int) -> str:
    return (datetime.fromisoformat(T0) + timedelta(seconds=sec)).isoformat()


class Noise(unittest.TestCase):
    def test_office_lock(self):
        self.assertTrue(is_noise(f"{ROOT}/~$Report.docx"))

    def test_hex_temp(self):
        self.assertTrue(is_noise(f"{ROOT}/3F2A1B9C"))

    def test_backup_and_ignored_exts(self):
        self.assertTrue(is_noise(f"{ROOT}/a.bak"))
        self.assertTrue(is_noise(f"{ROOT}/x.pyc"))
        self.assertTrue(is_noise(f"{ROOT}/events.jsonl"))

    def test_noise_dirs(self):
        self.assertTrue(is_noise(f"{ROOT}/__pycache__/x.py"))
        self.assertTrue(is_noise(f"{ROOT}/node_modules/lib/index.js"))

    def test_real_file_is_not_noise(self):
        self.assertFalse(is_noise(f"{ROOT}/src/app.py"))
        self.assertFalse(is_noise(f"{ROOT}/cad/Assembly.sldasm"))


class Churn(unittest.TestCase):
    def test_repeats_coalesced(self):
        f = FileChurnFilter(coalesce_sec=2)
        evs = []
        evs += f.feed(RawFsEvent(_ts(0), f"{ROOT}/a.py"))
        evs += f.feed(RawFsEvent(_ts(1), f"{ROOT}/a.py"))   # within window -> coalesced
        evs += f.feed(RawFsEvent(_ts(300), f"{ROOT}/a.py"))  # later -> new save
        self.assertEqual([e.type for e in evs], [FILE_CHANGE, FILE_CHANGE])

    def test_noise_dropped(self):
        f = FileChurnFilter()
        self.assertEqual(f.feed(RawFsEvent(_ts(0), f"{ROOT}/~$a.docx")), [])
        self.assertEqual(f.feed(RawFsEvent(_ts(0), f"{ROOT}/3F2A1B9C")), [])


class Author(unittest.TestCase):
    CFG = ActorConfig(human_ids=("naorzadok",), agent_ids=("claude", "[bot]"))

    def test_classify(self):
        self.assertEqual(classify_author("claude[bot] <x@y>", self.CFG), "agent")
        self.assertEqual(classify_author("Naor <naorzadok@gmail.com>", self.CFG), "human")
        self.assertIsNone(classify_author("Stranger <s@s.com>", self.CFG))

    def test_priority_author_beats_everything(self):
        actor, method, _ = attribute_actor("claude[bot]", human_active=True,
                                           burst_size=1, cfg=self.CFG, burst_threshold=5)
        self.assertEqual((actor, method), ("agent", "git_author"))

    def test_human_input(self):
        actor, method, _ = attribute_actor("", human_active=True,
                                           burst_size=1, cfg=self.CFG, burst_threshold=5)
        self.assertEqual((actor, method), ("human", "input"))

    def test_machine_burst(self):
        actor, method, _ = attribute_actor("", human_active=False,
                                           burst_size=6, cfg=self.CFG, burst_threshold=5)
        self.assertEqual((actor, method), ("agent", "burst"))

    def test_no_human_default_agent(self):
        actor, method, _ = attribute_actor("", human_active=False,
                                           burst_size=1, cfg=self.CFG, burst_threshold=5)
        self.assertEqual((actor, method), ("agent", "no_human"))


class GitCommit(unittest.TestCase):
    def test_actor_from_author(self):
        cfg = ActorConfig(human_ids=("naorzadok",), agent_ids=("claude", "[bot]"))
        e = make_git_commit(ROOT, "claude[bot] <bot@x>", "gen", _ts(0), actor_config=cfg)
        self.assertEqual(e.type, GIT_COMMIT)
        self.assertEqual(e.payload["actor"], "agent")
        self.assertEqual(e.payload["repo"], ROOT)


class ShouldCaptureDiff(unittest.TestCase):
    ON = DiffPolicy(capture_diffs=True)

    def test_off_by_default(self):
        self.assertFalse(should_capture_diff("src/app.py", None))
        self.assertFalse(should_capture_diff("src/app.py", DiffPolicy()))

    def test_on_for_ordinary_file(self):
        self.assertTrue(should_capture_diff("src/app.py", self.ON))

    def test_secret_file_excluded(self):
        self.assertFalse(should_capture_diff(".env", self.ON))
        self.assertFalse(should_capture_diff("config/id_rsa", self.ON))

    def test_ignore_glob_excluded(self):
        pol = DiffPolicy(capture_diffs=True, ignore_globs=("*.sql",))
        self.assertFalse(should_capture_diff("db/schema.sql", pol))
        self.assertTrue(should_capture_diff("db/loader.py", pol))


class DiffFnWiring(unittest.TestCase):
    """The churn filter attaches a diff only when policy is on and a diff_fn is set;
    a failing/None diff_fn never drops the FILE_CHANGE event."""

    def _filter(self, diff_fn, policy):
        return FileChurnFilter(diff_fn=diff_fn, policy=policy)

    def test_diff_attached_when_on(self):
        f = self._filter(lambda p: "DIFFTEXT", DiffPolicy(capture_diffs=True))
        evs = f.feed(RawFsEvent(_ts(0), f"{ROOT}/src/app.py"))
        self.assertEqual(evs[0].payload.get("diff"), "DIFFTEXT")

    def test_no_diff_when_off(self):
        f = self._filter(lambda p: "DIFFTEXT", DiffPolicy(capture_diffs=False))
        evs = f.feed(RawFsEvent(_ts(0), f"{ROOT}/src/app.py"))
        self.assertNotIn("diff", evs[0].payload)

    def test_none_diff_no_field(self):
        f = self._filter(lambda p: None, DiffPolicy(capture_diffs=True))
        evs = f.feed(RawFsEvent(_ts(0), f"{ROOT}/src/app.py"))
        self.assertNotIn("diff", evs[0].payload)

    def test_secret_file_no_diff_fn_called(self):
        called = []
        f = self._filter(lambda p: called.append(p) or "X", DiffPolicy(capture_diffs=True))
        evs = f.feed(RawFsEvent(_ts(0), f"{ROOT}/.env"))
        self.assertNotIn("diff", evs[0].payload)
        self.assertEqual(called, [])             # short-circuited before the shell-out

    def test_diff_fn_raising_never_drops_event(self):
        def _boom(_p):
            raise RuntimeError("git exploded")
        f = self._filter(_boom, DiffPolicy(capture_diffs=True))
        evs = f.feed(RawFsEvent(_ts(0), f"{ROOT}/src/app.py"))
        self.assertEqual(len(evs), 1)            # event survives
        self.assertNotIn("diff", evs[0].payload)


class GitDiffBoundedRead(unittest.TestCase):
    """V-03 — the live git shell-out reads at most max_bytes(+1) so a multi-GB diff
    can't OOM the capture process. Verified with a fake `git` that floods stdout."""

    def test_read_is_bounded(self):
        flood = b"+" + b"A" * (5 * 1024 * 1024)   # 5 MB; far over the cap

        class _FakeStdout:
            def read(self, n): return flood[:n]   # honors the bounded n

        class _FakeProc:
            stdout = _FakeStdout()
            def kill(self): pass
            def wait(self, timeout=None): return 0

        import subprocess as _sub
        real_popen = _sub.Popen
        _sub.Popen = lambda *a, **k: _FakeProc()
        try:
            out = _git_diff_worktree("C:/repo", "big.bundle", max_bytes=4096)
        finally:
            _sub.Popen = real_popen
        self.assertIsNotNone(out)
        self.assertLessEqual(len(out.encode("utf-8")), 4096 + 1)


if __name__ == "__main__":
    unittest.main()
