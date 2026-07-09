# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A private, local work journal: an OS-level signal pipeline that silently observes a workday — and what your AI agents did — and distills it into per-project narrative journals via an LLM. **The entire product lives in the `throughlog/` package** (pure-stdlib core + optional capture adapters/integrations). There is no separate legacy package; the original Iteration-1 scripts were removed and remain recoverable from git history only.

## Commands

The core analysis pipeline (`throughlog.schema`, `throughlog.privacy`, `throughlog.timeline`, `throughlog.categorize`, `throughlog.synthesize`, `throughlog.llm`, `sim`) is **pure standard library** — no install or venv needed to run or test it. Python ≥ 3.12.

```bash
# Unit suite (deterministic, ~345 tests, no network)
python -m unittest discover -s tests -p "test_*.py"
python -m unittest tests.test_categorize                       # one module
python -m unittest tests.test_categorize.ClassName.test_name   # one test

# Scenario simulator — declarative case-matrix rows in sim/scenarios/*.json,
# replayed through the REAL bus (gate + persistence)
python -m sim.simulator --all
python -m sim.simulator --scenario sim/scenarios/m1_allowlist_drop.json

# Zero-config first run — generate a synthetic demo day + open the dashboard (no key, no setup)
python -m throughlog.cli demo                              # the guided tour (throughlog/demo.py); --no-serve to skip the browser

# Run the analysis pipeline over already-captured events (no key needed with --no-llm)
python -m throughlog.cli synthesize --replay --no-llm     # deterministic only; falls back to the demo day if no corpus on disk
python -m throughlog.cli synthesize --date 20260506       # data/events/<date>.jsonl
python -m throughlog.cli synthesize --events path/to/log.jsonl

# Ask a natural-language question about the synthesized journals (deterministic retrieval; LLM optional)
python -m throughlog.cli ask --no-llm "what did I ship on checkout this week?"   # prints the matching sections
python -m throughlog.cli ask --project checkout "any open threads?"              # + one grounded LLM answer with a key

# Live LLM smokes (need a key — see Config). Each is self-contained & safe to re-run.
python -m throughlog.llm.client  --smoke                  # connectivity
python -m throughlog.categorize  --smoke                  # Phase 1 on an ambiguous event
python -m throughlog.synthesize  --smoke                  # Phase 2 archive + overview + exec summary

# Privacy audit — prove a persisted store would leak nothing if sent to a model
python -m throughlog.cli demo --no-serve && python -m throughlog.privacy.gate --audit data/demo/20260624.jsonl

# Live capture (needs the optional extras: pip install -e .[capture])
python -m throughlog.cli capture                          # supervisor, Ctrl+C to stop
python -m throughlog.cli tray                             # capture behind a tray icon
python -m throughlog.cli autostart enable [--tray]        # Windows Task Scheduler: capture at logon
python -m throughlog.cli schedule  enable --time 22:30    # Windows Task Scheduler: nightly synthesis

# Drop-in AI-agent hooks — safe, idempotent install into the host tool's own settings file
python -m throughlog.cli hook enable|disable|status claude-code|cursor [--scope user|project]

# Guided, approval-gated onboarding (hooks → projects → key → nightly → autostart → launch)
python -m throughlog.cli setup                            # interactive; per-step opt-in
python -m throughlog.cli setup --plan                     # read-only: detected state + recommended steps
```

There is no linter/formatter configured; match the surrounding style (type hints, `from __future__ import annotations`, module-level docstrings explaining *why*).

## Architecture

A typical day: `capture` runs all day appending gated events to a thin log → `synthesize` (e.g. nightly) turns that log into journals.

```
sources/ (adapters) ─► bus.emit ─► privacy/gate ─► data/events/YYYYMMDD.jsonl
                                                          │  (reconcile to real order)
   Phase 1  throughlog/categorize.py   deterministic signal stack; LLM only for ambiguity
                                                          │
   Phase 2  throughlog/synthesize.py   deterministic archive + LLM overview/entries/daily/exec summary
```

### Product surface beyond capture+synthesize (v2 commands)

These all stay on the deterministic side (no new core deps; the LLM only ever runs in categorize/synthesize, plus three read-only, off-pipeline uses below — the `tl ask` Q&A, the `tl summarize` period rollup, and the opt-in `tl init --llm` enrichment):
- **`tl init`** (`throughlog/onboard.py`) — auto-discover git repos under a root into `projects.json` (paths, normalized git remotes, inferred keywords/window_patterns). Merge-only; never clobbers existing projects. **A project does not require git** — a path-only entry works (path is the *strongest* categorization signal and drives the allowlist); the dashboard's "Add project" and `appconfig.add_project` accept any folder. **`--llm`** (opt-in, default off) enriches each discovered entry via one *metadata-only* call (`build_repo_digest` → README excerpt + names-only file tree + language markers; **never file contents**) that may propose `description`/`keywords`/`window_patterns`/`entry_extract`/`domains`/`jira_prefixes` — it **never** sets `signals.paths`/`git_remotes`, so the model can't widen the privacy allowlist; degrades to the deterministic entry on no key / `LLMError` / bad JSON (`onboard.enrich_project`).
- **`tl serve`** (`throughlog/server.py`) — stdlib `http.server` dashboard over the journal (overview / per-project overview / reconciled timeline / period summaries / live-capture badge). Pure view builders + injection-safe markdown; thin HTTP driver. Default port 8799. The journal *views* are read-only, but a CSRF-guarded **Settings page** writes `config.json`/`projects.json` through the guarded, atomic, known-keys-only chokepoint `appconfig.py` (LLM, privacy, automation, **entries/summary cadence**, **add/scan projects**, **opt-in init enrichment**) — `update_*` per section; enum-validated server-side.
- **`tl ask`** (`throughlog/ask.py`) — read-only natural-language Q&A over the synthesized journals. Deterministic keyword retrieval over already-gated journal markdown selects passages; one **optional, read-only** LLM call answers grounded strictly in them (egress-re-scrubbed in `client.chat`; degrades to printing the passages on no key / `--no-llm` / `LLMError`). Sits *outside* the capture→synthesize pipeline — never touches the bus, never writes, only reads already-gated output. Prompt in `llm/prompts.py::build_ask_prompt`.
- **`tl summarize`** (`throughlog/synthesize.py::summarize_period`) — (re)build one **cross-project weekly/monthly retrospective** (`journal/summaries/<period>.md`) from the already-written, gated entries/archive sections for that period. Read-only over journal output (same posture as `tl ask`); one LLM call, degrades to a deterministic concat of the per-project sections (C5). The automatic path is `synthesize` when `synthesis.summary_cadence` is on; this command is the on-demand/backfill convenience.
- **`tl report`** (`throughlog/report.py`) — push the daily standup / weekly / **monthly** summary to stdout / Slack webhook / GitHub issue-PR comment. Pure parsers+formatters over `daily.md`+`executive_summary.md`; `--weekly`/`--monthly` **prefer the synthesized `summaries/<period>.md`** when present, else fall back to regluing the daily paragraphs. Thin transports with injectable opener.
- **`tl pull --github`** (`throughlog/sources/github_pull.py`) — pull tracked repos' commits/PRs/CI as events. **All pulled remote work → `AGENT_REPORT`** (the schema's "ingested agent/remote report"; not path-gated, unlike `GIT_COMMIT`). Bot/app author → `kind=agent`, human → `kind=remote`. Only repos in `projects.json` `git_remotes` are pulled.
- **`tl relay`** (`throughlog/relay.py`) + **`tl sync`** (`throughlog/sync.py`) — self-hostable multi-account cloud relay (token→account; `POST /report` ingests, `POST /sync` accepts already-gated events, `GET /events` reads) and opt-in device sync. **Cloud privacy rule:** only events carrying a `privacy` stamp ever leave the machine and every payload is egress-re-scrubbed before send (`sync.prepare_for_egress`); raw content never syncs; the relay is self-hostable.
- **`tl setup`** (`throughlog/setup_flow.py` pure core + `cli.cmd_setup` thin driver) — guided, **approval-gated** onboarding that turns on the whole product one opt-in step at a time by *composing existing guarded chokepoints* (no new pipeline path, never touches the gate): `hooks.install_hook` → `onboard.init_registry` → `appconfig.update_llm` (write-only key) / `update_schedule` (no-admin nightly) → `deploy.enable_autostart` → `tl up`. `detect_state()`/`plan_steps()` are pure/side-effect-free so `--plan` prints state + recommendations read-only (the call an installing agent makes). **Safety:** a non-interactive/EOF run *declines* every step (never applies a default without consent); project discovery only ever scans a folder the user explicitly names (it widens the allowlist). The agent-facing trigger is the repo-root `AGENTS.md`, which tells any installing agent to run this flow with the user's per-step approval and to explain the LLM-key trade-off.
- **Agent SDK + hooks** (`throughlog/agent_sdk.py`, `throughlog/hooks.py`, `integrations/`) — `build_report()` emits the schema-v2 `AGENT_REPORT` the `/report` endpoint validates; `AgentReporter` POSTs it (drop-folder fallback, defaulting to the same folder `capture` watches — see `integrations/_common.py`). Claude Code hook (`integrations/claude_code/`) + Cursor hook (`integrations/cursor/`) + JS client wrap the one documented contract; `tl hook enable|disable|status claude-code|cursor` (`throughlog/hooks.py`) is the safe, idempotent installer that merges into each tool's own settings file (`~/.claude/settings.json` nested shape, `~/.cursor/hooks.json` flat shape) without disturbing unrelated hooks already configured there. `categorize.py::_event_signals` has an `AGENT_REPORT` branch so `repo`/`files`/`summary` attribute via the normal signal stack.

`throughlog/deploy.py` is cross-platform: macOS launchd plist, Linux crontab, and (for *nightly synthesis*) Windows Task Scheduler XML — pure builders, dispatched by platform. **Capture-at-logon on Windows uses the per-user Startup folder, NOT Task Scheduler**, because `schtasks /Create` requires elevation (a non-admin user hits "Access is denied"); the Startup-folder launcher (`enable_autostart` → a `.lnk` to `pythonw -m throughlog.cli up --no-browser`) needs no admin and runs detached/windowless. The no-admin nightly path is the in-process `throughlog/nightly.py` timer (`schedule.synthesize_at` config), which `tl up` runs while open — distinct from the admin `tl schedule` (schtasks) path. `tl up` is single-instance and capture-coherent: it won't bind a second port or start a second supervisor when one is already running (it reads the shared `daemon_status.json` heartbeat via `server.capture_is_live`). `os_focus.py` focus/idle probes are platform-dispatched (Windows/macOS/Linux) behind the same `capture_live` driver; the deterministic `FocusSessionizer` is OS-agnostic.

### The determinism boundary (the central design rule)

The **capture→synthesize pipeline** is deterministic, stdlib-only, and simulator-testable **except exactly two places**: Phase 1 categorization of *genuinely ambiguous* intent (`throughlog/categorize.py`), and Phase 2 overview/entries/exec-summary prose (`throughlog/synthesize.py`). The capture layer and source adapters **never** touch an LLM. When adding logic, keep it deterministic unless it provably belongs in one of those two LLM places.

Three further LLM uses sit **outside** the pipeline and are all read-only over already-gated output (or metadata), opt-in, and never required — they never touch the bus and never write events: the `tl ask` Q&A, the **period-summary rollup** (`summarize_period`, over gated entries/archive markdown), and **opt-in `tl init --llm` enrichment** (over a metadata-only folder digest — never file contents, never the allowlist). Each re-runs the egress gate inside `client.chat` and degrades deterministically.

**LLM failure is never fatal.** Any LLM error (`LLMError`) must degrade gracefully, never crash and never drop an event:
- Phase 1 → the event's attribution becomes `method="needs_review"` (project stays unassigned).
- Phase 2 → the deterministic `archive.md` is still written (it is built from event data alone and written *first*); overview/entries/daily/exec fall back to deterministic concatenation. A period summary falls back to a deterministic concat of the period's per-project sections; init enrichment falls back to the deterministic project entry.

### Source-agnostic event model (`throughlog/schema.py`)

Every signal source emits the same `NormalizedEvent` (schema v2): focus sessions, file changes, git commits, narration, clipboard, idle, deep-work, long-run, agent reports. Window focus is *just one adapter* — the system keeps producing a timeline when the OS is blind (opaque apps, remote sessions, autonomous AI-agent work via `AGENT_REPORT`). The schema round-trips losslessly to/from JSON and has a rule-based `validate()` reused by agent ingestion to reject spoofed reports. Adding a signal type = add an event type here + an adapter, not a new pipeline path.

### Privacy is a deterministic gate, not an LLM concern (`throughlog/privacy/`)

`throughlog/bus.py::EventBus.emit` is the **only** path to disk, and it runs `privacy/gate.py` on every event before persistence. The gate (deterministic): enforces the **allowlist** (only directories belonging to a tracked project are observable — `config.py::allowlist_roots` derives this from `projects.json` signal paths + `privacy.allowlist_extra`), types/summarizes clipboard instead of storing raw content (drops credential-shaped), strips raw content fields, recursively redacts secrets and normalizes home paths to `~`, then stamps a `Privacy` audit trail. A second, independent **egress** gate (`privacy/egress.py`) re-scrubs every prompt inside `llm/client.py::chat` before any network send — so a gate bug still cannot leak.

### Opt-in file diffs (default OFF) — `throughlog/privacy/diff_policy.py`

By default the gate **strips** `diff`/`body` (metadata-by-default). Set `privacy.capture_diffs: true` to instead keep a **scrubbed, size-capped** working-tree diff per tracked-file change / commit. A frozen `DiffPolicy` (`config.py::diff_policy_from`) is threaded through the one chokepoint (`bus → gate`); `DEFAULT_POLICY` captures nothing, so with the toggle off behavior is byte-identical to before the feature existed. Key invariants when touching this path:
- **First-party only.** Diff/body retention applies to `FILE_CHANGE`/`GIT_COMMIT` only (`gate.py::_DIFF_AWARE_TYPES`). `AGENT_REPORT` (the spoof surface) is always stripped regardless of the toggle, and the relay always gates with `DEFAULT_POLICY` (capture off) — never honor a sender toggle.
- **Three-layer ignore** (`diff_policy.py`): git's own `.gitignore` (free — diffs come from `git diff`); a hardcoded secrets-file denylist (`is_secret_file`, basename-scoped — enforced even for *tracked* files); and user globs (`path_ignored`) from per-project `signals.ignore_globs` + an optional repo-root `.tlignore`. A multi-file commit diff is decomposed per-file (`split_diff_by_file`) and each hunk gated independently, so a committed `.env` hunk can't ride along.
- **Sidecar storage.** The scrubbed diff is parked on a transient `payload["_diff_clean"]`; the bus writes it to `data/diffs/<sha256>.patch` (content-addressed) and replaces it with a `diff_ref`. The diff text **never** lands in the thin-log and **never** syncs (sidecars carry no `privacy` stamp and live outside `data/events/`). `_`-prefixed payload keys are dropped by `schema.py::to_dict`/`to_json` as a structural barrier, so a transient can never serialize even through a crash/error window.
- **Never crash, never drop.** `scrub_diff` is fully `try`-guarded and degrades to "no diff" (binary/PEM-block/oversized/non-string all suppress, never raise); the event itself is always persisted.
- **Never to an LLM.** Diff text lives only in the sidecar; summaries show a `(+diff)` marker, never the body, so nothing diff-related reaches `client.chat`. `python -m throughlog.privacy.gate --audit <events> --diffs <dir>` proves the store *and* the sidecars would leak nothing.
- Adjacent opt-ins on the same machinery: full commit `body` + `diffstat` (capture-on), and a capped/scrubbed clipboard `preview` (`privacy.clipboard_preview`).

### Adapter pattern: pure core + thin live driver (`throughlog/sources/`)

Each adapter splits its deterministic *logic* (a clock-injected, dependency-free core — e.g. `os_focus.FocusSessionizer`, `proc_monitor.LongRunTracker`, `fs_git.FileChurnFilter`, `intent/ladder.py`) from a thin live *driver* (`*_live(emitter, stop)`) that reads real OS state and is the only part importing optional deps (`psutil`, `watchdog`, `pyperclip`, `keyboard`, `uiautomation`). The same core runs under live capture and under `sim/simulator.py`. **Test the core directly; never require the live driver in a test.** The supervisor (`throughlog/capture.py`) runs every driver in its own thread feeding one thread-safe emitter, so the gate still runs per-event and one failing source can't take the others down.

### Intent resolution (`throughlog/intent/ladder.py`)

A deterministic priority ladder turns raw session signals into an intent descriptor, recording which rung produced it: UIA text → window title → process cmdline/cwd → saved artifact → human narration → input density → `needs_review` (the `resolve_intent` rung order — note narration ranks *above* the weak input-density guess; the module docstring's ordering is stale). It never fabricates intent (terse/filler narration falls through). The Phase-1 LLM may later refine a `needs_review`/low-confidence result but is never required to.

### Timeline reconciliation (`throughlog/timeline.py`)

Events are persisted in *arrival* order; `reconcile()` produces the *real* order: sort by effective wall time (`ts_wall` corrected by `clock_offset_sec`, so late/skewed agent reports land where they belong), de-dup by `event_id` keeping the more-trusted/earlier copy, and drop `rejected`-trust events from the trusted timeline while leaving them in the log for audit. Pure functions over dicts — JSONL is the store, no database.

### Categorization signal stack (`throughlog/categorize.py`)

`signal_stack()` scores an event against every project's `projects.json` signals and the strongest wins. The actual max scores (precedence): path 0.95 > jira 0.85 > git-remote 0.82 > domain 0.80 > title-keyword ≤0.78 > window-pattern 0.75 > narration-keyword 0.72 > app 0.70 — title-keyword scales with hit count (`0.50 + 0.08·hits`, capped 0.78), so its rank is data-dependent; `app` is the *weakest* signal, not mid-pack. ≥ `THRESHOLD` (0.51) → assign deterministically, **no LLM call**. Only the ambiguous, text-bearing residue is batched into **one** LLM call (per-event calls die on rate-limited free models); hallucinated project ids and sub-threshold answers become `needs_review`.

### LLM client (`throughlog/llm/client.py`)

stdlib-`urllib` POST to an OpenAI-compatible `/chat/completions` (OpenRouter default; same shape works for Ollama). Retries transient transport/429/5xx with capped backoff; terminal failure raises `LLMError`. Tolerant of provider quirks (content-as-list, gpt-oss `reasoning`-only replies). Prompt construction lives in `throughlog/llm/prompts.py`; structured-answer parsing is the *caller's* job (kept separate from transport retries).

**Client-side pacing (`throughlog/llm/ratelimit.py`).** `llm.max_requests_per_min` (product default **18** in `config.example.json`, `LLMConfig` field default **0 = disabled** for byte-identity) gates a sliding-60s-window `RateLimiter` applied inside `_post` — the *physical-request* layer, so retries **and** fallback-model requests are paced too (they all count against a free tier's per-minute limit; pacing them is what stops a 429→retry→429 spiral). It **only delays, never refuses** a call the pipeline needs. Pure + clock-injectable (shares the client's injected `sleep`, so tests never really wait); `<=0` is a no-op. Editable from Settings → LLM (`appconfig.update_llm`).

## Config & data layout

- **`config.json`** is gitignored (copy from `config.example.json`). LLM key resolves as inline `llm.api_key` first, else `$OPENROUTER_API_KEY`. Default model `openai/gpt-oss-120b:free`. The pipeline runs deterministically with no key (prints a notice, behaves like `--no-llm`).
- **`projects.json`** — the project registry. `signals.*` drive both categorization *and* the privacy allowlist, so the registry is security-relevant, not just a hint. Like `config.json` it is **gitignored and user-specific**; `projects.example.json` ships as the template and `load_projects()` falls back to it when `projects.json` is absent (fails closed: the example's paths don't exist, so nothing is observed until you configure or run `tl init`).
- **`throughlog/demo.py`** — the built-in demo day. `tl demo` generates a small, deterministic, synthetic corpus (two projects + a Claude Code agent thread), synthesizes it keylessly, and serves the dashboard. Because the keyless run produces no LLM tiers, `seed_demo_journal()` also writes hand-authored *illustrative* `overview.md` + `entries/<YYYY-MM>.md` fixtures (clearly labeled synthetic) so the tour shows all three tiers and the entries↔overview detail contrast. This is the zero-config first run; nothing personal or large is committed. `synthesize --replay` falls back to it when no corpus exists on disk.
- **`data/`** and **`journal/`** are gitignored runtime output (so nothing captured ever lands in the repo). `data/events/YYYYMMDD.jsonl` is the live thin-log; `data/diffs/<sha256>.patch` holds the opt-in scrubbed diff sidecars (only when `capture_diffs` is on; purgeable, never synced); `data/demo/` holds the regenerated demo store + journal. Per-project output is a **three-tier journal**: `journal/project_<id>/{archive.md (append-only, deterministic, raw), entries/<period>.md (append-by-day, LLM, detail-preserving — the opt-in tier-2 detailed entries; one file per period — `<YYYY-MM>` monthly by default or `<YYYY-Www>` weekly per `synthesis.entry_period`; entries stay per-day either way), overview.md (LLM living doc, rewritten each run, high-level)}`, plus `journal/daily.md`, `journal/executive_summary.md`, and `journal/summaries/<period>.md` (the cross-project weekly/monthly retrospective tier — `2026-W26.md` / `2026-06.md`, idempotently overwritten).
- **`config.json` synthesis block:** `write_entries` (default **ON** in `config.example.json`; the `SynthesisOptions`/function default is OFF so library callers and tests stay byte-identical), `entry_max_tokens` (1500), `entry_period` (`month` | `week`, default `month` — entries file grouping only, entries stay per-day), and `summary_cadence` (`off` | `weekly` | `monthly`; product default `weekly` in `config.example.json`, function default `off` for byte-identity — drives the `journal/summaries/<period>.md` rollup). Per-project `signals.entry_extract` (optional list) tunes what the entry call must capture; absent, it falls back to the project description + keywords. A fifth toggle `skip_unchanged` (default **off**) is the opt-in re-run economy: `synthesize.run` fingerprints each project's event batch (sha256 over the gated event dicts) into `journal/.synth_state.json` and, when the batch is unchanged since the last run, **reuses the existing overview/entries instead of re-billing the LLM** (the deterministic archive is still rewritten; the stored daily paragraph is reused so the exec summary stays complete). Any change re-synthesizes the project fully — it never skips work the pipeline needs. Also on the `synthesize` CLI as `--skip-unchanged/--no-skip-unchanged`. All toggles are editable from the dashboard Settings page (`appconfig.update_synthesis`).
- **`config.json` init block:** `llm_enrich` (default **off**) — opt-in metadata-only LLM enrichment of newly-added projects (dashboard "Add project" / `tl init --llm`). Editable from Settings (`appconfig.update_init`); the server only builds a client when this is on **and** a key resolves.
- **`config.json` privacy block** (all default-off / safe): `capture_diffs`, `diff_max_lines` (400), `diff_max_bytes` (65536), `ignore_globs`, `clipboard_preview`, `clipboard_preview_chars` (256). Per-project `signals.ignore_globs` adds additive diff-ignore globs.

## Conventions

- Events are **never dropped or lost** by downstream failures — they become `needs_review` (Phase 1) or survive in the deterministic archive (Phase 2). Preserve this when editing either phase.
- Adding/changing a case the pipeline must handle: add a `sim/scenarios/*.json` row (named after the case-matrix id, e.g. `a2_*`, `c4_*`) and assert it through the real bus, in addition to a unit test.
- Tests insert the repo root on `sys.path` and use inline fixtures decoupled from the live `projects.json`/`config.json`; keep tests offline (inject a fake `opener`/`sleep` into `LLMClient`).
