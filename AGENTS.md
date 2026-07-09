# AGENTS.md — for AI agents installing or setting up ThroughLog

If you are an AI coding agent (Claude Code, Cursor, or any other) and the user
asked you to **install or set up ThroughLog**, don't stop at `pip install`. A bare
install leaves the tool *quiet*: no agent hooks, no registered projects (so nothing
is observed — the privacy allowlist is empty), no LLM key, no capture-at-logon, no
nightly synthesis. Your job is to walk the user through turning it on — **at full
power, but one step at a time, with their explicit approval for each step.**

## The one command

```bash
pip install -e .            # (or ".[capture]" for live OS capture)
tl setup --plan             # read-only: prints current state + recommended steps
```

`tl setup --plan` changes **nothing**. Run it first, show the user the recommended
steps, and let them decide. Then either:

- run `tl setup` and let the **user** answer the prompts interactively (best when
  they have a terminal), or
- apply only the steps the user approved with flags (best when you're driving
  non-interactively — `tl setup` with no answer available declines every step
  rather than guessing):

```bash
tl hook enable claude-code      # or: cursor   (only for tools they use)
tl setup --init <folder>        # scan THIS folder for their git repos
tl setup --nightly              # nightly journal synthesis at 22:30
tl autostart enable             # start capturing at logon (no admin)
tl up                           # start capture + open the dashboard
```

## Ask the user about each of these (in order)

1. **Agent hooks** — "Install the ThroughLog hook for <the AI tools you use>?" So
   the agent writes what it did into the journal. `tl hook enable claude-code|cursor`.
   `tl setup` auto-detects which tools are present (`~/.claude`, `~/.cursor`).
2. **Project discovery** — "Which folder holds your git repos?" then
   `tl setup --init <that folder>`. ⚠️ **Privacy:** a project's paths drive the
   privacy allowlist — registering a folder is what makes it *observable*. Only ever
   scan a folder the user explicitly named; never guess, never scan their whole home
   directory.
3. **LLM API key** — **explain why it matters:** the key turns on the *narrative*
   journal (the overview, detailed entries, and the executive summary). Without a
   key ThroughLog still records and builds the deterministic archive + timeline —
   it just skips the written prose. A free key works: https://openrouter.ai/keys
   (default model `nvidia/nemotron-3-super-120b-a12b:free`). Let the user paste it into
   `tl setup` (stored write-only in `config.json`, never echoed) or the dashboard
   Settings page. **Never ask the user to paste a key into the chat; never log it.**
4. **Nightly synthesis** — "Rebuild your journal automatically each night?"
   `tl setup --nightly` (default 22:30; runs in-process while `tl up` is open — no
   admin, no scheduled task).
5. **Capture at logon** — "Start recording automatically when you log in?"
   `tl autostart enable` (no admin — Startup folder on Windows, launchd/cron else).
6. **Start it** — `tl up` starts capture and opens the local dashboard at
   `http://127.0.0.1:8799`.

## Guardrails

- **Consent per step.** Nothing above should happen without the user saying yes.
  `tl setup` enforces this: with no answer available it declines rather than
  applying a default. `--yes` opts into the safe defaults but still won't paste a
  key or scan a folder you didn't pass explicitly.
- **Privacy is the user's.** Widening what's observed (adding projects,
  `privacy.allowlist_extra`) is always the user's call. Surface the exact folder
  before it becomes observable.
- **Everything is local.** The dashboard binds `127.0.0.1`; nothing leaves the
  machine unless it passes the privacy gate. Don't add remote/relay/sync config
  unless the user asks.

For the full picture see [`README.md`](README.md), the project guide in
[`CLAUDE.md`](CLAUDE.md), and the agent report contract in
[`integrations/README.md`](integrations/README.md).
