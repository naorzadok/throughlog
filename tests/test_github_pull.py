import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.sources import github_pull as gh
from throughlog.schema import AGENT_REPORT
from throughlog.categorize import categorize_events


# --------------------------------------------------------------------------- #
# Authorship classification
# --------------------------------------------------------------------------- #
class Authorship(unittest.TestCase):
    def test_bot_by_type(self):
        actor, kind, ident = gh.author_identity({"login": "dependabot", "type": "Bot"})
        self.assertEqual((actor, kind), ("agent", "agent"))
        self.assertEqual(ident, "agent:dependabot")

    def test_bot_by_login_suffix(self):
        self.assertTrue(gh.is_bot({"login": "claude[bot]", "type": "User"}))

    def test_known_agent_login(self):
        actor, kind, _ = gh.author_identity({"login": "copilot", "type": "User"})
        self.assertEqual((actor, kind), ("agent", "agent"))

    def test_human(self):
        actor, kind, ident = gh.author_identity({"login": "alice", "type": "User"})
        self.assertEqual((actor, kind), ("human", "remote"))
        self.assertEqual(ident, "github:alice")


# --------------------------------------------------------------------------- #
# Transformers
# --------------------------------------------------------------------------- #
class Transformers(unittest.TestCase):
    def test_commit_human(self):
        ev = gh.commit_to_event({
            "sha": "deadbeef1234",
            "commit": {"author": {"name": "Alice", "date": "2026-06-21T08:00:00+00:00"},
                       "message": "Fix bug\n\nbody"},
            "author": {"login": "alice", "type": "User"},
            "files": [{"filename": "src/a.py"}],
        }, "github.com/acme/api")
        self.assertEqual(ev.type, AGENT_REPORT)
        self.assertEqual(ev.source.kind, "remote")
        self.assertEqual(ev.source.identity, "github:alice")
        self.assertEqual(ev.payload["actor"], "human")
        self.assertEqual(ev.ts_wall, "2026-06-21T08:00:00+00:00")
        self.assertIn("commit deadbee: Fix bug", ev.payload["summary"])
        self.assertEqual(ev.payload["files"], ["src/a.py"])

    def test_commit_bot(self):
        ev = gh.commit_to_event({
            "sha": "f00",
            "commit": {"author": {"name": "x", "date": "2026-06-21T08:00:00+00:00"},
                       "message": "chore: bump"},
            "author": {"login": "renovate[bot]", "type": "Bot"},
        }, "github.com/acme/api")
        self.assertEqual(ev.source.kind, "agent")
        self.assertEqual(ev.payload["actor"], "agent")

    def test_pull_request_bot_opened(self):
        ev = gh.pull_request_to_event({
            "number": 7, "title": "Add retry", "state": "open",
            "user": {"login": "claude[bot]", "type": "Bot"},
            "created_at": "2026-06-21T09:00:00+00:00",
            "updated_at": "2026-06-21T09:00:00+00:00",
        }, "github.com/acme/api")
        self.assertEqual(ev.source.kind, "agent")
        self.assertEqual(ev.payload["summary"], "opened PR #7: Add retry")
        self.assertEqual(ev.payload["repo"], "github.com/acme/api")

    def test_pull_request_merged_verb_and_ts(self):
        ev = gh.pull_request_to_event({
            "number": 9, "title": "Done", "state": "closed",
            "merged_at": "2026-06-22T10:00:00+00:00",
            "user": {"login": "bob", "type": "User"},
        }, "github.com/acme/api")
        self.assertIn("merged PR #9", ev.payload["summary"])
        self.assertEqual(ev.ts_wall, "2026-06-22T10:00:00+00:00")

    def test_workflow_run(self):
        ev = gh.workflow_run_to_event({
            "name": "CI", "conclusion": "success",
            "updated_at": "2026-06-21T09:10:00+00:00",
        }, "github.com/acme/api")
        self.assertEqual(ev.source.kind, "remote")
        self.assertEqual(ev.source.identity, "ci:CI")
        self.assertIn("CI CI: success", ev.payload["summary"])


# --------------------------------------------------------------------------- #
# Selection + attribution
# --------------------------------------------------------------------------- #
class SelectionAndAttribution(unittest.TestCase):
    def test_tracked_remotes_and_owner_repo(self):
        projects = [{"id": "p1", "signals": {"git_remotes": ["github.com/acme/api.git"]}},
                    {"id": "p2", "signals": {"git_remotes": ["gitlab.com/x/y"]}}]
        tr = gh.tracked_remotes(projects)
        self.assertEqual(tr["github.com/acme/api"], "p1")     # .git stripped
        self.assertEqual(gh.owner_repo("github.com/acme/api"), "acme/api")
        self.assertIsNone(gh.owner_repo("gitlab.com/x/y"))    # non-GitHub

    def test_pulled_pr_attributes_to_project(self):
        ev = gh.pull_request_to_event({
            "number": 3, "title": "x", "state": "open",
            "user": {"login": "claude[bot]", "type": "Bot"},
            "created_at": "2026-06-21T09:00:00+00:00",
        }, "github.com/acme/api")
        projects = [{"id": "acme-api", "status": "active",
                     "signals": {"git_remotes": ["github.com/acme/api"]}}]
        categorize_events([ev], projects)
        self.assertEqual(ev.attribution.project_id, "acme-api")
        self.assertEqual(ev.attribution.method, "signal_git")


# --------------------------------------------------------------------------- #
# Live driver — injected fetch, dedup across passes
# --------------------------------------------------------------------------- #
class _Collector:
    def __init__(self): self.events = []
    def emit(self, ev): self.events.append(ev); return True


def _fetch(commits=None, pulls=None):
    def _f(url, token, **kw):
        if "/commits" in url:
            return commits or []
        if "/pulls" in url:
            return pulls or []
        return []
    return _f


class LiveDriver(unittest.TestCase):
    def test_pull_once_emits_and_dedups(self):
        commits = [{"sha": "s1", "commit": {"author": {"name": "a", "date": "2026-06-21T08:00:00+00:00"},
                                            "message": "m1"}, "author": {"login": "a", "type": "User"}}]
        pulls = [{"number": 1, "title": "t", "state": "open",
                  "user": {"login": "a", "type": "User"},
                  "updated_at": "2026-06-21T09:00:00+00:00"}]
        col = _Collector()
        state: dict = {}
        n1 = gh.pull_repo_once(col, "github.com/acme/api", token="t",
                               fetch=_fetch(commits, pulls), state=state)
        self.assertEqual(n1, 2)
        # second pass with identical data: nothing new (dedup by sha / (num,updated))
        n2 = gh.pull_repo_once(col, "github.com/acme/api", token="t",
                               fetch=_fetch(commits, pulls), state=state)
        self.assertEqual(n2, 0)
        # a PR update (new updated_at) re-emits just the PR
        pulls2 = [{**pulls[0], "updated_at": "2026-06-21T10:00:00+00:00"}]
        n3 = gh.pull_repo_once(col, "github.com/acme/api", token="t",
                               fetch=_fetch(commits, pulls2), state=state)
        self.assertEqual(n3, 1)

    def test_only_tracked_github_repos_pulled(self):
        projects = [{"id": "p", "signals": {"git_remotes": ["github.com/acme/api"]}},
                    {"id": "q", "signals": {"git_remotes": ["gitlab.com/x/y"]}}]
        col = _Collector()
        total = gh.pull_github_live(
            col, token="t", projects=projects, once=True,
            fetch=_fetch([], [{"number": 1, "title": "t", "state": "open",
                               "user": {"login": "a", "type": "User"},
                               "updated_at": "2026-06-21T09:00:00+00:00"}]))
        self.assertEqual(total, 1)   # only the GitHub repo contributed


if __name__ == "__main__":
    unittest.main()
