"""Config + project registry loading, and the privacy allowlist.

The privacy allowlist DEFAULTS to the tracked-project paths
(`projects.json` -> signals.paths), plus any explicit `privacy.allowlist_extra`
in config.json. Rather than watching every drive, you only observe directories
that belong to a project you are actually tracking.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
PROJECTS_PATH = BASE_DIR / "projects.json"
PROJECTS_EXAMPLE_PATH = BASE_DIR / "projects.example.json"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else CONFIG_PATH
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_projects(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load the project registry. `projects.json` is user-specific and gitignored
    (like config.json) because its signal paths define the privacy allowlist; on a
    fresh clone it is absent, so we fall back to the shipped example template. The
    example's paths don't exist on the user's machine, so the allowlist fails
    closed (nothing observed) until they configure or run `tl init`."""
    if path:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("projects", [])
    src = PROJECTS_PATH if PROJECTS_PATH.exists() else PROJECTS_EXAMPLE_PATH
    if not src.exists():
        return []
    with open(src, encoding="utf-8") as f:
        return json.load(f).get("projects", [])


def allowlist_roots(config: dict[str, Any] | None = None,
                    projects: list[dict[str, Any]] | None = None) -> list[Path]:
    """Resolve the directory allowlist: every tracked project's signal paths,
    plus config `privacy.allowlist_extra`. Returns absolute, existing-or-not paths.
    """
    if projects is None:
        projects = load_projects()
    if config is None:
        try:
            config = load_config()
        except FileNotFoundError:
            config = {}

    roots: list[Path] = []
    for proj in projects:
        for raw in proj.get("signals", {}).get("paths", []) or []:
            roots.append(Path(raw))

    for raw in config.get("privacy", {}).get("allowlist_extra", []) or []:
        roots.append(Path(raw))

    # De-dup by normalized form, preserve order.
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = os.path.normcase(os.path.abspath(str(r)))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def data_dir(config: dict[str, Any] | None = None) -> Path:
    cfg = config if config is not None else (load_config() if CONFIG_PATH.exists() else {})
    return BASE_DIR / cfg.get("paths", {}).get("data_dir", "data")


def budget_for_model(model: str) -> int:
    """Resolve the ``"auto"`` per-call input budget from the configured model's tier.
    Chunking is a fidelity tax paid only to protect a WEAK model — a strong long-context
    model should be fed raw detail — so the budget scales with model capability:

    * free tier (``:free``, the default Nemotron/Qwen) -> 6000 (conservative);
    * a known frontier long-context family -> ~200000 (effectively "never chunk");
    * anything else (a capable paid/local model) -> 16000.
    """
    m = (model or "").lower()
    if ":free" in m:
        return 6000
    frontier = ("claude", "sonnet", "opus", "haiku", "gpt-4", "gpt-5", "gpt4", "o1",
                "o3", "o4", "gemini")
    if any(k in m for k in frontier):
        return 200000
    return 16000


def _resolve_input_budget(raw: Any, config: dict[str, Any]) -> int:
    """Map the ``max_input_tokens`` config value to an int budget: ``"auto"`` -> tier
    (via ``budget_for_model`` on ``llm.model``); an int/number -> itself; junk -> 0
    (the legacy condense path, byte-identical)."""
    if isinstance(raw, str):
        if raw.strip().lower() == "auto":
            return budget_for_model((config.get("llm", {}) or {}).get("model", ""))
        try:
            return int(raw.strip())
        except ValueError:
            return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def synthesis_options_from(config: dict[str, Any] | None = None):
    """Build the Phase-2 SynthesisOptions from `config.synthesis.*`. The PRODUCT default
    is journaling ON (the tier-2 detailed journal is valuable and the calls are cheap on
    free models), so a missing/legacy `synthesis` block still enables it — distinct from
    the SynthesisOptions() library default, which is OFF for test/caller byte-identity.

    The batching knobs (`entry_batch`/`max_input_tokens`/`max_batch_days`) default to the
    OLD per-day, condense-on-overflow behavior when absent, so a legacy config.json upgrades
    without a surprise; a fresh `config.example.json` opts into adaptive/auto/7."""
    from throughlog.synthesize import SynthesisOptions

    if config is None:
        try:
            config = load_config()
        except FileNotFoundError:
            config = {}
    syn = config.get("synthesis", {}) or {}
    period = str(syn.get("entry_period", "month")).strip().lower()
    cadence = str(syn.get("summary_cadence", "off")).strip().lower()
    batch = str(syn.get("entry_batch", "day")).strip().lower()
    return SynthesisOptions(
        write_entries=bool(syn.get("write_entries", True)),
        entry_max_tokens=int(syn.get("entry_max_tokens", 1500)),
        entry_period=period if period in ("month", "week") else "month",
        summary_cadence=cadence if cadence in ("off", "weekly", "monthly") else "off",
        skip_unchanged=bool(syn.get("skip_unchanged", False)),
        entry_batch=batch if batch in ("day", "week", "adaptive") else "day",
        max_input_tokens=_resolve_input_budget(syn.get("max_input_tokens", 0), config),
        max_batch_days=max(1, int(syn.get("max_batch_days", 7) or 7)),
    )


def diff_policy_from(config: dict[str, Any] | None = None,
                     projects: list[dict[str, Any]] | None = None):
    """Build the (opt-in, default-OFF) DiffPolicy from `config.privacy.*` plus the
    per-project `signals.ignore_globs`. With no config the policy captures nothing,
    so the gate strips diffs exactly as before."""
    from throughlog.privacy.diff_policy import DiffPolicy

    if config is None:
        try:
            config = load_config()
        except FileNotFoundError:
            config = {}
    if projects is None:
        projects = load_projects()

    priv = config.get("privacy", {}) or {}
    globs: list[str] = []
    for proj in projects:
        for g in proj.get("signals", {}).get("ignore_globs", []) or []:
            globs.append(g)
    for g in priv.get("ignore_globs", []) or []:
        globs.append(g)

    return DiffPolicy(
        capture_diffs=bool(priv.get("capture_diffs", False)),
        max_lines=int(priv.get("diff_max_lines", 400)),
        max_bytes=int(priv.get("diff_max_bytes", 65536)),
        ignore_globs=tuple(dict.fromkeys(globs)),          # de-dup, preserve order
        clipboard_preview=bool(priv.get("clipboard_preview", False)),
        clipboard_preview_chars=int(priv.get("clipboard_preview_chars", 256)),
    )
