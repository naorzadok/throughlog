# ThroughLog agent integrations — the journal + audit trail for your AI agents

Any agent (local or in the cloud) can write itself into your work journal with a
single HTTP POST. This is the wedge: **add one hook and your AI tools record what
they did**, trust-classified and attributed to the right project, in the same
private journal as your own work.

Everything here is a thin wrapper over the one documented contract below — the
`POST /report` endpoint served by `tl capture` (`sources/agent_ingest.serve_http`,
default `http://127.0.0.1:8787/report`) or by a cloud relay (same contract).

## The report contract

POST a JSON object (schema v2). Minimum viable report:

```json
{
  "schema_version": 2,
  "type": "AGENT_REPORT",
  "source": { "kind": "agent", "adapter": "claude-code", "identity": "agent:claude-code" },
  "ts_wall": "2026-06-24T15:04:05+03:00",
  "payload": { "summary": "refactored the parser; tests green" }
}
```

- **`payload.summary`** is what lands in the journal.
- **`payload.repo`** (a repo path or git remote like `github.com/me/app`) and
  **`payload.files`** drive project attribution — the same deterministic signal
  stack as every other source. `payload.project_hint` adds a keyword signal.
- The server **decides trust** (you can't self-certify): a malformed report is
  rejected as an audit stub, a well-formed but suspicious one is demoted to
  `low_trust`, and `recv_ts` is stamped at intake so a late/out-of-order report
  still lands at its real time on the timeline. Reports are **never dropped**.

Responses: `202` accepted, `422` rejected (malformed), `400` non-JSON.

## Endpoint + auth (environment)

| var           | default                          | meaning                              |
| ------------- | -------------------------------- | ------------------------------------ |
| `TL_ENDPOINT` | `http://127.0.0.1:8787/report`   | local endpoint or a cloud relay URL  |
| `TL_TOKEN`    | —                                | bearer token (relay / multi-user)    |
| `TL_DROP_DIR` | —                                | fallback folder if endpoint is down  |

## Clients

- **Python** — `throughlog.agent_sdk`:
  ```python
  from throughlog.agent_sdk import AgentReporter
  AgentReporter(identity="agent:ci", token="...").report(
      "deployed v1.2.3", repo="github.com/me/app", status="success")
  ```
  or as a CLI (used by hooks): `python -m throughlog.agent_sdk --summary "did X" --repo ...`.
- **JS / Node 18+** — [`js/tl-report.mjs`](js/tl-report.mjs) (`fetch`-based, same shape).
- **Anything else** — just POST the JSON above (curl, n8n HTTP node, a webhook).

## Drop-in hooks

The fastest way to install either hook is the built-in installer — it safely
merges into whatever's already in the settings file (never clobbers unrelated
hooks) and is idempotent (re-running replaces a stale path instead of
duplicating):

```bash
python -m throughlog.cli hook enable claude-code   # -> ~/.claude/settings.json
python -m throughlog.cli hook enable cursor        # -> ~/.cursor/hooks.json
python -m throughlog.cli hook status  claude-code|cursor
python -m throughlog.cli hook disable claude-code|cursor
# --scope project installs into ./.claude/ or ./.cursor/ instead of the user config
```

- **Claude Code** — [`claude_code/tl_hook.py`](claude_code/tl_hook.py). Reads the
  hook payload on stdin and posts an `AGENT_REPORT`. It **never blocks Claude Code**
  (always exits 0). What `tl hook enable claude-code` writes to `settings.json`
  (or wire it in by hand):
  ```json
  {
    "hooks": {
      "PostToolUse": [
        { "matcher": "Edit|Write|MultiEdit",
          "hooks": [ { "type": "command",
            "command": "python /ABS/PATH/throughlog/integrations/claude_code/tl_hook.py" } ] }
      ],
      "Stop": [
        { "hooks": [ { "type": "command",
          "command": "python /ABS/PATH/throughlog/integrations/claude_code/tl_hook.py" } ] }
      ]
    }
  }
  ```
- **Cursor** — [`cursor/tl_hook.py`](cursor/tl_hook.py). Same contract, adapted to
  Cursor's [hooks](https://cursor.com/docs/hooks) payload (`afterFileEdit`,
  `stop`). What `tl hook enable cursor` writes to `hooks.json`:
  ```json
  {
    "version": 1,
    "hooks": {
      "afterFileEdit": [
        { "command": "python /ABS/PATH/throughlog/integrations/cursor/tl_hook.py" }
      ],
      "stop": [
        { "command": "python /ABS/PATH/throughlog/integrations/cursor/tl_hook.py" }
      ]
    }
  }
  ```
- **CI (GitHub Actions etc.)** — call the Python or JS client at the end of a job;
  point `TL_ENDPOINT` at your relay and pass `TL_TOKEN`. A bot/agent-authored run
  becomes a first-class `AGENT_REPORT` thread in the journal.

Both hooks fall back to the local drop folder (`data/agent_drop/` — the same one
`tl capture` already watches) when the endpoint is unreachable, so "add one hook"
is true even if capture isn't running the instant the hook fires.

Not covered yet: **Aider** has no hook/webhook mechanism, but its auto-commits
are already captured by the existing deterministic git-commit watcher; **Antigravity**
has a hooks system but is new enough that its payload shape isn't build-worthy yet.

## Verify it end-to-end

```bash
# 1) start capture (serves the /report endpoint)
python -m throughlog.cli capture

# 2) from anywhere, post a report
python -m throughlog.agent_sdk --summary "smoke from the SDK" --repo "$(pwd)"

# 3) synthesize + view — the agent report appears in the journal/timeline
python -m throughlog.cli synthesize
python -m throughlog.cli serve
```
