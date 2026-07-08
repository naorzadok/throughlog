"""GitHub pull source — capture work that never touched the local machine.

Cloud agents that open PRs, teammates' commits, CI runs: this adapter pulls them
from the GitHub API and emits them as ordinary events through the *same*
``bus.emit`` -> privacy gate as every local source. It is not a new pipeline — it
is one more source adapter with the usual split: pure transformers (API JSON ->
NormalizedEvent), tested offline by replaying captured payloads, and a thin live
driver that does the HTTP.

Two deliberate modeling choices:

  * **Everything pulled is an ``AGENT_REPORT``.** The schema defines AGENT_REPORT as
    an "ingested agent/**remote** report" with ``source.kind`` in {``agent``,
    ``remote``}, and — unlike ``GIT_COMMIT`` — it is *not* path-gated. A pulled
    commit has no local checkout path to allowlist-check, so emitting it as a
    GIT_COMMIT would (correctly) be dropped by the gate as ``not_in_allowlist``.
    Modeling remote work as a remote report is both schema-correct and avoids that.
  * **Human vs. agent is preserved**, not erased: a bot/app author yields
    ``source.kind="agent"`` + ``identity="agent:<login>"`` (so "what my agents did
    in the cloud" is a first-class diary thread); a human yields
    ``source.kind="remote"`` + ``identity="github:<login>"``. ``payload.actor``
    records the split too.

Governance: only repos whose ``git_remotes`` appear in ``projects.json`` are pulled
(``tracked_remotes``), and ``payload.repo`` (the remote) drives project attribution
via the categorizer's git-remote signal. Auth is a token from gitignored
``config.json`` (``integrations.github.token``) or ``$GITHUB_TOKEN``. No LLM.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

from throughlog.schema import NormalizedEvent, make_event, AGENT_REPORT, now_iso
from throughlog.sources.fs_git import ActorConfig, classify_author


# --------------------------------------------------------------------------- #
# Authorship classification (human vs agent/bot)
# --------------------------------------------------------------------------- #
def is_bot(user: dict[str, Any] | None) -> bool:
    """True if a GitHub user object denotes a bot/app account."""
    if not user:
        return False
    if str(user.get("type", "")).lower() == "bot":
        return True
    login = str(user.get("login", "")).lower()
    return "[bot]" in login


def author_identity(user: dict[str, Any] | None, *,
                    cfg: ActorConfig | None = None) -> tuple[str, str, str]:
    """Return ``(actor, source_kind, identity)`` for a GitHub user object.
    Bot/app or known-agent login -> agent; otherwise a remote human."""
    cfg = cfg or ActorConfig()
    login = ((user or {}).get("login") or (user or {}).get("name") or "unknown")
    if is_bot(user) or classify_author(login, cfg) == "agent":
        return "agent", "agent", f"agent:{login}"
    return "human", "remote", f"github:{login}"


def _agent_report(*, kind: str, identity: str, ts: str,
                  payload: dict[str, Any]) -> NormalizedEvent:
    return make_event(AGENT_REPORT, kind=kind, adapter="github_pull",
                      identity=identity, ts_wall=ts, payload=payload)


# --------------------------------------------------------------------------- #
# Pure transformers (GitHub REST JSON -> NormalizedEvent)
# --------------------------------------------------------------------------- #
def commit_to_event(commit: dict[str, Any], repo_remote: str, *,
                    cfg: ActorConfig | None = None) -> NormalizedEvent:
    """Map a GitHub commit object to an AGENT_REPORT."""
    cfg = cfg or ActorConfig()
    c = commit.get("commit", {}) or {}
    cauthor = c.get("author", {}) or {}
    ts = cauthor.get("date") or now_iso()
    sha_full = str(commit.get("sha", ""))
    sha = sha_full[:7]
    message = (c.get("message", "") or "")
    first_line = message.splitlines()[0] if message else ""
    user = commit.get("author") or {"login": cauthor.get("name", "")}
    actor, kind, identity = author_identity(user, cfg=cfg)
    files = [f.get("filename") for f in (commit.get("files") or []) if f.get("filename")]
    return _agent_report(kind=kind, identity=identity, ts=ts, payload={
        "summary": f"commit {sha}: {first_line}".strip(),
        "repo": repo_remote, "tool": "github", "actor": actor,
        "sha": sha_full, "message": first_line, "files": files,
        "kind_detail": "commit",
    })


def pull_request_to_event(pr: dict[str, Any], repo_remote: str, *,
                          cfg: ActorConfig | None = None) -> NormalizedEvent:
    """Map a GitHub pull-request object to an AGENT_REPORT. A bot-authored PR is the
    marquee 'a cloud agent worked on the repo and opened a PR' case."""
    cfg = cfg or ActorConfig()
    user = pr.get("user") or {}
    actor, kind, identity = author_identity(user, cfg=cfg)
    number = pr.get("number")
    title = pr.get("title", "")
    if pr.get("merged_at"):
        verb = "merged"
    elif pr.get("state") == "closed":
        verb = "closed"
    elif pr.get("draft"):
        verb = "drafted"
    else:
        verb = "opened"
    ts = pr.get("merged_at") or pr.get("updated_at") or pr.get("created_at") or now_iso()
    return _agent_report(kind=kind, identity=identity, ts=ts, payload={
        "summary": f"{verb} PR #{number}: {title}".strip(),
        "repo": repo_remote, "tool": "github", "actor": actor,
        "pr_number": number, "state": pr.get("state", ""),
        "url": pr.get("html_url", ""), "kind_detail": "pull_request",
    })


def workflow_run_to_event(run: dict[str, Any], repo_remote: str, *,
                          cfg: ActorConfig | None = None) -> NormalizedEvent:
    """Map a GitHub Actions workflow run to a remote AGENT_REPORT (CI activity)."""
    name = run.get("name") or "workflow"
    conclusion = run.get("conclusion") or run.get("status") or ""
    ts = run.get("updated_at") or run.get("created_at") or now_iso()
    return _agent_report(kind="remote", identity=f"ci:{name}", ts=ts, payload={
        "summary": f"CI {name}: {conclusion}".strip(),
        "repo": repo_remote, "tool": "github-actions",
        "conclusion": conclusion, "url": run.get("html_url", ""),
        "kind_detail": "workflow_run",
    })


# --------------------------------------------------------------------------- #
# Repo selection from the registry (allowlist governance at pull-time)
# --------------------------------------------------------------------------- #
def _normalize_remote(remote: str) -> str:
    r = (remote or "").strip().rstrip("/")
    if r.lower().endswith(".git"):
        r = r[:-4]
    return r


def tracked_remotes(projects: list[dict[str, Any]]) -> dict[str, str]:
    """``{normalized_remote: project_id}`` for every git remote in the registry.
    Only these repos are ever pulled."""
    out: dict[str, str] = {}
    for p in projects:
        for r in (p.get("signals", {}) or {}).get("git_remotes", []) or []:
            if r:
                out[_normalize_remote(r)] = p.get("id", "")
    return out


def owner_repo(remote: str) -> str | None:
    """``github.com/owner/repo`` -> ``owner/repo``; None for non-GitHub remotes."""
    r = _normalize_remote(remote)
    marker = "github.com/"
    if marker not in r:
        return None
    return r.split(marker, 1)[1] or None


# --------------------------------------------------------------------------- #
# Live driver (thin; HTTP fetch is injectable so tests replay payloads offline)
# --------------------------------------------------------------------------- #
def _default_fetch(url: str, token: str | None, *, timeout: float = 15.0) -> Any:
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "throughlog"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def pull_repo_once(emitter: Any, remote: str, *, token: str | None,
                   fetch: Callable[..., Any], state: dict[str, Any],
                   cfg: ActorConfig | None = None, per_page: int = 30) -> int:
    """One pull pass for one repo. Dedups commits by sha and PRs by (number,
    updated_at) via ``state`` so repeated polls only emit genuinely new activity.
    Returns the number of events emitted."""
    repo = owner_repo(remote)
    if repo is None:
        return 0
    base = f"https://api.github.com/repos/{repo}"
    seen = state.setdefault(remote, {"commits": set(), "pulls": {}})
    emitted = 0

    for c in fetch(f"{base}/commits?per_page={per_page}", token) or []:
        sha = c.get("sha")
        if sha and sha not in seen["commits"]:
            seen["commits"].add(sha)
            emitter.emit(commit_to_event(c, remote, cfg=cfg))
            emitted += 1

    for pr in fetch(
            f"{base}/pulls?state=all&sort=updated&direction=desc&per_page={per_page}",
            token) or []:
        num = pr.get("number")
        upd = pr.get("updated_at")
        if num is not None and seen["pulls"].get(num) != upd:
            seen["pulls"][num] = upd
            emitter.emit(pull_request_to_event(pr, remote, cfg=cfg))
            emitted += 1
    return emitted


def pull_github_live(emitter: Any, *, token: str | None,
                     projects: list[dict[str, Any]],
                     fetch: Callable[..., Any] | None = None, stop: Any = None,
                     interval_sec: float = 300.0, once: bool = False,
                     state: dict[str, Any] | None = None,
                     cfg: ActorConfig | None = None) -> int:
    """Pull tracked GitHub repos into the bus. Loops every ``interval_sec`` until
    ``stop`` is set; ``once=True`` does a single pass (for ``tl pull``). Returns
    the total events emitted. One failing repo never stops the others."""
    import threading

    stop = stop or threading.Event()
    fetch = fetch or _default_fetch
    state = state if state is not None else {}
    remotes = [r for r in tracked_remotes(projects) if owner_repo(r)]
    total = 0
    try:
        while not stop.is_set():
            for remote in remotes:
                try:
                    total += pull_repo_once(emitter, remote, token=token,
                                            fetch=fetch, state=state, cfg=cfg)
                except Exception:
                    continue          # a bad repo/response never sinks the pass
            if once:
                break
            stop.wait(interval_sec)
    except KeyboardInterrupt:
        pass
    return total
