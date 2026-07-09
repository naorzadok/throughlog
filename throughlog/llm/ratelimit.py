"""A tiny request-rate gate for the LLM egress door — the "timing manager".

Free-tier providers throttle bursts with HTTP 429 (OpenRouter's ``:free`` models cap
at ~20 requests/min), and every 429'd *retry* is itself a fresh request the limiter
counts — so an un-paced run spirals: bursting past the limit provokes retries that push
it further past. This gate **paces** the physical requests instead. It only ever *delays*
a call until a slot is free; it **never refuses one** — a call the pipeline decided it
needs always goes through, just not faster than the configured rate.

Pure and clock-injectable so the pacing decision is unit-testable with no real time
(`monotonic` and `sleep` are injected). A sliding 60-second window maps 1:1 to the
provider's "N requests / minute" wording. ``max_per_min <= 0`` disables the gate
entirely, so with the knob unset behavior is byte-identical to having no gate at all.

Wired into ``throughlog.llm.client.LLMClient._post`` (the physical-request layer) so
retries and fallback-model requests are paced too — they are exactly the requests that
count against the per-minute limit.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

WINDOW_SEC = 60.0


class RateLimiter:
    """Allow at most ``max_per_min`` calls to :meth:`acquire` within any trailing
    60-second window, blocking (sleeping) the caller when the window is full.

    Thread-safe: the capture supervisor runs source adapters in their own threads, so a
    shared client's requests must serialize through one gate. The lock is intentionally
    held across the sleep — that is what makes concurrent callers queue instead of all
    waking to fire at once."""

    def __init__(self, max_per_min: int, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        try:
            self.max_per_min = int(max_per_min)
        except (TypeError, ValueError):
            self.max_per_min = 0
        self._mono = monotonic
        self._sleep = sleep
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.max_per_min > 0

    def acquire(self) -> None:
        """Reserve one request slot, sleeping until one is free. Returns once the call
        may proceed — it paces, it never rejects (so a needed pass is delayed, not
        blocked). A no-op when the gate is disabled (``max_per_min <= 0``)."""
        if self.max_per_min <= 0:
            return
        with self._lock:
            self._evict(self._mono())
            if len(self._hits) >= self.max_per_min:
                # Window full: wait until the oldest hit ages out of the 60s window.
                wait = WINDOW_SEC - (self._mono() - self._hits[0])
                if wait > 0:
                    self._sleep(wait)
                self._evict(self._mono())
            self._hits.append(self._mono())

    def _evict(self, now: float) -> None:
        """Drop timestamps that have fallen out of the trailing window."""
        while self._hits and now - self._hits[0] >= WINDOW_SEC:
            self._hits.popleft()
