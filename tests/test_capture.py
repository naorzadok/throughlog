import json
import os
import sys
import time
import threading
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.capture import ThreadSafeEmitter, SourceSpec, Supervisor, build_sources
from throughlog.schema import make_event, FOCUS_SESSION


class FakeBus:
    """A bus stand-in whose emit is deliberately non-atomic, so a missing lock
    in the emitter would corrupt the count under concurrency."""

    def __init__(self):
        self.events = []
        self.count = 0

    def emit(self, event):
        c = self.count
        time.sleep(0)              # encourage a thread switch mid read-modify-write
        self.count = c + 1
        self.events.append(event)
        return True

    def stats(self):
        return {"written": self.count}

    def close(self):
        pass


def _ev():
    return make_event(FOCUS_SESSION, kind="os", adapter="os_focus", payload={})


class Emitter(unittest.TestCase):
    def test_forwards_to_bus(self):
        bus = FakeBus()
        em = ThreadSafeEmitter(bus)
        self.assertTrue(em.emit(_ev()))
        self.assertEqual(bus.count, 1)

    def test_pause_drops_events(self):
        bus = FakeBus()
        em = ThreadSafeEmitter(bus)
        em.paused.set()
        self.assertFalse(em.emit(_ev()))
        self.assertEqual(bus.count, 0)
        self.assertEqual(em.suppressed, 1)
        em.paused.clear()
        self.assertTrue(em.emit(_ev()))
        self.assertEqual(bus.count, 1)

    def test_thread_safe_under_concurrency(self):
        bus = FakeBus()
        em = ThreadSafeEmitter(bus)
        n_threads, per = 8, 200

        def worker():
            for _ in range(per):
                em.emit(_ev())

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(bus.count, n_threads * per)   # no lost updates


class BuildSources(unittest.TestCase):
    def test_default_set(self):
        names = [s.name for s in build_sources(
            {}, ["C:/proj"], agent_drop_dir="drop")]
        self.assertEqual(names, ["os_focus", "proc_monitor", "fs_git",
                                 "clipboard", "agent_ingest"])

    def test_flags_disable_sources(self):
        names = [s.name for s in build_sources(
            {}, [], enable_clipboard=False, enable_agents=False, agent_drop_dir="drop")]
        # no roots -> no fs_git; clipboard/agents disabled
        self.assertEqual(names, ["os_focus", "proc_monitor"])

    def test_clipboard_respects_config(self):
        cfg = {"capture": {"enable_clipboard": False}}
        names = [s.name for s in build_sources(cfg, ["C:/proj"])]
        self.assertNotIn("clipboard", names)


class SupervisorLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sal_cap_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_start_run_stop_join(self):
        bus = FakeBus()

        def fake_source(emitter, stop):
            while not stop.is_set():
                emitter.emit(_ev())
                stop.wait(0.005)

        sup = Supervisor(bus, [SourceSpec("fake", fake_source)],
                         status_path=self.tmp / "status.json")
        sup.start()
        deadline = time.time() + 2.0
        while bus.count == 0 and time.time() < deadline:
            time.sleep(0.005)
        sup.stop()
        sup.join(timeout=2.0)
        self.assertTrue(sup.stopped)
        self.assertGreater(bus.count, 0)
        self.assertFalse(any(t.is_alive() for t in sup._threads))

    def test_bad_source_does_not_crash_supervisor(self):
        bus = FakeBus()

        def boom(emitter, stop):
            raise RuntimeError("kaboom")

        def good(emitter, stop):
            emitter.emit(_ev())
            stop.wait(0.5)

        sup = Supervisor(bus, [SourceSpec("boom", boom), SourceSpec("good", good)])
        sup.start()
        deadline = time.time() + 2.0
        while bus.count == 0 and time.time() < deadline:
            time.sleep(0.005)
        sup.stop()
        sup.join(timeout=2.0)
        self.assertIn("boom", sup.errors)
        self.assertIn("kaboom", sup.errors["boom"])
        self.assertGreater(bus.count, 0)            # the good source still ran

    def test_status_written_and_parseable(self):
        bus = FakeBus()
        sup = Supervisor(bus, [SourceSpec("fake", lambda e, s: None)],
                         status_path=self.tmp / "daemon_status.json")
        sup.write_status()
        data = json.loads((self.tmp / "daemon_status.json").read_text(encoding="utf-8"))
        self.assertTrue(data["alive"])
        self.assertEqual(data["sources"], ["fake"])
        self.assertIn("stats", data)
        self.assertIn("pid", data)

    def test_toggle_pause(self):
        sup = Supervisor(FakeBus(), [])
        self.assertFalse(sup.paused.is_set())
        sup.toggle_pause()
        self.assertTrue(sup.paused.is_set())
        self.assertTrue(sup.emitter.paused.is_set())   # shares the same Event
        sup.toggle_pause()
        self.assertFalse(sup.paused.is_set())


if __name__ == "__main__":
    unittest.main()
