"""ThroughLog onboarding — `tl init` zero-friction project discovery.

Hand-authoring ``projects.json`` (paths, keywords, apps, domains, regexes) is the
single biggest wall between cloning this repo and seeing a first diary. This module
removes it: scan a root directory for git repositories and generate a ready-to-edit
``projects.json`` — one project per repo, with ``signals.paths`` + ``git_remotes``
read straight from the repo, and ``keywords`` / ``window_patterns`` inferred from the
repo name and its README.

Pure standard library, fully deterministic — no LLM, no network, no new deps. The
output is a *starting point a human edits*, not a final config; we never fabricate
intent we can't read off disk.

Security note: ``signals.paths`` drives the privacy allowlist
(``config.allowlist_roots``), so registering a project here is what *makes a directory
observable*. Discovery only ever proposes repos found under the root the user named,
and merging never overwrites or drops an existing project.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

# Directories never worth descending into when scanning for repos.
_SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__",
    "dist", "build", ".idea", ".vscode", "target", ".gradle", ".tox",
    "site-packages", ".mypy_cache", ".pytest_cache",
}

# A marker file in a repo root -> apps that tend to be open when working in it.
# Low-confidence (app is the weakest categorization signal) but free to infer.
_LANG_APPS: dict[str, list[str]] = {
    "package.json": ["node.exe", "Code.exe"],
    "pnpm-lock.yaml": ["node.exe", "Code.exe"],
    "pyproject.toml": ["python.exe", "Code.exe"],
    "setup.py": ["python.exe", "Code.exe"],
    "requirements.txt": ["python.exe", "Code.exe"],
    "Cargo.toml": ["cargo.exe", "Code.exe"],
    "go.mod": ["go.exe", "Code.exe"],
    "build.gradle": ["studio64.exe", "java.exe"],
    "build.gradle.kts": ["studio64.exe", "java.exe"],
    "pom.xml": ["java.exe", "idea64.exe"],
    "Gemfile": ["ruby.exe", "Code.exe"],
}

_README_NAMES = (
    "README.md", "README.MD", "Readme.md", "readme.md",
    "README", "README.rst", "README.txt",
)


# --------------------------------------------------------------------------- #
# Git remote parsing
# --------------------------------------------------------------------------- #
def normalize_remote(url: str) -> str | None:
    """Reduce any git remote URL to the ``host/owner/repo`` form the registry uses
    (matching e.g. ``github.com/naorzadok/throughlog``). Returns None for
    anything that isn't a recognizable remote. Handles https/ssh URLs and the
    scp-like ``git@host:owner/repo`` syntax; strips a trailing ``.git``."""
    u = (url or "").strip()
    if not u:
        return None

    # scp-like:  user@host:owner/repo(.git)
    m = re.match(r"^[\w.+-]+@([^:/]+):(.+)$", u)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        # scheme://[user@]host/owner/repo(.git)
        m = re.match(r"^[a-zA-Z][\w+.\-]*://(?:[^@/]+@)?([^/]+)/(.+)$", u)
        if not m:
            return None
        host, path = m.group(1), m.group(2)

    path = path.strip().rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    host = host.split("@")[-1].strip()
    return f"{host}/{path}" if (host and path) else None


def parse_git_remotes(repo: Path) -> list[str]:
    """Read ``.git/config`` and return normalized remotes, origin first, de-duped."""
    cfg = repo / ".git" / "config"
    if not cfg.exists():
        return []
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    origin: list[str] = []
    others: list[str] = []
    in_remote = False
    is_origin = False
    for line in text.splitlines():
        st = line.strip()
        m = re.match(r'^\[remote\s+"([^"]+)"\]', st)
        if m:
            in_remote, is_origin = True, (m.group(1) == "origin")
            continue
        if st.startswith("["):
            in_remote = False
            continue
        if in_remote:
            mm = re.match(r"^url\s*=\s*(.+)$", st)
            if mm:
                norm = normalize_remote(mm.group(1))
                if norm:
                    (origin if is_origin else others).append(norm)

    out: list[str] = []
    for r in origin + others:
        if r not in out:
            out.append(r)
    return out


# --------------------------------------------------------------------------- #
# Name / README inference
# --------------------------------------------------------------------------- #
def _split_slug(name: str) -> list[str]:
    """Tokenize a repo name into lowercase words, splitting on separators and
    camelCase boundaries: ``throughlog`` / ``smartActivityLogger`` ->
    ``[smart, activity, logger]``."""
    words: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", name):
        if not part:
            continue
        for w in re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", part):
            words.append(w.lower())
    return words


def read_readme_title(repo: Path) -> str | None:
    """Return the first markdown heading text from the repo's README, or None.
    We only trust an explicit heading — we don't guess a title from prose/badges."""
    for nm in _README_NAMES:
        p = repo / nm
        if not p.exists():
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in lines[:15]:
            s = line.strip()
            if s.startswith("#"):
                title = s.lstrip("#").strip()
                return title or None
        return None
    return None


def infer_keywords(name: str, readme_title: str | None) -> list[str]:
    """Keywords from the repo name (spaced + slug forms) and the README title."""
    out: list[str] = []
    words = _split_slug(name)
    candidates = []
    if words:
        candidates.append(" ".join(words))         # e.g. "my cool project"
    candidates.append(name.replace("_", "-").lower())
    candidates.append(name.lower())
    if readme_title:
        candidates.append(readme_title.strip().lower())
    for k in candidates:
        k = k.strip()
        if k and k not in out:
            out.append(k)
    return out


def infer_window_patterns(name: str) -> list[str]:
    """Title regexes that match the repo identity in a window title. Words are
    joined with ``\\W*`` so separators (``-``/``_``/space) all match."""
    out: list[str] = []
    words = _split_slug(name)
    if words:
        out.append(".*" + r"\W*".join(re.escape(w) for w in words) + ".*")
    slug = name.lower()
    pat = ".*" + re.escape(slug) + ".*"
    if pat not in out:
        out.append(pat)
    return out


def infer_apps(repo: Path) -> list[str]:
    """Best-effort app list from language marker files in the repo root."""
    apps: list[str] = []
    for marker, names in _LANG_APPS.items():
        if (repo / marker).exists():
            for n in names:
                if n not in apps:
                    apps.append(n)
    return apps


# --------------------------------------------------------------------------- #
# Repo discovery
# --------------------------------------------------------------------------- #
def find_git_repos(root: Path, max_depth: int = 4) -> list[Path]:
    """Return the roots of every git repo under ``root`` (a repo = a dir containing
    ``.git``). Does not descend into a repo once found, skips heavy/vendored dirs,
    and never follows symlinks. Deterministic order (sorted)."""
    root = Path(root)
    try:
        root = root.resolve()
    except OSError:
        return []
    found: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        if (d / ".git").exists():
            found.append(d)
            return                       # a repo is a leaf for our purposes
        if depth >= max_depth:
            return
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for e in entries:
            if e.name in _SKIP_DIRS:
                continue
            try:
                if e.is_dir() and not e.is_symlink():
                    walk(e, depth + 1)
            except OSError:
                continue

    walk(root, 0)
    return found


def _slug_id(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "project"


def _unique_id(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def _norm_path(p: str) -> str:
    import os
    return os.path.normcase(os.path.abspath(str(p)))


def build_project(repo: Path, today: str, taken_ids: set[str]) -> dict[str, Any]:
    """Construct one project registry entry for ``repo`` (does not mutate ``taken_ids``)."""
    name = repo.name
    readme_title = read_readme_title(repo)
    remotes = parse_git_remotes(repo)
    pid = _unique_id(_slug_id(name), taken_ids)
    return {
        "id": pid,
        "name": readme_title or name,
        "status": "active",
        "description": (f"Auto-discovered git repo at {repo}. "
                        "Edit this description and tune the signals below."),
        "created": today,
        "last_updated": today,
        "signals": {
            "paths": [str(repo)],
            "git_remotes": remotes,
            "jira_prefixes": [],
            "keywords": infer_keywords(name, readme_title),
            "apps": infer_apps(repo),
            "domains": list(remotes),       # repo page visits attribute too
            "window_patterns": infer_window_patterns(name),
        },
    }


# --------------------------------------------------------------------------- #
# LLM-assisted enrichment (opt-in, METADATA-ONLY, never required)
# --------------------------------------------------------------------------- #
def _read_readme_excerpt(repo: Path, max_chars: int) -> str:
    """First ``max_chars`` of the repo's README (whole file, title included), or ''. The
    README is project-authored prose meant to describe the project — safe metadata."""
    for nm in _README_NAMES:
        p = repo / nm
        if p.exists():
            try:
                return p.read_text(encoding="utf-8", errors="replace").strip()[:max_chars]
            except OSError:
                return ""
    return ""


def _shallow_tree(root: Path, *, max_entries: int = 60, max_depth: int = 2) -> list[str]:
    """A bounded, NAMES-ONLY tree of the folder (dirs + files), skipping heavy/vendored
    dirs and hidden entries (so dotfiles like ``.env`` are never even named). Never reads
    file contents."""
    lines: list[str] = []

    def walk(d: Path, depth: int, prefix: str) -> None:
        if depth > max_depth or len(lines) >= max_entries:
            return
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for e in entries:
            if len(lines) >= max_entries:
                lines.append(f"{prefix}…")
                return
            if e.name in _SKIP_DIRS or e.name.startswith("."):
                continue
            try:
                is_dir = e.is_dir() and not e.is_symlink()
            except OSError:
                continue
            lines.append(f"{prefix}{e.name}{'/' if is_dir else ''}")
            if is_dir:
                walk(e, depth + 1, prefix + "  ")

    walk(root, 1, "")
    return lines


def build_repo_digest(folder: str | Path, *, max_chars: int = 4000) -> str:
    """A METADATA-ONLY digest of a folder for LLM-assisted init: README excerpt, detected
    language/build markers, and a bounded names-only file tree. NEVER includes file
    contents, so it is safe to send to a model under the project's egress rules. Pure
    standard library, deterministic, size-capped."""
    repo = Path(folder).expanduser()
    parts: list[str] = []
    readme = _read_readme_excerpt(repo, max_chars=max(0, max_chars - 800))
    if readme:
        parts.append("README (excerpt):\n" + readme)
    markers = [m for m in _LANG_APPS if (repo / m).exists()]
    if markers:
        parts.append("Language/build markers: " + ", ".join(sorted(markers)))
    tree = _shallow_tree(repo)
    if tree:
        parts.append("File tree (names only):\n" + "\n".join(tree))
    return "\n\n".join(parts).strip()[:max_chars]


# Descriptive signal fields the LLM may PROPOSE. ``paths``/``git_remotes`` are deliberately
# absent — they are owned by the deterministic scanner (paths drive the privacy allowlist).
_ENRICH_LIST_FIELDS = ("keywords", "window_patterns", "journal_extract",
                       "domains", "jira_prefixes")


def _parse_enrich_json(raw: str) -> dict[str, Any] | None:
    """Tolerantly extract the single JSON object an enrichment reply should be (strips a
    code fence, takes the outermost ``{...}``). Returns None on anything unparseable."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j == -1 or j < i:
        return None
    try:
        obj = json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def enrich_project(entry: dict[str, Any], digest: str, client: Any) -> dict[str, Any]:
    """Refine a deterministically-scanned project ``entry`` with ONE metadata-only LLM call.

    Merges the model's proposed descriptive signals (``description`` + the
    ``_ENRICH_LIST_FIELDS`` lists) into the entry. **Never** sets ``signals.paths`` or
    ``git_remotes`` — those stay deterministic so the model can never widen the privacy
    allowlist. Degrades to the UNMODIFIED entry on no client / ``LLMError`` / bad JSON;
    enrichment is always optional and never required."""
    if client is None or not (digest or "").strip():
        return entry
    from throughlog.llm.client import LLMError
    from throughlog.llm.prompts import build_init_enrich_prompt

    system, user = build_init_enrich_prompt(entry, digest)
    try:
        raw = client.chat(system, user, max_tokens=600)
    except LLMError:
        return entry
    data = _parse_enrich_json(raw)
    if not data:
        return entry

    out = json.loads(json.dumps(entry))            # deep copy (JSON-safe entry)
    sig = out.setdefault("signals", {})
    desc = data.get("description")
    if isinstance(desc, str) and desc.strip():
        out["description"] = desc.strip()
    for fld in _ENRICH_LIST_FIELDS:
        vals = data.get(fld)
        if not isinstance(vals, list):
            continue
        merged = list(sig.get(fld) or [])
        for v in vals:
            sv = str(v).strip()
            if sv and sv not in merged:
                merged.append(sv)
        if merged:
            sig[fld] = merged
    return out


# --------------------------------------------------------------------------- #
# Discovery + merge
# --------------------------------------------------------------------------- #
def discover_projects(root: str | Path,
                      existing: list[dict[str, Any]] | None = None,
                      *, today: str | None = None,
                      max_depth: int = 4,
                      client: Any = None) -> list[dict[str, Any]]:
    """Discover repos under ``root`` and return *new* project entries — those whose
    path isn't already registered in ``existing``. Ids are made unique against the
    existing registry. Never returns duplicates of already-tracked repos. When ``client``
    is given, each new entry is enriched via one metadata-only LLM call (best-effort)."""
    existing = existing or []
    today = today or date.today().isoformat()
    taken_ids = {p.get("id") for p in existing if p.get("id")}
    known_paths = {
        _norm_path(pp)
        for p in existing
        for pp in (p.get("signals", {}) or {}).get("paths", []) or []
    }

    new: list[dict[str, Any]] = []
    for repo in find_git_repos(Path(root), max_depth=max_depth):
        if _norm_path(str(repo)) in known_paths:
            continue
        proj = build_project(repo, today, taken_ids)
        if client is not None:
            proj = enrich_project(proj, build_repo_digest(repo), client)
        taken_ids.add(proj["id"])
        known_paths.add(_norm_path(str(repo)))
        new.append(proj)
    return new


def init_registry(root: str | Path, projects_path: str | Path, *,
                  today: str | None = None, max_depth: int = 4,
                  dry_run: bool = False, client: Any = None
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Path]:
    """Discover repos under ``root`` and merge them into ``projects_path``.

    Returns ``(discovered, existing, path)``. Existing projects are preserved
    verbatim and listed first; discovery only *appends*. Writes pretty JSON
    (2-space indent, trailing newline) unless ``dry_run``. When ``client`` is given,
    each discovered entry is enriched via one metadata-only LLM call (best-effort).
    """
    projects_path = Path(projects_path)
    existing: list[dict[str, Any]] = []
    if projects_path.exists():
        try:
            existing = json.loads(
                projects_path.read_text(encoding="utf-8")).get("projects", [])
        except (OSError, json.JSONDecodeError):
            existing = []

    discovered = discover_projects(root, existing=existing,
                                   today=today, max_depth=max_depth, client=client)
    if discovered and not dry_run:
        merged = {"projects": existing + discovered}
        projects_path.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    return discovered, existing, projects_path
