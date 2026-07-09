import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.llm.ratelimit import RateLimiter, WINDOW_SEC


class FakeClock:
    """A virtual clock: ``sleep`` records the delay and advances ``now`` by it, so the
    limiter's pacing is exercised deterministically with no real time."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.slept.append(dt)
        self.t += dt


def _limiter(max_per_min, clock):
    return RateLimiter(max_per_min, monotonic=clock.monotonic, sleep=clock.sleep)


class Disabled(unittest.TestCase):
    def test_zero_is_noop(self):
        clock = FakeClock()
        rl = _limiter(0, clock)
        self.assertFalse(rl.enabled)
        for _ in range(100):
            rl.acquire()
        self.assertEqual(clock.slept, [])          # never paces when disabled

    def test_negative_is_noop(self):
        clock = FakeClock()
        rl = _limiter(-5, clock)
        for _ in range(10):
            rl.acquire()
        self.assertEqual(clock.slept, [])

    def test_bad_value_coerces_to_disabled(self):
        self.assertEqual(RateLimiter("nonsense").max_per_min, 0)
        self.assertEqual(RateLimiter(None).max_per_min, 0)


class Pacing(unittest.TestCase):
    def test_bursts_up_to_limit_then_sleeps_a_full_window(self):
        clock = FakeClock()
        rl = _limiter(2, clock)
        rl.acquire()                                # t=0, slot 1
        rl.acquire()                                # t=0, slot 2 — window now full
        self.assertEqual(clock.slept, [])
        rl.acquire()                                # must wait a full 60s (oldest at t=0)
        self.assertEqual(clock.slept, [WINDOW_SEC])
        self.assertEqual(clock.t, WINDOW_SEC)

    def test_partial_wait_only_for_remaining_window(self):
        clock = FakeClock()
        rl = _limiter(1, clock)
        rl.acquire()                                # t=0
        clock.t = 10.0                              # 10s of real work elapses
        rl.acquire()                                # oldest is 50s from ageing out
        self.assertEqual(clock.slept, [WINDOW_SEC - 10.0])

    def test_natural_ageing_needs_no_sleep(self):
        clock = FakeClock()
        rl = _limiter(2, clock)
        rl.acquire()
        rl.acquire()                                # window full at t=0
        clock.t = WINDOW_SEC + 1.0                  # both slots have aged out
        rl.acquire()
        self.assertEqual(clock.slept, [])           # room again without pacing

    def test_identical_burst_frees_together_after_one_wait(self):
        clock = FakeClock()
        rl = _limiter(3, clock)
        for _ in range(3):                          # fill the window all at t=0
            rl.acquire()
        for _ in range(3):                          # they age out together -> one wait frees 3
            rl.acquire()
        self.assertEqual(clock.slept, [WINDOW_SEC])

    def test_spaced_fills_pace_each_extra_call(self):
        clock = FakeClock()
        rl = _limiter(2, clock)
        rl.acquire()                                # t=0
        clock.t = 30.0
        rl.acquire()                                # t=30 -> window holds {0, 30}
        rl.acquire()                                # waits 30s (until the t=0 hit ages out)
        rl.acquire()                                # waits another 30s (until t=30 ages out)
        self.assertEqual(clock.slept, [30.0, 30.0])


class ThreadSafety(unittest.TestCase):
    def test_concurrent_acquire_does_not_race(self):
        # A generous limit means no real sleeping; we only assert the lock keeps the
        # internal deque consistent under contention (no exception, bounded window).
        rl = RateLimiter(10_000)
        errors: list[BaseException] = []

        def worker():
            try:
                for _ in range(50):
                    rl.acquire()
            except BaseException as exc:            # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(len(rl._hits), 10_000)


if __name__ == "__main__":
    unittest.main()
