# ThroughLog agent integrations — the diary + audit trail for your AI agents

Any agent (local or in the cloud) can write itself into your work journal with a
single HTTP POST. This is the wedge: **add one hook and your AI tools record what
they did**, trust-classified and attributed to the right project, in the same
private diary as your own work.

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

- **`payload.summary`** is what lands in the diary.
- **`payload.repo`** (a repo path or git remote like `github.com/me/app`) and
  **`payload.files`** drive project attribution — the same deterministic signal
  stack as every other source. `payload.project_hint` adds a keyword signal.
- The server **decides trust** (you can't self-certify): a malformed report is
  rejected as an audit stub, a well-formed but suspicious one is demoted to
  `low_trust`, and `recv_ts` is stamped at intake so a late/out-of-order report
  still lands at its real time on the timeline. Reports are **never dropped**.

Responses: `202` accepted, `422` rejected (malformed), `400` non-JSON.

## Endpoint + auth (environment)

| var            | default                          | meaning                              |
| -------------- | -------------------------------- | ------------------------------------ |
| `SAL_ENDPOINT` | `http://127.0.0.1:8787/report`   | local endpoint or a cloud relay URL  |
| `SAL_TOKEN`    | —                                | bearer token (relay / multi-user)    |
| `SAL_DROP_DIR` | —                                | fallback folder if endpoint is down  |

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

- **Claude Code** — [`claude_code/tl_hook.py`](claude_code/tl_hook.py). Reads the
  hook payload on stdin and posts an `AGENT_REPORT`. It **never blocks Claude Code**
  (always exits 0). Wire it into `settings.json`:
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
- **CI (GitHub Actions etc.)** — call the Python or JS client at the end of a job;
  point `SAL_ENDPOINT` at your relay and pass `SAL_TOKEN`. A bot/agent-authored run
  becomes a first-class `AGENT_REPORT` thread in the diary.

## Verify it end-to-end

```bash
# 1) start capture (serves the /report endpoint)
python -m throughlog.cli capture

# 2) from anywhere, post a report
python -m throughlog.agent_sdk --summary "smoke from the SDK" --repo "$(pwd)"

# 3) synthesize + view — the agent report appears in the diary/timeline
python -m throughlog.cli synthesize
python -m throughlog.cli serve
```
