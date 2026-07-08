"""ThroughLog — v2 (clean rebuild).

A source-agnostic activity telemetry pipeline. Every source adapter emits a
NormalizedEvent onto a shared timeline; a deterministic privacy gate runs at
capture-time so nothing is persisted or sent to an LLM before passing it.

See the design doc / case matrix in the approved plan for the full architecture.
"""

__version__ = "2.0.0-dev"
