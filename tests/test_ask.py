import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import ask
from throughlog.llm.client import LLMError
from throughlog.llm import prompts


class _FakeClient:
    """Captures the last prompt; returns canned text or raises LLMError."""

    def __init__(self, reply="ANSWER", error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.system = self.user = None
        self.calls = 0

    def chat(self, system, user, *, temperature=0.0, max_tokens=1500, label=""):
        self.calls += 1
        self.system, self.user = system, user
        if self.error:
            raise self.error
        return self.reply


# --------------------------------------------------------------------------- #
# Pure retrieval
# --------------------------------------------------------------------------- #
class SplitMarkdown(unittest.TestCase):
    def test_splits_on_headings_and_keeps_preamble(self):
        md = "intro line\n\n# Title\nbody a\n\n## Sub\nbody b"
        out = ask.split_markdown(md)
        self.assertEqual(out[0], ("", "intro line"))
        self.assertEqual(out[1], ("Title", "body a"))
        self.assertEqual(out[2], ("Sub", "body b"))

    def test_empty_text_is_no_passages(self):
        self.assertEqual(ask.split_markdown(""), [])


class Ranking(unittest.TestCase):
    def setUp(self):
        self.passages = [
            ask.Passage("checkout/overview › Narrative",
                        "Fixed the coupon rounding bug in the checkout cart and committed it."),
            ask.Passage("infra/overview › Narrative",
                        "Upgraded the database and rotated the deploy credentials."),
            ask.Passage("checkout/archive › Files",
                        "coupon.ts cart.ts"),
        ]

    def test_relevant_passage_ranks_first(self):
        ranked = ask.rank_passages("what coupon work did I do in checkout?", self.passages)
        self.assertTrue(ranked)
        self.assertIn("coupon", ranked[0].text.lower())
        self.assertTrue(ranked[0].score >= ranked[-1].score)

    def test_unrelated_question_returns_nothing(self):
        self.assertEqual(ask.rank_passages("kubernetes ingress tls", self.passages), [])

    def test_stopwords_do_not_score(self):
        # A question of pure stopwords yields no query terms -> no matches.
        self.assertEqual(ask.rank_passages("what did i do", self.passages), [])

    def test_ranking_is_deterministic(self):
        q = "checkout coupon cart"
        a = [(p.source, p.score) for p in ask.rank_passages(q, self.passages)]
        b = [(p.source, p.score) for p in ask.rank_passages(q, self.passages)]
        self.assertEqual(a, b)


class LoadCorpus(unittest.TestCase):
    def _write(self, root: Path):
        (root / "project_checkout").mkdir(parents=True)
        (root / "project_checkout" / "overview.md").write_text(
            "# Checkout\n## Current State\nCoupon rounding fixed.", encoding="utf-8")
        (root / "project_infra").mkdir(parents=True)
        (root / "project_infra" / "overview.md").write_text(
            "# Infra\n## Current State\nDatabase upgraded.", encoding="utf-8")
        (root / "daily.md").write_text("## 2026-06-26\n**checkout** — shipped coupon fix",
                                       encoding="utf-8")

    def test_labels_strip_project_prefix_and_keep_heading(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root)
            corpus = ask.load_corpus(root)
            labels = {p.source for p in corpus}
            self.assertIn("checkout/overview › Current State", labels)
            self.assertTrue(any(s.startswith("daily") for s in labels))

    def test_project_filter(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root)
            corpus = ask.load_corpus(root, project="checkout")
            self.assertTrue(corpus)
            self.assertTrue(all("infra" not in p.source for p in corpus))

    def test_missing_dir_is_empty(self):
        self.assertEqual(ask.load_corpus(Path("does-not-exist-xyz")), [])


# --------------------------------------------------------------------------- #
# answer() — the thin LLM driver
# --------------------------------------------------------------------------- #
class AnswerDriver(unittest.TestCase):
    def setUp(self):
        self.passages = [
            ask.Passage("checkout/overview › Narrative",
                        "Fixed the coupon rounding bug and committed CHK-241."),
        ]

    def test_no_match_short_circuits_without_calling_llm(self):
        client = _FakeClient()
        ans = ask.answer("unrelated kubernetes question", self.passages, client)
        self.assertFalse(ans.used_llm)
        self.assertEqual(client.calls, 0)
        self.assertEqual(ans.text, ask._NO_MATCH)

    def test_no_client_returns_deterministic_passages(self):
        ans = ask.answer("coupon rounding", self.passages, None)
        self.assertFalse(ans.used_llm)
        self.assertIn("coupon rounding", ans.text.lower())
        self.assertIn("checkout/overview › Narrative", ans.sources)

    def test_client_answer_passes_through_and_sees_question(self):
        client = _FakeClient(reply="You fixed coupon rounding [checkout/overview › Narrative].")
        ans = ask.answer("what coupon work did I do?", self.passages, client)
        self.assertTrue(ans.used_llm)
        self.assertEqual(ans.text, "You fixed coupon rounding [checkout/overview › Narrative].")
        self.assertIn("coupon", client.user.lower())
        self.assertIn("checkout/overview › Narrative", client.user)

    def test_llm_error_degrades_never_raises(self):
        client = _FakeClient(error=LLMError("rate limited"))
        ans = ask.answer("coupon rounding", self.passages, client)
        self.assertFalse(ans.used_llm)
        self.assertIn("rate limited", ans.error)
        self.assertIn("coupon", ans.text.lower())

    def test_empty_llm_reply_falls_back_to_passages(self):
        ans = ask.answer("coupon rounding", self.passages, _FakeClient(reply="   "))
        self.assertTrue(ans.used_llm)
        self.assertIn("coupon", ans.text.lower())


class AskPrompt(unittest.TestCase):
    def test_prompt_carries_question_and_sources(self):
        system, user = prompts.build_ask_prompt(
            "what did I ship?", [("checkout/overview › Narrative", "shipped coupon fix")])
        self.assertIn("what did I ship?", user)
        self.assertIn("checkout/overview › Narrative", user)
        self.assertIn("shipped coupon fix", user)
        self.assertIn("only", system.lower())   # grounded-only instruction


if __name__ == "__main__":
    unittest.main()
