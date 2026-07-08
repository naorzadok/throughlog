"""Deterministic privacy layer.

The gate is a mandatory chokepoint: the event bus runs `gate()` before any
persistence, and `egress.egress_check()` runs again before any remote-LLM send.
Redaction never depends on an LLM.
"""
