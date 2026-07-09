import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import hooks


CMD = 'python "C:/repo/integrations/claude_code/tl_hook.py"'
CMD2 = 'python "C:/repo2/integrations/claude_code/tl_hook.py"'
CUR_CMD = 'python "C:/repo/integrations/cursor/tl_hook.py"'
CUR_CMD2 = 'python "C:/repo2/integrations/cursor/tl_hook.py"'


class MergeClaudeCode(unittest.TestCase):
    def test_preserves_real_pretooluse_entry(self):
        # Shaped exactly like this machine's real ~/.claude/settings.json.
        settings = {
            "effortLevel": "medium",
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Edit|Write|MultiEdit",
                     "hooks": [{"type": "command",
                                "command": "python3 C:/Users/naorz/.claude/hooks/security_reminder_hook.py"}]}
                ]
            },
        }
        out = hooks.merge_claude_code(settings, CMD)
        self.assertEqual(out["effortLevel"], "medium")
        self.assertEqual(out["hooks"]["PreToolUse"],
                         settings["hooks"]["PreToolUse"])
        self.assertEqual(len(out["hooks"]["PostToolUse"]), 1)
        self.assertEqual(out["hooks"]["PostToolUse"][0]["matcher"],
                         "Edit|Write|MultiEdit")
        self.assertEqual(out["hooks"]["PostToolUse"][0]["hooks"][0]["command"], CMD)
        self.assertEqual(len(out["hooks"]["Stop"]), 1)
        self.assertEqual(out["hooks"]["Stop"][0]["hooks"][0]["command"], CMD)
        self.assertNotIn("matcher", out["hooks"]["Stop"][0])

    def test_idempotent_no_duplicates(self):
        out = hooks.merge_claude_code({}, CMD)
        out = hooks.merge_claude_code(out, CMD)
        self.assertEqual(len(out["hooks"]["PostToolUse"]), 1)
        self.assertEqual(len(out["hooks"]["Stop"]), 1)

    def test_replaces_stale_command_path(self):
        out = hooks.merge_claude_code({}, CMD)
        out = hooks.merge_claude_code(out, CMD2)
        self.assertEqual(len(out["hooks"]["PostToolUse"]), 1)
        self.assertEqual(out["hooks"]["PostToolUse"][0]["hooks"][0]["command"], CMD2)
        self.assertEqual(out["hooks"]["Stop"][0]["hooks"][0]["command"], CMD2)


class StripClaudeCode(unittest.TestCase):
    def test_removes_only_ours(self):
        settings = {
            "effortLevel": "medium",
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Edit|Write|MultiEdit",
                     "hooks": [{"type": "command",
                                "command": "python3 C:/Users/naorz/.claude/hooks/security_reminder_hook.py"}]}
                ]
            },
        }
        merged = hooks.merge_claude_code(settings, CMD)
        out = hooks.strip_claude_code(merged)
        self.assertEqual(out["effortLevel"], "medium")
        self.assertEqual(out["hooks"]["PreToolUse"], settings["hooks"]["PreToolUse"])
        self.assertNotIn("PostToolUse", out["hooks"])
        self.assertNotIn("Stop", out["hooks"])

    def test_drops_hooks_key_when_empty(self):
        out = hooks.merge_claude_code({}, CMD)
        out = hooks.strip_claude_code(out)
        self.assertNotIn("hooks", out)

    def test_keeps_hooks_key_when_others_remain(self):
        settings = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "x"}]}]}}
        merged = hooks.merge_claude_code(settings, CMD)
        out = hooks.strip_claude_code(merged)
        self.assertIn("hooks", out)
        self.assertIn("PreToolUse", out["hooks"])


class MergeCursor(unittest.TestCase):
    def test_installs_afterfileedit_and_stop(self):
        out = hooks.merge_cursor({}, CUR_CMD)
        self.assertEqual(out["version"], 1)
        self.assertEqual(out["hooks"]["afterFileEdit"], [{"command": CUR_CMD}])
        self.assertEqual(out["hooks"]["stop"], [{"command": CUR_CMD}])

    def test_preserves_unrelated_event_and_version(self):
        settings = {"version": 1, "hooks": {"beforeSubmitPrompt": [{"command": "other.py"}]}}
        out = hooks.merge_cursor(settings, CUR_CMD)
        self.assertEqual(out["hooks"]["beforeSubmitPrompt"], [{"command": "other.py"}])

    def test_idempotent_no_duplicates(self):
        out = hooks.merge_cursor({}, CUR_CMD)
        out = hooks.merge_cursor(out, CUR_CMD)
        self.assertEqual(len(out["hooks"]["afterFileEdit"]), 1)
        self.assertEqual(len(out["hooks"]["stop"]), 1)

    def test_replaces_stale_command_path(self):
        out = hooks.merge_cursor({}, CUR_CMD)
        out = hooks.merge_cursor(out, CUR_CMD2)
        self.assertEqual(out["hooks"]["afterFileEdit"], [{"command": CUR_CMD2}])
        self.assertEqual(out["hooks"]["stop"], [{"command": CUR_CMD2}])


class StripCursor(unittest.TestCase):
    def test_removes_only_ours(self):
        settings = {"version": 1, "hooks": {"beforeSubmitPrompt": [{"command": "other.py"}]}}
        merged = hooks.merge_cursor(settings, CUR_CMD)
        out = hooks.strip_cursor(merged)
        self.assertEqual(out["hooks"]["beforeSubmitPrompt"], [{"command": "other.py"}])
        self.assertNotIn("afterFileEdit", out["hooks"])
        self.assertNotIn("stop", out["hooks"])

    def test_drops_hooks_key_when_empty(self):
        out = hooks.merge_cursor({}, CUR_CMD)
        out = hooks.strip_cursor(out)
        self.assertNotIn("hooks", out)
        self.assertEqual(out["version"], 1)


class Installed(unittest.TestCase):
    def test_claude_code_installed_flag(self):
        self.assertFalse(hooks._cc_installed({}))
        merged = hooks.merge_claude_code({}, CMD)
        self.assertTrue(hooks._cc_installed(merged))
        self.assertFalse(hooks._cc_installed(hooks.strip_claude_code(merged)))

    def test_cursor_installed_flag(self):
        self.assertFalse(hooks._cur_installed({}))
        merged = hooks.merge_cursor({}, CUR_CMD)
        self.assertTrue(hooks._cur_installed(merged))
        self.assertFalse(hooks._cur_installed(hooks.strip_cursor(merged)))


class HookCommand(unittest.TestCase):
    def test_quotes_both_path_components(self):
        cmd = hooks.hook_command("claude-code", python="C:/Program Files/Python/python.exe")
        self.assertTrue(cmd.startswith('"C:/Program Files/Python/python.exe"'))
        self.assertIn("tl_hook.py", cmd)

    def test_no_quotes_needed_for_simple_paths(self):
        cmd = hooks.hook_command("cursor", python="python")
        self.assertTrue(cmd.startswith("python "))
        self.assertNotIn('"', cmd)

    def test_selects_right_script_per_tool(self):
        cc = hooks.hook_command("claude-code", python="python")
        cur = hooks.hook_command("cursor", python="python")
        self.assertIn(str(hooks._SCRIPT_REL["claude-code"]).replace("\\", "/"),
                     cc.replace("\\", "/"))
        self.assertIn(str(hooks._SCRIPT_REL["cursor"]).replace("\\", "/"),
                     cur.replace("\\", "/"))


if __name__ == "__main__":
    unittest.main()
