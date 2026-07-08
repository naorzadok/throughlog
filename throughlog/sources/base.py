"""SourceAdapter interface.

Every source (os_focus, fs_git, intent_bridge, agent_ingest) follows the same
contract: it produces NormalizedEvents and hands each to the bus, which runs the
privacy gate before anything is persisted. Adapters NEVER write to disk and NEVER
talk to an LLM — they are part of the deterministic capture layer.

The contract is deliberately tiny. A "driver" (live OS loop, file watcher, drop
folder, etc.) turns real-world signals into events and calls ``bus.emit``. The
deterministic *logic* of each adapter is kept in a pure, clock-injected core
(e.g. ``FocusSessionizer``) so the same code runs under live capture and under
the scenario simulator.
"""

from __future__ import annotations

from typing import Protocol

from throughlog.schema import NormalizedEvent


class Emitter(Protocol):
    """Anything the adapter can push a finished event into (the bus, or a list)."""

    def emit(self, event: NormalizedEvent) -> bool: ...


class SourceAdapter(Protocol):
    """A runnable source. ``run`` blocks, feeding events to ``emitter`` until
    stopped; deterministic cores are tested directly, not through ``run``."""

    name: str

    def run(self, emitter: Emitter) -> None: ...
