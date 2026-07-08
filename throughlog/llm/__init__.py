"""LLM access — the ONLY door to a remote model, used in just two permitted
places: Phase 1 categorization of genuinely ambiguous intent, and Phase 2 prose
synthesis. Everything else in the pipeline is deterministic and never calls out.

The client is stdlib-only (urllib), targets any OpenAI-compatible chat endpoint
(OpenRouter by default; Ollama/Anthropic-compatible swappable via config), and
re-runs the egress gate on every outbound prompt as defense-in-depth — so a gate
bug upstream still cannot leak a secret to a third party.
"""
