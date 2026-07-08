import http.client
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog import server
from throughlog.schema import make_event, FOCUS_SESSION, DEEP_WORK, GIT_COMMIT, FILE_CHANGE


# --------------------------------------------------------------------------- #
# md_to_html — pure renderer (escaping + the journal subset)
# --------------------------------------------------------------------------- #
class MarkdownRenderer(unittest.TestCase):
    def test_escapes_html_to_prevent_injection(self):
        out = server.md_to_html("a <script>alert(1)</script> & b")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("&amp;", out)

    def test_headings(self):
        out = server.md_to_html("# Title\n## Sub\n### Deep")
        self.assertIn("<h1>Title</h1>", out)
        self.assertIn("<h2>Sub</h2>", out)
        self.assertIn("<h3>Deep</h3>", out)

    def test_bullets_dash_and_unicode_bullet(self):
        out = server.md_to_html("- one\n• two\n* three")
        self.assertEqual(out.count("<ul>"), 1)        # consecutive items, one list
        self.assertEqual(out.count("</ul>"), 1)
        self.assertEqual(out.count("<li>"), 3)
        self.assertIn("<li>one</li>", out)
        self.assertIn("<li>two</li>", out)

    def test_bold_and_inline_code(self):
        out = server.md_to_html("**Status:** active and `code.py`")
        self.assertIn("<strong>Status:</strong>", out)
        self.assertIn("<code>code.py</code>", out)

    def test_horizontal_rule(self):
        out = server.md_to_html("a\n\n---\n\nb")
        self.assertIn("<hr>", out)

    def test_paragraphs_separated_by_blank_lines(self):
        out = server.md_to_html("line one\n\nline two")
        self.assertEqual(out.count("<p>"), 2)


# --------------------------------------------------------------------------- #
# status_badge — live / paused / offline / not-running
# --------------------------------------------------------------------------- #
class DiffRendering(unittest.TestCase):
    """read_diff verifies the content hash; timeline_html renders a collapsed,
    injection-safe <details> diff only when a valid diff_ref resolves."""

    def _write_diff(self, data_dir: Path, body: str) -> str:
        import hashlib
        ref = hashlib.sha256(body.encode("utf-8")).hexdigest()
        d = data_dir / "diffs"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{ref}.patch").write_text(body, encoding="utf-8")
        return ref

    def test_read_diff_roundtrip_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d)
            ref = self._write_diff(dd, "+ok line\n")
            self.assertEqual(server.read_diff(dd, ref), "+ok line\n")
            self.assertEqual(server.read_diff(dd, "deadbeef" * 8), "")  # missing
            self.assertEqual(server.read_diff(dd, "not-a-hash"), "")    # malformed ref

    def test_read_diff_rejects_tampered_sidecar(self):
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d)
            ref = self._write_diff(dd, "+original\n")
            (dd / "diffs" / f"{ref}.patch").write_text("+TAMPERED\n", encoding="utf-8")
            self.assertEqual(server.read_diff(dd, ref), "")            # hash mismatch

    def test_timeline_renders_escaped_diff(self):
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d)
            body = '+evil = "<script>alert(1)</script>"\n'
            ref = self._write_diff(dd, body)
            ev = make_event(FILE_CHANGE, kind="fs", adapter="fs_git",
                            payload={"path": "throughlog/app.py", "diff_ref": ref})
            out = server.timeline_html([ev], dd)
            self.assertIn("<details", out)
            self.assertNotIn("<script>alert(1)</script>", out)        # escaped
            self.assertIn("&lt;script&gt;", out)

    def test_timeline_without_data_dir_has_no_diff(self):
        ev = make_event(FILE_CHANGE, kind="fs", adapter="fs_git",
                        payload={"path": "x", "diff_ref": "a" * 64})
        self.assertNotIn("<details", server.timeline_html([ev]))


class StatusBadge(unittest.TestCase):
    def _now(self):
        return datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)

    def test_none_is_not_running(self):
        self.assertEqual(server.status_badge(None)[0], "off")

    def test_fresh_alive_is_recording(self):
        hb = (self._now() - timedelta(seconds=5)).isoformat()
        cls, label = server.status_badge({"alive": True, "heartbeat": hb}, now=self._now())
        self.assertEqual(cls, "live")
        self.assertEqual(label, "Recording")

    def test_paused(self):
        hb = (self._now() - timedelta(seconds=5)).isoformat()
        cls, _ = server.status_badge({"alive": True, "paused": True, "heartbeat": hb},
                                     now=self._now())
        self.assertEqual(cls, "paused")

    def test_stale_heartbeat_is_offline(self):
        hb = (self._now() - timedelta(minutes=10)).isoformat()
        cls, _ = server.status_badge({"alive": True, "heartbeat": hb}, now=self._now())
        self.assertEqual(cls, "off")


# --------------------------------------------------------------------------- #
# End-to-end HTTP smoke (in-process, ephemeral port, fully offline)
# --------------------------------------------------------------------------- #
class HttpSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sal_server_test_"))
        journal_root = cls.tmp / "journal"
        pdir = journal_root / "project_demo"
        pdir.mkdir(parents=True)
        (pdir / "overview.md").write_text(
            "# Demo Project\n**Status:** active\n\n## Current State\n"
            "Wrote the dashboard and verified <it> renders.\n", encoding="utf-8")
        (pdir / "archive.md").write_text("---\n## 2026-06-24\n### Sessions\n- 10:00 — work\n",
                                         encoding="utf-8")
        (journal_root / "executive_summary.md").write_text(
            "# Executive Summary\n\nShipped the dashboard.\n", encoding="utf-8")
        (journal_root / "daily.md").write_text("## 2026-06-24\n\n**demo** — built the UI\n",
                                          encoding="utf-8")

        data = cls.tmp / "data"
        events = data / "events"
        events.mkdir(parents=True)
        evs = [
            make_event(FOCUS_SESSION, kind="os", adapter="os_focus",
                       payload={"anchor": "editor", "duration_sec": 600},
                       ts_wall="2026-06-24T10:00:00+00:00"),
            make_event(GIT_COMMIT, kind="vcs", adapter="fs_git",
                       payload={"repo": "demo", "message": "feat: dashboard"},
                       ts_wall="2026-06-24T11:00:00+00:00"),
        ]
        (events / "20260624.jsonl").write_text(
            "\n".join(e.to_json() for e in evs) + "\n", encoding="utf-8")

        cls.httpd = server.make_server(
            "127.0.0.1", 0, journal_dir=journal_root, data_dir_path=data,
            registry={"demo": "Demo Project"})
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        return resp.status, body

    def test_overview_lists_project_and_exec_summary(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("Demo Project", body)
        self.assertIn("Shipped the dashboard.", body)
        self.assertIn('href="/project/demo"', body)

    def test_project_page_renders_overview_and_escapes(self):
        status, body = self._get("/project/demo")
        self.assertEqual(status, 200)
        self.assertIn("Current State", body)
        self.assertNotIn("<it>", body)            # escaped, not a live tag
        self.assertIn("&lt;it&gt;", body)
        self.assertIn('href="/archive/demo"', body)

    def test_timeline_uses_day_file(self):
        status, body = self._get("/timeline?date=20260624")
        self.assertEqual(status, 200)
        self.assertIn("feat: dashboard", body)
        self.assertIn("2 events", body)

    def test_api_status_is_json(self):
        status, body = self._get("/api/status")
        self.assertEqual(status, 200)
        import json
        self.assertIsInstance(json.loads(body), dict)   # no status file => {}

    def test_unknown_path_404(self):
        status, _ = self._get("/nope")
        self.assertEqual(status, 404)


# --------------------------------------------------------------------------- #
# Time-per-project chart (pure, deterministic)
# --------------------------------------------------------------------------- #
class ProjectChart(unittest.TestCase):
    # A project path NOT under $HOME so home-normalization leaves it unchanged and
    # the (ungated) event path matches it exactly in this pure unit test.
    PROJ = {"id": "p1", "name": "Proj One", "status": "active",
            "signals": {"paths": ["/work/proj"], "keywords": [], "git_remotes": [],
                        "jira_prefixes": [], "apps": [], "domains": [],
                        "window_patterns": []}}

    def _focus(self, dur, f):
        return make_event(FOCUS_SESSION, kind="os", adapter="os_focus",
                          payload={"anchor": "editor", "duration_sec": dur,
                                   "active_file": f})

    def test_durations_sum_per_project(self):
        evs = [self._focus(1200, "/work/proj/a.py"),
               make_event(DEEP_WORK, kind="os", adapter="os_focus",
                          payload={"duration_sec": 600, "active_file": "/work/proj/b.py"})]
        rows = server.project_durations(evs, [self.PROJ])
        self.assertEqual(rows, [("Proj One", 1800)])

    def test_unattributed_events_excluded(self):
        rows = server.project_durations([self._focus(900, "/elsewhere/x.py")], [self.PROJ])
        self.assertEqual(rows, [])

    def test_svg_is_escaped_and_clamped(self):
        evil = {**self.PROJ, "name": "<script>x</script>"}
        rows = server.project_durations([self._focus(60, "/work/proj/a.py")], [evil])
        svg = server.project_time_svg(rows)
        self.assertTrue(svg.startswith("<svg"))
        self.assertNotIn("<script>x</script>", svg)
        self.assertIn("&lt;script&gt;", svg)

    def test_empty_rows_degrade(self):
        self.assertIn("No tracked", server.project_time_svg([]))

    def test_fmt_dur(self):
        self.assertEqual(server._fmt_dur(40 * 60), "40m")
        self.assertEqual(server._fmt_dur(95 * 60), "1h35m")


# --------------------------------------------------------------------------- #
# CSRF guard (pure)
# --------------------------------------------------------------------------- #
class CsrfGuard(unittest.TestCase):
    def test_valid_token_no_origin(self):
        ok = server.csrf_ok({"Host": "127.0.0.1:8799"}, {"_token": ["t"]}, "t")
        self.assertTrue(ok)

    def test_missing_or_wrong_token(self):
        self.assertFalse(server.csrf_ok({}, {}, "t"))
        self.assertFalse(server.csrf_ok({}, {"_token": ["wrong"]}, "t"))

    def test_matching_origin_passes(self):
        h = {"Host": "127.0.0.1:8799", "Origin": "http://127.0.0.1:8799"}
        self.assertTrue(server.csrf_ok(h, {"_token": ["t"]}, "t"))

    def test_foreign_origin_rejected(self):
        h = {"Host": "127.0.0.1:8799", "Origin": "http://evil.example"}
        self.assertFalse(server.csrf_ok(h, {"_token": ["t"]}, "t"))


# --------------------------------------------------------------------------- #
# Automation card — pure render (autostart + nightly synthesis toggles)
# --------------------------------------------------------------------------- #
class AutomationSettings(unittest.TestCase):
    def test_off_shows_turn_on_controls(self):
        out = server.settings_html({}, [], token="t",
                                   automation={"capture": False, "synthesis": False})
        self.assertIn("/settings/autostart", out)
        self.assertIn("/settings/schedule", out)
        self.assertIn("Turn on", out)
        self.assertIn('name="tray"', out)          # tray opt-in only offered when off
        self.assertIn('name="time"', out)

    def test_on_shows_turn_off_and_no_tray(self):
        out = server.settings_html(
            {}, [], token="t",
            automation={"capture": True, "synthesis": True, "synthesis_time": "23:15"})
        self.assertIn("Turn off", out)
        self.assertIn("runs hidden in the background", out)   # no-admin capture pill
        self.assertNotIn('name="tray"', out)       # no re-opt-in once enabled

    def test_nightly_time_is_prefilled(self):
        out = server.settings_html(
            {}, [], token="t",
            automation={"capture": False, "synthesis": True, "synthesis_time": "06:45"})
        self.assertIn('value="06:45"', out)        # configured time round-trips
        self.assertIn("runs nightly at 06:45", out)

    def test_no_automation_arg_degrades_to_off(self):
        out = server.settings_html({}, [], token="t")   # None -> all off, no crash
        self.assertIn("Automation", out)
        self.assertIn("Turn on", out)

    def test_projects_card_has_scan_form(self):
        out = server.settings_html({}, [], token="t")
        self.assertIn("/settings/scan", out)
        self.assertIn("Scan for projects", out)
        self.assertIn('name="root"', out)

    def test_reasoning_select_renders_and_preselects(self):
        out = server.settings_html({"llm": {"reasoning_effort": "high"}}, [], token="t")
        self.assertIn('name="reasoning_effort"', out)
        self.assertIn('<option value="high" selected>High</option>', out)

    def test_reasoning_select_defaults_to_provider_default(self):
        out = server.settings_html({}, [], token="t")     # unset -> "Default" selected
        self.assertIn('<option value="" selected>Default</option>', out)


# --------------------------------------------------------------------------- #
# POST routes — CSRF, ask, actions, settings (in-process, offline)
# These never write real config.json/projects.json: config-mutating routes are
# covered by test_appconfig with temp paths; here we exercise routing + guards
# with an injected controller and read-only/confirm-only paths.
# --------------------------------------------------------------------------- #
class PostRoutes(unittest.TestCase):
    TOKEN = "fixed-test-token"

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sal_post_test_"))
        journal_root = cls.tmp / "journal"
        pdir = journal_root / "project_demo"
        pdir.mkdir(parents=True)
        (pdir / "overview.md").write_text(
            "# Demo\n## Current State\nShipped the checkout flow.\n", encoding="utf-8")
        (journal_root / "executive_summary.md").write_text("# Exec\n\nA day.\n", encoding="utf-8")
        sdir = journal_root / "summaries"
        sdir.mkdir()
        (sdir / "2026-W26.md").write_text(
            "# Weekly summary — 2026-W26\n\nShipped a lot this week.\n", encoding="utf-8")
        data = cls.tmp / "data"
        (data / "events").mkdir(parents=True)

        cls.synth_calls = []
        controller = server.Controller(
            on_synthesize=lambda: cls.synth_calls.append(1))
        cls.httpd = server.make_server(
            "127.0.0.1", 0, journal_dir=journal_root, data_dir_path=data,
            registry={"demo": "Demo"}, projects=[], controller=controller,
            csrf_token=cls.TOKEN)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        out = resp.read().decode("utf-8")
        conn.close()
        return resp.status, out

    def _form(self, **kw):
        from urllib.parse import urlencode
        return (urlencode(kw),
                {"Content-Type": "application/x-www-form-urlencoded"})

    def test_overview_has_controls_and_ask(self):
        status, body = self._req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Ask your", body)
        self.assertIn("Synthesize now", body)
        self.assertIn(self.TOKEN, body)            # forms carry the token

    def test_settings_page_has_key_field_and_token(self):
        status, body = self._req("GET", "/settings")
        self.assertEqual(status, 200)
        self.assertIn('name="api_key"', body)
        self.assertIn(self.TOKEN, body)

    def test_ask_with_token_returns_passages(self):
        # Force the offline path (no LLM): the handler resolves server._llm_client at
        # call time, so patching the module global keeps the test hermetic even if a
        # real key is configured on this machine.
        import unittest.mock as mock
        with mock.patch.object(server, "_llm_client", return_value=None):
            body, headers = self._form(_token=self.TOKEN, q="checkout")
            status, out = self._req("POST", "/ask", body=body, headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("checkout", out.lower())

    def test_post_without_token_is_forbidden(self):
        body, headers = self._form(q="x")
        status, _ = self._req("POST", "/action/synthesize", body=body, headers=headers)
        self.assertEqual(status, 403)

    def test_post_with_foreign_origin_forbidden(self):
        body, headers = self._form(_token=self.TOKEN)
        headers["Origin"] = "http://evil.example"
        status, _ = self._req("POST", "/action/synthesize", body=body, headers=headers)
        self.assertEqual(status, 403)

    def test_synthesize_action_invokes_controller(self):
        before = len(self.synth_calls)
        body, headers = self._form(_token=self.TOKEN)
        status, _ = self._req("POST", "/action/synthesize", body=body, headers=headers)
        self.assertEqual(status, 303)             # redirect, no re-POST on reload
        self.assertEqual(len(self.synth_calls), before + 1)

    def test_autostart_enable_invokes_deploy(self):
        # The handler resolves server._set_autostart at call time, so patching the
        # module global keeps this offline — the real scheduler is never touched.
        import unittest.mock as mock
        with mock.patch.object(server, "_set_autostart",
                               return_value=(True, "ok")) as m:
            body, headers = self._form(_token=self.TOKEN, action="enable", tray="1")
            status, _ = self._req("POST", "/settings/autostart",
                                  body=body, headers=headers)
        self.assertEqual(status, 303)
        m.assert_called_once_with(True, tray=True)

    def test_autostart_disable_invokes_deploy(self):
        import unittest.mock as mock
        with mock.patch.object(server, "_set_autostart",
                               return_value=(True, "ok")) as m:
            body, headers = self._form(_token=self.TOKEN, action="disable")
            status, _ = self._req("POST", "/settings/autostart",
                                  body=body, headers=headers)
        self.assertEqual(status, 303)
        m.assert_called_once_with(False, tray=False)

    # -- Journal & summaries settings -------------------------------------- #
    def test_settings_has_journal_card(self):
        status, body = self._req("GET", "/settings")
        self.assertEqual(status, 200)
        self.assertIn("Journal &amp; summaries", body)
        self.assertIn('action="/settings/synthesis"', body)
        self.assertIn('action="/settings/init"', body)

    def test_synthesis_route_writes_validated_patch(self):
        import unittest.mock as mock
        from throughlog import appconfig
        with mock.patch.object(appconfig, "update_synthesis") as m:
            body, headers = self._form(_token=self.TOKEN, write_entries="1",
                                       entry_period="week", summary_cadence="monthly")
            status, _ = self._req("POST", "/settings/synthesis", body=body, headers=headers)
        self.assertEqual(status, 303)
        patch = m.call_args.args[0]
        self.assertIs(patch["write_entries"], True)
        self.assertEqual(patch["entry_period"], "week")
        self.assertEqual(patch["summary_cadence"], "monthly")

    def test_synthesis_route_rejects_bad_enum(self):
        import unittest.mock as mock
        from throughlog import appconfig
        with mock.patch.object(appconfig, "update_synthesis") as m:
            body, headers = self._form(_token=self.TOKEN, entry_period="yearly",
                                       summary_cadence="hourly")
            self._req("POST", "/settings/synthesis", body=body, headers=headers)
        patch = m.call_args.args[0]
        self.assertNotIn("entry_period", patch)        # invalid enums dropped
        self.assertNotIn("summary_cadence", patch)
        self.assertIs(patch["write_entries"], False)     # unchecked checkbox -> False

    def test_init_route_writes_enrich_toggle(self):
        import unittest.mock as mock
        from throughlog import appconfig
        with mock.patch.object(appconfig, "update_init") as m:
            body, headers = self._form(_token=self.TOKEN, llm_enrich="1")
            status, _ = self._req("POST", "/settings/init", body=body, headers=headers)
        self.assertEqual(status, 303)
        self.assertIs(m.call_args.args[0]["llm_enrich"], True)

    # -- Summaries view ----------------------------------------------------- #
    def test_summaries_route_renders(self):
        status, body = self._req("GET", "/summaries")
        self.assertEqual(status, 200)
        self.assertIn("2026-W26", body)
        self.assertIn("Shipped a lot this week", body)

    def test_summary_path_traversal_falls_back(self):
        status, body = self._req("GET", "/summary/..%2f..%2fetc")
        self.assertEqual(status, 200)
        self.assertIn("2026-W26", body)                  # crafted period -> newest valid file

    def test_overview_shows_latest_summary(self):
        status, body = self._req("GET", "/")
        self.assertIn("Latest summary", body)
        self.assertIn("Shipped a lot this week", body)

    def test_add_project_enrich_gated_on_toggle_and_key(self):
        import unittest.mock as mock
        from throughlog import appconfig
        folder = self.tmp / "enrich_repo"
        folder.mkdir(exist_ok=True)
        sentinel = object()
        # Toggle ON + key resolvable -> a client is passed to add_project.
        with mock.patch.object(appconfig, "init_enrich_enabled", return_value=True), \
             mock.patch.object(server, "_llm_client", return_value=sentinel), \
             mock.patch.object(appconfig, "allowlist_delta", return_value=[]), \
             mock.patch.object(appconfig, "add_project",
                               return_value={"id": "enrich_repo"}) as m:
            body, headers = self._form(_token=self.TOKEN, folder=str(folder), confirm="1")
            self._req("POST", "/settings/project", body=body, headers=headers)
        self.assertIs(m.call_args.kwargs["client"], sentinel)
        # Toggle OFF -> client is None regardless of key.
        with mock.patch.object(appconfig, "init_enrich_enabled", return_value=False), \
             mock.patch.object(appconfig, "allowlist_delta", return_value=[]), \
             mock.patch.object(appconfig, "add_project",
                               return_value={"id": "enrich_repo"}) as m2:
            body, headers = self._form(_token=self.TOKEN, folder=str(folder), confirm="1")
            self._req("POST", "/settings/project", body=body, headers=headers)
        self.assertIsNone(m2.call_args.kwargs["client"])

    def test_schedule_enable_passes_time(self):
        import unittest.mock as mock
        with mock.patch.object(server, "_set_nightly",
                               return_value=(True, "ok")) as m:
            body, headers = self._form(_token=self.TOKEN, action="enable", time="07:15")
            status, _ = self._req("POST", "/settings/schedule",
                                  body=body, headers=headers)
        self.assertEqual(status, 303)
        m.assert_called_once_with(True, time_hhmm="07:15")

    def test_schedule_rejects_bad_time_without_calling_deploy(self):
        import unittest.mock as mock
        with mock.patch.object(server, "_set_nightly") as m, \
             mock.patch.object(server, "automation_state",
                               return_value={"capture": False, "synthesis": False}):
            body, headers = self._form(_token=self.TOKEN, action="enable", time="99:99")
            status, out = self._req("POST", "/settings/schedule",
                                    body=body, headers=headers)
        self.assertEqual(status, 400)
        self.assertIn("Invalid time", out)
        m.assert_not_called()

    def test_automation_route_requires_token(self):
        body, headers = self._form(action="enable")     # no _token -> forbidden
        status, _ = self._req("POST", "/settings/autostart", body=body, headers=headers)
        self.assertEqual(status, 403)

    def test_add_project_shows_confirmation_before_writing(self):
        # An uncovered temp folder -> the widening interstitial, NOT an immediate add.
        folder = self.tmp / "newrepo"
        folder.mkdir()
        body, headers = self._form(_token=self.TOKEN, folder=str(folder))
        status, out = self._req("POST", "/settings/project", body=body, headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("widens", out.lower())
        self.assertIn('name="confirm"', out)      # second-step confirm form

    def test_scan_nonfolder_is_rejected(self):
        body, headers = self._form(_token=self.TOKEN, root=str(self.tmp / "nope"))
        status, out = self._req("POST", "/settings/scan", body=body, headers=headers)
        self.assertEqual(status, 400)
        self.assertIn("Not a folder", out)

    def test_scan_empty_folder_shows_scan_page_not_write(self):
        # A real folder with no git repos -> the scan result page (nothing added),
        # never an immediate write.
        empty = self.tmp / "no_repos_here"
        empty.mkdir()
        body, headers = self._form(_token=self.TOKEN, root=str(empty))
        status, out = self._req("POST", "/settings/scan", body=body, headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("No new git repos", out)

    def test_quit_route_is_noop_without_a_bound_server(self):
        # The shared test server never calls bind_server, so can_quit is False and
        # /action/quit must just redirect (NEVER shut the server down).
        body, headers = self._form(_token=self.TOKEN)
        status, _ = self._req("POST", "/action/quit", body=body, headers=headers)
        self.assertEqual(status, 303)


# --------------------------------------------------------------------------- #
# Capture coherence — one engine, three hosts; `tl up` reads the shared status
# --------------------------------------------------------------------------- #
class CaptureIsLive(unittest.TestCase):
    def _write_status(self, d: Path, *, alive: bool, heartbeat: datetime):
        import json
        (d).mkdir(parents=True, exist_ok=True)
        (d / "daemon_status.json").write_text(
            json.dumps({"alive": alive, "paused": False,
                        "heartbeat": heartbeat.isoformat()}), encoding="utf-8")

    def test_no_status_file_is_not_live(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(server.capture_is_live(Path(t)))

    def test_fresh_heartbeat_is_live(self):
        now = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as t:
            self._write_status(Path(t), alive=True, heartbeat=now)
            self.assertTrue(server.capture_is_live(Path(t), now=now))

    def test_stale_heartbeat_is_not_live(self):
        now = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as t:
            self._write_status(Path(t), alive=True, heartbeat=now - timedelta(hours=1))
            self.assertFalse(server.capture_is_live(Path(t), now=now))


# --------------------------------------------------------------------------- #
# Controller — the dashboard Quit (fool-proof stop for detached/no-console runs)
# --------------------------------------------------------------------------- #
class ControllerQuit(unittest.TestCase):
    class _FakeSup:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    class _FakeHttpd:
        def __init__(self):
            self.shut = False

        def shutdown(self):
            self.shut = True

    def test_can_quit_only_after_bind(self):
        c = server.Controller()
        self.assertFalse(c.can_quit)
        c.bind_server(self._FakeHttpd())
        self.assertTrue(c.can_quit)

    def test_quit_stops_supervisor_and_shuts_server(self):
        sup, httpd = self._FakeSup(), self._FakeHttpd()
        c = server.Controller(supervisor=sup)
        c.bind_server(httpd)
        c.quit()
        # quit() shuts down on a side thread; give it a moment to run.
        import time
        for _ in range(50):
            if sup.stopped and httpd.shut:
                break
            time.sleep(0.01)
        self.assertTrue(sup.stopped)
        self.assertTrue(httpd.shut)


# --------------------------------------------------------------------------- #
# Control bar — Quit button only when the app can actually quit
# --------------------------------------------------------------------------- #
class ControlBar(unittest.TestCase):
    def test_quit_hidden_by_default(self):
        out = server.control_bar_html("t", has_capture=True, paused=False)
        self.assertNotIn("/action/quit", out)

    def test_quit_shown_when_can_quit(self):
        out = server.control_bar_html("t", has_capture=True, paused=False,
                                      can_quit=True)
        self.assertIn("/action/quit", out)
        self.assertIn("Quit app", out)


if __name__ == "__main__":
    unittest.main()
