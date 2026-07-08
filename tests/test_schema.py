import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.schema import NormalizedEvent, make_event, validate, FILE_CHANGE


class SchemaRoundTrip(unittest.TestCase):
    def test_dict_round_trip(self):
        e = make_event(FILE_CHANGE, kind="fs", adapter="fs_git", payload={"path": "x"})
        e2 = NormalizedEvent.from_dict(e.to_dict())
        self.assertEqual(e2.type, e.type)
        self.assertEqual(e2.source.kind, "fs")
        self.assertEqual(e2.payload, {"path": "x"})
        self.assertEqual(e2.event_id, e.event_id)

    def test_json_round_trip(self):
        e = make_event(FILE_CHANGE, kind="fs", adapter="fs_git", payload={"path": "x"})
        e2 = NormalizedEvent.from_json(e.to_json())
        self.assertEqual(e2.to_dict(), e.to_dict())


class TransientPayloadKeys(unittest.TestCase):
    """V-02 — payload keys beginning with `_` are transients that must NEVER be
    serialized to disk or egress, regardless of any writer/crash ordering."""

    def test_underscore_keys_dropped_from_dict_and_json(self):
        e = make_event(FILE_CHANGE, kind="fs", adapter="fs_git",
                       payload={"path": "x", "_diff_clean": "SECRET DIFF BODY"})
        self.assertNotIn("_diff_clean", e.to_dict()["payload"])
        self.assertIn("path", e.to_dict()["payload"])
        self.assertNotIn("SECRET DIFF BODY", e.to_json())
        self.assertNotIn("_diff_clean", e.to_json())

    def test_in_memory_payload_untouched(self):
        # serialization filters; it does not mutate the live object.
        e = make_event(FILE_CHANGE, kind="fs", adapter="fs_git",
                       payload={"path": "x", "_diff_clean": "body"})
        e.to_json()
        self.assertIn("_diff_clean", e.payload)


class SchemaValidation(unittest.TestCase):
    def test_valid(self):
        e = make_event(FILE_CHANGE, kind="fs", adapter="fs_git", payload={"path": "x"})
        self.assertEqual(validate(e.to_dict()), [])

    def test_bad_type(self):
        d = make_event(FILE_CHANGE, kind="fs", adapter="fs_git").to_dict()
        d["type"] = "NOPE"
        self.assertTrue(any("type" in m for m in validate(d)))

    def test_bad_source_kind(self):
        d = make_event(FILE_CHANGE, kind="fs", adapter="fs_git").to_dict()
        d["source"]["kind"] = "alien"
        self.assertTrue(validate(d))

    def test_bad_timestamp(self):
        d = make_event(FILE_CHANGE, kind="fs", adapter="fs_git").to_dict()
        d["ts_wall"] = "not-a-date"
        self.assertTrue(any("ts_wall" in m for m in validate(d)))


if __name__ == "__main__":
    unittest.main()
