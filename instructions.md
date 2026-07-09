# ThroughLog — User Guide

A complete, from-scratch walkthrough. If you've never seen this project before, read
this top to bottom and you'll understand **what it does**, **why it's built the way it
is**, and **exactly how to install, configure, and run it**.

---

## 1. What is this?

ThroughLog watches what you do on your computer all day — which windows you
focus, which files you save, what you copy, when you walk away — and at the end of the
day it writes that activity up as **a plain-English journal, one per project**, plus a
short **executive summary** across everything. You never log anything by hand.

Think of it as an **automatic, self-writing journal of your work** — including the work your
AI agents do. Instead of trying to remember "what did I actually get done this week?",
you open `journal/` (or the local dashboard) and read it.

It's designed to keep working in the situations that defeat normal time-trackers:

- **Opaque apps** — CAD, design tools, remote desktops where the OS can't see what
  you're doing. It infers "deep work" from focus + activity even with zero readable text.
- **Long unattended jobs** — a 6-hour simulation running while you're away is recorded
  as real work (`LONG_RUN`), not as "idle".
- **AI agents — local *and* cloud** — work done by autonomous agents (a Claude Code run
  on your machine, or a cloud agent that opens a PR you never touched) is a first-class
  source and gets folded into the same journal.
- **Work that never touched this machine** — commits, PRs and CI runs on tracked GitHub
  repos can be *pulled in* so the journal is complete even for cloud/remote activity.

And once the journal exist, you can **read them in a browser** (`tl serve`), **push** a
standup to Slack or a GitHub comment (`tl report`), and **sync** across devices through
a self-hostable relay (`tl relay` / `tl sync`) — all without weakening the privacy
guarantees below.

---

## 2. The big idea, in one picture

```
  SOURCES                  PRIVACY GATE             TIMELINE            ANALYSIS              SHARE
  (what happened)       (redact before save)     (put in order)      (write it up)        (read/push)

  window focus  ┐                                                  ┌ Phase 1: sort each   ┌ serve  (browser)
  file saves    │     ┌────────────────────┐    ┌────────────┐     │ event into a project │ report (Slack/GH)
  clipboard     ├───► │ allowlist → redact  ├──► │ reconcile, ├──► │ (rules first, LLM    │ sync   (devices,
  whisper notes │     │ → drop secrets      │    │ de-dup     │    │ only if ambiguous)   │        via relay)
  AI-agent runs │     │ → stamp audit trail │    └────────────┘     │                      └────────────────────
  pulled GitHub ┘     └────────────────────┘           │           └ Phase 2: write the
   (commits/PRs)              │                  data/events/         journal + summary
                       nothing reaches disk      YYYYMMDD.jsonl       (LLM writes prose)
                       until it passes here                                  │
                              │                                       ┌──────┴───── before any cloud send,
                       (a SECOND independent egress re-scrub runs ────┘             every payload is
                        before anything is sent to an LLM or relay)                 re-scrubbed again
```

Two principles are worth internalizing, because they explain every design choice:

1. **Privacy is a deterministic gate, not an afterthought.** Before *anything* is saved
   to disk — and again before *anything* is sent to a cloud LLM **or a relay** — every
   event passes through `throughlog/privacy/`. It only keeps activity inside folders you've
   explicitly allow-listed, it keeps *metadata* (which file, which app, when) rather than
   file contents, it scrubs anything that looks like an API key / password / token, and it
   rewrites your home folder path `C:\Users\you` as `~`. **Raw clipboard text is never
   stored** — it's summarized ("url copied", "code snippet 120 chars") or dropped if it
   looks like a credential. This redaction is plain code (regex + rules); it never depends
   on the AI. A second, independent egress gate re-scrubs every outbound payload, so even a
   bug in the first gate cannot leak.

2. **The AI is used in exactly two narrow places.** Everything else is deterministic and
   testable. The LLM is only called (a) to categorize a handful of genuinely ambiguous
   events, and (b) to write the journal prose. If the LLM is offline or has no API key, the
   pipeline still runs and still produces a deterministic archive — it just skips the
   pretty prose. Your events are never lost because a model failed. The capture layer, the
   dashboard, onboarding, push outputs, account pull, and the relay are **all
   deterministic** — they never touch an LLM.

---

## 3. What it captures — and what it never captures

| Captured (as metadata) | Never captured |
|---|---|
| Active window title + app name, and how long | File **contents** |
| File **save events** under allow-listed project folders | Activity outside your allow-listed folders |
| Clipboard, **typed/summarized** (e.g. "url copied") | **Raw** clipboard text |
| Keyboard *density* (how busy), not keystrokes | Actual keystrokes / what you typed |
| Idle start/end, deep-work and long-run sessions | Anything matching a secret/token/password pattern |
| Git commit author/message under allow-listed repos | Your home path verbatim (normalized to `~`) |
| **AI-agent runs** — what an agent did / which files it touched | Raw agent payloads (validated, trust-classified, scrubbed) |
| **Pulled GitHub** commits / PRs / CI runs on tracked repos | Repos **not** in your `projects.json` (only tracked remotes pull) |

If a project folder isn't in your allowlist, the logger simply doesn't watch it. The
allowlist **is** the privacy boundary. The same rule governs the cloud: only events that
already passed the gate ever leave the machine, and only repos you've listed are pulled.

---

## 4. Before you start

- **Operating system.** The analysis pipeline, dashboard, onboarding, report, pull, and
  relay run on **Windows, macOS, and Linux** (pure standard library). **Live capture** is
  most complete on **Windows** today; macOS/Linux focus-and-idle probes and the
  autostart/schedule mechanism (launchd / cron) are implemented but less battle-tested than
  the Windows path. If you're on macOS/Linux you can always analyze, serve, report, pull,
  relay and sync; live capture is best-effort.
- **Python 3.12+**. Check with `python --version`.
- **(Optional) a free OpenRouter API key** for the journal prose. Without it, you still get
  the deterministic archive of everything — you just don't get the written-up journal.

---

## 5. Install

Open a terminal in the project folder
(`C:\Users\dev\Desktop\projects\throughlog`).

**Step 1 — (recommended) make a virtual environment.** Live capture needs a few OS
libraries; a venv keeps them tidy.

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1          # Windows PowerShell
# source venv/bin/activate           # macOS / Linux
```

> A `venv\` already exists in this checkout with the capture libraries installed. If you
> use it, run the capture commands with `.\venv\Scripts\python.exe -m throughlog.cli ...` (or
> activate it as above). The plain `python` on this machine is the system interpreter and
> does **not** have the capture libraries — it can still run analysis, the dashboard, the
> report/pull/relay commands, and the tests.

**Step 2 — install the package.**

```powershell
pip install -e .            # core only: enough to analyze, serve, report, pull, relay
pip install -e .[capture]   # ALSO installs the live-capture + tray libraries
```

`-e` is an *editable* install: it links the package in place so `python -m throughlog.cli ...`
(and the `tl` command) work from anywhere, and your edits take effect immediately. The
`[capture]` extra adds `psutil`, `watchdog`, `pyperclip`, `keyboard`, `uiautomation`,
`pystray`, and `Pillow` — needed only to record live and to show the tray icon. Everything
else (synthesize, serve, report, pull, relay, sync) is pure standard library and needs no
extras.

**Step 3 — confirm it works, and see what it produces.**

```powershell
python -m throughlog.cli --help
python -m unittest discover -s tests -p "test_*.py"    # ~240 tests, expect OK
python -m throughlog.cli demo                                  # the 30-second guided tour ↓
```

`tl demo` needs **no config, no API key, and nothing captured**. It generates a small
synthetic workday — two projects plus a Claude Code **AI-agent thread** — synthesizes it
deterministically, and opens the dashboard on it. That's the fastest way to understand
what the tool produces before you point it at your own work. (Add `--no-serve` to just
write the files; everything it writes lands under `data/demo/` and is gitignored.)

---

## 6. Configure (point it at *your* work)

> Skipping ahead just to look around? You don't need any of this — run `tl demo`. The
> steps below are for capturing your **own** activity.

Two files drive everything, both in the project root, both **gitignored** (they hold your
paths and keys): **`projects.json`** and **`config.json`**. Each ships as a
`*.example.json` template — copy it, or (for the registry) let `tl init` build it for you.

### 6.1 Fastest start: `tl init` (auto-build the project registry)

Hand-authoring `projects.json` is a wall. `tl init` scans a folder for git repos and
generates a project per repo — with `signals.paths`, normalized `git_remotes`, and
inferred `keywords` / `window_patterns` — so you go from clone to first journal in minutes
with no manual regex authoring.

```powershell
tl init                          # scan ~/projects (the default root)
tl init C:\Users\dev\Desktop\projects   # scan a specific root
tl init <root> --dry-run         # show what WOULD be added; write nothing
tl init <root> --depth 3         # limit how deep to scan (default 4)
tl init <root> --out other.json  # write/merge into a different file
```

It is **merge-only**: it never clobbers or overwrites projects you've already defined — it
only adds repos it hasn't seen. After it runs, open `projects.json` and improve the
`description`, `keywords`, and `window_patterns` (those make attribution much better).

### 6.2 `projects.json` — the most important file

This is the registry of projects you want journal for. The richer each definition, the
better the logger attributes your activity. A project looks like this:

```json
{
  "projects": [
    {
      "id": "monthly-budget-analysis",
      "name": "Monthly Budget Analysis",
      "status": "active",
      "description": "Aggregating bank/credit-card statements into one Excel workbook and deriving monthly spend by category. Python parses and dedups; Excel holds the pivots and charts.",
      "signals": {
        "paths":           ["C:\\Users\\dev\\Desktop\\projects\\budget-app"],
        "keywords":        ["budget", "expenses", "bank statement", "pivot table"],
        "apps":            ["EXCEL.EXE", "python.exe", "Code.exe"],
        "domains":         [],
        "window_patterns": [".*budget.*", ".*bank.*statement.*"],
        "git_remotes":     [],
        "jira_prefixes":   []
      }
    }
  ]
}
```

| Field | What it does |
|---|---|
| `id` | URL-safe slug; becomes the journal folder name `journal/project_<id>/`. |
| `name` | Human-readable title shown in the journal. |
| `description` | 2–4 sentences. **The single most valuable field** — it's what the LLM reads when rules can't decide. |
| `signals.paths` | **The privacy allowlist.** Folders to watch; file saves/windows under them are a strong match. Anything outside every project's paths (and `allowlist_extra`) is never recorded. |
| `signals.keywords` | Words matched in window titles / clipboard / whisper notes. |
| `signals.apps` | Process names, e.g. `EXCEL.EXE`, `chrome.exe`. |
| `signals.domains` | URL/domain substrings for browser work. |
| `signals.window_patterns` | Regexes matched against the full window title. |
| `signals.git_remotes` | Remote URLs. Used to attribute commits **and** to decide which repos `tl pull --github` is allowed to pull. |
| `signals.jira_prefixes` | Ticket prefixes, e.g. `PROJ`, `INFRA`. |

> **The allowlist is the privacy boundary.** Only put folders in `signals.paths` (or
> `privacy.allowlist_extra` in `config.json`) that you're comfortable having observed.
> Everything else on disk is invisible to the logger. Likewise, **only repos listed in
> `signals.git_remotes` are ever pulled** by `tl pull`.

### 6.3 `config.json` — settings, keys, and integrations

Copy the example and edit it. `config.json` is gitignored, so your keys never get
committed.

```powershell
copy config.example.json config.json     # Windows
# cp config.example.json config.json      # macOS / Linux
```

It looks like this (the integration sections are all **optional** and **off** until you
fill them in):

```json
{
  "llm": {
    "provider": "openrouter",
    "base_url": "https://openrouter.ai/api/v1",
    "model": "openai/gpt-oss-120b:free",
    "model_fallback": "qwen/qwen3-next-80b-a3b-instruct:free",
    "api_key_env": "OPENROUTER_API_KEY",
    "api_key": "",
    "timeout_sec": 600,
    "max_retries": 3
  },
  "privacy": { "allowlist_extra": [] },
  "paths": { "data_dir": "data", "journal_dir": "journal" },

  "report":       { "slack_webhook": "", "github_token": "" },
  "integrations": { "github": { "token": "" } },
  "relay":        { "tokens": { "CHANGE-ME-secret-token": "my-account" } },
  "sync":         { "endpoint": "", "token": "" }
}
```

**Setting your LLM API key** — pick one (don't paste a key into a chat or commit it):

- **Inline:** put it in `"api_key": "sk-or-..."` in `config.json` (gitignored).
- **Environment variable (preferred):** set `OPENROUTER_API_KEY` and leave `api_key` blank.
  ```powershell
  setx OPENROUTER_API_KEY "sk-or-...your-key..."     # Windows; open a new terminal after
  # export OPENROUTER_API_KEY="sk-or-..."             # macOS / Linux
  ```

Get a free key at <https://openrouter.ai/>. The default model `openai/gpt-oss-120b:free`
is free-tier. **No key? No problem** — run analysis with `--no-llm` and you still get the
full deterministic archive.

The other sections (used only by the commands that need them):

| Section | Used by | What it holds |
|---|---|---|
| `privacy.allowlist_extra` | the gate | extra folders to watch beyond your projects' paths. |
| `paths` | everything | where events (`data/`) and journal (`journal/`) are written. |
| `report.slack_webhook` / `report.github_token` | `tl report` | a Slack incoming-webhook URL and/or a GitHub token for posting standups. |
| `integrations.github.token` | `tl pull --github` | a GitHub token for pulling tracked repos' commits/PRs/CI. |
| `relay.tokens` | `tl relay` | a `{ "secret-token": "account-name" }` map — one entry per account that may report/sync. |
| `sync.endpoint` / `sync.token` | `tl sync` | the relay base URL and your account token, for pushing/pulling between devices. |

Every secret here can also come from an environment variable instead (see each command
below), so you never have to keep tokens in a file.

---

## 7. Everyday use

The day has two halves: **capture** (record all day) and **synthesize** (turn the
recording into journal, e.g. each night). Then you **read**, **share**, and optionally
**sync** the result.

### 7.1 Capture your day (console)

```powershell
.\venv\Scripts\python.exe -m throughlog.cli capture        # start recording; Ctrl+C to stop
python -m throughlog.cli capture --no-clipboard --no-agents # opt out of sources you don't want
```

This starts the **supervisor**: it runs every source at once (focus, process/long-run,
the file/git watcher, clipboard, and the agent drop-folder), each in its own thread, all
feeding one privacy-gated bus. It writes a heartbeat to `data/daemon_status.json`, shuts
down cleanly on Ctrl+C (each source flushes its in-progress session), and one failing
source never takes the others down. The tool excludes its own `data/` and `journal/`
folders so it never records itself.

### 7.2 Capture with the tray icon (recommended)

```powershell
.\venv\Scripts\python.exe -m throughlog.cli tray
```

Same recording, but behind a **system-tray icon** so it's visible and controllable
without a console:

- **green** icon = recording, **amber** = paused;
- the menu shows a live status line (events captured, sources alive);
- **Pause / Resume** — flip the privacy pause;
- **Whisper note…** — type what you're doing (see below);
- **Synthesize now** — build journal from what's captured so far;
- **Open journal folder** / **Quit** — Quit shuts capture down cleanly.

### 7.3 Controls while capturing

- **Whisper note** (`Ctrl+Shift+M`, or the tray menu) — pops a one-line box: *"What are
  you working on?"* This is the highest-value signal you can give, because it tells the
  logger your *intent* directly. Tip: add **"since 2pm"** to relabel an earlier window
  retroactively.
- **Privacy pause** (`Ctrl+Shift+P`, or the tray menu) — while paused, events are
  **dropped, not buffered**. Nothing you do during a pause is ever stored. Use it for
  anything you don't want recorded.

### 7.4 Turn the day into journal (synthesize)

```powershell
python -m throughlog.cli synthesize                 # analyze everything captured in data/events/
python -m throughlog.cli synthesize --date 20260622 # just one day
python -m throughlog.cli synthesize --no-llm        # deterministic archive only, no API key needed
python -m throughlog.cli synthesize --replay --no-llm  # no corpus yet? falls back to the built-in demo day
```

This runs both analysis phases: **Phase 1** sorts each event into a project (rules first,
LLM only for the genuinely ambiguous handful), and **Phase 2** writes the prose.

### 7.5 Reading the output (files)

Everything lands in `journal/`:

| File | What it is |
|---|---|
| `journal/project_<id>/archive.md` | **Append-only, deterministic.** One section per day: sessions, commits, files, narration. Written even with no LLM, never rewritten. This is your permanent factual record. |
| `journal/project_<id>/overview.md` | A **living narrative**, rewritten by the LLM each run — current state, ongoing threads, the story of the project. |
| `journal/daily.md` | A shared cross-project log, newest day on top. |
| `journal/executive_summary.md` | A short LLM summary across **all** projects — your "what happened lately" overview. |

A good first run to try right now, before you've captured anything: **`python -m throughlog.cli
demo`** generates a small synthetic day (two projects plus an AI-agent thread),
synthesizes it with no key, and opens the dashboard on it — the 30-second guided tour.
Add `--no-serve` to just write the files.

### 7.6 See it in a browser: `tl serve`

The journal are nice files, but the dashboard is what makes the project *legible at a
glance* (and screenshot-able). It's a tiny, local, read-only web server over whatever
you've already synthesized — **pure standard library, no new dependencies**.

```powershell
python -m throughlog.cli serve                 # http://127.0.0.1:8799  (auto-opens a browser)
python -m throughlog.cli serve --port 9000 --no-browser
python -m throughlog.cli serve --journal-dir some/dir --data some/data
```

Views: an overview of all projects, each project's rendered `overview.md`, the reconciled
timeline of the day, the executive summary, and a **live-capture badge** (green when
`data/daemon_status.json` shows a fresh heartbeat). It binds to `127.0.0.1` (your machine
only) by default. The fastest way to see it on a fresh clone — one command, no key, no
config:

```powershell
python -m throughlog.cli demo                  # builds a demo day AND opens the dashboard on it
```

Or point `serve` at any journal you've already built:

```powershell
python -m throughlog.cli synthesize --replay --no-llm
python -m throughlog.cli serve
```

---

## 8. Share your journal: `tl report`

A journal in a folder is passive. `tl report` formats the standup/summary and delivers it
where work happens — to your terminal, a Slack channel, or a GitHub issue/PR comment. It's
a thin formatter over the synthesized `daily.md` + `executive_summary.md`; it never calls
an LLM.

```powershell
python -m throughlog.cli report                       # print today's standup to stdout (default)
python -m throughlog.cli report --weekly              # summarize the last 7 days instead
python -m throughlog.cli report --date 2026-06-22     # a specific day

# Post to Slack (needs an incoming-webhook URL):
python -m throughlog.cli report --slack
python -m throughlog.cli report --slack --slack-webhook https://hooks.slack.com/services/XXX

# Post as a comment on a GitHub issue or PR (needs a token):
python -m throughlog.cli report --github owner/repo#42
python -m throughlog.cli report --github owner/repo#42 --github-token ghp_xxx
```

Where it reads each secret (in order): the `--slack-webhook` / `--github-token` flag, then
the `$TL_SLACK_WEBHOOK` / `$GITHUB_TOKEN` environment variable, then
`report.slack_webhook` / `report.github_token` in `config.json`.

---

## 9. Capturing work that never touched this machine

### 9.1 Pull cloud/account work: `tl pull --github`

Some work happens off your keyboard — commits, PRs, reviews and CI runs on a shared repo,
including **commits/PRs authored by a cloud coding agent**. `tl pull --github` reaches out
to GitHub and folds that activity into the same thin-log, through the same privacy gate.

```powershell
python -m throughlog.cli pull --github               # one pass over tracked repos
python -m throughlog.cli pull --github --watch        # keep polling (default every 300s)
python -m throughlog.cli pull --github --token ghp_xxx --interval 120
```

- **Only repos in `projects.json` `signals.git_remotes` are pulled** — the registry is the
  allowlist here too.
- **Human vs. agent** is preserved: bot/app-authored commits and PRs are recorded as the
  agent's activity; human-authored ones as remote human work. Either way the pulled item
  flows through `bus.emit` → the gate like any other source, so the same redaction applies.
- The token comes from `--token`, else `$GITHUB_TOKEN`, else `integrations.github.token` in
  `config.json`.

After a pull, run `synthesize` and the cloud work shows up in the journal alongside your
local work.

### 9.2 AI-agent reports (local agents)

Autonomous agents are a first-class source. There are two ways to feed an agent's work in;
both end up validated, trust-classified, reconciled by the agent's own timestamp, and
folded into the journal.

**(a) Drop a JSON file** into `data/agent_drop/` — this works out of the box while `tl
capture` is running (the supervisor watches that folder). A minimal report
(`data/agent_drop/run-42.json`):

```json
{
  "type": "AGENT_REPORT",
  "source": { "kind": "agent", "adapter": "claude-code", "identity": "agent:claude-1" },
  "ts_wall": "2026-06-22T02:14:00+03:00",
  "payload": {
    "project_hint": "throughlog",
    "summary": "Refactored the capture supervisor; added cooperative shutdown.",
    "files": ["throughlog/capture.py", "throughlog/tray.py"]
  }
}
```

**(b) Use the Agent SDK / drop-in hooks** (`throughlog/agent_sdk.py`, `integrations/`). The SDK's
`AgentReporter` builds the same report and POSTs it to a `/report` endpoint (default
`http://127.0.0.1:8787/report`); if no listener is up it **falls back to writing into the
drop folder**, so it never blocks the agent. The ready-made **Claude Code** and **Cursor**
hooks (`integrations/claude_code/tl_hook.py`, `integrations/cursor/tl_hook.py`) are the
highest-leverage integration — add one and that tool writes itself into your work journal.
Install either with the built-in installer, which safely merges into whatever's already in
the settings file instead of overwriting it:

```powershell
python -m throughlog.cli hook enable claude-code
python -m throughlog.cli hook enable cursor
python -m throughlog.cli hook status claude-code|cursor
python -m throughlog.cli hook disable claude-code|cursor
```

Each hook reads three optional env vars: `TL_ENDPOINT` (where to POST; default the 8787
endpoint), `TL_TOKEN` (a relay bearer token), and `TL_DROP_DIR` (the fallback folder —
defaults to `data/agent_drop/` when unset). See `integrations/README.md` for the one
documented JSON contract and the JS client.

The logger never trusts a report blindly: malformed reports are **rejected** (recorded as
an audit stub, never silently dropped), and well-formed-but-suspicious ones (e.g. an
unknown identity or a future-dated timestamp) are kept but flagged **low-trust**. Disable
the local drop-folder source with `--no-agents`.

---

## 10. Going multi-device / cloud: `tl relay` + `tl sync`

This is the optional, **self-hostable** layer that makes the journal reachable beyond one
machine — cloud agents report in, and you read your journal from another device — **without
weakening the privacy moat.**

**Run the relay** (a small multi-account HTTP service: `POST /report` for agents,
`POST /sync` for device sync, `GET /events` to read back; bearer-token auth per account):

```powershell
python -m throughlog.cli relay                      # http://127.0.0.1:8788
python -m throughlog.cli relay --host 0.0.0.0 --port 8788 --store data/relay
```

Accounts come from `relay.tokens` in `config.json` — a `{ "secret-token": "account" }`
map. A request with `Bearer <secret-token>` is scoped to that account; an unknown token
gets `401`; `/healthz` needs no auth. Point a cloud agent's `TL_ENDPOINT` / `TL_TOKEN`
(or the Claude Code hook) at this relay and its reports land in that account's store.

**Sync a device** to/from the relay:

```powershell
python -m throughlog.cli sync push                  # send this machine's gated events up
python -m throughlog.cli sync push --date 20260622  # just one day
python -m throughlog.cli sync pull                   # fetch the account's events back down
python -m throughlog.cli sync pull --since 2026-06-20T00:00:00+00:00
```

Endpoint + token resolve from `--endpoint` / `--token`, else `$TL_RELAY_ENDPOINT` /
`$TL_TOKEN`, else `sync.endpoint` / `sync.token` in `config.json`.

**The cloud privacy stance (the guardrail):**

- **Gated-only egress.** `tl sync push` will only send events that already carry a
  privacy stamp (i.e. already passed the gate); anything ungated is **blocked**, not sent.
  Every payload is **re-scrubbed again** on the way out. **Raw content never syncs.**
- **Self-hostable.** Run your own relay so your data never leaves infrastructure you
  control. There is no mandatory hosted service.
- **Opt-in + scoped.** Sync and every account integration are off until you configure them,
  and limited to the projects/repos you've allow-listed.
- The relay also refuses to silently drop anything: a malformed report is stored as a
  `rejected` audit stub (`422`), mirroring the local gate's behavior.

---

## 11. Run it hands-free (autostart + nightly schedule)

So you never have to remember. Each registers an independent OS scheduled job — **Windows
Task Scheduler**, **macOS launchd**, or **Linux cron**, chosen automatically for your
platform (no admin rights needed on Windows). They generate the task definition internally,
so there's no fragile command-line quoting.

```powershell
# Start capturing automatically every time you log in:
python -m throughlog.cli autostart enable           # headless capture at logon
python -m throughlog.cli autostart enable --tray     # ...or bring up the tray icon at logon
python -m throughlog.cli autostart disable           # stop auto-starting
python -m throughlog.cli autostart status            # is the task registered?

# Write the journal automatically every night at 22:30:
python -m throughlog.cli schedule enable --time 22:30
python -m throughlog.cli schedule enable --time 07:00 --no-llm   # e.g. a deterministic morning run
python -m throughlog.cli schedule disable
python -m throughlog.cli schedule status
```

A typical hands-free setup: `autostart enable --tray` (records whenever you're logged in,
with a visible icon) + `schedule enable --time 22:30` (journal appear overnight). In the
morning, open `journal/` or run `tl serve`.

> The scheduled tasks launch the project's `venv` Python if it exists (so the capture
> libraries are present), otherwise the Python that registered them. Keep the venv around
> if you rely on autostart. (Windows is the most battle-tested path; macOS launchd and
> Linux cron are generated the same way but less heavily exercised.)

---

## 12. Command reference

| Command | What it does |
|---|---|
| `tl demo` | Zero-config guided tour: generate a synthetic demo day (no key) and open the dashboard on it. Flags: `--no-serve`, `--no-browser`, `--port`. |
| `tl init [root]` | Auto-discover git repos under `root` (default `~/projects`) into `projects.json`. Merge-only. Flags: `--dry-run`, `--depth N` (default 4), `--out PATH`. |
| `tl capture` | Record live in the console (`Ctrl+C` to stop). Flags: `--no-clipboard`, `--no-agents`, `--no-hotkeys`, `--heartbeat SEC`. |
| `tl tray` | Record live behind a tray icon. Same source flags. |
| `tl synthesize` | Build journal from captured events. Sources: `--date YYYYMMDD`, `--events FILE`, `--replay` (default: all of `data/events/`). `--no-llm` = deterministic only. `--journal-dir DIR` to override output. |
| `tl serve` | Local read-only dashboard over the journal. Flags: `--host`, `--port` (default 8799), `--journal-dir`, `--data`, `--no-browser`. |
| `tl report` | Push the daily/weekly standup. Target: `--stdout` (default), `--slack` (`--slack-webhook URL`), `--github OWNER/REPO#N` (`--github-token TOKEN`). Scope: `--date YYYY-MM-DD`, `--weekly`. |
| `tl pull --github` | Pull tracked GitHub repos' commits/PRs/CI into the thin-log. Flags: `--token`, `--watch`, `--interval SEC` (default 300). Only `projects.json` `git_remotes` are pulled. |
| `tl relay` | Run the self-hostable multi-account relay (agent `/report` + `/sync` + `/events`). Flags: `--host`, `--port` (default 8788), `--store DIR`. Accounts from `relay.tokens`. |
| `tl sync {push\|pull}` | Push gated events up / pull the account's events down. Flags: `--endpoint`, `--token`, `--date YYYYMMDD` (push), `--since ISO` (pull). |
| `tl autostart {enable\|disable\|status}` | Capture-on-logon task (Win Task Scheduler / launchd / cron). `--tray`; `--no-clipboard` / `--no-agents` pass through. |
| `tl schedule {enable\|disable\|status}` | Nightly synthesis task. `--time HH:MM` (default 22:30); `--no-llm`. |
| `tl hook {enable\|disable\|status} {claude-code\|cursor}` | Install/remove the drop-in AI-agent hook in the tool's settings file. `--scope {user\|project}` (default `user`). |

(`tl ...` works after `pip install -e .`; otherwise use `python -m throughlog.cli ...`. Run live
capture/tray with `.\venv\Scripts\python.exe` so the capture libraries are present.)

---

## 13. Where everything lives

```
throughlog/
├── throughlog/                     # the program (the v2 package)
│   ├── capture.py           #   live-capture supervisor
│   ├── tray.py              #   tray UI
│   ├── deploy.py            #   autostart + nightly scheduling (Task Sched / launchd / cron)
│   ├── cli.py               #   the `tl` command (all subcommands)
│   ├── onboard.py           #   `tl init` — repo discovery → projects.json
│   ├── server.py            #   `tl serve` — the local dashboard
│   ├── report.py            #   `tl report` — Slack / GitHub / stdout push
│   ├── relay.py             #   `tl relay` — self-hostable multi-account cloud relay
│   ├── sync.py              #   `tl sync` — gated device sync (the egress boundary)
│   ├── agent_sdk.py         #   the agent report builder + AgentReporter client
│   ├── hooks.py             #   `tl hook` — installer for the Claude Code / Cursor hooks
│   ├── privacy/             #   the gate: allowlist, redactors, egress check
│   ├── sources/             #   focus, fs/git, clipboard, agent ingest, proc monitor, github_pull
│   ├── categorize.py        #   Phase 1 (rules + LLM fallback)
│   └── synthesize.py        #   Phase 2 (overview + exec summary)
├── integrations/            # drop-in agent hooks (Claude Code + Cursor, JS client, contract)
│   ├── _common.py           #   shared hook plumbing (stdin parse, send, drop-folder default)
│   └── demo.py              #   `tl demo` — the built-in synthetic demo day
├── projects.json            # your project registry / allowlist (gitignored; create it, or `tl init`)
├── projects.example.json    # template for the above (the demo's two sample projects)
├── config.json              # your settings + keys (gitignored; you create it)
├── config.example.json      # template for the above
├── instructions.md          # this file
├── sim/scenarios/           # declarative end-to-end cases replayed through the real bus
├── data/                    # captured output (gitignored — nothing here is committed)
│   ├── events/YYYYMMDD.jsonl #   the gated, thin event log
│   ├── demo/                #   `tl demo` regenerates its store + journal here
│   ├── agent_drop/          #   drop agent reports here
│   ├── relay/               #   the relay's per-account store (when you run `tl relay`)
│   └── daemon_status.json   #   capture heartbeat (alive/paused/counts)
├── journal/                 # the journal + summaries you read
└── venv/                    # virtual env with the capture libraries
```

`data/`, `config.json`, and `projects.json` are gitignored — your captured activity, your
keys, and your project list never leave the machine via git.

---

## 14. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: psutil` (or watchdog/pyperclip/keyboard/pystray) when capturing | You're on the system Python. Use `.\venv\Scripts\python.exe -m throughlog.cli capture`, or `pip install -e .[capture]` into your active environment. |
| "no allowlist roots — fs watching is off" | No project in `projects.json` has a `signals.paths` entry (and `privacy.allowlist_extra` is empty). Add at least one folder, or run `tl init`. |
| "no API key resolved — running deterministic-only" | Expected without a key. Set `OPENROUTER_API_KEY` or `llm.api_key`, or just keep using `--no-llm`. The archive is still written. |
| Diaries have an `archive.md` but no `overview.md` prose | The LLM was off/unreachable. Events are safe; re-run `synthesize` once the key/connection is sorted. |
| `tl serve` shows nothing | You haven't synthesized yet. Run `tl demo` (builds a demo day and serves it), or `synthesize` then `serve`. |
| `tl report --slack/--github` says "no webhook/token" | Provide it via the flag, the env var (`$TL_SLACK_WEBHOOK` / `$GITHUB_TOKEN`), or the `report.*` section of `config.json`. |
| `tl pull` says "nothing to pull" | No tracked repo has a GitHub remote in `signals.git_remotes`. Add the remote (or run `tl init`, which fills it in). |
| `tl sync push` reports `blocked=N, sent=0` | Those events have no privacy stamp (weren't gated). Only gate-passed events sync — by design. Re-capture through the bus; never hand-edit events into the log. |
| Captured nothing during a test in a temp folder | Folders under `AppData\…\Temp` are treated as noise and dropped. Test inside a real allow-listed project folder. |
| Is it still recording? | Check `data/daemon_status.json` — a fresh `heartbeat` timestamp and `alive: true` mean it's running. `tl serve` shows the same as a badge. |

---

## 15. Verify the install is healthy

```powershell
python -m unittest discover -s tests -p "test_*.py"   # ~240 deterministic tests, expect OK
python -m sim.simulator --all                          # 17 end-to-end scenarios through the real bus
python -m throughlog.cli demo --no-serve                      # builds the demo day's journal, no key
python -m throughlog.privacy.gate --audit data/demo/20260624.jsonl   # prove the store would leak nothing (RESULT: CLEAN)
```

If you have an API key and want to confirm the LLM path end-to-end:

```powershell
python -m throughlog.llm.client  --smoke   # connectivity
python -m throughlog.categorize  --smoke   # Phase 1 on an ambiguous event
python -m throughlog.synthesize  --smoke   # Phase 2 archive + overview + exec summary
```

---

## 16. The mental model, in three sentences

You tell it which folders and repos belong to which projects (`projects.json`, or let
`tl init` draft it); it watches only those, scrubbing anything sensitive before it ever
hits disk — and again before anything leaves the machine. It records all day
(`capture`/`tray`) and can also fold in your AI agents and pulled cloud work, keeping thin
metadata about what happened — never file contents, never raw clipboard, never secrets.
Then it writes that up into per-project journal and an executive summary (`synthesize`)
that you read in a browser (`serve`), push to your team (`report`), and optionally sync
across devices through a self-hostable relay (`relay`/`sync`) — using AI only to sort the
ambiguous cases and phrase the prose, and even with the AI switched off you still get a
complete factual archive.
