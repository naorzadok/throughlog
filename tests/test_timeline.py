import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.timeline import reconcile, effective_dt


def _ev(eid, ts, *, trust="validated", recv="", offset=0.0, tag=""):
    return {"event_id": eid, "ts_wall": ts, "recv_ts": recv, "trust": trust,
            "clock_offset_sec": offset, "payload": {"tag": tag}}


class Reconcile(unittest.TestCase):
    def test_orders_by_ts_wall_not_arrival(self):
        events = [
            _ev("b", "2026-06-21T14:00:00+03:00", tag="afternoon"),
            _ev("a", "2026-06-21T10:00:00+03:00", recv="2026-06-21T14:05:00+03:00", tag="late_morning"),
            _ev("c", "2026-06-21T16:00:00+03:00", tag="evening"),
        ]
        tags = [e["payload"]["tag"] for e in reconcile(events)]
        self.assertEqual(tags, ["late_morning", "afternoon", "evening"])

    def test_dedup_prefers_higher_trust(self):
        events = [
            _ev("dup", "2026-06-21T10:00:00+03:00", trust="low_trust", tag="lo"),
            _ev("dup", "2026-06-21T10:00:00+03:00", trust="validated", tag="hi"),
        ]
        out = reconcile(events)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["payload"]["tag"], "hi")

    def test_rejected_dropped_from_timeline(self):
        events = [
            _ev("ok", "2026-06-21T10:00:00+03:00", tag="ok"),
            _ev("bad", "2026-06-21T11:00:00+03:00", trust="rejected", tag="bad"),
        ]
        tags = [e["payload"]["tag"] for e in reconcile(events)]
        self.assertEqual(tags, ["ok"])

    def test_clock_offset_shifts_effective_order(self):
        # 'skew' has a later wall clock but its clock runs 7200s fast -> earlier really.
        events = [
            _ev("base", "2026-06-21T10:00:00+03:00", tag="base"),
            _ev("skew", "2026-06-21T11:00:00+03:00", offset=7200, tag="skew"),
        ]
        tags = [e["payload"]["tag"] for e in reconcile(events)]
        self.assertEqual(tags, ["skew", "base"])

    def test_effective_dt_unparseable(self):
        self.assertIsNone(effective_dt({"ts_wall": "not-a-date"}))


if __name__ == "__main__":
    unittest.main()
