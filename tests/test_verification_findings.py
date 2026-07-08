"""Verification & stress-test reproductions (read-only QA pass).

This module is NOT part of the product. It began as an adversarial verification
harness written by an external QA pass; each test encodes a CLAIM the code makes
about itself (from a docstring, CLAUDE.md, a type, or a name). The findings it
surfaced (F-01 … F-07) have since been FIXED, so every test now PASSES and serves
as a regression guard for that fix.

    python -m unittest tests.test_verification_findings -v

Test naming:
  * ``RegressionGuards`` — these PIN the now-fixed findings F-01 … F-07 (see
    verification.md for the id mapping). Each reproduced a defect before the fix
    and now passes. The three precedence findings (F-02/F-03/F-04) were resolved
    as docstring fixes, so their guards assert the IMPLEMENTED-and-now-correctly-
    documented order (the implementation was the intended source of truth).
  * ``VerifiedClaims`` — these PASS; they confirm the positive claims that DID
    hold up, and (in the F-05 case) contrast the correct component with the
    buggy one.

Nothing here imports into or mutates production source. Offline, deterministic,
no network, no optional deps.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.schema import (
    make_event, NormalizedEvent, AGENT_REPORT, SCHEMA_VERSION,
    FOCUS_SESSION, FILE_CHANGE, GIT_COMMIT,
)

TS = "2026-06-21T15:00:00+03:00"


# ===========================================================================
#  REGRESSION GUARDS  (reproduced F-01…F-07 before the fix; now all PASS)
# ===========================================================================
class RegressionGuards(unittest.TestCase):

    # -- F-01: malformed clock_offset_sec crashes timeline reconciliation ---- #
    def test_F01_reconcile_must_not_crash_on_nonnumeric_clock_offset(self):
        """timeline.reconcile is documented as robust ("unparseable timestamps
        sort last but stay in the timeline (never dropped)") and the project's
        central invariant is that events are NEVER dropped or crashed. A bad
        `clock_offset_sec` (string/list) escapes the try/except in effective_dt
        and raises, taking down the whole batch."""
        from throughlog.timeline import reconcile
        events = [
            {"event_id": "a", "ts_wall": "2026-06-24T10:00:00+00:00",
             "clock_offset_sec": "abc", "trust": "validated"},
        ]
        # Documented contract: this returns a timeline, never raises.
        out = reconcile(events)              # <-- raises ValueError today
        self.assertEqual(len(out), 1)

    def test_F01b_spoofed_agent_report_must_not_crash_synthesis(self):
        """schema.validate() is documented as "reused by agent ingestion to
        reject malformed/spoofed reports". A report carrying a non-numeric
        `clock_offset_sec` passes validation (trust=validated), persists through
        the gate+bus, then crashes the next `synthesize.load_events` for the
        ENTIRE day's log — a single hostile report = a synthesis DoS."""
        from throughlog.sources.agent_ingest import ingest_report
        from throughlog.bus import EventBus
        from throughlog.privacy.allowlist import Allowlist
        from throughlog.synthesize import load_events

        raw = {
            "schema_version": SCHEMA_VERSION, "type": AGENT_REPORT,
            "source": {"kind": "agent", "adapter": "x", "identity": "agent:y"},
            "ts_wall": "2026-06-24T09:05:00+00:00",
            "clock_offset_sec": "abc",
            "payload": {"summary": "pwned"},
        }
        ev = ingest_report(raw)
        # The spoofed report is accepted, not rejected:
        self.assertEqual(ev.trust, "validated")  # documents the validation gap

        d = tempfile.mkdtemp()
        bus = EventBus(os.path.join(d, "events"), Allowlist([]))
        self.assertTrue(bus.emit(ev))            # persists through gate+bus
        bus.close()
        path = os.path.join(d, "events",
                            os.listdir(os.path.join(d, "events"))[0])
        # Documented contract: synthesis never crashes on persisted data.
        events = load_events(path)               # <-- raises ValueError today
        self.assertEqual(len(events), 1)

    # -- F-02: precedence — jira (0.85) outranks git-remote (0.82) ----------- #
    def test_F02_signal_order_jira_outranks_git_remote(self):
        """Precedence regression guard (F-02 resolved as a docstring fix): the
        IMPLEMENTED and CLAUDE.md-documented order is jira (0.85) > git-remote
        (0.82). A project matching BOTH a git remote and a jira ticket resolves
        via the (stronger) jira signal. The stale categorize.py docstring that
        claimed git>jira has been corrected to match this."""
        from throughlog.categorize import signal_stack
        proj = [{"id": "p", "name": "P", "status": "active", "signals": {
            "paths": [], "git_remotes": ["github.com/acme/widget"],
            "jira_prefixes": ["WID"], "keywords": [], "apps": [], "domains": [],
            "window_patterns": []}}]
        ev = make_event(GIT_COMMIT, kind="git", adapter="fs_git", ts_wall=TS,
                        payload={"repo": "x",
                                 "message": "WID-12 pushed to github.com/acme/widget"})
        _, _, method, _ = signal_stack(ev, proj)
        # Confirmed precedence: jira (0.85) outranks git-remote (0.82).
        self.assertEqual(method, "signal_jira")

    # -- F-03: precedence — dense title-keyword (<=0.78) outranks pattern (0.75) #
    def test_F03_signal_order_title_keyword_density_outranks_window_pattern(self):
        """Precedence regression guard (F-03 resolved as a docstring fix): with
        >=4 keyword hits the title-keyword score caps at 0.78, which outranks the
        window-pattern score (0.75). A project matching a window pattern AND
        several title keywords resolves via the keyword density. The stale
        categorize.py docstring has been corrected to reflect that title-keyword
        is data-dependent, not strictly the weakest signal."""
        from throughlog.categorize import signal_stack
        proj = [{"id": "q", "name": "Q", "status": "active", "signals": {
            "paths": [], "git_remotes": [], "jira_prefixes": [],
            "keywords": ["alpha", "beta", "gamma", "delta"], "apps": [],
            "domains": [], "window_patterns": [".*widget.*"]}}]
        ev = make_event(FOCUS_SESSION, kind="os", adapter="os_focus", ts_wall=TS,
                        payload={"anchor": "widget alpha beta gamma delta",
                                 "process": "", "satellites": []})
        _, _, method, _ = signal_stack(ev, proj)
        # Confirmed precedence: dense title-keyword (<=0.78) outranks window-pattern (0.75).
        self.assertEqual(method, "signal_keyword")

    # -- F-04: intent ladder rung order (narration before input) ------------- #
    def test_F04_intent_ladder_narration_rung_before_input(self):
        """Precedence regression guard (F-04 resolved as a docstring fix): the
        IMPLEMENTED ladder and CLAUDE.md evaluate narration (rung 5) BEFORE the
        weak input-density guess (rung 6) — explicit human intent beats a
        mechanical keystroke heuristic. With BOTH present, narration wins. The
        stale ladder.py docstring that ordered input-before-narration has been
        corrected to match this."""
        from throughlog.intent.ladder import resolve_intent, IntentSignals
        r = resolve_intent(IntentSignals(
            narration="refactoring the parser", keys=100, duration_sec=10))
        # Confirmed ladder: narration outranks the weak input-density rung.
        self.assertEqual(r.method, "narration")

    # -- F-05: categorizer path-match ignores directory boundaries ----------- #
    def test_F05_path_attribution_respects_directory_boundary(self):
        """signal_path is "path under project directory" (0.95). Path matching
        must respect a directory boundary — an event under `~/proj/app-v2` must
        NOT be claimed by a sibling project rooted at `~/proj/app`. (The privacy
        allowlist gets this right; the categorizer uses naive substring
        containment and gets it wrong.)"""
        from throughlog.categorize import signal_stack
        projects = [
            {"id": "app", "name": "app", "status": "active", "signals": {
                "paths": ["~/proj/app"], "git_remotes": [], "jira_prefixes": [],
                "keywords": [], "apps": [], "domains": [], "window_patterns": []}},
            {"id": "app-v2", "name": "app-v2", "status": "active", "signals": {
                "paths": ["~/proj/app-v2"], "git_remotes": [], "jira_prefixes": [],
                "keywords": [], "apps": [], "domains": [], "window_patterns": []}},
        ]
        ev = make_event(FILE_CHANGE, kind="fs", adapter="fs_git", ts_wall=TS,
                        payload={"path": "~/proj/app-v2/src/main.ts"})
        pid, _, _, _ = signal_stack(ev, projects)
        # Correct attribution is the project that actually contains the file.
        self.assertEqual(pid, "app-v2")          # actual today: "app" (substring match)

    # -- Risk #1 (same family as F-01): synthesis must not crash on a junk
    #    payload numeric — here a non-numeric duration_sec --------------------- #
    def test_R1_synthesis_survives_nonnumeric_duration_sec(self):
        """verification.md §7 risk #1: the deterministic archive
        (synthesize.build_archive_section) is the C5 'never crashes' safety net,
        written before any LLM call. A persisted FOCUS_SESSION carrying a junk
        duration_sec must not crash it (the same type-discipline gap as F-01's
        clock_offset_sec)."""
        from throughlog.synthesize import summarize_event, build_archive_section
        ev = make_event(FOCUS_SESSION, kind="os", adapter="os_focus", ts_wall=TS,
                        payload={"anchor": "editor", "process": "code.exe",
                                 "duration_sec": "abc", "satellites": []})
        # Neither the per-event summary nor the deterministic archive may raise.
        self.assertIsInstance(summarize_event(ev), str)
        self.assertIn("editor", build_archive_section("2026-06-21", [ev]))

    # -- F-06: redactor leaks a short key=value credential ------------------- #
    def test_F06_redactor_does_not_leak_short_kv_secret(self):
        """redactors.py: "Conservative by design: it is fine to over-redact a
        token; it is never acceptable to leak one." The kv_secret pattern is
        built specifically for `pwd=<value>`, but its `{4,}` value floor lets a
        short secret pass through verbatim into a persisted/egress-clean payload."""
        from throughlog.privacy import redactors
        clean, reds = redactors.scrub("pwd=cat")
        # Documented contract: a recognized credential shape never leaks.
        self.assertNotIn("cat", clean)           # actual today: "pwd=cat" unredacted
        self.assertTrue(reds)

    # -- F-07: a valid (tz-naive) timestamp is mislabeled "unparseable" ------ #
    def test_F07_valid_naive_timestamp_not_marked_unparseable(self):
        """agent_ingest._anomalies parses ts_wall and `now` and compares them.
        When ts_wall is a VALID but tz-naive ISO string (the same shape the
        bundled corpus uses) the naive-vs-aware comparison raises TypeError,
        which is mislabeled "unparseable_ts_wall" — and the far-future check is
        silently bypassed."""
        from throughlog.sources.agent_ingest import ingest_report
        raw = {
            "schema_version": SCHEMA_VERSION, "type": AGENT_REPORT,
            "source": {"kind": "agent", "adapter": "x", "identity": "agent:y"},
            "ts_wall": "2026-06-24 09:05:00",       # valid ISO-8601, just naive
            "payload": {"summary": "did work"},
        }
        ev = ingest_report(raw, now="2026-06-24T12:00:00+00:00")
        reasons = ev.payload.get("trust_reasons", [])
        # A parseable timestamp must not be reported as unparseable.
        self.assertNotIn("unparseable_ts_wall", reasons)  # actual today: present


# ===========================================================================
#  RISK-REGISTER HARDENING  (verification.md §7 latent fragilities, now closed)
# ===========================================================================
class RiskRegisterHardening(unittest.TestCase):
    """§7 listed latent fragilities (not reproduced as failures at audit time).
    These guards pin the hardening that closed each one. R1 lives in
    RegressionGuards (it shared F-01's type-discipline root cause)."""

    # -- #2: mixed tz-naive/aware events must order deterministically -------- #
    def test_R2_timeline_orders_naive_and_aware_by_utc(self):
        """effective_dt returned tz-naive for a naive ts_wall and tz-aware for an
        aware one; _sort_key then called .timestamp() on a mix, so interleaved
        ordering was host-timezone-dependent. A naive ts_wall is now interpreted
        as UTC, making effective_dt always tz-aware and the order deterministic."""
        from datetime import datetime, timezone
        from throughlog.timeline import effective_dt, reconcile

        dt = effective_dt({"ts_wall": "2026-06-24T10:00:00"})   # naive input
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo)                          # always tz-aware now
        self.assertEqual(dt, datetime(2026, 6, 24, 10, 0, 0, tzinfo=timezone.utc))

        # Integration: a naive 10:00 (=10:00Z) must sort AFTER an aware 09:30Z,
        # regardless of the machine's local zone.
        aware = {"event_id": "aware", "ts_wall": "2026-06-24T09:30:00+00:00",
                 "trust": "validated"}
        naive = {"event_id": "naive", "ts_wall": "2026-06-24T10:00:00",
                 "trust": "validated"}
        order = [e["event_id"] for e in reconcile([naive, aware])]
        self.assertEqual(order, ["aware", "naive"])

    # -- #3: a genuine signal-stack tie must not resolve by registry order --- #
    def test_R3_signal_stack_tie_is_ambiguous_not_first_listed(self):
        """`if score > best[1]` (strict >) silently resolved ties by projects.json
        order. Two projects sharing the top assignable score now return
        method='ambiguous_tie' (pid=None), routing the event to needs_review/LLM
        instead of a position-dependent guess."""
        from throughlog.categorize import signal_stack, categorize_events
        shared = {"paths": ["~/work/shared"], "git_remotes": [], "jira_prefixes": [],
                  "keywords": [], "apps": [], "domains": [], "window_patterns": []}
        projects = [
            {"id": "alpha", "name": "alpha", "status": "active", "signals": dict(shared)},
            {"id": "beta", "name": "beta", "status": "active", "signals": dict(shared)},
        ]
        ev = make_event(FILE_CHANGE, kind="fs", adapter="fs_git", ts_wall=TS,
                        payload={"path": "~/work/shared/main.py"})
        pid, score, method, _ = signal_stack(ev, projects)
        self.assertIsNone(pid)
        self.assertEqual(method, "ambiguous_tie")
        self.assertAlmostEqual(score, 0.95)
        # And the caller does not silently assign it:
        categorize_events([ev], projects, client=None)
        self.assertEqual(ev.attribution.method, "needs_review")

    # -- #4: a compound secret key must not leak its value ------------------- #
    def test_R4_compound_kv_secret_does_not_leak(self):
        """`\\bsecret\\b` missed `secret_key=` (underscore is a word char), so the
        value leaked. The kv_secret alternation now covers compound keys; the
        high-entropy backstop floor was also lowered 24->16."""
        from throughlog.privacy import redactors
        for blob, leak in (("secret_key=topSecretValue99", "topSecretValue99"),
                           ("client_secret=anotherHidden42", "anotherHidden42"),
                           ("private_key=zzz9TopSecretKey", "zzz9TopSecretKey")):
            clean, reds = redactors.scrub(blob)
            self.assertNotIn(leak, clean, blob)
            self.assertIn("kv_secret", reds, blob)
        # 16-char high-entropy unnamed token is now caught by the backstop.
        clean, reds = redactors.scrub("deploy A1b2C3d4E5f6G7h8")
        self.assertNotIn("A1b2C3d4E5f6G7h8", clean)
        self.assertIn("high_entropy_token", reds)

    # -- #5: re-synthesizing a day must be idempotent, not duplicate --------- #
    def test_R5_archive_idempotent_on_resynthesis(self):
        """archive.md is append-only across days, but re-running synthesis for the
        SAME day appended a duplicate '## <date>' section. It now replaces that
        day's section, so a re-run is idempotent."""
        from throughlog.schema import make_event as _mk
        from throughlog.synthesize import run
        proj = {"id": "p", "name": "P"}

        def attributed():
            e = _mk(FOCUS_SESSION, kind="os", adapter="os_focus",
                    payload={"anchor": "editor", "process": "code.exe",
                             "duration_sec": 1800, "satellites": []},
                    ts_wall="2026-06-21T10:00:00+03:00")
            e.attribution.project_id = "p"
            e.attribution.confidence = 0.95
            e.attribution.method = "signal_path"
            return e

        d = tempfile.mkdtemp()
        run([attributed()], [proj], diaries_dir=d, client=None, today="2026-06-21")
        archive_path = os.path.join(d, "project_p", "archive.md")
        with open(archive_path, encoding="utf-8") as f:
            after_first = f.read()
        run([attributed()], [proj], diaries_dir=d, client=None, today="2026-06-21")
        with open(archive_path, encoding="utf-8") as f:
            after_second = f.read()
        self.assertEqual(after_first, after_second)             # idempotent
        self.assertEqual(after_second.count("## 2026-06-21"), 1)  # no duplicate

    # -- #6: a symlink/junction must not bypass the allowlist boundary ------- #
    def test_R6_allowlist_resolves_symlink_escape(self):
        """allows() compared string paths, so a link named under an allowed root
        but resolving OUTSIDE it was falsely allowed. Roots and candidates are now
        realpath-resolved before the boundary check."""
        from throughlog.privacy.allowlist import Allowlist
        root = tempfile.mkdtemp()
        outside = tempfile.mkdtemp()
        secret = os.path.join(outside, "secret.txt")
        with open(secret, "w", encoding="utf-8") as f:
            f.write("x")
        link = os.path.join(root, "link")
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError) as exc:
            self.skipTest(f"cannot create symlink on this host: {exc}")

        a = Allowlist([root])
        # Escapes via the link -> resolves outside the root -> denied.
        self.assertFalse(a.allows(os.path.join(link, "secret.txt")))
        # A genuine file under the root is still allowed (no regression).
        real = os.path.join(root, "real.txt")
        with open(real, "w", encoding="utf-8") as f:
            f.write("y")
        self.assertTrue(a.allows(real))

    # -- #7: concurrent emits must not lose writes or lose-update the counter  #
    def test_R7_threadsafe_emitter_under_concurrent_load(self):
        """The 'one failing source can't take the others down' guarantee rests on
        a thread-safe emitter that was never stress-tested; the suppressed counter
        also did a non-atomic += outside the lock. Both are pinned here."""
        import threading
        from throughlog.capture import ThreadSafeEmitter

        class _ListBus:
            def __init__(self):
                self.events = []

            def emit(self, ev):
                # Read-modify-write that corrupts if not serialized by the lock.
                cur = list(self.events)
                cur.append(ev)
                self.events = cur
                return True

        def hammer(emitter, n):
            for i in range(n):
                emitter.emit(i)

        # No lost writes under concurrency.
        bus = _ListBus()
        em = ThreadSafeEmitter(bus)
        threads = [threading.Thread(target=hammer, args=(em, 200)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(bus.events), 8 * 200)

        # Suppressed counter is atomic while paused.
        em2 = ThreadSafeEmitter(_ListBus())
        em2.paused.set()
        threads = [threading.Thread(target=hammer, args=(em2, 200)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(em2.suppressed, 8 * 200)


# ===========================================================================
#  VERIFIED CLAIMS  (these PASS — the positive claims that held up)
# ===========================================================================
class VerifiedClaims(unittest.TestCase):

    def test_demo_store_audits_clean(self):
        """demo.py claim: the synthetic store audits CLEAN (no egress leaks)."""
        from throughlog import demo
        from throughlog.privacy import egress
        leaks = 0
        for ev in demo.build_demo_events():
            blob = json.dumps(ev.to_dict().get("payload", {}), ensure_ascii=False)
            leaks += len(egress.assert_clean(blob))
        self.assertEqual(leaks, 0)

    def test_demo_categorizes_with_no_needs_review(self):
        """demo.py claim: "categorization resolves every event deterministically
        via the path/git signal stack — no LLM"."""
        from throughlog import demo
        from throughlog.categorize import categorize_events
        from throughlog.timeline import reconcile
        evs = [NormalizedEvent.from_dict(d)
               for d in reconcile([e.to_dict() for e in demo.build_demo_events()])]
        categorize_events(evs, demo.DEMO_PROJECTS, client=None)
        nr = [e for e in evs if e.attribution.method == "needs_review"]
        self.assertEqual(nr, [])

    def test_schema_round_trips_losslessly(self):
        """schema.py claim: "round-trips losslessly to/from JSON"."""
        e = make_event(FOCUS_SESSION, kind="os", adapter="os_focus",
                       payload={"anchor": "x", "n": 5, "nested": {"a": [1, 2]}},
                       ts_wall="2026-06-24T10:00:00+00:00")
        r = NormalizedEvent.from_dict(json.loads(e.to_json()))
        self.assertEqual(r.to_dict(), e.to_dict())

    def test_allowlist_respects_directory_boundary(self):
        """allowlist.allows uses a real path boundary (contrast with F-05): a
        sibling whose name extends an allowed root is NOT allowed."""
        from throughlog.privacy.allowlist import Allowlist
        a = Allowlist([os.path.join(os.sep, "proj", "app")])
        self.assertFalse(a.allows(os.path.join(os.sep, "proj", "app-v2", "x.ts")))
        self.assertTrue(a.allows(os.path.join(os.sep, "proj", "app", "x.ts")))

    def test_sync_blocks_ungated_and_rescrubs(self):
        """sync.prepare_for_egress claim: only privacy-stamped events are
        sendable, and each is egress-re-scrubbed."""
        from throughlog.sync import prepare_for_egress
        gated = {"event_id": "1", "privacy": {"gate_version": "1"},
                 "payload": {"note": "key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX12345"}}
        ungated = {"event_id": "2", "payload": {"note": "hi"}}
        send, blocked = prepare_for_egress([gated, ungated])
        self.assertEqual(blocked, ["2"])
        self.assertIn("REDACTED", send[0]["payload"]["note"])

    def test_relay_account_id_cannot_traverse(self):
        """relay._safe_account claim: "the account id is sanitized so a token can
        never escape its store path"."""
        from throughlog.relay import _safe_account
        for hostile in ("../../etc", "..", "..\\..\\windows", "/abs/path", "."):
            safe = _safe_account(hostile)
            self.assertNotIn("/", safe)
            self.assertNotIn("\\", safe)
            self.assertNotEqual(safe, "..")

    def test_server_markdown_is_injection_safe(self):
        """server.md_to_html claim: "Input is fully escaped ... injection-safe"."""
        from throughlog.server import md_to_html
        for payload in ("## <script>alert(1)</script>",
                        "- <img src=x onerror=alert(1)>",
                        "`<b>x</b>`", "**<i>y</i>**"):
            html = md_to_html(payload)
            # The safety property is that no *live* tag is emitted: every angle
            # bracket from the input is escaped (so "onerror" surviving as inert
            # &lt;img...&gt; text is fine — it cannot execute).
            self.assertNotIn("<script", html)
            self.assertNotIn("<img", html)
            self.assertIn("&lt;", html)


if __name__ == "__main__":
    unittest.main()
