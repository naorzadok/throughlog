import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.privacy import diff_policy as dp
from throughlog.privacy.diff_policy import (
    DiffPolicy, DEFAULT_POLICY, is_secret_file, path_ignored, parse_tlignore,
    split_diff_by_file, scrub_diff, make_clipboard_preview,
)

ON = DiffPolicy(capture_diffs=True)


def _diff(*body: str) -> str:
    head = ("diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,3 @@\n import os\n")
    return head + "".join(line + "\n" for line in body)


class SecretFile(unittest.TestCase):
    def test_dotenv_and_keys_match(self):
        for p in (".env", "config/.env", ".env.local", "server.pem", "tls.key",
                  "id_rsa", "id_ed25519", "~/.npmrc", "deploy/credentials.json"):
            self.assertTrue(is_secret_file(p), p)

    def test_boundary_not_substring(self):
        # contrast with the categorizer's substring bug (F-05): basename-scoped.
        for p in ("prevent.txt", "my.key.txt", "environment.py", "keyboard.py",
                  "src/app.py", "README.md"):
            self.assertFalse(is_secret_file(p), p)

    def test_both_separators(self):
        self.assertTrue(is_secret_file(r"C:\proj\secrets\.env"))
        self.assertTrue(is_secret_file("C:/proj/secrets/.env"))

    def test_empty(self):
        self.assertFalse(is_secret_file(""))


class PathIgnored(unittest.TestCase):
    def test_extension_glob(self):
        self.assertTrue(path_ignored("db/schema.sql", ("*.sql",)))
        self.assertFalse(path_ignored("src/app.py", ("*.sql",)))

    def test_prefix_boundary(self):
        # secrets/* must NOT match mysecrets/x (boundary, not substring).
        self.assertTrue(path_ignored("secrets/key.txt", ("secrets/*",)))
        self.assertFalse(path_ignored("mysecrets/key.txt", ("secrets/*",)))

    def test_dir_rule(self):
        self.assertTrue(path_ignored("build/out.js", ("build/",)))
        self.assertTrue(path_ignored("build", ("build/",)))
        self.assertFalse(path_ignored("builder/out.js", ("build/",)))

    def test_globstar_basename(self):
        self.assertTrue(path_ignored("a/b/c.key", ("**/*.key",)))

    def test_bare_segment(self):
        self.assertTrue(path_ignored("a/node_modules/b.js", ("node_modules",)))
        self.assertFalse(path_ignored("a/node_modules_x/b.js", ("node_modules",)))

    def test_both_separators(self):
        self.assertTrue(path_ignored(r"src\app.py", ("src/*",)))

    def test_empty_inputs(self):
        self.assertFalse(path_ignored("", ("*.sql",)))
        self.assertFalse(path_ignored("a.sql", ()))


class SalIgnore(unittest.TestCase):
    def test_parse_skips_comments_blanks(self):
        text = "# a comment\n\n*.sql\n  fixtures/**  \n# trailing\n"
        self.assertEqual(parse_tlignore(text), ("*.sql", "fixtures/**"))

    def test_line_cap(self):
        text = "\n".join(f"g{i}" for i in range(5000))
        self.assertEqual(len(parse_tlignore(text, max_lines=10)), 10)


class SplitByFile(unittest.TestCase):
    def test_multi_file(self):
        raw = ("diff --git a/src/app.py b/src/app.py\n@@ -1 +1 @@\n-a\n+b\n"
               "diff --git a/.env b/.env\n@@ -1 +1 @@\n-x\n+SECRET=1\n")
        out = split_diff_by_file(raw)
        self.assertEqual([rel for rel, _ in out], ["src/app.py", ".env"])
        self.assertIn("SECRET=1", out[1][1])

    def test_no_header_single_chunk(self):
        out = split_diff_by_file("just some text\nno header\n")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "")


class ScrubDiff(unittest.TestCase):
    def test_clean_captured(self):
        clean, codes = scrub_diff(_diff('+print("hi")'), ON)
        self.assertIsNotNone(clean)
        self.assertIn(dp.DIFF_CAPTURED, codes)
        self.assertNotIn(dp.DIFF_SCRUBBED, codes)
        self.assertIn('print("hi")', clean)

    def test_secret_redacted_in_place(self):
        clean, codes = scrub_diff(
            _diff('+TOKEN = "sk-ant-abcdefghijklmnopqrstuvwxyz0123"'), ON)
        self.assertIsNotNone(clean)
        self.assertIn(dp.DIFF_SCRUBBED, codes)
        self.assertNotIn("sk-ant-abcdefghijklmnopqrstuvwxyz0123", clean)
        # context preserved (the line is kept, the secret is masked)
        self.assertIn("import os", clean)

    def test_pem_block_suppressed_whole(self):
        body = ("-----BEGIN RSA PRIVATE KEY-----",
                "MIIEowIBAAKCAQEA0123456789abcdefABCDEF/+klmnopqrstuvwxyz",
                "QWERTYUIOPasdfghjklZXCVBNM1234567890+/=abcdefghijklmnop",
                "-----END RSA PRIVATE KEY-----")
        clean, codes = scrub_diff(_diff(*("+" + b for b in body)), ON)
        self.assertIsNotNone(clean)
        self.assertNotIn("MIIEowIBAAKCAQEA", clean)
        self.assertNotIn("QWERTYUIOPasdfghjkl", clean)
        self.assertIn("[REDACTED:private_key_block]", clean)

    def test_digit_free_long_token(self):
        # a digit-free base64-ish blob that redactors' digit+alpha backstop misses
        tok = "abcdefghABCDEFGHijklmnopQRSTUVWXyzABCDEFGHijklmnop"
        clean, codes = scrub_diff(_diff("+key = " + tok), ON)
        self.assertNotIn(tok, clean)
        self.assertIn(dp.DIFF_SCRUBBED, codes)

    def test_oversized_truncated(self):
        big = _diff(*[f"+line {i}" for i in range(1000)])
        clean, codes = scrub_diff(big, DiffPolicy(capture_diffs=True, max_lines=50))
        self.assertIn(dp.DIFF_TRUNCATED, codes)
        self.assertIn("[diff truncated]", clean)
        self.assertLessEqual(len(clean.splitlines()), 52)

    def test_byte_cap(self):
        big = _diff("+" + "x" * 200000)
        clean, codes = scrub_diff(big, DiffPolicy(capture_diffs=True, max_bytes=1024))
        self.assertIn(dp.DIFF_TRUNCATED, codes)
        self.assertLessEqual(len(clean.encode("utf-8")), 1024 + 64)

    def test_binary_suppressed(self):
        clean, codes = scrub_diff(
            "diff --git a/x.bin b/x.bin\nBinary files a/x.bin and b/x.bin differ\n", ON)
        self.assertIsNone(clean)
        self.assertEqual(codes, [dp.DIFF_BINARY_SUPPRESSED])

    def test_nul_byte_suppressed(self):
        clean, codes = scrub_diff("diff --git a/x b/x\n+\x00\x00\x00\x00\n", ON)
        self.assertIsNone(clean)
        self.assertEqual(codes, [dp.DIFF_BINARY_SUPPRESSED])

    def test_non_string_never_crashes(self):
        for junk in (None, 123, {"a": 1}, ["x"], b"bytes"):
            clean, codes = scrub_diff(junk, ON)  # type: ignore[arg-type]
            self.assertIsNone(clean)


class ClipboardPreview(unittest.TestCase):
    POL = DiffPolicy(capture_diffs=False, clipboard_preview=True, clipboard_preview_chars=20)

    def test_clean_preview(self):
        prev, codes = make_clipboard_preview("just some normal copied text here", self.POL)
        self.assertTrue(prev)
        self.assertEqual(codes, {dp.CLIPBOARD_PREVIEW})
        self.assertLessEqual(len(prev), 20)

    def test_secret_drops_whole_preview(self):
        # secret in the FIRST chars
        prev, codes = make_clipboard_preview("sk-ant-abcdefghij0123456789KLMNOP rest", self.POL)
        self.assertEqual(prev, "")
        self.assertEqual(codes, set())

    def test_straddle_secret_beyond_window_drops(self):
        # the secret sits AFTER the 20-char window; a naive cut would keep the head
        # clean, but whole-string detection must still drop the preview (V-04).
        raw = "harmless prefix texttttt password=Zk9secretvalue here"
        prev, codes = make_clipboard_preview(raw, self.POL)
        self.assertEqual(prev, "")

    def test_empty(self):
        self.assertEqual(make_clipboard_preview("", self.POL), ("", set()))


if __name__ == "__main__":
    unittest.main()
