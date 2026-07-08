<p align="center">
  <img src="assets/banner_journal.png" alt="ThroughLog - the self-writing interactive journal for your workday, and your AI agents" width="920">
</p>

**The self-writing interactive journal for your work — and your AI agents. Private by construction, local by default.**

A self-writing interactive journal that silently observes your day — *and records what your AI agents did* — and distills it into per-project narrative journals using a local (or cloud) LLM. Zero manual logging. Nothing leaves your machine unless it has passed the privacy gate, and the optional cloud relay is self-hostable.

## Quickstart (30 seconds, no key)

```bash
git clone https://github.com/naorzadok/throughlog && cd throughlog
pip install -e .            # pure stdlib core — no heavy deps
python -m throughlog.cli demo      # builds a synthetic day + opens the dashboard
```

`tl demo` needs no config, no API key, and nothing captured: it generates a small, fully synthetic workday — two projects **plus a Claude Code AI-agent thread** — runs the real pipeline over it, and opens the local dashboard so you can see exactly what the tool produces. Then point it at your own work with `tl init` + `tl capture`.

### The app: `tl up`

For everyday use there's a single command that **starts capture and opens the dashboard together** — the dashboard is the app:

```bash
python -m throughlog.cli up          # start recording + open the control panel at http://127.0.0.1:8799
```

Not a developer? Run the one-step bootstrap instead — it checks Python, sets up a venv, installs everything, seeds your config, and launches the app:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1   # Windows  (add -Shortcut for a desktop icon)
```
```bash
./scripts/bootstrap.sh                                            # macOS / Linux
```

`python -m throughlog.cli shortcut create` adds a double-clickable desktop + Start-menu launcher (Windows). The dashboard is a **local control panel** — everything is on `127.0.0.1`, nothing is exposed to the network:

- **Settings** — paste your LLM API key (write-only — never shown back), pick the model and **reasoning effort** (Default/Low/Medium/High — applied only by models that support thinking, ignored otherwise), flip privacy toggles, and **add projects**: add one folder, or **scan a folder for git repos** and add them all at once. Adding folders widens what's observed, so the UI shows you exactly which directories become observable and asks you to confirm first. Power users can still hand-edit `config.json` / `projects.json`; the UI writes the same files (merge-only, atomic).
- **Automation** — set-and-forget toggles, **no admin rights needed**: **Start capturing at logon** (on Windows this drops a launcher in your Startup folder — *not* a scheduled task, so there's no "Access is denied") and **Synthesize the journal every night** at a time you pick. Nightly runs *inside the always-on app*, so it just needs the app to be running — which it is, once start-at-logon is on.
- **Ask your journal** — a search box over your journal (the `tl ask` Q&A, in the browser). Works without a key (shows the matching journal sections); with a key it answers in prose, grounded strictly in your gated journal.
- **Time per project** — a per-day chart of where your focus time went.
- **Controls** — Pause/Resume capture, "Synthesize now", and **Quit app** (a clean stop even when the app was launched hidden with no console), without leaving the page.

**One engine, three front-ends.** `tl capture` (terminal), `tl tray` (tray icon), and `tl up` (dashboard) all run the *same* recorder and write the *same* status file — so the dashboard's **Recording** badge is always truthful no matter which one started it. You do **not** need the tray to record; if `tl up` shows *Recording*, it's recording. `tl up` is single-instance and won't start a second recorder: launch it twice and the second just opens the browser to the running one; if capture is already running via the tray, `tl up` serves the dashboard over it instead of double-recording.

**Run it detached (no terminal).** Turn on *Start capturing at logon* (or run `tl autostart enable`), or use `tl shortcut create` for a double-click icon. Both launch via `pythonw.exe` (`tl up --no-browser`), so there's no console window — the only window is your browser tab. Stop it any time with **Quit app** on the dashboard (or the tray's Quit). Add `--tray` to autostart if you'd rather have a visible tray icon than a hidden background process.

---

## Architecture

The whole product lives in one clean package, `throughlog/` — a pure-stdlib analysis core (no runtime deps), with optional capture adapters and integrations layered on top.

### Principles

- **Source-agnostic event timeline.** Every signal source (window focus, fs/git, narration/clipboard, autonomous agents) emits into one normalized v2 schema (`throughlog/schema.py`). Focus is just one adapter — the system keeps working when the OS is blind (opaque apps, remote sessions, AI-agent work).
- **Privacy is a deterministic gate.** `throughlog/privacy/` redacts *before anything is persisted* and re-checks *before any LLM egress*. Redaction never depends on the LLM. Only allow-listed directories are observed; clipboard is typed/summarized, never stored raw; home paths are normalized to `~`.
- **Diffs are opt-in (default OFF).** File contents and diffs are stripped by default. Set `privacy.capture_diffs: true` to keep a scrubbed, size-capped working-tree diff per change in a purgeable sidecar (`data/diffs/`) that **never** syncs. A three-layer ignore system protects you: your repo's `.gitignore`, a hardcoded secrets-file denylist (`.env`, `*.key`, …) enforced even for tracked files, and your own globs (`signals.ignore_globs` / a repo-root `.tlignore`). `python -m throughlog.privacy.gate --audit <events> --diffs data/diffs` proves nothing would leak.
- **A strict determinism boundary.** Everything is deterministic, stdlib-only, and simulator-tested **except two places**: Phase 1 categorization of genuinely ambiguous intent, and Phase 2 overview/exec-summary prose.
- **Robust to weak/free models.** The LLM client (`throughlog/llm/client.py`) is a stdlib-`urllib` OpenAI-compatible client (OpenRouter free model by default) with retries, tolerant JSON recovery, and a hard rule: any LLM failure becomes `needs_review` — events are never dropped or the run crashed.

### Pipeline

```
sources/ (adapters) ─► privacy/gate ─► timeline (reconcile) ─► data/events/*.jsonl
                                                                      │
   Phase 1  throughlog/categorize.py  signal stack (deterministic), LLM only for ambiguity
                                                                      │
   Phase 2  throughlog/synthesize.py  archive (deterministic) + overview + entries + daily + exec summary (LLM)
```

### The product surface

| Command | What it does |
| --- | --- |
| `tl up` | **The app**: start live capture *and* open the dashboard/control panel in one command. Settings, Ask, time-per-project chart, pause/synthesize controls — all in the browser. |
| `tl demo` | Zero-config guided tour: generate a synthetic demo day (no key) and open the dashboard on it. The fastest way to see what the tool produces. |
| `tl init [root]` | Auto-discover git repos under a root into a ready-to-edit `projects.json` (paths, git remotes, inferred keywords) — clone to first journal in under 2 minutes. |
| `tl serve` | A stdlib local **dashboard** over the journal: overview, per-project pages, reconciled timeline, live-capture status. (`tl up` = this + capture + write access.) |
| `tl shortcut create` | Create a double-clickable desktop + Start-menu launcher for `tl up` (Windows). |
| `tl capture` / `tray` | Live capture supervisor (Windows/macOS/Linux), optionally behind a tray icon. |
| `tl synthesize` | Turn a captured thin-log into per-project journals + daily + executive summary. |
| `tl report [--slack\|--github OWNER/REPO#N]` | Push the daily standup / weekly summary to stdout, a Slack webhook, or a GitHub issue/PR comment. |
| `tl pull --github` | Pull tracked repos' commits / PRs / CI — including **bot/agent-authored** PRs — as events, so cloud work that never touched this machine shows up. |
| `tl relay` / `tl sync` | Run a self-hostable, multi-account **cloud relay** (cloud agents report in) and opt-in, gated-only **sync** across devices. |
| `tl autostart` / `schedule` | Capture at login (no admin — Startup folder on Windows; launchd/cron elsewhere) + nightly synthesis. |

**Agent-native.** Any AI agent — local or in the cloud — writes itself into your journal with one POST. See [`integrations/`](integrations/) for the documented report contract, a Python/JS SDK (`throughlog.agent_sdk`), and a drop-in **Claude Code hook**: "add one hook and Claude Code writes itself into your work journal," trust-classified and attributed to the right project.

### Run the analysis pipeline

The core pipeline (categorize + synthesize) is **pure stdlib** — no install needed to run it over already-captured events:

```bash
# The built-in demo day (synthetic, ships in-source) — no key, no capture needed:
python -m throughlog.cli synthesize --replay --no-llm     # deterministic only (offline); falls back to the demo day
python -m throughlog.cli demo                             # same data, but also opens the dashboard

# A specific captured day or an explicit thin-log file:
python -m throughlog.cli synthesize --date 20260506
python -m throughlog.cli synthesize --events path/to/log.jsonl
```

Output lands in `journal/` (override with `--journal-dir DIR`) as a three-tier per-project journal: `archive.md` (deterministic, always written) → `entries/<period>.md` (LLM, append-by-day, detail-preserving — the opt-in detailed entries, on by default; `--no-entries` to skip; grouped monthly `<YYYY-MM>` or weekly `<YYYY-Www>` per `synthesis.entry_period`) → `overview.md` (LLM living doc, high-level), plus `daily.md`, `executive_summary.md`, and an optional cross-project weekly/monthly retrospective in `summaries/<period>.md` (`synthesis.summary_cadence`, or build one on demand with `tl summarize --week|--month`).

### Capture a real workday (live)

Live capture needs the OS adapters' optional dependencies:

```bash
pip install -e .[capture]        # psutil, watchdog, pyperclip, keyboard, uiautomation
python -m throughlog.cli capture        # start the supervisor; Ctrl+C to stop
python -m throughlog.cli capture --no-clipboard --no-agents
```

The **supervisor** (`throughlog/capture.py`) runs every source adapter concurrently — focus sessionization, process/`LONG_RUN` monitoring, the allowlist-scoped fs/git watcher, clipboard, and the agent drop-folder — each in its own thread, all feeding **one thread-safe emitter** so the privacy gate still runs on every event before it is persisted to `data/events/YYYYMMDD.jsonl`. It writes a heartbeat to `data/daemon_status.json`, shuts down cleanly on Ctrl+C/SIGTERM (each source flushes its in-flight session), and never lets one failing source take the others down. Optional hotkeys: `ctrl+shift+m` whisper note, `ctrl+shift+p` privacy pause. Only allow-listed project directories are watched, and the tool's own `data/`/`journal/` are excluded so it never captures itself.

A typical day is then: `capture` runs all day → `synthesize` (e.g. nightly) turns the captured thin-log into journals.

### Run it hands-free (tray + autostart + nightly schedule)

```bash
python -m throughlog.cli tray               # capture behind a tray icon (pause/whisper/synthesize/quit)
python -m throughlog.cli autostart enable   # start capture automatically at every logon  (add --tray)
python -m throughlog.cli schedule  enable --time 22:30   # run synthesis nightly at 22:30
python -m throughlog.cli autostart disable  # remove the logon task
python -m throughlog.cli schedule  disable  # remove the nightly task
```

On Windows, **`autostart`** installs a per-user **Startup-folder launcher** (no admin rights — `schtasks /Create` needs elevation, which is why the old Task-Scheduler path failed with "Access is denied"); on macOS/Linux it uses launchd/cron (also per-user). **`schedule`** still registers a Task Scheduler / launchd / cron job for synthesis-while-nothing-is-open *(needs admin on Windows)* — the no-admin alternative is the in-app nightly timer (the Automation toggle in the dashboard, or `schedule.synthesize_at` in `config.json`), which `tl up` runs while it's open. The tray icon is green while recording, amber while paused. See [`instructions.md`](instructions.md) for a full from-scratch walkthrough.

### Configure the LLM

Copy `config.example.json` to `config.json` (gitignored) and set your key — either inline in `llm.api_key`, or via the `OPENROUTER_API_KEY` environment variable (the inline key wins if both are set). Get a free key at [openrouter.ai](https://openrouter.ai/). The default model `openai/gpt-oss-120b:free` is free-tier. Optionally set `llm.reasoning_effort` (`low`/`medium`/`high`; empty = provider default) to turn up thinking on models that support it — it's sent as OpenRouter's unified `reasoning` param and safely ignored by models that don't.

### Verify

The first two commands run on a **fresh clone with zero setup** — no API key, no `pip install`, no `config.json` (Python ≥ 3.12 only); they're fully deterministic and offline. The `--smoke` checks below are optional and need a key.

```bash
python -m unittest discover -s tests -p "test_*.py"   # deterministic unit suite (459 tests)
python -m sim.simulator --all                          # case-matrix scenarios (O1–O4, A1–A7, C1–C7, M1)
python -m throughlog.llm.client  --smoke                      # live: connectivity
python -m throughlog.categorize  --smoke                      # live: Phase 1 on an ambiguous event
python -m throughlog.synthesize  --smoke                      # live: Phase 2 archive + overview + exec summary
```

---

## What It Does

The system watches your machine all day — active window titles, file saves, clipboard content, keyboard activity density — and every night automatically groups those raw signals into project-specific journal entries written in plain prose. The end result is an `overview.md` per project that reads like a coherent technical journal: what you worked on, what changed, what decisions were made.

---

## Full walkthrough

`README.md` is the tour; [`instructions.md`](instructions.md) is the complete from-scratch guide — install, configure, capture, synthesize, serve, report, pull, relay/sync, autostart, and troubleshooting.

## License

[Apache 2.0](LICENSE) © Naor Zadok

