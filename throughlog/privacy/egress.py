"""Egress control — the second, independent gate before any remote-LLM send.

Even though every persisted event has already passed the gate, prose assembled
for an LLM prompt (Phase 1 / Phase 2) is re-scrubbed here as defense-in-depth.
Local Ollama is in-machine but still passes through this for consistency.
"""

from __future__ import annotations

from throughlog.privacy import redactors


def egress_check(text: str) -> tuple[str, list[str]]:
    """Scrub text bound for a remote model. Returns (clean_text, leaks_caught).

    A non-empty `leaks_caught` from already-gated data indicates a gate bug;
    the text is still scrubbed so nothing leaks regardless.
    """
    return redactors.scrub(text)


def assert_clean(text: str) -> list[str]:
    """Return the list of secret/path leaks present in `text` (empty => clean)."""
    _, leaks = redactors.scrub(text)
    return leaks
