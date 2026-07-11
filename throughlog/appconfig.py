"""Guarded writers for ``config.json`` / ``projects.json`` behind the settings UI.

The dashboard ([throughlog/server.py](server.py)) lets a non-technical user configure ThroughLog
from the browser instead of hand-editing JSON. Those writes are security-relevant —
``projects.json`` ``signals.paths`` drive the privacy allowlist
(:func:`throughlog.config.allowlist_roots`), so adding a project is what *makes a directory
observable*. This module is the deliberately conservative chokepoint for them:

  * **Pure + atomic.** No ``http.server`` import; every write goes through a
    tmp-file ``replace`` so a crash never leaves a half-written config (same trick as
    :meth:`throughlog.capture.Supervisor.write_status`).
  * **Known keys only.** Config writes touch a fixed allow-set per section and
    preserve every other key already on disk (so a UI bug can't drop your relay
    tokens). Seeds from ``config.example.json`` on first write.
  * **Merge-only projects.** :func:`add_project` never edits or deletes an existing
    project (same contract as :func:`throughlog.onboard.init_registry`); it reuses
    :func:`throughlog.onboard.build_project` to infer signals from the folder on disk.
  * **Confirm-before-widening.** :func:`allowlist_delta` lets the caller show exactly
    which directory would become observable, so the UI can require an explicit
    confirmation before the allowlist grows. No LLM, no network, fully testable.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from throughlog import config as cfgmod

# The only keys the settings UI may write, per config section. Anything else on
# disk is preserved untouched.
ALLOWED_LLM = frozenset({"api_key", "model", "base_url", "model_fallback",
                         "provider", "api_key_env", "local_endpoint", "local_model",
                         "reasoning_effort", "max_requests_per_min"})
ALLOWED_PRIVACY = frozenset({"capture_diffs", "clipboard_preview", "diff_max_lines",
                             "diff_max_bytes", "clipboard_preview_chars",
                             "allowlist_extra", "ignore_globs"})
ALLOWED_SYNTHESIS = frozenset({"write_entries", "entry_period", "summary_cadence",
                               "entry_max_tokens", "skip_unchanged",
                               "entry_batch", "max_input_tokens", "max_batch_days"})
# Weekdays a project may be scheduled on (plus "daily" = every run), for the per-project
# synthesis-day picker. Enforced by update_project_synthesis so a bad value can't land.
SYNTHESIS_DAYS = frozenset({"daily", "mon", "tue", "wed", "thu", "fri", "sat", "sun"})
ALLOWED_INIT = frozenset({"llm_enrich"})


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #
def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Pretty-print ``data`` to ``path`` via a tmp-file swap (readers never see a
    half-written file). 2-space indent + trailing newline, matching the rest of the
    repo's JSON style."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def _load_config_or_seed(path: Path) -> dict[str, Any]:
    """Current ``config.json`` if present, else the shipped example as a starting
    point (so the first browser write produces a complete, valid file)."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    example = cfgmod.BASE_DIR / "config.example.json"
    if example.exists():
        try:
            return json.loads(example.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


# --------------------------------------------------------------------------- #
# Config writes (known keys only, unknown keys preserved)
# --------------------------------------------------------------------------- #
def update_config_section(section: str, patch: dict[str, Any], *,
                          allowed: frozenset[str],
                          config_path: str | Path | None = None) -> dict[str, Any]:
    """Merge ``patch`` into ``config[section]`` for keys in ``allowed`` only, leaving
    every other key/section on disk intact, and atomically write the file. ``None``
    values in ``patch`` are ignored (lets the UI omit "leave unchanged" fields such
    as a blank API-key box). Returns the full config that was written."""
    path = Path(config_path) if config_path else cfgmod.CONFIG_PATH
    cfg = _load_config_or_seed(path)
    sect = dict(cfg.get(section) or {})
    for key, value in patch.items():
        if key in allowed and value is not None:
            sect[key] = value
    cfg[section] = sect
    _atomic_write_json(path, cfg)
    return cfg


def update_llm(patch: dict[str, Any], *,
               config_path: str | Path | None = None) -> dict[str, Any]:
    return update_config_section("llm", patch, allowed=ALLOWED_LLM,
                                 config_path=config_path)


def update_privacy(patch: dict[str, Any], *,
                   config_path: str | Path | None = None) -> dict[str, Any]:
    return update_config_section("privacy", patch, allowed=ALLOWED_PRIVACY,
                                 config_path=config_path)


def update_synthesis(patch: dict[str, Any], *,
                     config_path: str | Path | None = None) -> dict[str, Any]:
    """Write the journal/summary knobs (``config.synthesis.*``) from the settings UI —
    ``write_entries``, ``entry_period``, ``summary_cadence``, ``entry_max_tokens``.
    Enum validation is the caller's job; this only enforces the key allow-set."""
    return update_config_section("synthesis", patch, allowed=ALLOWED_SYNTHESIS,
                                 config_path=config_path)


def update_init(patch: dict[str, Any], *,
                config_path: str | Path | None = None) -> dict[str, Any]:
    """Write the init knobs (``config.init.*``) — currently just ``llm_enrich`` (opt-in
    metadata-only LLM enrichment of newly-added projects)."""
    return update_config_section("init", patch, allowed=ALLOWED_INIT,
                                 config_path=config_path)


def init_enrich_enabled(config: dict[str, Any] | None = None) -> bool:
    """True if opt-in LLM enrichment of new projects is turned on (``config.init.llm_enrich``).
    The server pairs this with a resolvable key before ever building a client."""
    cfg = config if config is not None else (
        cfgmod.load_config() if cfgmod.CONFIG_PATH.exists() else {})
    return bool((cfg.get("init") or {}).get("llm_enrich", False))


def update_schedule(time_hhmm: str | None, *,
                    config_path: str | Path | None = None) -> dict[str, Any]:
    """Set or clear the in-app nightly-synthesis time (``config.schedule.synthesize_at``).

    This is the **no-admin** nightly path: the always-on app (``tl up``) runs an
    in-process timer that synthesizes at this time, so no elevated scheduled task is
    needed (``schtasks /Create`` requires admin). A falsy ``time_hhmm`` clears the
    key (the timer becomes a no-op). Atomic; every other config key is preserved."""
    path = Path(config_path) if config_path else cfgmod.CONFIG_PATH
    cfg = _load_config_or_seed(path)
    sched = dict(cfg.get("schedule") or {})
    if time_hhmm:
        sched["synthesize_at"] = time_hhmm
    else:
        sched.pop("synthesize_at", None)
    cfg["schedule"] = sched
    _atomic_write_json(path, cfg)
    return cfg


def nightly_time(config: dict[str, Any] | None = None) -> str | None:
    """The configured in-app nightly-synthesis time, or ``None`` if not set."""
    cfg = config if config is not None else (
        cfgmod.load_config() if cfgmod.CONFIG_PATH.exists() else {})
    val = (cfg.get("schedule") or {}).get("synthesize_at")
    return val if (isinstance(val, str) and val.strip()) else None


def key_is_set(config: dict[str, Any] | None = None) -> bool:
    """True if an LLM key is resolvable (inline ``llm.api_key`` or its env var), so
    the UI can show "key is set" without ever echoing the secret back."""
    cfg = config if config is not None else (
        cfgmod.load_config() if cfgmod.CONFIG_PATH.exists() else {})
    llm = cfg.get("llm") or {}
    if (llm.get("api_key") or "").strip():
        return True
    env_name = llm.get("api_key_env") or "OPENROUTER_API_KEY"
    return bool(os.environ.get(env_name))


# --------------------------------------------------------------------------- #
# Project writes (merge-only) + allowlist preview
# --------------------------------------------------------------------------- #
def _read_projects(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("projects", [])
    except (OSError, json.JSONDecodeError):
        return []


def _tracked_paths(projects: list[dict[str, Any]]) -> set[str]:
    from throughlog import onboard
    return {
        onboard._norm_path(pp)
        for p in projects
        for pp in (p.get("signals", {}) or {}).get("paths", []) or []
    }


def allowlist_delta(folder: str | Path,
                    projects_path: str | Path | None = None) -> list[str]:
    """The absolute directories that would become observable if ``folder`` were
    added as a project — i.e. ``[]`` when it is already covered by the allowlist,
    else the one new root. Drives the UI's confirm-before-widening prompt."""
    p = Path(folder).expanduser()
    try:
        ap = p.resolve()
    except OSError:
        ap = p
    path = Path(projects_path) if projects_path else cfgmod.PROJECTS_PATH
    existing = _read_projects(path)
    known = _tracked_paths(existing)
    if os.path.normcase(os.path.abspath(str(ap))) in known:
        return []
    return [str(ap)]


def add_project(folder: str | Path, *, projects_path: str | Path | None = None,
                today: str | None = None, client: Any = None) -> dict[str, Any]:
    """Infer one project entry from ``folder`` (via :func:`throughlog.onboard.build_project`)
    and append it to ``projects.json`` (merge-only). Returns the new entry. Works for
    non-git folders too (``build_project`` just yields empty ``git_remotes``).

    When ``client`` is given, the entry is enriched by one metadata-only LLM call
    (:func:`throughlog.onboard.enrich_project`) — descriptive signals only; ``signals.paths``
    stays deterministic so the privacy allowlist is never widened by the model.

    Raises ``ValueError`` if ``folder`` is not a real directory or its path is
    already tracked — the UI surfaces the message verbatim. Existing projects are
    preserved byte-for-byte; only an append ever happens."""
    from throughlog import onboard

    p = Path(folder).expanduser()
    if not p.is_dir():
        raise ValueError(f"Not a directory: {folder}")

    path = Path(projects_path) if projects_path else cfgmod.PROJECTS_PATH
    existing = _read_projects(path)
    if onboard._norm_path(str(p)) in _tracked_paths(existing):
        raise ValueError(f"Already tracked: {p}")

    taken_ids = {pr.get("id") for pr in existing if pr.get("id")}
    entry = onboard.build_project(p, today or date.today().isoformat(), taken_ids)
    if client is not None:
        entry = onboard.enrich_project(entry, onboard.build_repo_digest(p), client)
    _atomic_write_json(path, {"projects": existing + [entry]})
    return entry


def update_project_synthesis(project_id: str, day: str, *,
                             projects_path: str | Path | None = None) -> dict[str, Any]:
    """Set one project's synthesis weekday (``synthesis.day``) in ``projects.json``,
    preserving every other field on that project (and every other project). ``day`` must
    be in :data:`SYNTHESIS_DAYS`; ``"daily"`` clears the schedule (the project synthesizes
    every run, the default). Raises ``ValueError`` on an unknown id or an invalid day —
    the UI surfaces the message verbatim, same as :func:`add_project`."""
    day = str(day or "daily").strip().lower()
    if day not in SYNTHESIS_DAYS:
        raise ValueError(f"Invalid synthesis day: {day}")
    path = Path(projects_path) if projects_path else cfgmod.PROJECTS_PATH
    projects = _read_projects(path)
    for proj in projects:
        if proj.get("id") == project_id:
            syn = dict(proj.get("synthesis") or {})
            if day == "daily":
                syn.pop("day", None)
            else:
                syn["day"] = day
            if syn:
                proj["synthesis"] = syn
            else:
                proj.pop("synthesis", None)
            _atomic_write_json(path, {"projects": projects})
            return proj
    raise ValueError(f"Unknown project: {project_id}")


# --------------------------------------------------------------------------- #
# Scan a root for git repos (the `tl init` discovery, exposed to the UI)
# --------------------------------------------------------------------------- #
def scan_projects(root: str | Path, *, projects_path: str | Path | None = None,
                  max_depth: int = 4) -> list[dict[str, Any]]:
    """Preview the *new* projects a scan of ``root`` would add (merge-only; never
    writes). Reuses :func:`throughlog.onboard.discover_projects`, so already-tracked repos
    are skipped. Drives the UI's scan confirmation (each entry's first signal path is
    a directory that would become observable)."""
    from throughlog import onboard
    r = Path(root).expanduser()
    if not r.is_dir():
        raise ValueError(f"Not a directory: {root}")
    path = Path(projects_path) if projects_path else cfgmod.PROJECTS_PATH
    return onboard.discover_projects(r, existing=_read_projects(path),
                                     max_depth=max_depth)


def add_scanned_projects(root: str | Path, *,
                         projects_path: str | Path | None = None,
                         today: str | None = None,
                         max_depth: int = 4) -> list[dict[str, Any]]:
    """Discover repos under ``root`` and append them to ``projects.json``
    (merge-only, via :func:`throughlog.onboard.init_registry`). Returns the added entries."""
    from throughlog import onboard
    r = Path(root).expanduser()
    if not r.is_dir():
        raise ValueError(f"Not a directory: {root}")
    path = Path(projects_path) if projects_path else cfgmod.PROJECTS_PATH
    discovered, _existing, _path = onboard.init_registry(
        r, path, today=today, max_depth=max_depth)
    return discovered
