import io
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.schema import (
    make_event,
    FOCUS_SESSION, FILE_CHANGE, GIT_COMMIT, NARRATION, IDLE_START,
)
from throughlog.categorize import (
    signal_stack, categorize_events, event_summary,
    _extract_json_object, _parse_assignments, THRESHOLD,
)
from throughlog.llm.client import (
    LLMClient, LLMConfig, LLMError,
    _classify_error, _overall_exit,
    SMOKE_OK, SMOKE_HARD, SMOKE_RATELIMIT, SMOKE_NO_KEY,
)

TS = "2026-06-21T15:00:00+03:00"

# Inline registry mirroring projects.json structure (stable, decoupled from the
# live file). Real Windows project paths so the gate's `~` normalization matters.
PROJECTS = [
    {"id": "shoes", "name": "Shoes", "status": "active",
     "description": "training shoe research with lateral stability scoring",
     "signals": {
         "paths": [r"C:\Users\dev\Desktop\projects\shoe-research"],
         "git_remotes": [], "jira_prefixes": [],
         "keywords": ["training shoes", "crossfit", "heel drop"],
         "apps": ["EXCEL.EXE"], "domains": ["runrepeat.com"],
         "window_patterns": [".*shoe.*review.*"]}},
    {"id": "logger", "name": "Logger", "status": "active",
     "description": "activity logger pipeline",
     "signals": {
         "paths": [r"C:\Users\dev\Desktop\projects\throughlog"],
         "git_remotes": ["github.com/naorzadok/throughlog"],
         "jira_prefixes": ["TL"],
         "keywords": ["capture daemon", "synthesizer"],
         "apps": ["Code.exe"], "domains": [],
         "window_patterns": [".*throughlog.*"]}},
]


def focus(anchor, process="", active_file=None, intent_label=""):
    payload = {"anchor": anchor, "process": process, "active_file": active_file,
               "satellites": []}
    if intent_label:
        payload["intent"] = {"label": intent_label, "method": "narration", "confidence": 0.5}
    return make_event(FOCUS_SESSION, kind="os", adapter="os_focus",
                      payload=payload, ts_wall=TS)


# --------------------------------------------------------------------------- #
class SignalStack(unittest.TestCase):
    def test_path_under_project_dir_wins(self):
        # A gated FILE_CHANGE path (home already normalized to ~).
        ev = make_event(FILE_CHANGE, kind="fs", adapter="fs_git", ts_wall=TS,
                        payload={"path": "~/Desktop/projects/throughlog/throughlog/x.py"})
        pid, score, method, _ = signal_stack(ev, PROJECTS)
        self.assertEqual((pid, method), ("logger", "signal_path"))
        self.assertEqual(score, 0.95)

    def test_domain_in_title(self):
        pid, score, method, _ = signal_stack(focus("runrepeat.com best trainers"), PROJECTS)
        self.assertEqual((pid, method, score), ("shoes", "signal_domain", 0.80))

    def test_git_remote_in_commit_message(self):
        ev = make_event(GIT_COMMIT, kind="git", adapter="fs_git", ts_wall=TS,
                        payload={"repo": "~/Desktop/other/repo",
                                 "message": "mirror pushed to github.com/naorzadok/throughlog"})
        pid, score, method, _ = signal_stack(ev, PROJECTS)
        self.assertEqual((pid, method, score), ("logger", "signal_git", 0.82))

    def test_jira_prefix_in_title(self):
        pid, score, method, _ = signal_stack(focus("TL-142 fix the gate"), PROJECTS)
        self.assertEqual((pid, method, score), ("logger", "signal_jira", 0.85))

    def test_window_pattern(self):
        pid, score, method, _ = signal_stack(focus("my throughlog window"), PROJECTS)
        self.assertEqual((pid, method, score), ("logger", "signal_pattern", 0.75))

    def test_app_only_match(self):
        pid, score, method, _ = signal_stack(focus("notes.txt - Editor", process="Code.exe"),
                                             PROJECTS)
        self.assertEqual((pid, method, score), ("logger", "signal_app", 0.70))

    def test_narration_keyword_note(self):
        ev = make_event(NARRATION, kind="intent", adapter="intent_bridge", ts_wall=TS,
                        payload={"note": "rewriting the synthesizer module", "meaningful": True})
        pid, score, method, _ = signal_stack(ev, PROJECTS)
        self.assertEqual((pid, method, score), ("logger", "signal_note", 0.72))

    def test_title_keyword_density(self):
        pid, score, method, _ = signal_stack(focus("crossfit heel drop notes"), PROJECTS)
        self.assertEqual((pid, method), ("shoes", "signal_keyword"))
        self.assertGreaterEqual(score, 0.51)

    def test_no_signal_unresolved(self):
        pid, score, method, _ = signal_stack(focus("Solitaire", process="sol.exe"), PROJECTS)
        self.assertEqual((pid, score, method), (None, 0.0, "unresolved"))


# --------------------------------------------------------------------------- #
class FakeClient:
    """Stands in for LLMClient: returns a canned reply or raises."""
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0
        self.last = None

    def chat(self, system, user, **kw):
        self.calls += 1
        self.last = (system, user)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _assign_json(index, pid, conf):
    return json.dumps({"assignments": [
        {"index": index, "project_id": pid, "confidence": conf, "reason": "test"}]})


class Deterministic(unittest.TestCase):
    def test_confident_assigned_without_llm(self):
        client = FakeClient(RuntimeError("LLM must not be called"))
        ev = focus("crossfit heel drop comparison")     # keyword -> >=0.51
        categorize_events([ev], PROJECTS, client=client)
        self.assertEqual(ev.attribution.project_id, "shoes")
        self.assertEqual(client.calls, 0)

    def test_ambiguous_without_client_is_needs_review(self):
        ev = focus("Solitaire", process="sol.exe")
        categorize_events([ev], PROJECTS, client=None)
        self.assertEqual(ev.attribution.method, "needs_review")
        self.assertIsNone(ev.attribution.project_id)

    def test_idle_events_are_skipped(self):
        ev = make_event(IDLE_START, kind="os", adapter="os_focus", ts_wall=TS,
                        payload={"idle_after_sec": 700})
        categorize_events([ev], PROJECTS, client=None)
        self.assertIsNone(ev.attribution.method)       # untouched default

    def test_no_event_is_dropped(self):
        evs = [focus("crossfit heel drop"), focus("Solitaire", process="sol.exe")]
        out = categorize_events(evs, PROJECTS, client=None)
        self.assertEqual(len(out), 2)


class MockedLLM(unittest.TestCase):
    def _ambiguous(self):
        # signal-invisible, but text-bearing so it reaches the LLM
        return focus("Untitled", process="mystery.exe",
                     intent_label="planning the lateral stability writeup")

    def test_valid_assignment_uses_llm(self):
        ev = self._ambiguous()
        client = FakeClient(_assign_json(0, "shoes", 0.9))
        categorize_events([ev], PROJECTS, client=client)
        self.assertEqual(ev.attribution.project_id, "shoes")
        self.assertEqual(ev.attribution.method, "llm")
        self.assertEqual(client.calls, 1)

    def test_below_threshold_is_needs_review(self):
        ev = self._ambiguous()
        categorize_events([ev], PROJECTS, client=FakeClient(_assign_json(0, "shoes", 0.40)))
        self.assertEqual(ev.attribution.method, "needs_review")
        self.assertIsNone(ev.attribution.project_id)
        self.assertEqual(ev.attribution.confidence, 0.40)   # sub-threshold conf kept

    def test_hallucinated_project_id_rejected(self):
        ev = self._ambiguous()
        categorize_events([ev], PROJECTS, client=FakeClient(_assign_json(0, "does-not-exist", 0.99)))
        self.assertEqual(ev.attribution.method, "needs_review")
        self.assertIsNone(ev.attribution.project_id)

    def test_robust_parse_through_fences_and_think(self):
        ev = self._ambiguous()
        wrapped = ("<think>the footwear hint points to shoes</think>\n"
                   "Sure, here you go:\n```json\n" + _assign_json(0, "shoes", 0.8) + "\n```")
        categorize_events([ev], PROJECTS, client=FakeClient(wrapped))
        self.assertEqual(ev.attribution.project_id, "shoes")
        self.assertEqual(ev.attribution.method, "llm")


class C5Failure(unittest.TestCase):
    def test_llm_transport_failure_preserves_as_needs_review(self):
        evs = [MockedLLM()._ambiguous()]
        client = FakeClient(LLMError("openrouter down"))
        out = categorize_events(evs, PROJECTS, client=client)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].attribution.method, "needs_review")

    def test_unparseable_retries_then_needs_review(self):
        ev = MockedLLM()._ambiguous()
        client = FakeClient("I cannot help with that.")     # never valid JSON
        categorize_events([ev], PROJECTS, client=client)
        self.assertEqual(ev.attribution.method, "needs_review")
        self.assertEqual(client.calls, 2)                   # 1 + one stricter re-ask


class ParseHelpers(unittest.TestCase):
    def test_extract_object_from_prose(self):
        obj = _extract_json_object('blah {"assignments": []} trailing')
        self.assertEqual(obj, {"assignments": []})

    def test_parse_coerces_null_and_bad_index(self):
        raw = json.dumps({"assignments": [
            {"index": 1, "project_id": "null", "confidence": 0.2},
            {"index": "x", "project_id": "shoes", "confidence": 0.9},   # bad index dropped
        ]})
        parsed = _parse_assignments(raw, {"shoes", "logger"})
        self.assertIn(1, parsed)
        self.assertIsNone(parsed[1]["project_id"])
        self.assertNotIn("x", parsed)


# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, body: bytes):
        self._b = body
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _opener_returning(obj, capture=None):
    body = json.dumps(obj).encode("utf-8")
    def opener(req, timeout=None):
        if capture is not None:
            capture.append(req.data.decode("utf-8"))
        return _Resp(body)
    return opener


def _cfg(**kw):
    base = dict(model="test/model", api_key="test-key", api_key_env="SAL_UNUSED_ENV_XYZ",
                max_retries=3)
    base.update(kw)
    return LLMConfig(**base)


class Client(unittest.TestCase):
    def test_returns_content(self):
        opener = _opener_returning({"choices": [{"message": {"content": "pong"}}]})
        c = LLMClient(_cfg(), opener=opener, sleep=lambda _: None)
        self.assertEqual(c.chat("s", "u"), "pong")

    def test_egress_scrubs_outbound_secret(self):
        cap = []
        opener = _opener_returning({"choices": [{"message": {"content": "ok"}}]}, capture=cap)
        c = LLMClient(_cfg(), opener=opener, sleep=lambda _: None)
        c.chat("system", "leaked sk-ant-api03-SECRETKEY1234567890abcdefXYZ here")
        sent = cap[0]
        self.assertNotIn("sk-ant-api03-SECRETKEY", sent)
        self.assertIn("[REDACTED", sent)

    def test_reasoning_channel_fallback(self):
        opener = _opener_returning({"choices": [{"message": {"content": "", "reasoning": "pong"}}]})
        c = LLMClient(_cfg(), opener=opener, sleep=lambda _: None)
        self.assertEqual(c.chat("s", "u"), "pong")

    def test_retries_transient_then_succeeds(self):
        state = {"n": 0}
        good = json.dumps({"choices": [{"message": {"content": "pong"}}]}).encode()
        def opener(req, timeout=None):
            state["n"] += 1
            if state["n"] < 3:
                raise urllib.error.URLError("temporary")
            return _Resp(good)
        c = LLMClient(_cfg(max_retries=3), opener=opener, sleep=lambda _: None)
        self.assertEqual(c.chat("s", "u"), "pong")
        self.assertEqual(state["n"], 3)

    def test_http_400_is_terminal(self):
        def opener(req, timeout=None):
            raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"bad request"))
        c = LLMClient(_cfg(), opener=opener, sleep=lambda _: None)
        with self.assertRaises(LLMError):
            c.chat("s", "u")

    def test_missing_key_is_error(self):
        # api_key empty and the named env var unset -> no key resolvable
        os.environ.pop("SAL_UNUSED_ENV_XYZ", None)
        c = LLMClient(_cfg(api_key=""), opener=_opener_returning({}), sleep=lambda _: None)
        with self.assertRaises(LLMError):
            c.chat("s", "u")

    def test_no_reasoning_param_by_default(self):
        # Default config omits the reasoning field entirely (wire byte-compatible).
        cap = []
        opener = _opener_returning({"choices": [{"message": {"content": "ok"}}]},
                                   capture=cap)
        c = LLMClient(_cfg(), opener=opener, sleep=lambda _: None)
        c.chat("s", "u")
        self.assertNotIn("reasoning", json.loads(cap[0]))

    def test_reasoning_effort_sent_when_set(self):
        cap = []
        opener = _opener_returning({"choices": [{"message": {"content": "ok"}}]},
                                   capture=cap)
        c = LLMClient(_cfg(reasoning_effort="high"), opener=opener, sleep=lambda _: None)
        c.chat("s", "u")
        self.assertEqual(json.loads(cap[0])["reasoning"], {"effort": "high"})

    def test_from_config_validates_reasoning_effort(self):
        ok = LLMConfig.from_config({"llm": {"reasoning_effort": "MEDIUM"}})
        self.assertEqual(ok.reasoning_effort, "medium")          # normalized
        bad = LLMConfig.from_config({"llm": {"reasoning_effort": "turbo"}})
        self.assertEqual(bad.reasoning_effort, "")               # invalid -> default
        missing = LLMConfig.from_config({"llm": {}})
        self.assertEqual(missing.reasoning_effort, "")

    def test_from_config_reads_model_fallback(self):
        cfg = LLMConfig.from_config({"llm": {"model": "a", "model_fallback": "b"}})
        self.assertEqual(cfg.model_fallback, "b")
        self.assertEqual(LLMConfig.from_config({"llm": {"model": "a"}}).model_fallback, "")

    def test_model_chain_orders_dedups_and_skips_empty(self):
        self.assertEqual(LLMClient(_cfg())._model_chain(), ["test/model"])
        self.assertEqual(LLMClient(_cfg(model_fallback="backup"))._model_chain(),
                         ["test/model", "backup"])
        # fallback equal to primary -> not duplicated
        self.assertEqual(LLMClient(_cfg(model_fallback="test/model"))._model_chain(),
                         ["test/model"])

    def test_falls_back_to_second_model_when_primary_rate_limited(self):
        seen = []
        good = json.dumps({"choices": [{"message": {"content": "pong"}}]}).encode()

        def opener(req, timeout=None):
            model = json.loads(req.data.decode("utf-8"))["model"]
            seen.append(model)
            if model == "test/model":            # primary is 429 -> exhausts retries
                raise urllib.error.HTTPError("u", 429, "rate", {},
                                             io.BytesIO(b"rate-limited"))
            return _Resp(good)                   # fallback answers

        c = LLMClient(_cfg(model_fallback="test/backup"), opener=opener,
                      sleep=lambda _: None)
        self.assertEqual(c.chat("s", "u"), "pong")
        self.assertIn("test/model", seen)        # primary attempted
        self.assertEqual(seen[-1], "test/backup")  # then fell back

    def test_raises_only_after_all_models_exhausted(self):
        seen = []

        def opener(req, timeout=None):
            seen.append(json.loads(req.data.decode("utf-8"))["model"])
            raise urllib.error.HTTPError("u", 429, "rate", {}, io.BytesIO(b"rl"))

        c = LLMClient(_cfg(max_retries=1, model_fallback="test/backup"),
                      opener=opener, sleep=lambda _: None)
        with self.assertRaises(LLMError):
            c.chat("s", "u")
        self.assertEqual(seen, ["test/model", "test/backup"])   # both tried before failing

    def test_from_config_reads_max_requests_per_min(self):
        self.assertEqual(
            LLMConfig.from_config({"llm": {"max_requests_per_min": 18}}).max_requests_per_min, 18)
        self.assertEqual(LLMConfig.from_config({"llm": {}}).max_requests_per_min, 0)
        # junk / negative -> disabled (never a crash, never a negative rate)
        self.assertEqual(
            LLMConfig.from_config({"llm": {"max_requests_per_min": "x"}}).max_requests_per_min, 0)
        self.assertEqual(
            LLMConfig.from_config({"llm": {"max_requests_per_min": -4}}).max_requests_per_min, 0)

    def test_rate_limiter_disabled_by_default(self):
        # No config knob -> the gate is a no-op, so the wire path is byte-identical.
        self.assertFalse(LLMClient(_cfg())._limiter.enabled)

    def test_rate_limiter_paces_physical_requests(self):
        # Swap in a virtual-clock limiter so pacing is deterministic; this also proves
        # _post gates on acquire() (a paced request sleeps exactly one window).
        from throughlog.llm.ratelimit import RateLimiter, WINDOW_SEC
        slept: list[float] = []
        now = {"t": 0.0}

        def mono():
            return now["t"]

        def sleep(dt):
            slept.append(dt)
            now["t"] += dt

        opener = _opener_returning({"choices": [{"message": {"content": "ok"}}]})
        c = LLMClient(_cfg(max_requests_per_min=2), opener=opener, sleep=lambda _: None)
        c._limiter = RateLimiter(2, monotonic=mono, sleep=sleep)
        c.chat("s", "u")
        c.chat("s", "u")                       # two requests fill the 60s window at t=0
        self.assertEqual(slept, [])
        c.chat("s", "u")                       # third is paced by a full window
        self.assertEqual(slept, [WINDOW_SEC])


class SmokeDiagnostics(unittest.TestCase):
    """The smoke must tell a transient 429 (account fine, retry/credits) apart
    from a real auth/config problem — classification is the testable core."""

    def test_classify_rate_limit_is_transient(self):
        # the message a 429 leaves after the chain exhausts retries
        self.assertEqual(
            _classify_error("qwen/x:free: exhausted 3 retries: HTTP 429: rate-limited")[1],
            SMOKE_RATELIMIT)
        self.assertEqual(_classify_error("temporarily rate-limited upstream")[1],
                         SMOKE_RATELIMIT)

    def test_classify_auth_and_config_are_hard(self):
        self.assertEqual(_classify_error("test/m: HTTP 401: bad key")[1], SMOKE_HARD)
        self.assertEqual(_classify_error("test/m: HTTP 403: forbidden")[1], SMOKE_HARD)
        self.assertEqual(_classify_error("test/m: HTTP 404: no such model")[1], SMOKE_HARD)

    def test_classify_missing_key(self):
        self.assertEqual(
            _classify_error("no API key — set $OPENROUTER_API_KEY or llm.api_key")[1],
            SMOKE_NO_KEY)

    def test_classify_unknown_stays_hard_not_masked_as_transient(self):
        # a non-429 transport/parse failure must not look like "just retry"
        self.assertEqual(_classify_error("non-JSON response: <html>500</html>")[1],
                         SMOKE_HARD)

    def test_overall_exit_success_wins(self):
        self.assertEqual(_overall_exit([SMOKE_RATELIMIT, SMOKE_OK]), SMOKE_OK)

    def test_overall_exit_orders_key_then_hard_then_ratelimit(self):
        self.assertEqual(_overall_exit([SMOKE_RATELIMIT, SMOKE_NO_KEY, SMOKE_HARD]),
                         SMOKE_NO_KEY)
        self.assertEqual(_overall_exit([SMOKE_RATELIMIT, SMOKE_HARD]), SMOKE_HARD)
        self.assertEqual(_overall_exit([SMOKE_RATELIMIT, SMOKE_RATELIMIT]),
                         SMOKE_RATELIMIT)

    def test_overall_exit_empty_is_hard(self):
        self.assertEqual(_overall_exit([]), SMOKE_HARD)


if __name__ == "__main__":
    unittest.main()
