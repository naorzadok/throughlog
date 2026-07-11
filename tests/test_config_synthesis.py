"""config.synthesis_options_from — the batching knobs (entry_batch / max_input_tokens /
max_batch_days) and the model-tier "auto" budget resolution (budget_for_model).

Locks the byte-identity contract: a missing/legacy `synthesis` block yields the OLD
per-day, condense-on-overflow behavior (entry_batch=day, max_input_tokens=0).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.config import budget_for_model, synthesis_options_from


class BudgetForModel(unittest.TestCase):
    def test_free_tier_is_conservative(self):
        self.assertEqual(budget_for_model("nvidia/nemotron-3-super-120b-a12b:free"), 6000)
        self.assertEqual(budget_for_model("qwen/qwen3-next-80b-a3b-instruct:free"), 6000)

    def test_frontier_long_context_is_high(self):
        for m in ("anthropic/claude-sonnet-5", "openai/gpt-5", "google/gemini-2.5-pro"):
            self.assertEqual(budget_for_model(m), 200000)

    def test_other_capable_model_is_mid(self):
        self.assertEqual(budget_for_model("llama3.1:8b"), 16000)
        self.assertEqual(budget_for_model(""), 16000)


class OptionsFrom(unittest.TestCase):
    def test_legacy_missing_block_is_byte_identical_defaults(self):
        o = synthesis_options_from({})
        self.assertEqual(o.entry_batch, "day")
        self.assertEqual(o.max_input_tokens, 0)          # 0 => legacy condense path
        self.assertEqual(o.max_batch_days, 7)

    def test_auto_resolves_from_model(self):
        cfg = {"llm": {"model": "x:free"},
               "synthesis": {"entry_batch": "adaptive", "max_input_tokens": "auto"}}
        self.assertEqual(synthesis_options_from(cfg).max_input_tokens, 6000)
        cfg["llm"]["model"] = "anthropic/claude-sonnet-5"
        self.assertEqual(synthesis_options_from(cfg).max_input_tokens, 200000)

    def test_explicit_int_and_numeric_string(self):
        self.assertEqual(
            synthesis_options_from({"synthesis": {"max_input_tokens": 8000}}).max_input_tokens,
            8000)
        self.assertEqual(
            synthesis_options_from({"synthesis": {"max_input_tokens": "12000"}}).max_input_tokens,
            12000)

    def test_bad_values_fall_back_safely(self):
        o = synthesis_options_from({"synthesis": {"entry_batch": "weekly",   # not a valid enum
                                                  "max_input_tokens": "lots",
                                                  "max_batch_days": 0}})
        self.assertEqual(o.entry_batch, "day")           # unknown -> day
        self.assertEqual(o.max_input_tokens, 0)          # junk -> legacy
        self.assertEqual(o.max_batch_days, 7)            # invalid 0 -> default 7, never < 1

    def test_negative_batch_days_clamped_to_one(self):
        self.assertEqual(
            synthesis_options_from({"synthesis": {"max_batch_days": -3}}).max_batch_days, 1)


if __name__ == "__main__":
    unittest.main()
