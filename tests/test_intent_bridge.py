import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.schema import NARRATION, CLIPBOARD
from throughlog.intent.ladder import is_meaningful_narration, resolve_intent, IntentSignals
from throughlog.sources.intent_bridge import make_narration, ClipboardCapture
from throughlog.privacy.allowlist import Allowlist
from throughlog.privacy.gate import gate, Dropped

TS = "2026-06-21T14:30:00+03:00"
ALLOW = Allowlist([])


class Narration(unittest.TestCase):
    def test_meaningful(self):
        self.assertTrue(is_meaningful_narration("debugging the auth token refresh loop"))
        self.assertFalse(is_meaningful_narration("hmm"))
        self.assertFalse(is_meaningful_narration("ok stuff"))

    def test_make_narration_flags_meaningful(self):
        ev = make_narration("rewriting the privacy gate", TS)
        self.assertEqual(ev.type, NARRATION)
        self.assertTrue(ev.payload["meaningful"])

    def test_terse_note_is_flagged_not_meaningful(self):
        ev = make_narration("hmm ok", TS)
        self.assertFalse(ev.payload["meaningful"])

    def test_retroactive_since_parsed(self):
        ev = make_narration("finishing the watcher since 1pm", TS)
        self.assertIn("retroactive_since", ev.payload)
        self.assertIn("13:00:00", ev.payload["retroactive_since"])
        self.assertNotIn("since", ev.payload["note"].lower())

    def test_meaningful_narration_beats_input_density(self):
        # C6: explicit narration wins over a keystroke-rate guess; terse does not.
        good = resolve_intent(IntentSignals(title="Terminal", keys=200, duration_sec=100,
                                            narration="implementing the retry backoff"))
        self.assertEqual(good.method, "narration")
        terse = resolve_intent(IntentSignals(title="Terminal", keys=200, duration_sec=100,
                                             narration="hmm"))
        self.assertEqual(terse.method, "input")   # falls through; no fabrication


class Clipboard(unittest.TestCase):
    def test_dedup_consecutive(self):
        cap = ClipboardCapture()
        self.assertIsNotNone(cap.observe("hello world", TS))
        self.assertIsNone(cap.observe("hello world", TS))     # identical -> skipped
        self.assertIsNotNone(cap.observe("something else", TS))

    def test_url_typed_credential_dropped_through_gate(self):
        cap = ClipboardCapture()
        url = cap.observe("see https://example.com/secret-page now", TS)
        r = gate(url, ALLOW)
        self.assertEqual(r.payload.get("kind"), "url")
        self.assertNotIn("content", r.payload)
        self.assertNotIn("secret-page", r.to_json())

        cap2 = ClipboardCapture()
        cred = cap2.observe("sk-ant-api03-SECRETKEY1234567890abcdefXYZ", TS)
        self.assertIsInstance(gate(cred, ALLOW), Dropped)


if __name__ == "__main__":
    unittest.main()
