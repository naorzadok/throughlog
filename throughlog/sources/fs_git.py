"""fs/git adapter — deterministic file-change capture with actor attribution.

Two deterministic cores, both clock-injected and simulator-testable (no watchdog,
no git binary, no clock in the core):

  * ``FileChurnFilter`` (case O4) — turns the raw firehose of a save (lock files,
    hex temp scratch, .bak backups, plus several write notifications for the real
    file) into exactly ONE FILE_CHANGE per real save: noise paths are dropped and
    rapid repeats of the same real file are coalesced.

  * actor attribution (case A1) — splits FILE_CHANGE / GIT_COMMIT events by who
    did the work: git author is the strongest signal; otherwise recent human
    input vs a dense machine burst decides human vs agent. Never guesses an agent
    when a human was clearly typing.

The live driver lazily imports watchdog + shells out to git, so the cores and the
test path stay dependency-free.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import PureWindowsPath

from throughlog.schema import NormalizedEvent, make_event, FILE_CHANGE, GIT_COMMIT

# --------------------------------------------------------------------------- #
# Noise filtering (ported from capture_daemon: ~$ locks, ignore exts, hex temp)
# --------------------------------------------------------------------------- #
_NOISE_EXTS = {
    ".pyc", ".pyo", ".pyd", ".jsonl", ".log", ".tmp", ".temp", ".bak", ".swp",
    ".swo", ".swx", ".lock", ".crdownload", ".part", ".partial", ".old", ".orig",
}
_NOISE_NAME_PREFIXES = ("~$", ".~", "~")          # Office / editor lock files
_NOISE_DIR_PARTS = {
    "__pycache__", ".git", "node_modules", ".idea", ".vs", "appdata",
    "$recycle.bin", ".cache", ".pytest_cache", ".mypy_cache", "venv", ".venv",
}
_HEX_TEMP_RE = re.compile(r"^[0-9A-Fa-f]{8,}$")   # extensionless hex scratch (CAD/Excel)
_NUMBERED_BAK_RE = re.compile(r"\.\d+\.bak$|~\d+$|\.bak\d+$")


def is_noise(path: str) -> bool:
    p = PureWindowsPath(path)
    name = p.name
    ext = p.suffix.lower()
    if name.startswith(_NOISE_NAME_PREFIXES):
        return True
    if ext in _NOISE_EXTS:
        return True
    if not ext and _HEX_TEMP_RE.match(name):
        return True
    if _NUMBERED_BAK_RE.search(name):
        return True
    lowered = {part.lower() for part in p.parts}
    return bool(lowered & _NOISE_DIR_PARTS)


# --------------------------------------------------------------------------- #
# Actor attribution (human vs agent)
# --------------------------------------------------------------------------- #
@dataclass
class ActorConfig:
    human_ids: tuple[str, ...] = ()
    agent_ids: tuple[str, ...] = ("claude", "[bot]", "bot", "agent", "copilot", "cursor")


def classify_author(author: str, cfg: ActorConfig) -> str | None:
    """Decisive actor from a git author string, or None if it matches neither."""
    a = (author or "").lower()
    if not a:
        return None
    if any(tok.lower() in a for tok in cfg.agent_ids):
        return "agent"
    if any(tok.lower() in a for tok in cfg.human_ids):
        return "human"
    return None


def attribute_actor(author: str, human_active: bool, burst_size: int,
                    cfg: ActorConfig, burst_threshold: int) -> tuple[str, str, float]:
    """(actor, method, confidence). Priority: git author > live human input >
    machine burst / no-human."""
    decided = classify_author(author, cfg)
    if decided is not None:
        return decided, "git_author", 0.95
    if human_active:
        return "human", "input", 0.7
    # No author and no human at the keyboard -> machine-driven work.
    if burst_size >= burst_threshold:
        return "agent", "burst", 0.8
    return "agent", "no_human", 0.5


# --------------------------------------------------------------------------- #
# O4 — churn filter / coalescer
# --------------------------------------------------------------------------- #
@dataclass
class RawFsEvent:
    ts: str
    path: str
    action: str = "modified"     # created | modified | moved | deleted
    author: str = ""             # git author, when resolvable
    human_active: bool = False   # human key/mouse input within the last few seconds


def _elapsed(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def should_capture_diff(rel_path: str, policy) -> bool:
    """True if a diff may be generated for this file under the policy. False when
    capture is off, the file is on the secrets denylist, or it matches an ignore
    glob. Best-effort capture-side filter (the gate re-checks authoritatively from
    the real per-file diff headers)."""
    if policy is None or not getattr(policy, "capture_diffs", False):
        return False
    from throughlog.privacy.diff_policy import is_secret_file, path_ignored
    if is_secret_file(rel_path):
        return False
    if path_ignored(rel_path, getattr(policy, "ignore_globs", ())):
        return False
    return True


class FileChurnFilter:
    def __init__(self, *, coalesce_sec: float = 2.0, burst_window_sec: float = 5.0,
                 burst_threshold: int = 5, actor_config: ActorConfig | None = None,
                 diff_fn=None, policy=None) -> None:
        self.coalesce_sec = float(coalesce_sec)
        self.burst_window = float(burst_window_sec)
        self.burst_threshold = int(burst_threshold)
        self.actor_cfg = actor_config or ActorConfig()
        # Opt-in diff capture: `diff_fn(abs_path) -> str|None` (the live git shell-out)
        # and `policy` (the DiffPolicy). Both default None -> no diff is ever attached,
        # so the deterministic core and existing tests are unchanged.
        self.diff_fn = diff_fn
        self.policy = policy
        self._last_emit: dict[str, str] = {}     # path -> ts of last emitted change
        self._recent: deque[str] = deque()       # ts of recent emitted changes (burst window)

    def feed(self, ev: RawFsEvent) -> list[NormalizedEvent]:
        if is_noise(ev.path):
            return []                              # drop lock / temp / backup churn
        last = self._last_emit.get(ev.path)
        if last is not None and _elapsed(last, ev.ts) < self.coalesce_sec:
            return []                              # coalesce repeats of the same real file
        self._last_emit[ev.path] = ev.ts

        self._recent.append(ev.ts)
        while self._recent and _elapsed(self._recent[0], ev.ts) > self.burst_window:
            self._recent.popleft()
        burst_size = len(self._recent)

        actor, method, conf = attribute_actor(
            ev.author, ev.human_active, burst_size, self.actor_cfg, self.burst_threshold)
        e = make_event(FILE_CHANGE, kind="fs", adapter="fs_git",
                       payload={"path": ev.path, "action": ev.action, "actor": actor},
                       ts_wall=ev.ts)
        e.attribution.method = method
        e.attribution.confidence = conf

        if self.diff_fn is not None and should_capture_diff(ev.path, self.policy):
            try:
                diff = self.diff_fn(ev.path)
            except Exception:
                diff = None                        # never let a diff failure drop the event
            if diff:
                e.payload["diff"] = diff           # the gate scrubs + sidecars this
        return [e]


def make_git_commit(repo: str, author: str, message: str, ts: str,
                    files: list[str] | None = None,
                    actor_config: ActorConfig | None = None) -> NormalizedEvent:
    """Build a GIT_COMMIT event with the actor resolved from the author. ``repo``
    is the path the gate allowlist-checks."""
    cfg = actor_config or ActorConfig()
    actor = classify_author(author, cfg) or "human"   # commits default to human
    e = make_event(GIT_COMMIT, kind="git", adapter="fs_git", ts_wall=ts,
                   payload={"repo": repo, "author": author, "message": message,
                            "actor": actor, "files": files or []})
    e.attribution.method = "git_author"
    e.attribution.confidence = 0.95 if classify_author(author, cfg) else 0.5
    return e


# --------------------------------------------------------------------------- #
# Live git diff shell-out (only reached when diff capture is enabled)
# --------------------------------------------------------------------------- #
def _find_repo_root(path: str, _cache: dict[str, str | None] = {}) -> str | None:
    """Nearest ancestor directory containing a ``.git`` entry, or None. Memoized by
    the file's directory so a busy repo isn't walked on every save."""
    import os
    d = os.path.dirname(os.path.abspath(path))
    if d in _cache:
        return _cache[d]
    cur = d
    root: str | None = None
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            root = cur
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    _cache[d] = root
    return root


def _tlignore_globs(repo: str, _cache: dict[str, tuple[str, ...]] = {}) -> tuple[str, ...]:
    """Read + cache a repo-root ``.tlignore`` (gitignore-style globs). Additive
    ignore rules specific to the logger, without touching the repo's ``.gitignore``."""
    import os
    if repo in _cache:
        return _cache[repo]
    from throughlog.privacy.diff_policy import parse_tlignore
    globs: tuple[str, ...] = ()
    p = os.path.join(repo, ".tlignore")
    try:
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                globs = parse_tlignore(f.read())
    except OSError:
        globs = ()
    _cache[repo] = globs
    return globs


def _git_diff_worktree(repo: str, rel: str, max_bytes: int, timeout: float = 5.0) -> str | None:
    """Working-tree unified diff of one file vs HEAD/index, via ``git diff``.

    V-03 — the read is BOUNDED at the subprocess boundary (read at most max_bytes+1,
    then kill), so a multi-GB regenerated/minified file can't OOM the capture process
    before the cap is applied. argv list + ``--`` before the path => no shell, no
    injection, flag-like paths treated as pathspec. Any failure (no git, non-repo,
    decode error) degrades to None."""
    import subprocess
    try:
        proc = subprocess.Popen(
            ["git", "-C", repo, "--no-pager", "diff", "--no-color", "--", rel],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    try:
        data = proc.stdout.read(max_bytes + 1) if proc.stdout else b""
    except Exception:
        data = b""
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except Exception:
            pass
    if not data:
        return None
    return data.decode("utf-8", "replace")


def _make_diff_fn(policy):
    """Build the `diff_fn(abs_path)->str|None` closure for live capture, or None when
    capture is off. Enforces the secrets denylist, config ignore globs, and the repo's
    ``.tlignore`` capture-side before shelling out to git."""
    if policy is None or not getattr(policy, "capture_diffs", False):
        return None
    import os
    from throughlog.privacy.diff_policy import is_secret_file, path_ignored

    def _diff_fn(abs_path: str) -> str | None:
        repo = _find_repo_root(abs_path)
        if not repo:
            return None                            # not under a git repo -> v1 skip
        rel = os.path.relpath(abs_path, repo).replace("\\", "/")
        globs = tuple(policy.ignore_globs) + _tlignore_globs(repo)
        if is_secret_file(rel) or path_ignored(rel, globs):
            return None
        return _git_diff_worktree(repo, rel, policy.max_bytes)

    return _diff_fn


# --------------------------------------------------------------------------- #
# Live driver — lazy watchdog + git; deterministic cores above stay dep-free.
# --------------------------------------------------------------------------- #
def watch_live(emitter, roots, *, stop=None, human_active_fn=lambda: False,
               exclude=(), policy=None, **cfg) -> None:
    """Watch allowlisted roots, run the churn filter, push FILE_CHANGE events.
    ``human_active_fn`` should report whether a human input happened recently
    (wired to the focus adapter's input signal). ``exclude`` is a list of
    directories whose subtrees are ignored — used to skip the tool's own
    ``data/`` and ``journal/`` so its status/log writes don't self-pollute
    capture. Runs until ``stop`` (a threading.Event) is set or KeyboardInterrupt,
    then stops the observer. Roots that do not exist are skipped so a partial
    allowlist still watches."""
    import os
    import threading
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    from throughlog.schema import now_iso

    stop = stop or threading.Event()
    churn = FileChurnFilter(diff_fn=_make_diff_fn(policy), policy=policy, **cfg)
    excl = [os.path.normcase(os.path.abspath(str(e))) for e in (exclude or [])]

    def _excluded(path: str) -> bool:
        np = os.path.normcase(os.path.abspath(str(path)))
        return any(np == e or np.startswith(e + os.sep) for e in excl)

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            if event.is_directory or _excluded(event.src_path):
                return
            raw = RawFsEvent(ts=now_iso(), path=event.src_path,
                             action=event.event_type, human_active=human_active_fn())
            for ev in churn.feed(raw):
                emitter.emit(ev)

    from pathlib import Path
    observer = Observer()
    handler = _Handler()
    watched = 0
    for root in roots:
        if Path(root).is_dir():
            observer.schedule(handler, str(root), recursive=True)
            watched += 1
    if watched == 0:
        return                         # nothing to watch -> exit cleanly
    observer.start()
    try:
        while not stop.is_set():
            stop.wait(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
