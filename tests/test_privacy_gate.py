import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.schema import (make_event, NormalizedEvent, FILE_CHANGE, GIT_COMMIT,
                        CLIPBOARD, NARRATION, AGENT_REPORT)
from throughlog.privacy.allowlist import Allowlist
from throughlog.privacy.gate import gate, Dropped
from throughlog.privacy import redactors, egress
from throughlog.privacy.diff_policy import DiffPolicy

ALLOW = Allowlist(["C:/Users/dev/Desktop/projects/throughlog"])
INSIDE = "C:/Users/dev/Desktop/projects/throughlog/throughlog/bus.py"
REPO = "C:/Users/dev/Desktop/projects/throughlog"
ON = DiffPolicy(capture_diffs=True)


def _diff(path: str, *body: str) -> str:
    head = (f"diff --git a/{path} b/{path}\n--- a/{path}\n"
            f"+++ b/{path}\n@@ -1,1 +1,2 @@\n import os\n")
    return head + "".join(line + "\n" for line in body)


class AllowlistGate(unittest.TestCase):
    def test_drops_outside(self):
        e = make_event(FILE_CHANGE, kind="fs", adapter="fs_git",
                       payload={"path": "C:/Windows/System32/x.dll"})
        self.assertIsInstance(gate(e, ALLOW), Dropped)

    def test_passes_inside_and_normalizes_home(self):
        e = make_event(FILE_CHANGE, kind="fs", adapter="fs_git", payload={"path": INSIDE})
        r = gate(e, ALLOW)
        self.assertIsInstance(r, NormalizedEvent)
        self.assertNotIn("Users", r.to_json())  # home prefix normalized to ~
        self.assertIn("path", r.privacy.redactions)

    def test_missing_path_dropped(self):
        e = make_event(FILE_CHANGE, kind="fs", adapter="fs_git", payload={})
        self.assertIsInstance(gate(e, ALLOW), Dropped)


class SecretRedaction(unittest.TestCase):
    def test_secret_scrubbed(self):
        e = make_event(NARRATION, kind="intent", adapter="intent_bridge",
                       payload={"note": "key sk-ant-api03-ABCDEFGHIJ1234567890abcdef here"})
        r = gate(e, ALLOW)
        self.assertNotIn("sk-ant-api03", r.to_json())
        self.assertIn("anthropic_key", r.privacy.redactions)

    def test_password_kv(self):
        out, found = redactors.redact_secrets("password=hunter2SuperSecret;")
        self.assertNotIn("hunter2SuperSecret", out)
        self.assertIn("kv_secret", found)

    def test_egress_catches_secret(self):
        self.assertIn("aws_access_key", egress.assert_clean("key AKIA1234567890ABCDEF"))


class ClipboardTyping(unittest.TestCase):
    def test_url_typed_not_raw(self):
        e = make_event(CLIPBOARD, kind="intent", adapter="intent_bridge",
                       payload={"content": "copied https://example.com/secret-page text"})
        r = gate(e, ALLOW)
        self.assertNotIn("content", r.payload)
        self.assertEqual(r.payload.get("kind"), "url")
        self.assertNotIn("secret-page", r.to_json())

    def test_credential_dropped(self):
        e = make_event(CLIPBOARD, kind="intent", adapter="intent_bridge",
                       payload={"content": "sk-ant-api03-SECRET1234567890abcdefGHIJ"})
        self.assertIsInstance(gate(e, ALLOW), Dropped)


class HomePath(unittest.TestCase):
    def test_normalize(self):
        out = redactors.normalize_home_paths(r"C:\Users\dev\Desktop\x")
        self.assertNotIn("Users", out)  # user-specific segment collapsed away
        self.assertTrue(out.startswith("~"))


class DiffCapture(unittest.TestCase):
    """Step 3a — opt-in, default-OFF diff capture (Features A/B/C)."""

    def _fc(self, path: str, diff: str):
        return make_event(FILE_CHANGE, kind="fs", adapter="fs_git",
                          payload={"path": f"{INSIDE.rsplit('/', 1)[0]}/{path}", "diff": diff})

    def test_default_off_strips_diff(self):
        e = self._fc("app.py", _diff("throughlog/app.py", '+print("hi")'))
        r = gate(e, ALLOW)                      # DEFAULT_POLICY
        self.assertNotIn("diff", r.payload)
        self.assertNotIn("_diff_clean", r.payload)
        self.assertIn("content_stripped", r.privacy.redactions)

    def test_on_clean_sets_transient(self):
        e = self._fc("app.py", _diff("throughlog/app.py", '+print("hi")'))
        r = gate(e, ALLOW, ON)
        self.assertIn("_diff_clean", r.payload)
        self.assertIn('print("hi")', r.payload["_diff_clean"])
        self.assertIn("diff_captured", r.privacy.redactions)
        # the transient must never serialize (V-02)
        self.assertNotIn("_diff_clean", r.to_json())

    def test_secret_in_diff_absent(self):
        e = self._fc("app.py", _diff("throughlog/app.py",
                     '+API_KEY = "sk-ant-abcdefghij0123456789KLMNOPqrst"'))
        r = gate(e, ALLOW, ON)
        self.assertNotIn("sk-ant-abcdefghij0123456789KLMNOPqrst", r.to_json())
        self.assertNotIn("sk-ant-abcdefghij0123456789KLMNOPqrst",
                         r.payload.get("_diff_clean", ""))
        self.assertIn("diff_scrubbed", r.privacy.redactions)

    def test_env_file_change_yields_no_diff(self):
        e = self._fc(".env", _diff(".env", "+SECRET=hunter2value"))
        r = gate(e, ALLOW, ON)
        self.assertNotIn("_diff_clean", r.payload)
        self.assertIn("diff_suppressed_ignored", r.privacy.redactions)

    def test_multi_file_commit_drops_env_hunk(self):
        # V-01 — a committed .env hunk inside an allowed multi-file diff is dropped,
        # the ordinary hunk is kept.
        multi = (_diff("throughlog/app.py", '+x = 1')
                 + "diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n"
                   "@@ -1 +1 @@\n+OPENAI_KEY=sk-ant-zzzzzzzzzzzzzzzzzzzzzzzz\n")
        e = make_event(GIT_COMMIT, kind="git", adapter="fs_git",
                       payload={"repo": REPO, "diff": multi})
        r = gate(e, ALLOW, ON)
        clean = r.payload.get("_diff_clean", "")
        self.assertIn("x = 1", clean)
        self.assertNotIn("sk-ant-zzzz", r.to_json())
        self.assertIn("diff_suppressed_ignored", r.privacy.redactions)

    def test_ignore_glob_suppresses(self):
        pol = DiffPolicy(capture_diffs=True, ignore_globs=("*.py",))
        e = self._fc("app.py", _diff("throughlog/app.py", "+x = 1"))
        r = gate(e, ALLOW, pol)
        self.assertNotIn("_diff_clean", r.payload)
        self.assertIn("diff_suppressed_ignored", r.privacy.redactions)

    def test_non_string_diff_never_crashes(self):
        for junk in (123, {"a": 1}, ["x"], None):
            e = make_event(FILE_CHANGE, kind="fs", adapter="fs_git",
                           payload={"path": INSIDE, "diff": junk})
            r = gate(e, ALLOW, ON)
            self.assertIsInstance(r, NormalizedEvent)
            self.assertNotIn("diff", r.payload)

    def test_commit_body_and_diffstat_kept_when_on(self):
        e = make_event(GIT_COMMIT, kind="git", adapter="fs_git",
                       payload={"repo": REPO, "body": "Full message\n\nDetails here.",
                                "diffstat": " throughlog/app.py | 2 +-\n 1 file changed"})
        r = gate(e, ALLOW, ON)
        self.assertEqual(r.payload.get("body"), "Full message\n\nDetails here.")
        self.assertIn("commit_body_captured", r.privacy.redactions)
        self.assertIn("diffstat_captured", r.privacy.redactions)

    def test_body_stripped_when_off(self):
        e = make_event(GIT_COMMIT, kind="git", adapter="fs_git",
                       payload={"repo": REPO, "body": "Full message"})
        r = gate(e, ALLOW)                      # default off
        self.assertNotIn("body", r.payload)

    def test_agent_report_body_diff_always_stripped(self):
        # V-07 — the spoof surface never gets the retention upgrade, even capture-on.
        e = make_event(AGENT_REPORT, kind="agent", adapter="agent_ingest",
                       payload={"repo": REPO, "summary": "did work",
                                "body": "should be stripped",
                                "diff": _diff("throughlog/app.py", "+secret_stuff = 1")})
        r = gate(e, ALLOW, ON)
        self.assertNotIn("body", r.payload)
        self.assertNotIn("diff", r.payload)
        self.assertNotIn("_diff_clean", r.payload)
        self.assertNotIn("secret_stuff", r.to_json())


class ClipboardPreviewGate(unittest.TestCase):
    PREVIEW = DiffPolicy(clipboard_preview=True, clipboard_preview_chars=40)

    def test_preview_added_when_on(self):
        e = make_event(CLIPBOARD, kind="intent", adapter="intent_bridge",
                       payload={"content": "a normal sentence I copied to the clipboard"})
        r = gate(e, ALLOW, self.PREVIEW)
        self.assertIn("preview", r.payload)
        self.assertIn("clipboard_preview", r.privacy.redactions)

    def test_no_preview_when_off(self):
        e = make_event(CLIPBOARD, kind="intent", adapter="intent_bridge",
                       payload={"content": "a normal sentence I copied to the clipboard"})
        r = gate(e, ALLOW)
        self.assertNotIn("preview", r.payload)

    def test_credential_clipboard_dropped_not_previewed(self):
        # a credential-bearing clipboard is dropped wholesale at the clipboard step —
        # the preview logic is never even reached, so nothing leaks.
        e = make_event(CLIPBOARD, kind="intent", adapter="intent_bridge",
                       payload={"content": "harmless head text padding password=Zk9secret tail"})
        self.assertIsInstance(gate(e, ALLOW, self.PREVIEW), Dropped)


if __name__ == "__main__":
    unittest.main()
