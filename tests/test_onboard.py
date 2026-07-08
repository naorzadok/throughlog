import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import onboard
from throughlog.categorize import signal_stack
from throughlog.schema import make_event, FOCUS_SESSION


def _make_repo(root: Path, name: str, *, remote: str | None = None,
               readme: str | None = None, markers: tuple[str, ...] = ()) -> Path:
    """Create a fake git repo dir: a .git/config (optionally with an origin url),
    an optional README, and optional language-marker files."""
    repo = root / name
    git = repo / ".git"
    git.mkdir(parents=True)
    cfg = "[core]\n\trepositoryformatversion = 0\n"
    if remote:
        cfg += f'[remote "origin"]\n\turl = {remote}\n'
    (git / "config").write_text(cfg, encoding="utf-8")
    if readme is not None:
        (repo / "README.md").write_text(readme, encoding="utf-8")
    for mk in markers:
        (repo / mk).write_text("", encoding="utf-8")
    return repo


# --------------------------------------------------------------------------- #
# normalize_remote — every URL flavour reduces to host/owner/repo
# --------------------------------------------------------------------------- #
class NormalizeRemote(unittest.TestCase):
    def test_https_strips_scheme_and_dotgit(self):
        self.assertEqual(
            onboard.normalize_remote("https://github.com/naorzadok/throughlog.git"),
            "github.com/naorzadok/throughlog")

    def test_https_without_dotgit(self):
        self.assertEqual(
            onboard.normalize_remote("https://github.com/owner/repo"),
            "github.com/owner/repo")

    def test_scp_like_ssh(self):
        self.assertEqual(
            onboard.normalize_remote("git@github.com:NoFriction-Labs/foldio.git"),
            "github.com/NoFriction-Labs/foldio")

    def test_ssh_scheme_with_user(self):
        self.assertEqual(
            onboard.normalize_remote("ssh://git@gitlab.com/grp/sub/proj.git"),
            "gitlab.com/grp/sub/proj")

    def test_garbage_is_none(self):
        self.assertIsNone(onboard.normalize_remote(""))
        self.assertIsNone(onboard.normalize_remote("not a url"))
        self.assertIsNone(onboard.normalize_remote("/local/path"))


# --------------------------------------------------------------------------- #
# parse_git_remotes — origin first, de-duped, ignores non-remote sections
# --------------------------------------------------------------------------- #
class ParseGitRemotes(unittest.TestCase):
    def test_origin_first_and_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _make_repo(Path(d), "r")
            (repo / ".git" / "config").write_text(
                '[remote "upstream"]\n\turl = https://github.com/up/stream.git\n'
                '[remote "origin"]\n\turl = git@github.com:me/repo.git\n'
                '[branch "main"]\n\turl = ignored\n',
                encoding="utf-8")
            self.assertEqual(
                onboard.parse_git_remotes(repo),
                ["github.com/me/repo", "github.com/up/stream"])

    def test_no_config_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "bare").mkdir()
            self.assertEqual(onboard.parse_git_remotes(Path(d) / "bare"), [])


# --------------------------------------------------------------------------- #
# Name / README inference
# --------------------------------------------------------------------------- #
class Inference(unittest.TestCase):
    def test_split_slug_kebab_and_camel(self):
        self.assertEqual(onboard._split_slug("my-cool-project"),
                         ["my", "cool", "project"])
        self.assertEqual(onboard._split_slug("myCoolProject"),
                         ["my", "cool", "project"])

    def test_keywords_include_spaced_and_slug_and_readme(self):
        kws = onboard.infer_keywords("my-cool-project", "My Cool Project")
        self.assertIn("my cool project", kws)
        self.assertIn("my-cool-project", kws)

    def test_window_patterns_compile_and_match_title(self):
        pats = onboard.infer_window_patterns("throughlog")
        self.assertTrue(pats)
        title = "throughlog - server.py - VS Code"
        self.assertTrue(any(re.search(p, title, re.I) for p in pats))

    def test_readme_title_first_heading(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _make_repo(Path(d), "proj",
                              readme="![badge](x)\n\n# My Cool Project\n\nblurb\n")
            self.assertEqual(onboard.read_readme_title(repo), "My Cool Project")

    def test_readme_title_none_when_no_heading(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _make_repo(Path(d), "proj", readme="just prose, no heading\n")
            self.assertIsNone(onboard.read_readme_title(repo))

    def test_infer_apps_from_markers(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _make_repo(Path(d), "py", markers=("pyproject.toml",))
            self.assertIn("python.exe", onboard.infer_apps(repo))


# --------------------------------------------------------------------------- #
# find_git_repos — nesting, skip dirs, repo-is-a-leaf
# --------------------------------------------------------------------------- #
class FindRepos(unittest.TestCase):
    def test_finds_repos_skips_vendored_and_does_not_descend(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _make_repo(root, "alpha")
            _make_repo(root / "nested", "beta")          # one level down
            # a repo containing a vendored sub-repo we must NOT report
            gamma = _make_repo(root, "gamma")
            _make_repo(gamma / "node_modules", "dep")    # under a skip dir
            (gamma / "sub").mkdir()                       # plain subdir, not a repo
            repos = {p.name for p in onboard.find_git_repos(root)}
            self.assertEqual(repos, {"alpha", "beta", "gamma"})


# --------------------------------------------------------------------------- #
# discover_projects / init_registry — merge semantics
# --------------------------------------------------------------------------- #
class DiscoverAndMerge(unittest.TestCase):
    def test_skips_already_registered_path_and_unique_ids(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            r1 = _make_repo(root, "demo")
            _make_repo(root, "demo2")
            existing = [{
                "id": "demo",
                "signals": {"paths": [str(r1)]},
            }]
            new = onboard.discover_projects(root, existing=existing, today="2026-06-24")
            ids = {p["id"] for p in new}
            paths = {p["signals"]["paths"][0] for p in new}
            # r1 already registered -> not re-added
            self.assertNotIn(os.path.normcase(str(r1)),
                             {os.path.normcase(p) for p in paths})
            # demo2's slug would be "demo2"; "demo" is taken but distinct, so kept
            self.assertEqual(ids, {"demo2"})

    def test_init_registry_writes_valid_merged_json(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _make_repo(root, "writeme",
                       remote="https://github.com/me/writeme.git",
                       readme="# Write Me\n")
            out = root / "projects.json"
            discovered, existing, path = onboard.init_registry(
                root, out, today="2026-06-24")
            self.assertEqual(len(discovered), 1)
            self.assertEqual(existing, [])
            data = json.loads(out.read_text(encoding="utf-8"))
            proj = data["projects"][0]
            self.assertEqual(proj["name"], "Write Me")
            self.assertEqual(proj["signals"]["git_remotes"], ["github.com/me/writeme"])

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _make_repo(root, "ghost")
            out = root / "projects.json"
            discovered, _, _ = onboard.init_registry(root, out, dry_run=True,
                                                      today="2026-06-24")
            self.assertEqual(len(discovered), 1)
            self.assertFalse(out.exists())


# --------------------------------------------------------------------------- #
# The real proof: a generated project actually attributes events
# --------------------------------------------------------------------------- #
class GeneratedConfigIsUsable(unittest.TestCase):
    def test_generated_path_signal_attributes_focus_event(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = _make_repo(root, "acme-api",
                              remote="https://github.com/acme/acme-api.git")
            projects = onboard.discover_projects(root, today="2026-06-24")
            self.assertEqual(len(projects), 1)
            # a focus event with a file under the repo must attribute to it
            ev = make_event(
                FOCUS_SESSION, kind="os", adapter="os_focus",
                payload={"anchor": "editor",
                         "active_file": str(repo / "src" / "main.py")},
                ts_wall="2026-06-24T10:00:00+00:00")
            pid, score, method, _ = signal_stack(ev, projects)
            self.assertEqual(pid, "acme-api")
            self.assertGreaterEqual(score, 0.51)


class _EnrichClient:
    """Returns a canned enrichment JSON; records the user prompt it was sent."""
    def __init__(self, reply):
        self.reply = reply
        self.user = ""

    def chat(self, system, user, **kw):
        self.user = user
        return self.reply


class RepoDigest(unittest.TestCase):
    def test_metadata_only_no_file_bodies(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _make_repo(Path(d), "widget", readme="# Widget\nA gadget service.",
                              markers=("pyproject.toml",))
            (repo / "secret.py").write_text("API_TOKEN = 'sk-supersecret-body'\n",
                                            encoding="utf-8")
            digest = onboard.build_repo_digest(repo)
            self.assertIn("Widget", digest)                 # README excerpt present
            self.assertIn("secret.py", digest)              # the NAME may appear (tree)
            self.assertIn("pyproject.toml", digest)         # language marker
            self.assertNotIn("sk-supersecret-body", digest)  # but never the file CONTENTS
            self.assertLessEqual(len(digest), 4000)

    def test_skips_dotfiles_in_tree(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _make_repo(Path(d), "app")
            (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
            digest = onboard.build_repo_digest(repo)
            self.assertNotIn(".env", digest)                # hidden entries are skipped


class EnrichProject(unittest.TestCase):
    def _base(self, d):
        repo = _make_repo(Path(d), "acme-api",
                          remote="https://github.com/acme/acme-api.git")
        return onboard.build_project(repo, "2026-06-24", set()), repo

    def test_merges_suggestions_never_touches_paths(self):
        with tempfile.TemporaryDirectory() as d:
            base, repo = self._base(d)
            paths_before = list(base["signals"]["paths"])
            remotes_before = list(base["signals"]["git_remotes"])
            client = _EnrichClient(
                '{"description":"An API.","keywords":["acme","billing"],'
                '"window_patterns":["acme.*api"],"jira_prefixes":["ACME"],'
                '"paths":["C:/evil"],"git_remotes":["evil/repo"]}')
            out = onboard.enrich_project(base, "digest", client)
            self.assertEqual(out["description"], "An API.")
            self.assertIn("billing", out["signals"]["keywords"])
            self.assertIn("ACME", out["signals"]["jira_prefixes"])
            # The model proposing paths/git_remotes is IGNORED (allowlist stays deterministic).
            self.assertEqual(out["signals"]["paths"], paths_before)
            self.assertEqual(out["signals"]["git_remotes"], remotes_before)

    def test_degrades_on_no_client_or_bad_json(self):
        with tempfile.TemporaryDirectory() as d:
            base, _ = self._base(d)
            self.assertIs(onboard.enrich_project(base, "digest", None), base)
            self.assertEqual(onboard.enrich_project(base, "digest",
                                                    _EnrichClient("not json at all")), base)

    def test_degrades_on_llm_error(self):
        from throughlog.llm.client import LLMError

        class _Boom:
            def chat(self, *a, **k):
                raise LLMError("down")

        with tempfile.TemporaryDirectory() as d:
            base, _ = self._base(d)
            self.assertIs(onboard.enrich_project(base, "digest", _Boom()), base)

    def test_discover_enriches_when_client_given(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _make_repo(root, "acme-api", remote="https://github.com/acme/acme-api.git",
                       readme="# Acme API\nBilling service.")
            client = _EnrichClient('{"keywords":["enriched-kw"]}')
            projects = onboard.discover_projects(root, today="2026-06-24", client=client)
            self.assertIn("enriched-kw", projects[0]["signals"]["keywords"])


if __name__ == "__main__":
    unittest.main()
