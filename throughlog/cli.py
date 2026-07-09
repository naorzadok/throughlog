"""tl — the ThroughLog command-line entry point — the deployable analysis surface.

    python -m throughlog.cli synthesize --replay              # the bundled real day
    python -m throughlog.cli synthesize --date 20260506       # data/events/20260506.jsonl
    python -m throughlog.cli synthesize --events some.jsonl    # an explicit thin-log file
    python -m throughlog.cli synthesize --replay --no-llm      # deterministic only (offline)

Wires the finished pipeline end-to-end: read a persisted thin-log -> reconcile to
real order -> Phase 1 categorize (deterministic signal stack, LLM only for genuine
ambiguity) -> Phase 2 synthesize (overview/archive/daily/executive summary). Live
capture (the source adapters feeding the bus) is a separate concern; this command
runs the analysis over events that have already been captured and gated.

`--no-llm` (or a missing API key) degrades gracefully: the deterministic archive
is still written and events are never dropped — the overview/exec prose is simply
skipped or falls back to a concatenation of the per-project notes.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any

from throughlog import synthesize
from throughlog.schema import NormalizedEvent
from throughlog.categorize import categorize_events
from throughlog.config import (
    load_config, load_projects, data_dir, synthesis_options_from, BASE_DIR,
)


# --------------------------------------------------------------------------- #
# Event sourcing
# --------------------------------------------------------------------------- #
def gather_events(paths: list[Path]) -> list[NormalizedEvent]:
    """Load one or more thin-log JSONL files and reconcile to a single ordered,
    de-duplicated timeline of NormalizedEvents."""
    from throughlog.timeline import load_jsonl, reconcile
    raw: list[dict] = []
    for p in paths:
        if p.exists():
            raw.extend(load_jsonl(p))
    return [NormalizedEvent.from_dict(d) for d in reconcile(raw)]


def _resolve_sources(args: argparse.Namespace, cfg: dict[str, Any]) -> list[Path]:
    if args.events:
        return [Path(args.events)]
    if args.replay:
        return sorted((BASE_DIR / "data" / "events_replay").glob("*.jsonl"))
    events_dir = data_dir(cfg) / "events"
    if args.date:
        return [events_dir / f"{args.date}.jsonl"]
    return sorted(events_dir.glob("*.jsonl"))


# --------------------------------------------------------------------------- #
# LLM client
# --------------------------------------------------------------------------- #
def build_client(cfg: dict[str, Any], *, enable: bool):
    """Return an LLMClient, or None when disabled or no key is resolvable.
    A missing key is a soft failure — the pipeline still runs deterministically."""
    if not enable:
        return None
    from throughlog.llm.client import LLMConfig, LLMClient
    llm_cfg = LLMConfig.from_config(cfg)
    if not llm_cfg.resolve_key():
        print("[tl] no API key resolved — running deterministic-only "
              "(set llm.api_key in config.json or $OPENROUTER_API_KEY).")
        return None
    return LLMClient(llm_cfg)


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def run_pipeline(events: list[NormalizedEvent], projects: list[dict[str, Any]], *,
                 journal_dir: str | Path, client: Any, today: str | None = None,
                 options: synthesize.SynthesisOptions | None = None
                 ) -> synthesize.SynthesisRun:
    """Phase 1 (categorize, in place) then Phase 2 (synthesize)."""
    categorize_events(events, projects, client=client)
    return synthesize.run(events, projects, journal_dir=journal_dir,
                          client=client, today=today, options=options)


def _attribution_counts(events: list[NormalizedEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ev in events:
        method = (ev.attribution.method if ev.attribution else None) or "unattributed"
        counts[method] = counts.get(method, 0) + 1
    return counts


def cmd_synthesize(args: argparse.Namespace) -> int:
    cfg = load_config() if (BASE_DIR / "config.json").exists() else {}
    projects = load_projects()
    sources = _resolve_sources(args, cfg)

    # Fresh-clone fallback: `--replay` with no captured corpus on disk (the
    # data/ thin-log is gitignored runtime output) falls back to the built-in
    # demo day, so the documented first command always produces something.
    if args.replay and not sources:
        from throughlog import demo as demomod
        print("[tl] no corpus in data/events_replay — using the built-in demo "
              "day (run `tl demo` for the guided dashboard).")
        events = demomod.build_demo_events()
        projects = demomod.DEMO_PROJECTS
    else:
        if not sources:
            print("[tl] no event files found for the requested source.")
            return 1
        events = gather_events(sources)

    if not events:
        print(f"[tl] sources had no events: {[str(s) for s in sources]}")
        return 1

    journal_dir = Path(args.journal_dir) if args.journal_dir \
        else BASE_DIR / cfg.get("paths", {}).get("journal_dir", "journal")
    today = args.today or date.today().isoformat()
    client = build_client(cfg, enable=not args.no_llm)

    from dataclasses import replace
    options = synthesis_options_from(cfg)
    if args.entries is not None:                # --entries / --no-entries overrides config
        options = replace(options, write_entries=args.entries)
    if args.summary is not None:                # --summary off|weekly|monthly overrides config
        options = replace(options, summary_cadence=args.summary)
    if args.skip_unchanged is not None:         # --skip-unchanged / --no-skip-unchanged
        options = replace(options, skip_unchanged=args.skip_unchanged)

    print(f"[tl] {len(events)} events from {len(sources)} file(s) "
          f"-> journal: {journal_dir}  (llm={'on' if client else 'off'}, "
          f"entries={'on' if options.write_entries else 'off'}/{options.entry_period}, "
          f"summary={options.summary_cadence}, "
          f"skip_unchanged={'on' if options.skip_unchanged else 'off'})")
    res = run_pipeline(events, projects, journal_dir=journal_dir,
                       client=client, today=today, options=options)

    print(f"[tl] attribution: {_attribution_counts(events)}")
    for pd in res.projects:
        flag = f"  [!] {pd.error or pd.entry_error}" if (pd.error or pd.entry_error) else ""
        jcalls = f" + {pd.entry_calls} entries" if pd.entry_calls else ""
        print(f"  {pd.project_id}: {pd.event_count} events, "
              f"{pd.llm_calls} llm call(s){jcalls}{flag}")
    if res.summaries:
        print(f"[tl] period summaries: {', '.join(res.summaries)}")
    if res.summary_error:
        print(f"[tl] summary: {res.summary_error}")
    if res.exec_error:
        print(f"[tl] exec summary: {res.exec_error}")
    print(f"[tl] done ({res.today}).")
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    """(Re)build one weekly/monthly cross-project summary from the journal on disk.

    Reads the already-synthesized entries/archive sections (never the bus) and writes
    journal/summaries/<period>.md. The automatic path is `synthesize` with
    synthesis.summary_cadence on; this is the on-demand/backfill convenience."""
    cfg = load_config() if (BASE_DIR / "config.json").exists() else {}
    journal_dir = Path(args.journal_dir) if args.journal_dir \
        else BASE_DIR / cfg.get("paths", {}).get("journal_dir", "journal")
    period = "week" if args.week else "month"
    anchor = (args.date or date.today().strftime("%Y%m%d")).replace("-", "")
    period_key = synthesize._period_key(anchor, period)
    client = build_client(cfg, enable=not args.no_llm)

    body, err = synthesize.summarize_period(journal_dir, period_key, period, client=client)
    if not body:
        print(f"[tl] no activity recorded for {period} {period_key} under {journal_dir}.")
        return 0
    synthesize._write_period_summary(journal_dir, period_key, period, body)
    if err:
        print(f"[tl] {err}")
    print(f"[tl] wrote {journal_dir / 'summaries' / (period_key + '.md')} "
          f"(llm={'on' if client else 'off'}).")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Auto-discover git repos under a root into a ready-to-edit projects.json."""
    from throughlog import onboard
    root = Path(args.root).expanduser()
    if not root.exists():
        print(f"[tl] scan root does not exist: {root}")
        return 1
    out_path = Path(args.out).expanduser() if args.out else (BASE_DIR / "projects.json")
    # Opt-in, metadata-only LLM enrichment of discovered repos (--llm). Best-effort:
    # build_client returns None with no key, so discovery still works deterministically.
    client = None
    if getattr(args, "llm", False):
        cfg = load_config() if (BASE_DIR / "config.json").exists() else {}
        client = build_client(cfg, enable=True)
        if client:
            print("[tl] LLM enrichment on — sending each repo's structure + README "
                  "(never file contents) to refine keywords/description.")
    discovered, existing, path = onboard.init_registry(
        root, out_path, max_depth=args.depth, dry_run=args.dry_run, client=client)

    if not discovered:
        print(f"[tl] no new git repos found under {root} "
              f"({len(existing)} already registered).")
        return 0

    verb = "would add" if args.dry_run else "added"
    print(f"[tl] {verb} {len(discovered)} project(s) (kept {len(existing)} existing):")
    for p in discovered:
        rem = p["signals"]["git_remotes"]
        tag = f"  [{rem[0]}]" if rem else ""
        print(f"  + {p['id']}: {p['signals']['paths'][0]}{tag}")
    if args.dry_run:
        print(f"[tl] dry run — nothing written. Re-run without --dry-run to write {path}.")
    else:
        print(f"[tl] wrote {path}. Review keywords + window_patterns, then run "
              f"`tl capture` or `tl synthesize`.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Serve the local dashboard over the already-synthesized journal."""
    from throughlog.server import serve, DEFAULT_PORT
    serve(host=args.host, port=args.port if args.port is not None else DEFAULT_PORT,
          journal_dir=args.journal_dir, data_dir_path=args.data,
          open_browser=not args.no_browser)
    return 0


def _server_running(host: str, port: int, *, timeout: float = 0.4) -> bool:
    """True if something is already serving on host:port — i.e. another `tl up`
    instance. Used to keep the app single-instance: a second launch just opens the
    browser to the running one instead of binding a second supervisor to the port."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def cmd_up(args: argparse.Namespace) -> int:
    """The one-command app: start live capture (best-effort) AND open the dashboard.

    Coherent across the three launch surfaces — `capture`, `tray`, and `up` all run
    the *same* engine and write the *same* status file:
      * If a dashboard is already up (e.g. autostart launched one), this just opens
        the browser to it and exits — never a second instance.
      * If capture is already recording elsewhere (the tray or `tl capture`), this
        serves the dashboard *only* and shows that live capture — it does not start a
        second engine (which would double-write events).
      * Otherwise it starts capture here. Capture is best-effort: if the `capture`
        extra is missing it still serves the dashboard read-only.
    With `schedule.synthesize_at` set, an in-process timer also synthesizes nightly."""
    import threading

    from throughlog.server import serve, DEFAULT_PORT, Controller, capture_is_live
    host = args.host
    port = args.port if args.port is not None else DEFAULT_PORT
    url = f"http://{host}:{port}/"

    # Single instance: don't stack a second app on top of a running one.
    if _server_running(host, port):
        print(f"[tl] already running at {url} — opening it.")
        if not args.no_browser:
            import webbrowser
            webbrowser.open(url)
        return 0

    cfg = load_config() if (BASE_DIR / "config.json").exists() else {}
    projects = load_projects()
    datadir = Path(args.data) if args.data else data_dir(cfg)

    rt = None
    hb_stop: threading.Event | None = None
    if not args.no_capture:
        if capture_is_live(datadir):
            print("[tl] capture is already running (tray or `tl capture`) — serving "
                  "the dashboard only; the badge shows that live capture.")
        else:
            try:
                from throughlog.capture import build_runtime
                rt = build_runtime(enable_clipboard=not args.no_clipboard,
                                   enable_agents=not args.no_agents,
                                   heartbeat_sec=args.heartbeat, cfg=cfg,
                                   projects=projects)
                rt.sup.start()
                hb_stop = threading.Event()

                def _heartbeat() -> None:
                    while not hb_stop.is_set():
                        try:
                            rt.sup.write_status()
                        except Exception:
                            pass
                        hb_stop.wait(args.heartbeat)

                threading.Thread(target=_heartbeat, name="tl-up-heartbeat",
                                 daemon=True).start()
                print(f"[tl] capture started — {len(rt.roots)} allowlist root(s), "
                      f"data: {rt.data_dir}")
                if not rt.roots:
                    print("[tl] no project folders yet — open Settings in the "
                          "dashboard to add one.")
            except Exception as exc:
                print(f"[tl] capture unavailable ({exc}). Serving read-only.")
                print("[tl] install capture extras with:  pip install -e .[capture]")
                rt = None

    # In-app nightly synthesis (no-admin): runs while this process is alive.
    from throughlog import appconfig, nightly as nightlymod
    nightly = nightlymod.NightlyTimer(appconfig.nightly_time(cfg), base_dir=BASE_DIR)
    nightly.start()
    if nightly.target:
        print(f"[tl] nightly synthesis at {nightly.target} (while the app is open).")

    controller = Controller(supervisor=(rt.sup if rt else None))
    try:
        serve(host=host, port=port, journal_dir=args.journal_dir, data_dir_path=args.data,
              projects=projects, controller=controller,
              open_browser=not args.no_browser)
    finally:
        nightly.stop()
        if rt is not None:
            if hb_stop is not None:
                hb_stop.set()
            rt.sup.stop()
            rt.sup.join()
            try:
                rt.sup.write_status(alive=False)
            except Exception:
                pass
            rt.bus.close()
            print(f"[tl] capture stopped. {rt.bus.stats()}")
    return 0


def cmd_shortcut(args: argparse.Namespace) -> int:
    """Create / remove the double-clickable launcher for `tl up` (Windows)."""
    from throughlog import deploy
    if args.action == "remove":
        ok, out = deploy.remove_shortcut()
    else:
        ok, out = deploy.install_shortcut()
    print(f"[tl] {out}")
    return 0 if ok else 1


def cmd_demo(args: argparse.Namespace) -> int:
    """Zero-config guided tour: generate a built-in demo day, synthesize it
    deterministically (no key), and open the dashboard on it. The one command a
    fresh clone runs to see what the product produces."""
    import shutil
    from throughlog import demo as demomod
    out = BASE_DIR / "data" / "demo"
    # Store under events/ (the shape the bus produces) so the dashboard's Timeline
    # and time-per-project chart discover the demo day via the normal events path.
    store = out / "events" / f"{demomod.DEMO_DAY}.jsonl"
    journal = out / "journal"

    # The archive is append-only, so regenerate from a clean slate each run —
    # the demo must show exactly one day, however many times it is invoked.
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    demomod.write_demo_thinlog(store)
    events = gather_events([store])
    print(f"[tl] demo: {len(events)} synthetic events -> {journal}  (no key needed)")
    res = run_pipeline(events, demomod.DEMO_PROJECTS, journal_dir=journal,
                       client=None, today=demomod.DEMO_TODAY)
    # The keyless run can't produce the two LLM tiers; lay down the illustrative
    # living-overview + detailed-entries fixtures so the tour shows all three tiers.
    demomod.seed_demo_journal(journal)
    print(f"[tl] attribution: {_attribution_counts(events)}")
    for pd in res.projects:
        print(f"  {pd.project_id}: {pd.event_count} events")

    if args.no_serve:
        print(f"[tl] demo journal written to {journal}. "
              f"Run `tl serve --journal-dir {journal}` to view, or omit --no-serve.")
        return 0

    from throughlog.server import serve, DEFAULT_PORT
    registry = {p["id"]: p["name"] for p in demomod.DEMO_PROJECTS}
    serve(host=args.host, port=args.port if args.port is not None else DEFAULT_PORT,
          journal_dir=journal, data_dir_path=out, registry=registry,
          projects=demomod.DEMO_PROJECTS, open_browser=not args.no_browser)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Push the daily standup / summary to stdout, Slack, or a GitHub comment."""
    import os
    from throughlog import report as rpt
    cfg = load_config() if (BASE_DIR / "config.json").exists() else {}
    journal_dir = Path(args.journal_dir) if args.journal_dir \
        else BASE_DIR / cfg.get("paths", {}).get("journal_dir", "journal")
    rcfg = cfg.get("report", {}) or {}
    inp = rpt.load_inputs(journal_dir, date=args.date,
                          weekly=args.weekly, monthly=args.monthly)
    rollup = args.weekly or args.monthly       # both pick a multi-day rollup format

    if args.slack:
        webhook = (args.slack_webhook or os.environ.get("TL_SLACK_WEBHOOK")
                   or rcfg.get("slack_webhook"))
        if not webhook:
            print("[tl] no Slack webhook — set --slack-webhook, $TL_SLACK_WEBHOOK, "
                  "or report.slack_webhook in config.json.")
            return 1
        res = rpt.post_slack(webhook, rpt.slack_payload(inp, weekly=rollup))
        print(f"[tl] slack: {'ok' if res.ok else 'FAILED — ' + res.error}")
        return 0 if res.ok else 1

    if args.github:
        token = (args.github_token or os.environ.get("GITHUB_TOKEN")
                 or rcfg.get("github_token"))
        if not token:
            print("[tl] no GitHub token — set --github-token, $GITHUB_TOKEN, "
                  "or report.github_token in config.json.")
            return 1
        try:
            body = rpt.github_markdown(inp, weekly=rollup)
            res = rpt.post_github_comment(args.github, body, token)
        except ValueError as exc:
            print(f"[tl] {exc}")
            return 1
        print(f"[tl] github {args.github}: {'ok' if res.ok else 'FAILED — ' + res.error}")
        return 0 if res.ok else 1

    print(rpt.stdout_text(inp, weekly=rollup))
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Answer a natural-language question grounded in the synthesized journal."""
    from throughlog import ask as askmod
    cfg = load_config() if (BASE_DIR / "config.json").exists() else {}
    journal_dir = Path(args.journal_dir) if args.journal_dir \
        else BASE_DIR / cfg.get("paths", {}).get("journal_dir", "journal")

    question = " ".join(args.question).strip()
    if not question:
        print('[tl] ask needs a question, e.g.  tl ask "what did I ship on checkout?"')
        return 1

    corpus = askmod.load_corpus(journal_dir, project=args.project)
    if not corpus:
        where = f"{journal_dir}" + (f" (project {args.project})" if args.project else "")
        print(f"[tl] no journal found in {where} — run `tl synthesize` or `tl demo` first.")
        return 1

    client = build_client(cfg, enable=not args.no_llm)
    ans = askmod.answer(question, corpus, client, top_k=args.top)
    print(ans.text)
    if ans.error:
        print(f"\n[tl] (llm unavailable: {ans.error} — showed retrieved sections instead)")
    if args.show_sources and ans.sources:
        print("\n— sources: " + ", ".join(dict.fromkeys(ans.sources)))
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    """Pull tracked remote accounts (GitHub) into the thin-log via the bus/gate."""
    import os
    from throughlog import config as cfgmod
    from throughlog.bus import EventBus
    from throughlog.privacy.allowlist import Allowlist
    from throughlog.sources import github_pull

    cfg = load_config() if (BASE_DIR / "config.json").exists() else {}
    projects = load_projects()
    token = (args.token or os.environ.get("GITHUB_TOKEN")
             or cfg.get("integrations", {}).get("github", {}).get("token"))
    if not token:
        print("[tl] no GitHub token — set --token, $GITHUB_TOKEN, or "
              "integrations.github.token in config.json.")
        return 1

    remotes = github_pull.tracked_remotes(projects)
    pullable = [r for r in remotes if github_pull.owner_repo(r)]
    if not pullable:
        print("[tl] no GitHub remotes in projects.json signals.git_remotes — nothing to pull.")
        return 0

    roots = cfgmod.allowlist_roots(cfg, projects)
    bus = EventBus(cfgmod.data_dir(cfg) / "events", Allowlist([str(r) for r in roots]))
    print(f"[tl] pulling {len(pullable)} GitHub repo(s){' (watch)' if args.watch else ''}…")
    try:
        n = github_pull.pull_github_live(bus, token=token, projects=projects,
                                         once=not args.watch, interval_sec=args.interval)
    finally:
        bus.close()
    print(f"[tl] pulled {n} event(s). {bus.stats()}")
    return 0


def cmd_relay(args: argparse.Namespace) -> int:
    """Run the self-hostable cloud relay (multi-account agent + sync endpoint)."""
    from throughlog import relay
    from throughlog import config as cfgmod
    cfg = load_config() if (BASE_DIR / "config.json").exists() else {}
    registry = relay.AccountRegistry.from_config(cfg)
    store_root = Path(args.store) if args.store else (cfgmod.data_dir(cfg) / "relay")
    relay.serve(host=args.host, port=args.port, store_root=store_root, registry=registry)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Push gated events to the relay, or pull the account's events back."""
    import os
    from throughlog import sync as syncmod
    from throughlog import config as cfgmod
    cfg = load_config() if (BASE_DIR / "config.json").exists() else {}
    scfg = cfg.get("sync", {}) or {}
    endpoint = args.endpoint or os.environ.get("TL_RELAY_ENDPOINT") or scfg.get("endpoint")
    token = args.token or os.environ.get("TL_TOKEN") or scfg.get("token")
    if not endpoint or not token:
        print("[tl] sync needs --endpoint and --token (or $TL_RELAY_ENDPOINT / "
              "$TL_TOKEN / config sync.*).")
        return 1

    if args.action == "push":
        from throughlog.timeline import load_jsonl
        from throughlog.schema import NormalizedEvent
        events_dir = cfgmod.data_dir(cfg) / "events"
        files = [events_dir / f"{args.date}.jsonl"] if args.date \
            else sorted(events_dir.glob("*.jsonl"))
        events: list[Any] = []
        for f in files:
            if f.exists():
                events.extend(NormalizedEvent.from_dict(d) for d in load_jsonl(f))
        res = syncmod.push(endpoint, token, events)
        print(f"[tl] sync push: {'ok' if res.ok else 'FAILED'} "
              f"sent={res.sent} blocked={res.blocked} {res.error}".rstrip())
        return 0 if res.ok else 1

    res = syncmod.pull(endpoint, token, since=args.since)
    n = len((res.body or {}).get("events", [])) if res.ok else 0
    print(f"[tl] sync pull: {'ok' if res.ok else 'FAILED'} events={n} {res.error}".rstrip())
    return 0 if res.ok else 1


def cmd_capture(args: argparse.Namespace) -> int:
    """Run the live capture supervisor until Ctrl+C."""
    from throughlog.capture import run_capture
    run_capture(enable_clipboard=not args.no_clipboard,
                enable_agents=not args.no_agents,
                hotkeys=not args.no_hotkeys,
                heartbeat_sec=args.heartbeat)
    return 0


def cmd_tray(args: argparse.Namespace) -> int:
    """Run live capture behind a system-tray icon (needs the `capture` extra)."""
    from throughlog.tray import run_tray
    run_tray(enable_clipboard=not args.no_clipboard,
             enable_agents=not args.no_agents,
             heartbeat_sec=args.heartbeat)
    return 0


def cmd_autostart(args: argparse.Namespace) -> int:
    """Enable / disable capturing at logon (no admin needed — on Windows this is a
    per-user Startup-folder launcher, not a scheduled task)."""
    from throughlog import deploy
    if args.action == "enable":
        ok, out = deploy.enable_autostart(
            tray=args.tray, no_clipboard=args.no_clipboard, no_agents=args.no_agents)
        if ok:
            kind = "tray" if args.tray else "headless app"
            run_now = "tray" if args.tray else "up"
            print(f"[tl] autostart enabled ({kind}) — capture starts at your next "
                  f"logon. Start it now with `tl {run_now}`.")
    elif args.action == "disable":
        ok, out = deploy.disable_autostart()
        if ok:
            print("[tl] autostart disabled.")
    else:
        ok, out = deploy.task_status(deploy.CAPTURE_TASK)
    if out:
        print(out)
    return 0 if ok else 1


def cmd_hook(args: argparse.Namespace) -> int:
    """Install/remove/inspect a drop-in AI-agent hook (Claude Code, Cursor)."""
    from throughlog import hooks
    if args.action == "enable":
        ok, out = hooks.install_hook(args.tool, scope=args.scope)
        if ok:
            print(f"[tl] {out}")
            print("[tl] make sure `tl capture` / `tl up` is running (or "
                  "`tl autostart enable`) so reports land immediately — a report "
                  "posted while capture is offline is still queued in the drop "
                  "folder and picked up on the next run.")
        else:
            print(f"[tl] {out}")
        return 0 if ok else 1
    if args.action == "disable":
        ok, out = hooks.uninstall_hook(args.tool, scope=args.scope)
    else:
        ok, out = hooks.hook_status(args.tool, scope=args.scope)
    print(f"[tl] {out}")
    return 0 if ok else 1


def cmd_schedule(args: argparse.Namespace) -> int:
    """Register / remove the nightly-synthesis Windows scheduled task."""
    from throughlog import deploy
    if args.action == "enable":
        ok, out = deploy.enable_nightly(time_hhmm=args.time, no_llm=args.no_llm)
        if ok:
            mode = " (deterministic-only)" if args.no_llm else ""
            print(f"[tl] nightly synthesis scheduled for {args.time}{mode} — "
                  f"task '{deploy.SYNTHESIS_TASK}'.")
    elif args.action == "disable":
        ok, out = deploy.disable_nightly()
        if ok:
            print(f"[tl] nightly synthesis disabled — removed task '{deploy.SYNTHESIS_TASK}'.")
    else:
        ok, out = deploy.task_status(deploy.SYNTHESIS_TASK)
    if out:
        print(out)
    return 0 if ok else 1


def _setup_run_init(root: Path) -> list[str]:
    """Scan ``root`` for git repos and merge them into projects.json (the same
    deterministic discovery as ``tl init``). Returns a one-item recap list on
    success, empty otherwise. Never scans anything the user didn't name."""
    from throughlog import onboard
    root = root.expanduser()
    if not root.exists():
        print(f"[tl] scan root does not exist: {root} — skipping project discovery.")
        return []
    out_path = BASE_DIR / "projects.json"
    discovered, existing, path = onboard.init_registry(
        root, out_path, max_depth=4, dry_run=False, client=None)
    if not discovered:
        print(f"[tl] no new git repos found under {root} "
              f"({len(existing)} already registered).")
        return []
    print(f"[tl] added {len(discovered)} project(s) (kept {len(existing)} existing):")
    for p in discovered:
        print(f"  + {p['id']}: {p['signals']['paths'][0]}")
    print(f"[tl] wrote {path}. Review keywords/window_patterns any time in Settings.")
    return [f"{len(discovered)} project(s)"]


def _setup_print_plan(state, steps) -> None:
    """Read-only: print the detected machine state + recommended steps. This is what
    ``tl setup --plan`` emits — the call an agent makes to surface options to the
    user without changing anything."""
    print("[tl] ThroughLog setup — current state (nothing has been changed):")
    print(f"     platform      : {state.platform}")
    print(f"     agent tools   : {', '.join(state.agent_tools) or 'none detected'}")
    for tool in state.agent_tools:
        print(f"       - {tool} hook : {'installed' if state.hooks_installed.get(tool) else 'not installed'}")
    print(f"     projects      : {state.project_count} registered")
    print(f"     LLM key       : {'set' if state.key_set else 'not set'}")
    print(f"     nightly       : {state.nightly_at or 'off'}")
    print(f"     capture@logon : {'on' if state.autostart_on else 'off'}")
    print(f"     recording now : {'yes' if state.capture_live else 'no'}")
    print("\n[tl] Recommended steps:")
    for s in steps:
        mark = "[done]" if s.done else ("[n/a] " if not s.applicable else "[todo]")
        print(f"  {mark} {s.label}")
        print(f"         why: {s.why}")
        print(f"         run: {' '.join(s.command)}")
    print("\n[tl] Apply interactively with `tl setup`, or non-interactively with flags, "
          "e.g. `tl setup --yes --init <folder>`.")


def cmd_setup(args: argparse.Namespace) -> int:
    """Guided, approval-gated onboarding — walk the user through the whole powerhouse
    one opt-in step at a time (agent hooks, project discovery, the LLM key, nightly
    synthesis, capture-at-logon, launching the app). Composes existing guarded
    building blocks; touches neither the pipeline nor the privacy gate.

    `--plan` prints the detected state + recommendations and changes nothing (the
    read-only call an installing agent makes). A non-interactive run with no explicit
    choice falls back to `--plan` rather than hanging on a prompt."""
    from throughlog import setup_flow as sf

    state = sf.detect_state()
    steps = sf.plan_steps(state)
    interactive = sys.stdin.isatty()

    explicit = any(v is not None and v is not False for v in (
        args.hooks, args.init, args.key, args.nightly, args.autostart, args.start,
    )) or args.no_init or args.no_nightly
    if args.plan or (not interactive and not args.yes and not explicit):
        _setup_print_plan(state, steps)
        return 0

    def want(flag: bool | None, question: str, *, default: bool = True) -> bool:
        if flag is not None:
            return flag
        return sf.confirm(question, default=default, assume_yes=args.yes)

    print("[tl] ThroughLog setup — turning on the powerhouse, one opt-in step at a time.\n")
    enabled: list[str] = []

    # 1. Agent hooks (Claude Code / Cursor) — record what your agents do.
    if args.hooks is not False:
        from throughlog import hooks
        if state.agent_tools:
            for tool in state.agent_tools:
                if state.hooks_installed.get(tool):
                    print(f"[tl] {tool} hook already installed — skipping.")
                    continue
                if want(args.hooks, f"Install the {tool} hook so it records into your journal?"):
                    ok, msg = hooks.install_hook(tool, scope=args.scope)
                    print(f"[tl] {msg}")
                    if ok:
                        enabled.append(f"{tool} hook")
        elif args.hooks:
            print("[tl] no supported agent tool detected (~/.claude, ~/.cursor) — skipping hooks.")

    # 2. Project discovery — ONLY ever on a folder the user explicitly names, because
    #    a project's paths widen the privacy allowlist (what becomes observable).
    if args.no_init:
        print("[tl] skipping project discovery.")
    elif args.init:
        enabled += _setup_run_init(Path(args.init))
    elif interactive and not args.yes:
        if sf.confirm("Scan a folder for your git repos now? (registers them so they're "
                      "observed — you'll name the folder)", default=(state.project_count == 0)):
            default_root = str(Path.home() / "projects")
            root = sf.ask_text(f"Folder to scan [{default_root}]:", default=default_root)
            if root:
                enabled += _setup_run_init(Path(root))
    else:
        print("[tl] project discovery needs an explicit folder (it widens what's observed) "
              "— run `tl setup --init <folder>` when ready.")

    # 3. LLM key — explain what it buys, then offer to paste it (write-only, never echoed).
    if state.key_set:
        print("[tl] LLM key already set — skipping.")
    elif want(args.key, "Set an LLM API key now? (enables the narrative journal)"):
        print("[tl] An API key turns on the overview / detailed entries / executive-summary "
              "prose. Without one, ThroughLog still records and builds the deterministic "
              "archive + timeline — you just don't get the written narrative.")
        print("[tl] Get a free key at https://openrouter.ai/keys "
              "(default model: openai/gpt-oss-120b:free).")
        key = sf.ask_text("Paste your API key (blank to skip):", default="", secret=True)
        if key:
            from throughlog import appconfig
            appconfig.update_llm({"api_key": key})
            print("[tl] API key saved to config.json (write-only — never shown again).")
            enabled.append("LLM key")
        else:
            print("[tl] no key entered — add one any time in the dashboard Settings.")

    # 4. Nightly synthesis — the no-admin, in-app timer (runs inside `tl up`).
    if state.nightly_at:
        print(f"[tl] nightly synthesis already set for {state.nightly_at} — skipping.")
    elif not args.no_nightly:
        time = args.nightly
        if time is None and sf.confirm(
                "Synthesize your journal every night at 22:30?", default=True, assume_yes=args.yes):
            time = "22:30"
        if time:
            from throughlog import appconfig
            appconfig.update_schedule(time)
            print(f"[tl] nightly synthesis set for {time} — runs while `tl up` is open.")
            enabled.append(f"nightly @ {time}")

    # 5. Capture at logon — record the workday automatically (no admin).
    if state.autostart_on:
        print("[tl] capture-at-logon already enabled — skipping.")
    elif want(args.autostart, "Start capturing automatically at logon?"):
        from throughlog import deploy
        ok, out = deploy.enable_autostart()
        if ok:
            print("[tl] capture-at-logon enabled — starts at your next login.")
            enabled.append("capture at logon")
        if out:
            print(out)

    # Recap before the (possibly blocking) launch.
    if enabled:
        print(f"\n[tl] Setup complete. Enabled: {', '.join(enabled)}.")
    else:
        print("\n[tl] Setup complete — nothing changed.")

    # 6. Start now (blocks: serves capture + dashboard).
    if state.capture_live:
        print("[tl] ThroughLog is already running.")
        return 0
    if want(args.start, "Start ThroughLog now (open the dashboard)?"):
        ns = argparse.Namespace(
            host="127.0.0.1", port=None, journal_dir=None, data=None,
            no_capture=False, no_clipboard=False, no_agents=False,
            no_browser=False, heartbeat=30.0)
        return cmd_up(ns)
    print("[tl] Start the app any time with `tl up`.")
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    syn = sub.add_parser("synthesize", help="categorize + synthesize captured events")
    src = syn.add_mutually_exclusive_group()
    src.add_argument("--events", help="explicit thin-log JSONL file")
    src.add_argument("--date", metavar="YYYYMMDD", help="data/events/<date>.jsonl")
    src.add_argument("--replay", action="store_true",
                     help="the bundled real-day corpus in data/events_replay/")
    syn.add_argument("--journal-dir", help="output directory (default: config paths.journal_dir)")
    syn.add_argument("--today", metavar="YYYY-MM-DD", help="date label for this run")
    syn.add_argument("--no-llm", action="store_true",
                     help="deterministic only: archive without overview/exec prose")
    syn.add_argument("--entries", action=argparse.BooleanOptionalAction, default=None,
                     help="force the tier-2 detailed entries on/off "
                          "(default: config synthesis.write_entries)")
    syn.add_argument("--summary", choices=("off", "weekly", "monthly"), default=None,
                     help="force the cross-project period summary cadence "
                          "(default: config synthesis.summary_cadence)")
    syn.add_argument("--skip-unchanged", action=argparse.BooleanOptionalAction,
                     default=None,
                     help="reuse a project's existing overview/entries when its events "
                          "are unchanged (saves LLM calls on re-runs; default: config "
                          "synthesis.skip_unchanged)")
    syn.set_defaults(func=cmd_synthesize)

    sm = sub.add_parser("summarize",
                        help="(re)build one weekly/monthly cross-project summary from the journal")
    sm_when = sm.add_mutually_exclusive_group()
    sm_when.add_argument("--week", action="store_true",
                         help="summarize the ISO week containing --date (default: this week)")
    sm_when.add_argument("--month", action="store_true",
                         help="summarize the calendar month containing --date")
    sm.add_argument("--date", metavar="YYYYMMDD",
                    help="a date in the target period (default: today)")
    sm.add_argument("--journal-dir", help="journal directory (default: config paths.journal_dir)")
    sm.add_argument("--no-llm", action="store_true",
                    help="deterministic only: concatenate the period's sections without prose")
    sm.set_defaults(func=cmd_summarize)

    ini = sub.add_parser("init",
                         help="auto-discover git repos under a root into projects.json")
    ini.add_argument("root", nargs="?", default=str(Path.home() / "projects"),
                     help="directory to scan for git repos (default ~/projects)")
    ini.add_argument("--out", metavar="PATH",
                     help="projects.json to write/merge (default: repo projects.json)")
    ini.add_argument("--depth", type=int, default=4, metavar="N",
                     help="max scan depth below the root (default 4)")
    ini.add_argument("--dry-run", action="store_true",
                     help="print what would be added; write nothing")
    ini.add_argument("--llm", action="store_true",
                     help="enrich each discovered repo via one metadata-only LLM call "
                          "(structure + README only; needs a key)")
    ini.set_defaults(func=cmd_init)

    sv = sub.add_parser("serve", help="serve the local dashboard over the journal")
    sv.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    sv.add_argument("--port", type=int, default=None, metavar="PORT",
                    help="bind port (default 8799)")
    sv.add_argument("--journal-dir", help="journal directory (default: config paths.journal_dir)")
    sv.add_argument("--data", help="data directory (default: config paths.data_dir)")
    sv.add_argument("--no-browser", action="store_true",
                    help="do not auto-open a browser window")
    sv.set_defaults(func=cmd_serve)

    dm = sub.add_parser("demo",
                        help="zero-config guided tour: build a demo day + open the dashboard")
    dm.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    dm.add_argument("--port", type=int, default=None, metavar="PORT",
                    help="bind port (default 8799)")
    dm.add_argument("--no-serve", action="store_true",
                    help="synthesize the demo journal but do not start the dashboard")
    dm.add_argument("--no-browser", action="store_true",
                    help="start the dashboard but do not auto-open a browser")
    dm.set_defaults(func=cmd_demo)

    up = sub.add_parser("up",
                        help="the one-command app: start capture + open the dashboard")
    up.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    up.add_argument("--port", type=int, default=None, metavar="PORT",
                    help="bind port (default 8799)")
    up.add_argument("--journal-dir", help="journal directory (default: config paths.journal_dir)")
    up.add_argument("--data", help="data directory (default: config paths.data_dir)")
    up.add_argument("--no-capture", action="store_true",
                    help="dashboard only — do not start live capture")
    up.add_argument("--no-clipboard", action="store_true", help="disable the clipboard source")
    up.add_argument("--no-agents", action="store_true", help="disable the agent drop-folder source")
    up.add_argument("--no-browser", action="store_true", help="do not auto-open a browser")
    up.add_argument("--heartbeat", type=float, default=30.0, metavar="SEC",
                    help="status-file heartbeat interval (default 30s)")
    up.set_defaults(func=cmd_up)

    sct = sub.add_parser("shortcut",
                         help="create/remove a desktop + Start-menu launcher for `tl up`")
    sct.add_argument("action", nargs="?", choices=["create", "remove"], default="create")
    sct.set_defaults(func=cmd_shortcut)

    rp = sub.add_parser("report",
                        help="push the daily standup/summary to stdout/Slack/GitHub")
    rp.add_argument("--date", metavar="YYYY-MM-DD", help="report a specific day (default: newest)")
    rp_roll = rp.add_mutually_exclusive_group()
    rp_roll.add_argument("--weekly", action="store_true",
                         help="weekly rollup (prefers the synthesized weekly summary)")
    rp_roll.add_argument("--monthly", action="store_true",
                         help="monthly rollup (prefers the synthesized monthly summary)")
    rp.add_argument("--journal-dir", help="journal directory (default: config paths.journal_dir)")
    rp.add_argument("--stdout", action="store_true", help="print to stdout (default)")
    rp.add_argument("--slack", action="store_true", help="post to a Slack incoming webhook")
    rp.add_argument("--slack-webhook", metavar="URL",
                    help="Slack webhook (else $TL_SLACK_WEBHOOK / report.slack_webhook)")
    rp.add_argument("--github", metavar="OWNER/REPO#N",
                    help="post a comment to a GitHub issue/PR")
    rp.add_argument("--github-token", metavar="TOKEN",
                    help="GitHub token (else $GITHUB_TOKEN / report.github_token)")
    rp.set_defaults(func=cmd_report)

    ak = sub.add_parser("ask",
                        help="ask a natural-language question about your journal")
    ak.add_argument("question", nargs="+",
                    help='the question, e.g. "what did I ship on checkout this week?"')
    ak.add_argument("--journal-dir", help="journal directory (default: config paths.journal_dir)")
    ak.add_argument("--project", metavar="ID", help="restrict to one project's journal")
    ak.add_argument("--top", type=int, default=6, metavar="N",
                    help="passages to retrieve (default 6)")
    ak.add_argument("--no-llm", action="store_true",
                    help="deterministic retrieval only — print the matching sections")
    ak.add_argument("--show-sources", action="store_true",
                    help="print which journal sections were used")
    ak.set_defaults(func=cmd_ask)

    pl = sub.add_parser("pull",
                        help="pull tracked GitHub repos (commits/PRs/CI) into the thin-log")
    pl.add_argument("--github", action="store_true", help="pull from GitHub (default)")
    pl.add_argument("--token", metavar="TOKEN",
                    help="GitHub token (else $GITHUB_TOKEN / integrations.github.token)")
    pl.add_argument("--watch", action="store_true", help="keep polling instead of one pass")
    pl.add_argument("--interval", type=float, default=300.0, metavar="SEC",
                    help="poll interval when --watch (default 300s)")
    pl.set_defaults(func=cmd_pull)

    rl = sub.add_parser("relay",
                        help="run the self-hostable cloud relay (agent + sync endpoint)")
    rl.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    rl.add_argument("--port", type=int, default=8788, help="bind port (default 8788)")
    rl.add_argument("--store", metavar="DIR", help="relay store root (default: data/relay)")
    rl.set_defaults(func=cmd_relay)

    sy = sub.add_parser("sync", help="push/pull gated events to/from the relay")
    sy.add_argument("action", choices=["push", "pull"])
    sy.add_argument("--endpoint", metavar="URL",
                    help="relay base URL (else $TL_RELAY_ENDPOINT / sync.endpoint)")
    sy.add_argument("--token", metavar="TOKEN",
                    help="account token (else $TL_TOKEN / sync.token)")
    sy.add_argument("--date", metavar="YYYYMMDD", help="push only one day's thin-log")
    sy.add_argument("--since", metavar="ISO", help="pull events at/after this ts_wall")
    sy.set_defaults(func=cmd_sync)

    cap = sub.add_parser("capture", help="run the live capture supervisor (Ctrl+C to stop)")
    cap.add_argument("--no-clipboard", action="store_true", help="disable the clipboard source")
    cap.add_argument("--no-agents", action="store_true", help="disable the agent drop-folder source")
    cap.add_argument("--no-hotkeys", action="store_true", help="disable whisper / pause hotkeys")
    cap.add_argument("--heartbeat", type=float, default=30.0, metavar="SEC",
                     help="status-file heartbeat interval (default 30s)")
    cap.set_defaults(func=cmd_capture)

    tr = sub.add_parser("tray", help="run live capture behind a system-tray icon")
    tr.add_argument("--no-clipboard", action="store_true", help="disable the clipboard source")
    tr.add_argument("--no-agents", action="store_true", help="disable the agent drop-folder source")
    tr.add_argument("--heartbeat", type=float, default=30.0, metavar="SEC",
                    help="status-file heartbeat interval (default 30s)")
    tr.set_defaults(func=cmd_tray)

    au = sub.add_parser("autostart",
                        help="register/remove capture-on-logon (Windows Task Scheduler)")
    au.add_argument("action", choices=["enable", "disable", "status"])
    au.add_argument("--tray", action="store_true",
                    help="launch the tray UI at logon instead of headless capture")
    au.add_argument("--no-clipboard", action="store_true")
    au.add_argument("--no-agents", action="store_true")
    au.set_defaults(func=cmd_autostart)

    sc = sub.add_parser("schedule",
                        help="register/remove nightly synthesis (Windows Task Scheduler)")
    sc.add_argument("action", choices=["enable", "disable", "status"])
    sc.add_argument("--time", default="22:30", metavar="HH:MM",
                    help="daily run time (default 22:30)")
    sc.add_argument("--no-llm", action="store_true",
                    help="schedule deterministic-only synthesis (no API key needed)")
    sc.set_defaults(func=cmd_schedule)

    hk = sub.add_parser("hook",
                        help="install/remove a drop-in AI-agent hook (Claude Code, Cursor)")
    hk.add_argument("action", choices=["enable", "disable", "status"])
    hk.add_argument("tool", choices=["claude-code", "cursor"])
    hk.add_argument("--scope", choices=["user", "project"], default="user",
                    help="user settings (~/.claude, ~/.cursor) or this repo's "
                         "project settings (default: user)")
    hk.set_defaults(func=cmd_hook)

    st = sub.add_parser(
        "setup",
        help="guided, approval-gated onboarding (hooks, projects, key, nightly, autostart)")
    st.add_argument("-y", "--yes", action="store_true",
                    help="accept the suggested default for every yes/no step (agent-safe; "
                         "still won't paste a key or scan a folder it wasn't given)")
    st.add_argument("--plan", action="store_true",
                    help="print the detected state + recommended steps and exit — changes nothing")
    st.add_argument("--hooks", action=argparse.BooleanOptionalAction, default=None,
                    help="install/skip the detected agent hooks")
    st.add_argument("--init", metavar="ROOT", default=None,
                    help="scan ROOT for git repos and register them (widens the allowlist)")
    st.add_argument("--no-init", action="store_true", help="skip project discovery")
    st.add_argument("--key", action=argparse.BooleanOptionalAction, default=None,
                    help="offer/skip the LLM API-key step")
    st.add_argument("--nightly", metavar="HH:MM", nargs="?", const="22:30", default=None,
                    help="enable nightly synthesis at HH:MM (default 22:30)")
    st.add_argument("--no-nightly", action="store_true", help="skip nightly synthesis")
    st.add_argument("--autostart", action=argparse.BooleanOptionalAction, default=None,
                    help="enable/skip capture-at-logon")
    st.add_argument("--start", action=argparse.BooleanOptionalAction, default=None,
                    help="start (or don't) the app at the end")
    st.add_argument("--scope", choices=["user", "project"], default="user",
                    help="hook install scope (default: user)")
    st.set_defaults(func=cmd_setup)
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
