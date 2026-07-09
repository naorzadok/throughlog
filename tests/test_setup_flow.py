"""Offline unit tests for the ``tl setup`` core (throughlog/setup_flow.py).

Pure and deterministic — no network, no real ``~``, no live capture. Every
OS-touching input to ``detect_state`` is injected (fake home dir, prebuilt cfg,
lambda probes), and the prompt helpers are driven with fake readers, so the whole
suite proves the *logic* of detection / recommendation / consent without side
effects. The apply loop itself lives in ``cli.cmd_setup`` (a thin driver over
these) and is exercised separately by the end-to-end ``tl setup --plan`` check."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import hooks, setup_flow as sf


# A cfg whose key env-var points at a name that is virtually never set, so
# ``key_is_set`` is deterministically False regardless of the ambient environment.
NO_KEY_CFG = {"llm": {"api_key_env": "TL_DEFINITELY_UNSET_ENV_XYZ"}}


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_home(root: Path, *, claude=False, claude_installed=False,
               cursor=False, cursor_installed=False) -> Path:
    """Build a fake home dir with (or without) each agent tool's config present, and
    optionally with our hook already merged in."""
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    if claude:
        p = home / hooks._SETTINGS_REL["claude-code"]
        p.parent.mkdir(parents=True, exist_ok=True)
        if claude_installed:
            _write_json(p, hooks.merge_claude_code({}, 'python "x/claude_code/tl_hook.py"'))
    if cursor:
        p = home / hooks._SETTINGS_REL["cursor"]
        p.parent.mkdir(parents=True, exist_ok=True)
        if cursor_installed:
            _write_json(p, hooks.merge_cursor({}, 'python "x/cursor/tl_hook.py"'))
    return home


class DetectState(unittest.TestCase):
    def _detect(self, home, base_dir, *, cfg=None, autostart=False, capture=False):
        return sf.detect_state(
            home=home, base_dir=base_dir, cfg=cfg if cfg is not None else NO_KEY_CFG,
            data=base_dir, autostart_probe=lambda: autostart,
            capture_probe=lambda: capture)

    def test_fresh_machine_detects_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)                      # no tools, no configs
            state = self._detect(home, root)
            self.assertEqual(state.agent_tools, ())
            self.assertEqual(state.project_count, 0)
            self.assertFalse(state.key_set)
            self.assertIsNone(state.nightly_at)
            self.assertFalse(state.autostart_on)
            self.assertFalse(state.capture_live)

    def test_tools_present_and_installed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root, claude=True, claude_installed=True,
                              cursor=True, cursor_installed=True)
            state = self._detect(home, root)
            self.assertEqual(set(state.agent_tools), {"claude-code", "cursor"})
            self.assertTrue(state.hooks_installed["claude-code"])
            self.assertTrue(state.hooks_installed["cursor"])

    def test_tool_present_but_hook_not_installed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root, claude=True, claude_installed=False)
            state = self._detect(home, root)
            self.assertEqual(state.agent_tools, ("claude-code",))
            self.assertFalse(state.hooks_installed["claude-code"])

    def test_project_count_from_registry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            _write_json(root / "projects.json",
                        {"projects": [{"id": "a"}, {"id": "b"}]})
            state = self._detect(home, root)
            self.assertEqual(state.project_count, 2)

    def test_key_and_nightly_from_cfg(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            cfg = {"llm": {"api_key": "sk-test"},
                   "schedule": {"synthesize_at": "09:15"}}
            state = self._detect(home, root, cfg=cfg)
            self.assertTrue(state.key_set)
            self.assertEqual(state.nightly_at, "09:15")

    def test_probes_are_reflected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            state = self._detect(home, root, autostart=True, capture=True)
            self.assertTrue(state.autostart_on)
            self.assertTrue(state.capture_live)

    def test_probe_exception_never_propagates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            def boom() -> bool:
                raise RuntimeError("probe blew up")
            state = sf.detect_state(home=home, base_dir=root, cfg=NO_KEY_CFG,
                                    data=root, autostart_probe=boom, capture_probe=boom)
            self.assertFalse(state.autostart_on)
            self.assertFalse(state.capture_live)


def _state(**over) -> sf.SetupState:
    base = dict(platform="linux", agent_tools=(), hooks_installed={},
                project_count=0, key_set=False, nightly_at=None,
                autostart_on=False, capture_live=False)
    base.update(over)
    return sf.SetupState(**base)


class PlanSteps(unittest.TestCase):
    def test_fresh_all_todo_no_tools(self):
        steps = sf.plan_steps(_state())
        first = steps[0]
        self.assertEqual(first.key, "hooks")
        self.assertFalse(first.applicable)           # no ~/.claude, ~/.cursor
        self.assertTrue(all(not s.done for s in steps))
        keys = [s.key for s in steps]
        self.assertEqual(keys, ["hooks", "projects", "llm-key",
                                "nightly", "autostart", "start"])

    def test_fully_set_up_all_done(self):
        steps = sf.plan_steps(_state(
            agent_tools=("claude-code", "cursor"),
            hooks_installed={"claude-code": True, "cursor": True},
            project_count=3, key_set=True, nightly_at="22:30",
            autostart_on=True, capture_live=True))
        self.assertTrue(all(s.done for s in steps))
        keys = [s.key for s in steps]
        self.assertEqual(keys, ["hook:claude-code", "hook:cursor", "projects",
                                "llm-key", "nightly", "autostart", "start"])

    def test_hook_step_command_per_tool(self):
        steps = sf.plan_steps(_state(
            agent_tools=("cursor",), hooks_installed={"cursor": False}))
        hook = next(s for s in steps if s.key == "hook:cursor")
        self.assertEqual(hook.command, ["tl", "hook", "enable", "cursor"])
        self.assertFalse(hook.done)

    def test_only_missing_key_is_todo(self):
        steps = sf.plan_steps(_state(
            agent_tools=("claude-code",),
            hooks_installed={"claude-code": True},
            project_count=1, key_set=False, nightly_at="22:30",
            autostart_on=True, capture_live=True))
        by_key = {s.key: s for s in steps}
        self.assertFalse(by_key["llm-key"].done)
        self.assertTrue(by_key["projects"].done)
        self.assertTrue(by_key["nightly"].done)
        self.assertTrue(by_key["autostart"].done)


class Confirm(unittest.TestCase):
    def test_assume_yes_is_true(self):
        self.assertTrue(sf.confirm("q?", default=False, assume_yes=True))

    def test_non_interactive_declines(self):
        # The safety rule: can't ask -> no, never the recommended default.
        self.assertFalse(sf.confirm("q?", default=True, interactive=False))

    def test_empty_line_accepts_default(self):
        self.assertTrue(sf.confirm("q?", default=True, interactive=True,
                                   reader=lambda _: ""))
        self.assertFalse(sf.confirm("q?", default=False, interactive=True,
                                    reader=lambda _: ""))

    def test_explicit_yes_no(self):
        self.assertTrue(sf.confirm("q?", interactive=True, reader=lambda _: "y"))
        self.assertTrue(sf.confirm("q?", interactive=True, reader=lambda _: "Yes"))
        self.assertFalse(sf.confirm("q?", interactive=True, reader=lambda _: "n"))
        self.assertFalse(sf.confirm("q?", interactive=True, reader=lambda _: "nope"))

    def test_eof_declines(self):
        def boom(_):
            raise EOFError
        self.assertFalse(sf.confirm("q?", default=True, interactive=True, reader=boom))


class AskText(unittest.TestCase):
    def test_non_interactive_returns_empty(self):
        self.assertEqual(sf.ask_text("root?", default="~/projects", interactive=False), "")

    def test_empty_line_accepts_default(self):
        self.assertEqual(
            sf.ask_text("root?", default="d", interactive=True, reader=lambda _: ""), "d")

    def test_typed_value_wins(self):
        self.assertEqual(
            sf.ask_text("root?", default="d", interactive=True, reader=lambda _: "x"), "x")

    def test_eof_returns_empty(self):
        def boom(_):
            raise EOFError
        self.assertEqual(
            sf.ask_text("root?", default="d", interactive=True, reader=boom), "")


if __name__ == "__main__":
    unittest.main()
