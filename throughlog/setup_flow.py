"""``tl setup`` — the guided, approval-gated onboarding flow.

``pip install -e .`` leaves ThroughLog *quiet*: no agent hooks, no registered
projects (so the privacy allowlist is empty and nothing is observed), no LLM key,
no capture-at-logon, no nightly synthesis. Each is a separate command a user has to
discover. This module is the single front door that walks them through turning the
whole thing on — **one step at a time, each gated by explicit approval** — by
*composing existing, already-guarded building blocks* rather than reinventing any
of them:

  * agent hooks   -> :func:`throughlog.hooks.install_hook`
  * project scan  -> :func:`throughlog.onboard.init_registry`  (widens the allowlist)
  * LLM key       -> :func:`throughlog.appconfig.update_llm`    (write-only)
  * nightly       -> :func:`throughlog.appconfig.update_schedule` (no-admin in-app)
  * capture@logon -> :func:`throughlog.deploy.enable_autostart`
  * start the app -> ``tl up``

The design goal is that an AI agent asked to *"install ThroughLog"* can surface all
of this to the user: :func:`detect_state` + :func:`plan_steps` are pure and
side-effect-free, so ``tl setup --plan`` prints the current state and the exact
commands without touching anything — the call an agent makes to ask the user what
they want. The apply half lives in the thin driver ``cli.cmd_setup``.

Nothing here touches the capture->synthesize pipeline or the privacy gate; it only
turns on features the user already had, behind approval.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from throughlog import config as cfgmod
from throughlog import deploy, hooks


# --------------------------------------------------------------------------- #
# State detection (pure aside from the two injectable OS probes)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SetupState:
    """A read-only snapshot of what is / isn't set up on this machine."""
    platform: str
    agent_tools: tuple[str, ...]              # present among hooks.TOOLS (~/.claude, ~/.cursor)
    hooks_installed: dict[str, bool]          # tool -> our hook already wired in?
    project_count: int                        # real projects.json entries (0 = example fallback)
    key_set: bool
    nightly_at: str | None
    autostart_on: bool
    capture_live: bool


def _tool_settings_path(home: Path, tool: str) -> Path:
    """The host tool's settings file under ``home`` — mirrors
    :func:`throughlog.hooks.settings_path` but with an injectable home so tests
    never read the real ``~``."""
    return home / hooks._SETTINGS_REL[tool]


def detect_state(*, home: Path | None = None, cfg: dict[str, Any] | None = None,
                 base_dir: Path | None = None, data: Path | None = None,
                 autostart_probe: Callable[[], bool] | None = None,
                 capture_probe: Callable[[], bool] | None = None) -> SetupState:
    """Compose the reused detectors into one snapshot. Every OS-touching input is
    injectable so the whole thing runs offline in a test with a fake home dir."""
    home = home or Path.home()
    base_dir = base_dir or cfgmod.BASE_DIR
    if cfg is None:
        cfg = cfgmod.load_config() if cfgmod.CONFIG_PATH.exists() else {}

    tools_present: list[str] = []
    installed: dict[str, bool] = {}
    for tool in hooks.TOOLS:
        path = _tool_settings_path(home, tool)
        if path.parent.exists():
            tools_present.append(tool)
            installed[tool] = hooks._INSTALLED[tool](hooks._read_json(path))

    projects_path = base_dir / "projects.json"
    project_count = len(cfgmod.load_projects(projects_path)) if projects_path.exists() else 0

    from throughlog import appconfig
    if autostart_probe is None:
        autostart_probe = lambda: deploy.task_status(deploy.CAPTURE_TASK)[0]
    if capture_probe is None:
        data_dir = data or cfgmod.data_dir(cfg)
        def capture_probe() -> bool:  # lazy: importing server pulls http.server
            from throughlog.server import capture_is_live
            return capture_is_live(data_dir)

    return SetupState(
        platform=deploy._platform(),
        agent_tools=tuple(tools_present),
        hooks_installed=installed,
        project_count=project_count,
        key_set=appconfig.key_is_set(cfg),
        nightly_at=appconfig.nightly_time(cfg),
        autostart_on=_safe(autostart_probe),
        capture_live=_safe(capture_probe),
    )


def _safe(probe: Callable[[], bool]) -> bool:
    """A status probe must never take the whole flow down."""
    try:
        return bool(probe())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Recommended steps (pure — the heart of ``--plan`` and the apply loop)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Step:
    """One thing the user can turn on: a stable ``key``, a human ``label``, the
    ``why`` (shown so the user can give informed consent — e.g. the LLM-key
    explanation), the concrete ``command`` it maps to, whether it ``applicable`` on
    this machine, and whether it is already ``done``."""
    key: str
    label: str
    why: str
    command: list[str] = field(default_factory=list)
    applicable: bool = True
    done: bool = False


_KEY_WHY = ("Enables the narrative journal — the overview, detailed entries, and the "
            "executive summary. Without a key ThroughLog still runs fully "
            "deterministically (archive + timeline); it just skips the prose. "
            "Prefer to keep everything on-machine? Skip the key and run a local model "
            "instead (`tl local pull nemotron-3-nano-4b` + `tl local serve`, or point it "
            "at Ollama) — no key, nothing leaves the box. See Settings → Local model.")


def plan_steps(state: SetupState) -> list[Step]:
    """The ordered onboarding steps for ``state``. Pure: no I/O, no prompts — this is
    exactly what ``tl setup --plan`` prints and what ``cmd_setup`` walks."""
    steps: list[Step] = []

    # 1. Agent hooks — one step per detected tool (Claude Code / Cursor).
    if state.agent_tools:
        for tool in state.agent_tools:
            steps.append(Step(
                key=f"hook:{tool}",
                label=f"Install the {tool} hook — record what {tool} does in your journal",
                why="Adds one hook so the agent writes an AGENT_REPORT after each edit / "
                    "at the end of a session, attributed to the right project.",
                command=["tl", "hook", "enable", tool],
                done=state.hooks_installed.get(tool, False),
            ))
    else:
        steps.append(Step(
            key="hooks",
            label="Install an AI-agent hook",
            why="No supported agent tool detected (looked for ~/.claude, ~/.cursor).",
            command=["tl", "hook", "enable", "claude-code"],
            applicable=False,
        ))

    # 2. Project discovery — this is what makes any folder observable (allowlist).
    steps.append(Step(
        key="projects",
        label="Discover your projects — scan a folder for git repos",
        why="Registers repos in projects.json. Their paths drive BOTH categorization "
            "AND the privacy allowlist, so this is what makes a directory observable — "
            "you confirm the folder first, nothing outside it is ever scanned.",
        command=["tl", "init", "<folder>"],
        done=state.project_count > 0,
    ))

    # 3. LLM key — with the explanation the user explicitly wanted.
    steps.append(Step(
        key="llm-key",
        label="Set an LLM API key",
        why=_KEY_WHY,
        command=["tl", "setup", "--key"],
        done=state.key_set,
    ))

    # 4. Nightly synthesis — the no-admin, in-app path (runs inside `tl up`).
    steps.append(Step(
        key="nightly",
        label="Synthesize your journal every night (default 22:30)",
        why="Rebuilds the journal automatically each night while the app is running. "
            "No admin — it's an in-process timer, not a scheduled task.",
        command=["tl", "setup", "--nightly"],
        done=bool(state.nightly_at),
    ))

    # 5. Capture at logon — record the workday automatically.
    steps.append(Step(
        key="autostart",
        label="Start capturing at logon",
        why="Records your workday automatically from login — no admin (Startup folder "
            "on Windows; launchd / cron elsewhere).",
        command=["tl", "autostart", "enable"],
        done=state.autostart_on,
    ))

    # 6. Start now.
    steps.append(Step(
        key="start",
        label="Start ThroughLog now (open the dashboard)",
        why="Launches live capture + the local control panel at http://127.0.0.1:8799.",
        command=["tl", "up"],
        done=state.capture_live,
    ))
    return steps


# --------------------------------------------------------------------------- #
# TTY-aware prompt helpers (never block a non-interactive / agent run)
# --------------------------------------------------------------------------- #
def _is_interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def confirm(question: str, *, default: bool = True, assume_yes: bool = False,
            interactive: bool | None = None, reader: Callable[[str], str] = input) -> bool:
    """Ask a yes/no question, safely. Because this gates state changes, "can't get an
    answer" must mean **no**, never "apply the recommended default":

      * ``assume_yes`` -> ``True`` (the caller explicitly opted in to all defaults).
      * not interactive (no TTY) -> ``False`` — an agent / piped run declines rather
        than silently enabling things without consent.
      * interactive: empty line (Enter) accepts ``default``; EOF / Ctrl-C -> ``False``.
    """
    if assume_yes:
        return True
    if interactive is None:
        interactive = _is_interactive()
    if not interactive:
        return False
    suffix = "Y/n" if default else "y/N"
    try:
        ans = reader(f"{question} [{suffix}] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not ans:
        return default
    return ans in ("y", "yes")


def ask_text(question: str, *, default: str = "", interactive: bool | None = None,
             secret: bool = False, reader: Callable[[str], str] | None = None) -> str:
    """Prompt for a line of text (a scan root, an API key). Returns ``""`` (which every
    caller treats as "skip") when it can't ask — not interactive, or EOF/Ctrl-C — so a
    scan root or key is never fabricated without the user actually typing it. An empty
    line at an interactive prompt accepts ``default``. ``secret=True`` reads without
    echo (the API key)."""
    if interactive is None:
        interactive = _is_interactive()
    if not interactive:
        return ""
    if reader is None:
        if secret:
            import getpass
            reader = getpass.getpass
        else:
            reader = input
    try:
        ans = reader(f"{question} ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    return ans or default
