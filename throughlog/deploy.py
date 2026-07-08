"""Deployment helpers — autostart capture at login, and schedule nightly synthesis.

Cross-platform. The same two logical tasks are registered with whatever the host
OS provides, all driven from pure, unit-tested definition builders:

  * ``SmartActivityLogger-Capture``          — runs ``throughlog.cli capture`` (or the tray)
                                                at every interactive login.
  * ``SmartActivityLogger-NightlySynthesis`` — runs ``throughlog.cli synthesize`` once a day.

  ===========  ============================  =======================================
  Platform     Mechanism                     Where it lives
  ===========  ============================  =======================================
  Windows      Task Scheduler (``schtasks``)  a generated Task 1.2 XML
  macOS        launchd (``launchctl``)        ~/Library/LaunchAgents/<label>.plist
  Linux        cron (``crontab``)             a marker-tagged line in the user crontab
  ===========  ============================  =======================================

The builders (``build_task_xml`` / ``launchd_plist`` / ``cron_line`` and the
``*_task_xml`` / ``*_plist`` / ``cron_*_line`` helpers) are pure and deterministic
(no OS calls), so they are unit tested directly; the ``enable_*`` / ``disable_*``
functions dispatch on platform and shell out to the native scheduler.

    python -m throughlog.cli autostart enable          # capture at login (headless)
    python -m throughlog.cli autostart enable --tray   # ...via the tray UI instead
    python -m throughlog.cli autostart disable
    python -m throughlog.cli schedule  enable --time 22:30
    python -m throughlog.cli schedule  disable

This module touches no LLM and no captured data — it only wires the two entry
points into the OS scheduler.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from throughlog import config as cfgmod

CAPTURE_TASK = "SmartActivityLogger-Capture"
SYNTHESIS_TASK = "SmartActivityLogger-NightlySynthesis"

# Friendly double-clickable launcher (Windows shortcut) for `tl up`.
SHORTCUT_NAME = "ThroughLog.lnk"

# At-logon launcher (Windows Startup-folder shortcut). We use the Startup folder —
# NOT Task Scheduler — for "start capturing at logon" because `schtasks /Create`
# requires elevation on a default Windows install (it writes under the protected
# root task folder), so a normal user hits "ERROR: Access is denied." Dropping a
# .lnk in the per-user Startup folder needs no admin, runs at every interactive
# logon, and (via pythonw) shows no console. The Task Scheduler XML builders below
# stay for the `tl schedule`/admin path.
AUTOSTART_SHORTCUT_NAME = "ThroughLog (autostart).lnk"

# launchd reverse-DNS labels (macOS).
CAPTURE_LABEL = "com.smartactivitylogger.capture"
SYNTHESIS_LABEL = "com.smartactivitylogger.synthesis"


# --------------------------------------------------------------------------- #
# Platform + where to point the scheduled task
# --------------------------------------------------------------------------- #
def _platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def python_exe() -> str:
    """The interpreter a scheduled task should launch. Prefer the project venv
    (it has the capture extras); fall back to the current interpreter."""
    win = cfgmod.BASE_DIR / "venv" / "Scripts" / "python.exe"
    posix = cfgmod.BASE_DIR / "venv" / "bin" / "python"
    if win.exists():
        return str(win)
    if posix.exists():
        return str(posix)
    return sys.executable


def repo_dir() -> str:
    return str(cfgmod.BASE_DIR)


def pythonw_exe() -> str:
    """The windowless interpreter for a GUI shortcut (``pythonw.exe`` beside the
    chosen ``python.exe``), so double-clicking the launcher shows no console."""
    base = Path(python_exe())
    cand = base.with_name("pythonw.exe")
    return str(cand) if cand.exists() else str(base)


# =========================================================================== #
# Desktop / Start-menu shortcut for `tl up` (Windows)  — pure builder + driver
# =========================================================================== #
def _ps_quote(s: str) -> str:
    """Escape a value for a single-quoted PowerShell string literal."""
    return str(s).replace("'", "''")


def shortcut_ps1(*, target: str, arguments: str, workdir: str, lnk_path: str,
                 description: str) -> str:
    """The PowerShell one-liner that creates a ``.lnk`` via ``WScript.Shell`` (pure /
    testable). Every interpolated value is single-quote escaped."""
    return (
        "$w = New-Object -ComObject WScript.Shell; "
        f"$s = $w.CreateShortcut('{_ps_quote(lnk_path)}'); "
        f"$s.TargetPath = '{_ps_quote(target)}'; "
        f"$s.Arguments = '{_ps_quote(arguments)}'; "
        f"$s.WorkingDirectory = '{_ps_quote(workdir)}'; "
        f"$s.Description = '{_ps_quote(description)}'; "
        "$s.Save()"
    )


def _shortcut_locations(*, desktop: bool, start_menu: bool) -> list[Path]:
    out: list[Path] = []
    if desktop:
        out.append(Path.home() / "Desktop" / SHORTCUT_NAME)
    if start_menu:
        appdata = os.environ.get("APPDATA")
        if appdata:
            out.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
                       / "Programs" / SHORTCUT_NAME)
    return out


def install_shortcut(*, desktop: bool = True, start_menu: bool = True
                     ) -> tuple[bool, str]:
    """Create a double-clickable shortcut that launches ``tl up`` with no console.
    Windows only (the project's primary target); other platforms get a clear note."""
    if _platform() != "windows":
        return False, ("shortcut creation is implemented for Windows only — on "
                       "macOS/Linux run `tl up` (or add it to your autostart).")
    target, workdir = pythonw_exe(), repo_dir()
    made: list[str] = []
    for lnk in _shortcut_locations(desktop=desktop, start_menu=start_menu):
        try:
            lnk.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"could not create {lnk.parent}: {exc}"
        ps = shortcut_ps1(target=target, arguments="-m throughlog.cli up", workdir=workdir,
                          lnk_path=str(lnk),
                          description="ThroughLog — capture + dashboard")
        ok, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
        if not ok:
            return False, out or f"failed to create {lnk}"
        made.append(str(lnk))
    return True, "created shortcut: " + "; ".join(made)


def remove_shortcut(*, desktop: bool = True, start_menu: bool = True
                    ) -> tuple[bool, str]:
    removed: list[str] = []
    for lnk in _shortcut_locations(desktop=desktop, start_menu=start_menu):
        try:
            if lnk.exists():
                lnk.unlink()
                removed.append(str(lnk))
        except OSError:
            pass
    if removed:
        return True, "removed shortcut: " + "; ".join(removed)
    return True, "no shortcut to remove."


def _capture_argv(*, tray: bool = False, no_clipboard: bool = False,
                  no_agents: bool = False) -> list[str]:
    """The ``-m throughlog.cli ...`` argument vector for the capture task (no interpreter)."""
    argv = ["-m", "throughlog.cli", "tray" if tray else "capture"]
    if no_clipboard:
        argv.append("--no-clipboard")
    if no_agents:
        argv.append("--no-agents")
    return argv


def _synthesis_argv(*, no_llm: bool = False) -> list[str]:
    argv = ["-m", "throughlog.cli", "synthesize"]
    if no_llm:
        argv.append("--no-llm")
    return argv


def autostart_argv(*, tray: bool = False, no_clipboard: bool = False,
                   no_agents: bool = False) -> list[str]:
    """The ``-m throughlog.cli ...`` argv the at-logon launcher runs (pure / testable).

    Default: the full app, headless — ``up --no-browser`` captures AND serves the
    dashboard with no browser popup and no console, so one logon launch gives the
    whole product. ``tray=True`` runs the tray front-end instead (visible icon +
    Quit). Either way capture is the same engine; the launcher just picks the host."""
    if tray:
        return _capture_argv(tray=True, no_clipboard=no_clipboard, no_agents=no_agents)
    argv = ["-m", "throughlog.cli", "up", "--no-browser"]
    if no_clipboard:
        argv.append("--no-clipboard")
    if no_agents:
        argv.append("--no-agents")
    return argv


# --------------------------------------------------------------------------- #
# Windows — at-logon capture via the per-user Startup folder (no admin needed)
# --------------------------------------------------------------------------- #
def _win_startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else (Path.home() / "AppData" / "Roaming")
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def autostart_shortcut_path() -> Path:
    """Where the at-logon launcher lives (Windows)."""
    return _win_startup_dir() / AUTOSTART_SHORTCUT_NAME


def _win_autostart_enable(*, tray: bool, no_clipboard: bool,
                          no_agents: bool) -> tuple[bool, str]:
    lnk = autostart_shortcut_path()
    try:
        lnk.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"could not access the Startup folder: {exc}"
    args = " ".join(autostart_argv(tray=tray, no_clipboard=no_clipboard,
                                   no_agents=no_agents))
    ps = shortcut_ps1(target=pythonw_exe(), arguments=args, workdir=repo_dir(),
                      lnk_path=str(lnk),
                      description="ThroughLog — start capturing at logon")
    ok, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    if not ok:
        return False, out or "could not create the Startup shortcut."
    kind = "tray" if tray else "headless app"
    return True, f"capture will start at logon ({kind}) — {lnk}"


def _win_autostart_disable() -> tuple[bool, str]:
    lnk = autostart_shortcut_path()
    try:
        if lnk.exists():
            lnk.unlink()
            return True, f"removed {lnk}"
    except OSError as exc:
        return False, f"could not remove {lnk}: {exc}"
    return True, "autostart was not enabled — nothing to remove."


def _win_autostart_status() -> tuple[bool, str]:
    lnk = autostart_shortcut_path()
    return (lnk.exists(),
            f"autostart shortcut: {lnk}" if lnk.exists()
            else f"no autostart shortcut ({lnk})")


# =========================================================================== #
# Windows — Task Scheduler XML (pure / testable)
# =========================================================================== #
def _logon_trigger() -> str:
    return "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>"


def _daily_trigger(time_hhmm: str, start_day: date) -> str:
    start = f"{start_day.isoformat()}T{time_hhmm}:00"
    return (f"<CalendarTrigger><StartBoundary>{start}</StartBoundary>"
            f"<Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval>"
            f"</ScheduleByDay></CalendarTrigger>")


def build_task_xml(*, command: str, arguments: str, workdir: str, description: str,
                   trigger_xml: str, execution_time_limit: str = "PT0S",
                   stop_on_batteries: bool = False) -> str:
    """Render a Task Scheduler 1.2 definition. ``execution_time_limit='PT0S'``
    means no time limit (right for the long-running capture task)."""
    batt = "true" if stop_on_batteries else "false"
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        f"  <RegistrationInfo><Description>{escape(description)}</Description>"
        "</RegistrationInfo>\n"
        f"  <Triggers>{trigger_xml}</Triggers>\n"
        '  <Principals><Principal id="Author">'
        "<LogonType>InteractiveToken</LogonType>"
        "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n"
        "  <Settings>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        f"<DisallowStartIfOnBatteries>{batt}</DisallowStartIfOnBatteries>"
        f"<StopIfGoingOnBatteries>{batt}</StopIfGoingOnBatteries>"
        "<StartWhenAvailable>true</StartWhenAvailable>"
        "<AllowHardTerminate>true</AllowHardTerminate>"
        f"<ExecutionTimeLimit>{execution_time_limit}</ExecutionTimeLimit>"
        "<Enabled>true</Enabled></Settings>\n"
        '  <Actions Context="Author"><Exec>'
        f"<Command>{escape(command)}</Command>"
        f"<Arguments>{escape(arguments)}</Arguments>"
        f"<WorkingDirectory>{escape(workdir)}</WorkingDirectory>"
        "</Exec></Actions>\n"
        "</Task>\n"
    )


def capture_task_xml(*, tray: bool = False, no_clipboard: bool = False,
                     no_agents: bool = False) -> str:
    args = " ".join(_capture_argv(tray=tray, no_clipboard=no_clipboard,
                                  no_agents=no_agents))
    return build_task_xml(
        command=python_exe(), arguments=args, workdir=repo_dir(),
        description="ThroughLog — live capture on logon.",
        trigger_xml=_logon_trigger(), execution_time_limit="PT0S")


def synthesis_task_xml(*, time_hhmm: str, no_llm: bool = False,
                       start_day: date | None = None) -> str:
    args = " ".join(_synthesis_argv(no_llm=no_llm))
    return build_task_xml(
        command=python_exe(), arguments=args, workdir=repo_dir(),
        description="ThroughLog — nightly synthesis of captured events.",
        trigger_xml=_daily_trigger(time_hhmm, start_day or date.today()),
        execution_time_limit="PT2H", stop_on_batteries=False)


# =========================================================================== #
# macOS — launchd plist (pure / testable)
# =========================================================================== #
def launchd_plist(*, label: str, program_args: list[str], workdir: str,
                  run_at_load: bool = False, keep_alive: bool = False,
                  calendar: tuple[int, int] | None = None,
                  stdout_path: str | None = None,
                  stderr_path: str | None = None) -> str:
    """Render a launchd user-agent plist. ``calendar=(hour, minute)`` adds a
    ``StartCalendarInterval`` (for the nightly job); ``keep_alive`` keeps the
    long-running capture agent up."""
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        f"  <key>Label</key><string>{escape(label)}</string>",
        "  <key>ProgramArguments</key>",
        "  <array>",
    ]
    body += [f"    <string>{escape(a)}</string>" for a in program_args]
    body += [
        "  </array>",
        f"  <key>WorkingDirectory</key><string>{escape(workdir)}</string>",
    ]
    if run_at_load:
        body.append("  <key>RunAtLoad</key><true/>")
    if keep_alive:
        body.append("  <key>KeepAlive</key><true/>")
    if calendar is not None:
        hour, minute = calendar
        body += [
            "  <key>StartCalendarInterval</key>",
            "  <dict>",
            f"    <key>Hour</key><integer>{int(hour)}</integer>",
            f"    <key>Minute</key><integer>{int(minute)}</integer>",
            "  </dict>",
        ]
    if stdout_path:
        body.append(f"  <key>StandardOutPath</key><string>{escape(stdout_path)}</string>")
    if stderr_path:
        body.append(f"  <key>StandardErrorPath</key><string>{escape(stderr_path)}</string>")
    body += ["</dict>", "</plist>", ""]
    return "\n".join(body)


def capture_plist(*, tray: bool = False, no_clipboard: bool = False,
                  no_agents: bool = False) -> str:
    argv = [python_exe(), *_capture_argv(tray=tray, no_clipboard=no_clipboard,
                                         no_agents=no_agents)]
    return launchd_plist(label=CAPTURE_LABEL, program_args=argv, workdir=repo_dir(),
                         run_at_load=True, keep_alive=True)


def synthesis_plist(*, time_hhmm: str, no_llm: bool = False) -> str:
    hour, minute = _parse_hhmm(time_hhmm)
    argv = [python_exe(), *_synthesis_argv(no_llm=no_llm)]
    return launchd_plist(label=SYNTHESIS_LABEL, program_args=argv, workdir=repo_dir(),
                         calendar=(hour, minute))


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path(label: str) -> Path:
    return _launch_agents_dir() / f"{label}.plist"


# =========================================================================== #
# Linux — cron (pure / testable)
# =========================================================================== #
def cron_command(argv: list[str], *, workdir: str, python: str | None = None) -> str:
    """``cd <workdir> && <python> <argv...>`` with POSIX quoting."""
    py = python or python_exe()
    parts = [py, *argv]
    cmd = " ".join(shlex.quote(p) for p in parts)
    return f"cd {shlex.quote(workdir)} && {cmd}"


def cron_line(*, schedule: str, argv: list[str], workdir: str, marker: str,
              python: str | None = None) -> str:
    """One marker-tagged crontab line. ``schedule`` is the time spec (``@reboot``
    or ``M H * * *``); ``marker`` is the trailing ``# <task>`` we use to find and
    remove our own lines without touching the user's other cron entries."""
    return f"{schedule} {cron_command(argv, workdir=workdir, python=python)}  # {marker}"


def cron_capture_line(*, tray: bool = False, no_clipboard: bool = False,
                      no_agents: bool = False) -> str:
    return cron_line(schedule="@reboot",
                     argv=_capture_argv(tray=tray, no_clipboard=no_clipboard,
                                        no_agents=no_agents),
                     workdir=repo_dir(), marker=CAPTURE_TASK)


def cron_synthesis_line(*, time_hhmm: str, no_llm: bool = False) -> str:
    hour, minute = _parse_hhmm(time_hhmm)
    return cron_line(schedule=f"{minute} {hour} * * *",
                     argv=_synthesis_argv(no_llm=no_llm),
                     workdir=repo_dir(), marker=SYNTHESIS_TASK)


def _parse_hhmm(time_hhmm: str) -> tuple[int, int]:
    hh, mm = time_hhmm.split(":")
    return int(hh), int(mm)


def merge_crontab(existing: str, line: str, marker: str) -> str:
    """Return ``existing`` crontab text with any prior ``marker`` line replaced by
    ``line`` (append if absent). Pure — the install function reads/writes crontab."""
    kept = [ln for ln in existing.splitlines() if not ln.rstrip().endswith(f"# {marker}")]
    kept.append(line)
    return "\n".join(kept).strip("\n") + "\n"


def strip_crontab(existing: str, marker: str) -> str:
    kept = [ln for ln in existing.splitlines() if not ln.rstrip().endswith(f"# {marker}")]
    text = "\n".join(kept).strip("\n")
    return (text + "\n") if text else ""


# =========================================================================== #
# Native scheduler wrappers (shell out; one per platform)
# =========================================================================== #
def _run(args: list[str], *, stdin: str | None = None) -> tuple[bool, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, input=stdin)
    except FileNotFoundError:
        return False, f"{args[0]} not found."
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, out


# -- Windows ---------------------------------------------------------------- #
def _win_register(task_name: str, xml: str) -> tuple[bool, str]:
    tmp = Path(tempfile.gettempdir()) / f"{task_name}.xml"
    tmp.write_text(xml, encoding="utf-16")          # header declares UTF-16
    try:
        return _run(["schtasks", "/Create", "/TN", task_name, "/XML", str(tmp), "/F"])
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _win_unregister(task_name: str) -> tuple[bool, str]:
    return _run(["schtasks", "/Delete", "/TN", task_name, "/F"])


def _win_status(task_name: str) -> tuple[bool, str]:
    return _run(["schtasks", "/Query", "/TN", task_name])


# -- macOS ------------------------------------------------------------------ #
def _mac_register(label: str, plist: str) -> tuple[bool, str]:
    path = _plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist, encoding="utf-8")
    _run(["launchctl", "unload", str(path)])        # idempotent reload
    ok, out = _run(["launchctl", "load", str(path)])
    return ok, out or f"loaded {path}"


def _mac_unregister(label: str) -> tuple[bool, str]:
    path = _plist_path(label)
    ok, out = _run(["launchctl", "unload", str(path)])
    try:
        path.unlink()
    except OSError:
        pass
    return ok, out or f"unloaded {path}"


def _mac_status(label: str) -> tuple[bool, str]:
    path = _plist_path(label)
    if not path.exists():
        return False, f"not installed ({path} missing)."
    ok, out = _run(["launchctl", "list", label])
    return ok, out or f"installed at {path}"


# -- Linux (cron) ----------------------------------------------------------- #
def _cron_read() -> str:
    ok, out = _run(["crontab", "-l"])
    # `crontab -l` exits non-zero with "no crontab for user" when empty.
    return out if (ok and out) else ""


def _cron_install(line: str, marker: str) -> tuple[bool, str]:
    existing = _cron_read()
    if existing and existing.startswith(("no crontab", "crontab not found")):
        existing = ""
    new = merge_crontab(existing, line, marker)
    ok, out = _run(["crontab", "-"], stdin=new)
    return ok, out or f"installed cron line for {marker}"


def _cron_remove(marker: str) -> tuple[bool, str]:
    existing = _cron_read()
    if not existing:
        return True, f"no crontab — nothing to remove for {marker}"
    new = strip_crontab(existing, marker)
    ok, out = _run(["crontab", "-"], stdin=new if new else "\n")
    return ok, out or f"removed cron line for {marker}"


def _cron_status(marker: str) -> tuple[bool, str]:
    existing = _cron_read()
    for ln in existing.splitlines():
        if ln.rstrip().endswith(f"# {marker}"):
            return True, ln
    return False, f"no cron line for {marker}"


# =========================================================================== #
# Public actions (dispatch on platform)
# =========================================================================== #
def enable_autostart(*, tray: bool = False, no_clipboard: bool = False,
                     no_agents: bool = False) -> tuple[bool, str]:
    plat = _platform()
    if plat == "windows":
        # Startup folder, not Task Scheduler — no elevation required (see the
        # AUTOSTART_SHORTCUT_NAME note). macOS launchd / Linux cron are already
        # per-user (no admin), so those keep their native mechanism.
        return _win_autostart_enable(tray=tray, no_clipboard=no_clipboard,
                                     no_agents=no_agents)
    if plat == "macos":
        return _mac_register(CAPTURE_LABEL, capture_plist(
            tray=tray, no_clipboard=no_clipboard, no_agents=no_agents))
    return _cron_install(cron_capture_line(
        tray=tray, no_clipboard=no_clipboard, no_agents=no_agents), CAPTURE_TASK)


def disable_autostart() -> tuple[bool, str]:
    plat = _platform()
    if plat == "windows":
        return _win_autostart_disable()
    if plat == "macos":
        return _mac_unregister(CAPTURE_LABEL)
    return _cron_remove(CAPTURE_TASK)


def enable_nightly(*, time_hhmm: str = "22:30", no_llm: bool = False) -> tuple[bool, str]:
    plat = _platform()
    if plat == "windows":
        return _win_register(SYNTHESIS_TASK, synthesis_task_xml(
            time_hhmm=time_hhmm, no_llm=no_llm))
    if plat == "macos":
        return _mac_register(SYNTHESIS_LABEL, synthesis_plist(
            time_hhmm=time_hhmm, no_llm=no_llm))
    return _cron_install(cron_synthesis_line(time_hhmm=time_hhmm, no_llm=no_llm),
                         SYNTHESIS_TASK)


def disable_nightly() -> tuple[bool, str]:
    plat = _platform()
    if plat == "windows":
        return _win_unregister(SYNTHESIS_TASK)
    if plat == "macos":
        return _mac_unregister(SYNTHESIS_LABEL)
    return _cron_remove(SYNTHESIS_TASK)


def task_status(task_name: str) -> tuple[bool, str]:
    """Status of a managed task, by its Windows task-name constant
    (CAPTURE_TASK / SYNTHESIS_TASK), resolved to the right mechanism per OS."""
    is_capture = (task_name == CAPTURE_TASK)
    plat = _platform()
    if plat == "windows":
        # Capture autostart is a Startup-folder shortcut; nightly stays a task.
        return _win_autostart_status() if is_capture else _win_status(task_name)
    if plat == "macos":
        return _mac_status(CAPTURE_LABEL if is_capture else SYNTHESIS_LABEL)
    return _cron_status(CAPTURE_TASK if is_capture else SYNTHESIS_TASK)


# Back-compat alias (older callers used these directly).
def unregister(task_name: str) -> tuple[bool, str]:
    return (disable_autostart() if task_name == CAPTURE_TASK else disable_nightly())
