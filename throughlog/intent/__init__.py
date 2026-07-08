"""Intent resolution — deterministic 'what is this work about' signal extraction.

No LLM here. The ladder produces the best available *intent descriptor* and
records which rung produced it; genuine ambiguity becomes needs_review, which
Phase 1 (the only categorization LLM) may later resolve."""
