"""tl serve — the local dashboard (M10): give the journal a face.

A read-only, stdlib-only web UI over the artifacts the pipeline already writes:
the per-project ``overview.md``, the cross-project ``executive_summary.md``, the
``daily.md`` feed, and a reconciled ``Timeline`` of the captured day — plus a live
capture badge read from ``data/daemon_status.json``.

Why this exists: the journal prose is already good; it just had no face. A local
dashboard is what makes the project screenshot-able and is the surface every later
phase (agent reports, cloud sync) renders into.

Design follows the repo's "pure core + thin driver" rule: the HTML is built by
**pure functions** (``md_to_html``, ``render_page``, ``overview_html`` …) that are
unit-tested directly; the ``http.server`` handler is a thin driver. No new runtime
dependencies — markdown is rendered by a tiny, escaping converter that covers
exactly the subset our journal uses. Nothing here touches an LLM or the privacy gate
(it only reads already-gated, already-synthesized output).
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from throughlog.config import BASE_DIR, data_dir, load_config, load_projects
from throughlog.schema import (
    FOCUS_SESSION, DEEP_WORK, LONG_RUN, FILE_CHANGE, GIT_COMMIT,
    NARRATION, CLIPBOARD, AGENT_REPORT,
)

DEFAULT_PORT = 8799   # not 8787 — that is the agent-ingest endpoint


# --------------------------------------------------------------------------- #
# Markdown — a tiny, safe converter for the journal subset (NO third-party dep).
# Everything is HTML-escaped first; only a known set of constructs is re-marked.
# --------------------------------------------------------------------------- #
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_LEADING_TIME = re.compile(r"^\[\d{1,2}:\d{2}\]\s*")


def _inline(escaped: str) -> str:
    """Apply inline **bold** and `code` to already-HTML-escaped text."""
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    escaped = _CODE.sub(r"<code>\1</code>", escaped)
    return escaped


def md_to_html(md: str) -> str:
    """Render the markdown subset our journal uses: # / ## / ### headings, bullet
    lists (``-`` ``*`` ``•``), ``---`` rules, **bold**, `code`, and paragraphs.
    Input is fully escaped before any tag is emitted, so it is injection-safe."""
    out: list[str] = []
    para: list[str] = []
    in_list = False

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in (md or "").splitlines():
        s = raw.strip()
        if not s:
            flush_para()
            close_list()
            continue
        if len(s) >= 3 and set(s) == {"-"}:                 # --- horizontal rule
            flush_para()
            close_list()
            out.append("<hr>")
            continue
        m = _HEADING.match(s)
        if m:
            flush_para()
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(html.escape(m.group(2)))}</h{level}>")
            continue
        if s[0] in "-*•" and (len(s) == 1 or s[1] == " "):  # bullet list item
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(html.escape(s[1:].strip()))}</li>")
            continue
        close_list()
        para.append(_inline(html.escape(s)))
    flush_para()
    close_list()
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Status badge (read from the capture supervisor's heartbeat file)
# --------------------------------------------------------------------------- #
def read_status(data_dir_path: Path) -> dict[str, Any] | None:
    p = data_dir_path / "daemon_status.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def status_badge(status: dict[str, Any] | None, *, now: datetime | None = None,
                 stale_after_sec: float = 120.0) -> tuple[str, str]:
    """(css_class, label) for the live-capture badge. A heartbeat older than
    ``stale_after_sec`` is treated as offline even if the file says ``alive``."""
    if not status:
        return ("off", "Not running")
    hb = str(status.get("heartbeat", ""))
    stale = True
    try:
        dt = datetime.fromisoformat(hb)
        ref = now or datetime.now(dt.tzinfo)
        stale = (ref - dt).total_seconds() > stale_after_sec
    except (ValueError, TypeError):
        stale = True
    if not status.get("alive", False) or stale:
        seen = hb[11:16] if len(hb) >= 16 else hb
        return ("off", f"Offline · last seen {seen}" if hb else "Offline")
    if status.get("paused"):
        return ("paused", "Paused")
    return ("live", "Recording")


def capture_is_live(data_dir_path: str | Path, *,
                    now: datetime | None = None) -> bool:
    """True if a capture supervisor is currently recording/paused (a fresh heartbeat
    in ``daemon_status.json``). Lets ``tl up`` avoid starting a SECOND engine when
    capture is already running — via the tray or ``tl capture`` — so the badge stays
    truthful across all three launch surfaces and events are never double-written."""
    cls, _ = status_badge(read_status(Path(data_dir_path)), now=now)
    return cls in ("live", "paused")


# --------------------------------------------------------------------------- #
# Journal discovery + small readers
# --------------------------------------------------------------------------- #
def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _overview_title(overview_path: Path) -> str:
    for line in _read(overview_path).splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def discover_projects(journal_dir: Path,
                      registry: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """[(project_id, display_name)] for every ``project_*`` dir that has output."""
    registry = registry or {}
    out: list[tuple[str, str]] = []
    if journal_dir.exists():
        for d in sorted(journal_dir.glob("project_*")):
            if not d.is_dir():
                continue
            pid = d.name[len("project_"):]
            name = registry.get(pid) or _overview_title(d / "overview.md") or pid
            out.append((pid, name))
    return out


def _overview_excerpt(overview_md: str, limit: int = 240) -> str:
    """The first prose paragraph under '## Current State' (or the first paragraph),
    truncated — used for the project card preview."""
    lines = overview_md.splitlines()
    body: list[str] = []
    capturing = False
    for line in lines:
        if line.startswith("## "):
            if capturing:
                break
            capturing = "current state" in line.lower()
            continue
        if capturing and line.strip():
            body.append(line.strip())
        elif capturing and body:
            break
    text = " ".join(body) or _first_paragraph(overview_md)
    text = re.sub(r"[*`#]", "", text).strip()
    return (text[:limit] + "…") if len(text) > limit else text


def _first_paragraph(md: str) -> str:
    buf: list[str] = []
    for line in md.splitlines():
        if line.startswith("#") or not line.strip():
            if buf:
                break
            continue
        buf.append(line.strip())
    return " ".join(buf)


# --------------------------------------------------------------------------- #
# Page shell (CSS + nav)
# --------------------------------------------------------------------------- #
_CSS = """
@font-face{font-family:"Space Grotesk";font-style:normal;font-weight:300 700;
font-display:swap;src:url("/static/space-grotesk.ttf") format("truetype")}
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1d212b;--bd:#262b36;--fg:#e6e9ef;
--mut:#9aa4b2;--acc:#7aa2f7;--green:#3fb950;--amber:#d29922;--grey:#6e7681;
--display:"Space Grotesk",-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.brand,h1,h2,h3{font-family:var(--display)}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
code{background:#11141a;border:1px solid var(--bd);border-radius:5px;
padding:.05em .4em;font:13px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace}
header{display:flex;align-items:center;justify-content:space-between;
padding:14px 22px;border-bottom:1px solid var(--bd);background:var(--panel)}
.brand{font-size:17px}.brand .sub{color:var(--mut);font-size:12px;margin-left:6px}
.badge{display:inline-flex;align-items:center;gap:8px;font-size:13px;
padding:6px 12px;border-radius:999px;border:1px solid var(--bd);background:var(--panel2)}
.badge .dot{width:9px;height:9px;border-radius:50%;background:var(--grey)}
.badge.live .dot{background:var(--green);box-shadow:0 0 0 3px rgba(63,185,80,.18)}
.badge.paused .dot{background:var(--amber)}
.badge.live{color:#7ee2a8}.badge.paused{color:#f0d58c}
.layout{display:grid;grid-template-columns:240px 1fr;min-height:calc(100vh - 57px)}
nav{border-right:1px solid var(--bd);background:var(--panel);padding:16px 12px}
nav a{display:block;padding:8px 12px;border-radius:8px;color:var(--fg);margin-bottom:2px}
nav a:hover{background:var(--panel2);text-decoration:none}
nav a.active{background:var(--acc);color:#0b0e14;font-weight:600}
.navlabel{color:var(--mut);font-size:11px;text-transform:uppercase;
letter-spacing:.08em;margin:18px 12px 6px}
main{padding:26px 30px;max-width:980px}
h1{font-size:24px;margin:.2em 0 .6em}h2{font-size:18px;margin:1.1em 0 .5em}
h3{font-size:15px;color:var(--mut);margin:1em 0 .3em;text-transform:uppercase;
letter-spacing:.04em}
.sech{margin-top:4px}
.card{background:var(--panel);border:1px solid var(--bd);border-radius:14px;
padding:18px 22px;margin-bottom:22px}
.card.overview h1{border-bottom:1px solid var(--bd);padding-bottom:.3em}
hr{border:0;border-top:1px solid var(--bd);margin:18px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}
.pcard{background:var(--panel2);border:1px solid var(--bd);border-radius:12px;
padding:16px 18px;display:block;color:var(--fg)}
.pcard:hover{border-color:var(--acc);text-decoration:none}
.pcard b{display:block;margin-bottom:6px}
.pcard p{color:var(--mut);font-size:13px;margin:0}
.empty{color:var(--mut)}
.timeline{display:flex;flex-direction:column;gap:2px}
.ev{display:grid;grid-template-columns:54px 92px 1fr;gap:12px;align-items:baseline;
padding:7px 10px;border-radius:8px}
.ev:hover{background:var(--panel2)}
.ev .evt{color:var(--mut);font:13px ui-monospace,Consolas,monospace}
.ev .evk{font-size:11px;text-transform:uppercase;letter-spacing:.04em;
color:var(--mut);border-left:3px solid var(--grey);padding-left:8px}
.ev.focus .evk,.ev.deep .evk{border-color:var(--acc)}
.ev.commit .evk{border-color:var(--green)}
.ev.file .evk{border-color:#a371f7}
.ev.note .evk{border-color:var(--amber)}
.ev.agent .evk{border-color:#f778ba}
.ev .evb{font-size:14px}
.ev .diff{margin-top:4px}
.ev .diff summary{cursor:pointer;color:var(--mut);font-size:12px}
.ev .diff pre{margin:6px 0 0;padding:10px;background:var(--panel2);border-radius:6px;
  overflow:auto;max-height:360px;font:12px ui-monospace,Consolas,monospace;white-space:pre}
.btn{font:14px inherit;cursor:pointer;color:var(--fg);background:var(--panel2);
border:1px solid var(--bd);border-radius:8px;padding:8px 14px}
.btn:hover{border-color:var(--acc)}.btn.primary{background:var(--acc);color:#0b0e14;
border-color:var(--acc);font-weight:600}.btn.warn{border-color:var(--amber)}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.controls form{display:inline;margin:0}
.ask form{display:flex;gap:8px;margin:.4em 0 0}
.ask .askin{flex:1;font:15px inherit;color:var(--fg);background:var(--panel2);
border:1px solid var(--bd);border-radius:8px;padding:9px 12px}
.ask .askin:focus{outline:none;border-color:var(--acc)}
.answer{margin-top:14px;border-top:1px solid var(--bd);padding-top:10px}
.chart text{fill:var(--mut);font:12px -apple-system,Segoe UI,Roboto,sans-serif}
.chart .lbl{fill:var(--fg)}
.chart rect{fill:var(--acc)}
.fld{display:block;margin:.6em 0}
.fld label{display:block;color:var(--mut);font-size:13px;margin-bottom:4px}
.fld input[type=text],.fld input[type=password],.fld input[type=number]{width:100%;
font:14px inherit;color:var(--fg);background:var(--panel2);border:1px solid var(--bd);
border-radius:8px;padding:8px 11px}
.fld select{appearance:none;-webkit-appearance:none;width:100%;max-width:260px;
font:14px inherit;color:var(--fg);color-scheme:dark;cursor:pointer;
background:linear-gradient(180deg,var(--panel2),#191d27);
border:1px solid var(--bd);border-radius:10px;padding:10px 40px 10px 13px;
background-image:url("data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20width='12'%20height='12'%20viewBox='0%200%2012%2012'%3E%3Cpath%20d='M2.5%204.5l3.5%203.5%203.5-3.5'%20stroke='%237aa2f7'%20stroke-width='1.6'%20fill='none'%20stroke-linecap='round'%20stroke-linejoin='round'/%3E%3C/svg%3E"),linear-gradient(180deg,var(--panel2),#191d27);
background-repeat:no-repeat;background-position:right 14px center,0 0;background-size:12px,100%;
transition:border-color .15s ease,box-shadow .15s ease}
.fld select:hover{border-color:var(--acc)}
.fld select:focus{outline:none;border-color:var(--acc);
box-shadow:0 0 0 3px rgba(122,162,247,.20)}
.fld select option{background:var(--panel2);color:var(--fg)}
.fld select option:checked{background:var(--panel2);color:var(--acc)}
.fld .hint{display:block;color:var(--mut);font-size:12px;margin-top:10px;line-height:1.4}
.fld.row{display:flex;align-items:center;gap:8px}.fld.row label{margin:0}
.proj{display:flex;justify-content:space-between;gap:12px;padding:9px 0;
border-bottom:1px solid var(--bd)}.proj:last-child{border-bottom:0}
.proj .meta{color:var(--mut);font-size:12px}
.note{color:var(--amber);font-size:13px;margin:.3em 0}
.ok{color:var(--green);font-size:13px;margin:.3em 0}
"""


def render_page(title: str, body: str, *, projects: list[tuple[str, str]],
                active: str = "", status: dict[str, Any] | None = None) -> str:
    badge_cls, badge_lbl = status_badge(status)
    proj_links = "".join(
        f'<a href="/project/{html.escape(pid, quote=True)}"'
        f' class="{"active" if active == pid else ""}">{html.escape(name)}</a>'
        for pid, name in projects
    ) or '<div class="navlabel" style="margin:6px 12px">none yet</div>'
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)} · ThroughLog</title><style>{_CSS}</style></head><body>"
        '<header><div class=brand><b>ThroughLog</b>'
        '<span class=sub>private · local</span></div>'
        f'<div class="badge {badge_cls}"><span class=dot></span>{html.escape(badge_lbl)}</div></header>'
        '<div class=layout><nav>'
        f'<a href="/" class="{"active" if active == "overview" else ""}">Overview</a>'
        f'<a href="/timeline" class="{"active" if active == "timeline" else ""}">Timeline</a>'
        f'<a href="/summaries" class="{"active" if active == "summaries" else ""}">Summaries</a>'
        f'<a href="/settings" class="{"active" if active == "settings" else ""}">Settings</a>'
        '<div class=navlabel>Projects</div>'
        f"{proj_links}</nav>"
        f"<main>{body}</main></div></body></html>"
    )


# --------------------------------------------------------------------------- #
# Views (pure, given the on-disk artifacts)
# --------------------------------------------------------------------------- #
def overview_html(journal_dir: Path, projects: list[tuple[str, str]], *,
                  controls_html: str = "", chart_svg: str = "",
                  ask_html: str = "") -> str:
    parts: list[str] = ["<h1>Overview</h1>"]

    if controls_html:
        parts.append(controls_html)
    if ask_html:
        parts.append(ask_html)
    if chart_svg:
        parts.append('<h2 class="sech">Time per project (today)</h2>'
                     f'<section class="card chart">{chart_svg}</section>')

    exec_md = _read(journal_dir / "executive_summary.md")
    if exec_md.strip():
        parts.append(f'<section class="card">{md_to_html(exec_md)}</section>')

    sdir = journal_dir / "summaries"
    sums = sorted((p.stem for p in sdir.glob("*.md")), reverse=True) if sdir.is_dir() else []
    if sums:
        latest = _read(sdir / f"{sums[0]}.md")
        parts.append('<h2 class="sech">Latest summary</h2>'
                     f'<section class="card">{md_to_html(latest)}'
                     '<p style="margin-top:14px"><a href="/summaries">All summaries →</a>'
                     '</p></section>')

    if projects:
        cards = []
        for pid, name in projects:
            excerpt = _overview_excerpt(_read(journal_dir / f"project_{pid}" / "overview.md"))
            cards.append(
                f'<a class="pcard" href="/project/{html.escape(pid, quote=True)}">'
                f'<b>{html.escape(name)}</b><p>{html.escape(excerpt) or "—"}</p></a>'
            )
        parts.append('<h2 class="sech">Projects</h2>'
                     f'<div class="grid">{"".join(cards)}</div>')
    else:
        parts.append('<section class="card"><p class="empty">No journal yet. Run '
                     '<code>python -m throughlog.cli synthesize --replay --no-llm</code> '
                     'then refresh.</p></section>')

    daily_md = _read(journal_dir / "daily.md")
    if daily_md.strip():
        parts.append('<h2 class="sech">Daily feed</h2>'
                     f'<section class="card">{md_to_html(daily_md)}</section>')
    return "\n".join(parts)


def project_html(journal_dir: Path, pid: str) -> str:
    pdir = journal_dir / f"project_{pid}"
    overview = _read(pdir / "overview.md")
    if not overview.strip():
        return ('<section class="card"><p class="empty">No overview for this project '
                'yet.</p></section>')
    pid_q = html.escape(pid, quote=True)
    links: list[str] = []
    if (pdir / "archive.md").exists():
        links.append(f'<a href="/archive/{pid_q}">View full archive →</a>')
    if (pdir / "entries").is_dir() and any((pdir / "entries").glob("*.md")):
        links.append(f'<a href="/entries/{pid_q}">View detailed entries →</a>')
    links_html = (f'<p style="margin-top:18px">{" &nbsp;·&nbsp; ".join(links)}</p>'
                  if links else "")
    return f'<article class="card overview">{md_to_html(overview)}{links_html}</article>'


def archive_html(journal_dir: Path, pid: str) -> str:
    archive = _read(journal_dir / f"project_{pid}" / "archive.md")
    if not archive.strip():
        return '<section class="card"><p class="empty">No archive yet.</p></section>'
    back = (f'<p><a href="/project/{html.escape(pid, quote=True)}">← back to overview</a></p>')
    return f'{back}<article class="card">{md_to_html(archive)}</article>'


def entries_html(journal_dir: Path, pid: str, month: str | None = None) -> str:
    """Render ONE month of the tier-2 detailed entries (newest by default), with links to
    the other months — never the whole year as a single page. ``month`` is honored only
    when it matches an existing month file, so a crafted value can never escape the dir."""
    entries_dir = journal_dir / f"project_{pid}" / "entries"
    months = sorted((p.stem for p in entries_dir.glob("*.md")), reverse=True) \
        if entries_dir.is_dir() else []
    if not months:
        return ('<section class="card"><p class="empty">No detailed entries yet.</p>'
                '</section>')
    sel = month if month in months else months[0]
    pid_q = html.escape(pid, quote=True)
    back = f'<p><a href="/project/{pid_q}">← back to overview</a></p>'
    nav = " &nbsp;·&nbsp; ".join(
        (f'<strong>{html.escape(m)}</strong>' if m == sel
         else f'<a href="/entries/{pid_q}/{html.escape(m, quote=True)}">{html.escape(m)}</a>')
        for m in months)
    body = md_to_html(_read(entries_dir / f"{sel}.md"))
    return (f'{back}<section class="card"><p>Entry periods: {nav}</p></section>'
            f'<article class="card">{body}</article>')


def summary_html(journal_dir: Path, period: str | None = None) -> str:
    """Render ONE period of the cross-project retrospective (newest by default), with links
    to the other periods. ``period`` is honored only when it matches an existing summary
    file stem, so a crafted value can never escape ``journal/summaries/``."""
    sdir = journal_dir / "summaries"
    periods = sorted((p.stem for p in sdir.glob("*.md")), reverse=True) \
        if sdir.is_dir() else []
    if not periods:
        return ('<h1>Summaries</h1><section class="card"><p class="empty">No weekly or '
                'monthly summaries yet. Turn one on in Settings → Journal &amp; summaries '
                '(and synthesize with an LLM key).</p></section>')
    sel = period if period in periods else periods[0]
    nav = " &nbsp;·&nbsp; ".join(
        (f'<strong>{html.escape(p)}</strong>' if p == sel
         else f'<a href="/summary/{html.escape(p, quote=True)}">{html.escape(p)}</a>')
        for p in periods)
    body = md_to_html(_read(sdir / f"{sel}.md"))
    return (f'<h1>Summaries</h1><section class="card"><p>Periods: {nav}</p></section>'
            f'<article class="card">{body}</article>')


_TYPE_CLASS = {
    FOCUS_SESSION: "focus", DEEP_WORK: "deep", LONG_RUN: "run",
    FILE_CHANGE: "file", GIT_COMMIT: "commit", NARRATION: "note",
    CLIPBOARD: "clip", AGENT_REPORT: "agent",
}


def timeline_events(data_dir_path: Path, date_key: str) -> list[Any]:
    """Reconciled events for a day. Falls back to the bundled replay corpus when no
    live capture exists yet, so the dashboard demos out of the box."""
    from throughlog import synthesize
    events_dir = data_dir_path / "events"
    candidates: list[Path] = []
    day_file = events_dir / f"{date_key}.jsonl"
    if day_file.exists():
        candidates = [day_file]
    elif events_dir.exists():
        candidates = sorted(events_dir.glob("*.jsonl"))
    if not candidates:
        candidates = sorted((BASE_DIR / "data" / "events_replay").glob("*.jsonl"))
    events: list[Any] = []
    for c in candidates:
        events.extend(synthesize.load_events(c))
    return events


def read_diff(data_dir_path: Path, ref: str) -> str:
    """Return the scrubbed sidecar diff for ``ref`` (a sha256), or "" if missing or
    tampered. The filename IS the content hash, so we re-verify before rendering —
    a dangling/forged ``diff_ref`` renders nothing rather than arbitrary bytes."""
    import hashlib
    if not ref or not re.fullmatch(r"[0-9a-f]{64}", str(ref)):
        return ""
    path = Path(data_dir_path) / "diffs" / f"{ref}.patch"
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != ref:
        return ""
    return body


def _diff_details(data_dir_path: Path | None, ev: Any) -> str:
    """A collapsed, injection-safe <details> diff block for an event with a diff_ref
    (only when capture_diffs was on). Empty string otherwise."""
    if data_dir_path is None:
        return ""
    ref = (getattr(ev, "payload", None) or {}).get("diff_ref")
    if not ref:
        return ""
    body = read_diff(data_dir_path, ref)
    if not body:
        return ""
    return (f'<details class="diff"><summary>diff</summary>'
            f'<pre>{html.escape(body)}</pre></details>')


def timeline_html(events: list[Any], data_dir_path: Path | None = None) -> str:
    from throughlog.synthesize import summarize_event
    if not events:
        return ('<h1>Timeline</h1><section class="card"><p class="empty">No captured '
                'events yet.</p></section>')
    rows = []
    for ev in events:
        line = (summarize_event(ev) or "").split("\n", 1)[0]
        text = _LEADING_TIME.sub("", line) or ev.type
        tm = ev.ts_wall[11:16] if len(ev.ts_wall) >= 16 else ev.ts_wall
        cls = _TYPE_CLASS.get(ev.type, "other")
        rows.append(
            f'<div class="ev {cls}"><span class="evt">{html.escape(tm)}</span>'
            f'<span class="evk">{html.escape(ev.type)}</span>'
            f'<span class="evb">{html.escape(text)}{_diff_details(data_dir_path, ev)}</span></div>'
        )
    return (f'<h1>Timeline <span class="empty" style="font-size:14px">· {len(events)} '
            f'events</span></h1><section class="card"><div class="timeline">'
            f'{"".join(rows)}</div></section>')


# --------------------------------------------------------------------------- #
# Time-per-project chart (pure, deterministic — no LLM)
# --------------------------------------------------------------------------- #
def _fmt_dur(secs: int) -> str:
    """Compact human duration: ``95m`` -> ``1h35m``, ``40m`` -> ``40m``."""
    mins = max(0, int(secs)) // 60
    h, m = divmod(mins, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def project_durations(events: list[Any],
                      projects: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """[(display_name, seconds)] of session time attributed to each project, busiest
    first. Categorizes deterministically (``client=None`` — never touches the LLM)
    then sums ``duration_sec`` over focus/deep-work/long-run sessions."""
    from throughlog.categorize import categorize_events
    from throughlog.synthesize import group_by_project

    if not events or not projects:
        return []
    try:
        categorize_events(events, projects, client=None)
        groups = group_by_project(events)
    except Exception:
        return []
    names = {p["id"]: p.get("name", p["id"]) for p in projects}
    rows: list[tuple[str, int]] = []
    for pid, evs in groups.items():
        secs = 0
        for ev in evs:
            if ev.type in (FOCUS_SESSION, DEEP_WORK, LONG_RUN):
                try:
                    secs += int((ev.payload or {}).get("duration_sec") or 0)
                except (TypeError, ValueError):
                    pass
        if secs > 0:
            rows.append((names.get(pid, pid), secs))
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def project_time_svg(rows: list[tuple[str, int]]) -> str:
    """Inline horizontal-bar SVG for :func:`project_durations` output (injection-safe:
    every label is HTML-escaped; bar widths are clamped). Themed via the page CSS."""
    if not rows:
        return '<p class="empty">No tracked session time yet today.</p>'
    pad_l, bar_w, row_h = 150, 360, 30
    maxv = max(s for _, s in rows) or 1
    height = row_h * len(rows) + 8
    parts = [f'<svg viewBox="0 0 {pad_l + bar_w + 90} {height}" '
             f'width="100%" preserveAspectRatio="xMinYMin meet" '
             f'role="img" aria-label="Time per project">']
    for i, (name, secs) in enumerate(rows):
        y = i * row_h + 4
        w = max(2, round(bar_w * secs / maxv))
        label = html.escape(name if len(name) <= 22 else name[:21] + "…")
        parts.append(f'<text class="lbl" x="{pad_l - 8}" y="{y + 16}" '
                     f'text-anchor="end">{label}</text>')
        parts.append(f'<rect x="{pad_l}" y="{y + 4}" width="{w}" height="18" rx="4"/>')
        parts.append(f'<text x="{pad_l + w + 7}" y="{y + 17}">{_fmt_dur(secs)}</text>')
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Control-panel views (forms) + the small bits they share
# --------------------------------------------------------------------------- #
def _hidden_token(token: str) -> str:
    return f'<input type="hidden" name="_token" value="{html.escape(token, quote=True)}">'


def _error_card(msg: str) -> str:
    return f'<section class="card"><p class="empty">{html.escape(msg)}</p></section>'


# Reasoning-effort levels exposed in Settings. "" = provider default (param omitted).
_REASONING_LEVELS = (("", "Default"), ("low", "Low"), ("medium", "Medium"),
                     ("high", "High"))


def _reasoning_select(current: Any) -> str:
    """A <select> for the LLM reasoning level. Honest about scope: it only affects
    models that support reasoning (e.g. gpt-oss / o-series) and is ignored otherwise,
    so no per-model capability detection is needed."""
    cur = str(current or "").strip().lower()
    opts = "".join(
        f'<option value="{val}"{" selected" if val == cur else ""}>{label}</option>'
        for val, label in _REASONING_LEVELS)
    return ('<span class="fld"><label for="reasoning_effort">Reasoning effort</label>'
            f'<select id="reasoning_effort" name="reasoning_effort">{opts}</select>'
            '<span class="hint">Only affects models that support thinking '
            '(gpt-oss, o-series, …); ignored otherwise.</span></span>')


def control_bar_html(token: str, *, has_capture: bool, paused: bool,
                     can_quit: bool = False) -> str:
    """The Overview action bar: Pause/Resume (only when capture is running here),
    Synthesize now (always), and Quit the app (only when ``tl up`` is the running
    process — the fool-proof stop when it was launched detached with no console)."""
    parts = ['<section class="card controls">']
    if has_capture:
        act = "/action/resume" if paused else "/action/pause"
        lbl = "Resume capture" if paused else "Pause capture"
        cls = "btn warn" if not paused else "btn primary"
        parts.append(f'<form method="post" action="{act}">{_hidden_token(token)}'
                     f'<button class="{cls}">{lbl}</button></form>')
    parts.append('<form method="post" action="/action/synthesize">'
                 f'{_hidden_token(token)}'
                 '<button class="btn">Synthesize now</button></form>')
    if can_quit:
        parts.append(
            '<form method="post" action="/action/quit" style="margin-left:auto">'
            f'{_hidden_token(token)}'
            '<button class="btn warn" '
            'onclick="return confirm(\'Stop capturing and close the app?\')">'
            'Quit app</button></form>')
    if not has_capture:
        parts.append('<span class="empty" style="font-size:13px">capture not running '
                     'here — start it with <code>tl up</code></span>')
    parts.append('</section>')
    return "".join(parts)


def ask_box_html(token: str, *, question: str = "", answer_html: str = "") -> str:
    return (
        '<section class="card ask"><h2 class="sech" style="margin-top:0">Ask your '
        'journal</h2><form method="post" action="/ask">'
        f'{_hidden_token(token)}'
        '<input class="askin" type="text" name="q" autocomplete="off" '
        'placeholder="e.g. what did I ship on checkout this week?" '
        f'value="{html.escape(question, quote=True)}">'
        '<button class="btn primary">Ask</button></form>'
        f'{answer_html}</section>'
    )


def answer_html(ans: Any) -> str:
    """Render an :class:`throughlog.ask.Answer` (markdown body + sources + degrade note)."""
    body = md_to_html(getattr(ans, "text", "") or "")
    srcs = [s for s in (getattr(ans, "sources", None) or [])]
    src_html = (f'<p class="empty" style="font-size:12px">sources: '
                f'{html.escape(", ".join(dict.fromkeys(srcs)))}</p>') if srcs else ""
    note = ('<p class="note">model unavailable — showing the matching overview '
            'sections.</p>') if getattr(ans, "error", "") else ""
    return f'<div class="answer">{note}{body}{src_html}</div>'


# --------------------------------------------------------------------------- #
# Local model management (Settings): pure detection + a single-flight downloader
# --------------------------------------------------------------------------- #
_BUNDLED_ENDPOINT = "http://127.0.0.1:8080"   # where `tl local serve` binds by default
_pull_lock = threading.Lock()


def _pull_status_path(models_dir: Path) -> Path:
    return models_dir / ".pull_status.json"


def read_pull_status(models_dir: Path) -> dict[str, Any] | None:
    """The current/last background download's progress, or None. Written by the pull thread,
    read by the settings page so the browser can watch it. Never raises."""
    try:
        return json.loads(_pull_status_path(models_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_pull_status(models_dir: Path, data: dict[str, Any]) -> None:
    try:
        models_dir.mkdir(parents=True, exist_ok=True)
        _pull_status_path(models_dir).write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def start_background_pull(ref: str, *, quant: str | None, models_dir: Path) -> bool:
    """Download a GGUF in a daemon thread, tracking progress in ``.pull_status.json`` so the
    synchronous dashboard stays responsive. Single-flight: returns False if one is already
    running. The thread never raises out (a failure is recorded as ``state=error``)."""
    from throughlog.llm import local as L
    if not _pull_lock.acquire(blocking=False):
        return False
    _write_pull_status(models_dir, {"ref": ref, "state": "downloading", "done": 0, "total": 0})

    def _run() -> None:
        try:
            def prog(done: int, total: int) -> None:
                _write_pull_status(models_dir, {"ref": ref, "state": "downloading",
                                                "done": done, "total": total})
            path = L.pull(ref, quant=quant, dest_dir=models_dir, progress=prog)
            _write_pull_status(models_dir, {"ref": ref, "state": "done",
                                            "file": str(path), "name": Path(path).name})
        except Exception as exc:                     # never crash the daemon thread
            _write_pull_status(models_dir, {"ref": ref, "state": "error", "error": str(exc)})
        finally:
            _pull_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return True


def local_model_choices(cfg: dict[str, Any], models_dir: Path, *,
                        opener: Any = None) -> list[dict[str, Any]]:
    """The local models the user can pick from — downloaded GGUFs (served via `tl local serve`)
    and any a running Ollama exposes. Pure over the filesystem + an injected opener, so it is
    unit-testable with no network. Each option's value encodes both the endpoint and the model
    id, so choosing it is unambiguous."""
    from throughlog.llm import local as L
    llm = cfg.get("llm") or {}
    is_local = str(llm.get("provider", "")).lower() == "local"
    cur_model = str(llm.get("local_model", ""))
    cur_endpoint = str(llm.get("local_endpoint", "")).rstrip("/")
    out: list[dict[str, Any]] = []
    for r in L.installed_models(models_dir):
        active = is_local and cur_model == L.DEFAULT_ALIAS and "8080" in cur_endpoint
        out.append({"value": f"bundled::{r['file']}", "kind": "bundled",
                    "endpoint": _BUNDLED_ENDPOINT, "model": L.DEFAULT_ALIAS, "file": r["file"],
                    "label": f"{r['name']} — {r['size_bytes'] / 1e9:.1f} GB (downloaded)",
                    "active": active})
    for r in L.ollama_models(opener=opener):
        active = is_local and cur_model == r["name"] and "11434" in cur_endpoint
        out.append({"value": f"ollama::{r['name']}", "kind": "ollama",
                    "endpoint": L.DEFAULT_OLLAMA, "model": r["name"],
                    "label": f"{r['name']} — Ollama", "active": active})
    return out


def local_settings_card(cfg: dict[str, Any], token: str, *,
                        models_dir: Path | None = None, opener: Any = None) -> str:
    """The "Local model" settings card: pick a detected model, download a new one (background,
    with progress), test the connection. Composes the pure detectors above + the shared form
    helpers; config writes go through the same guarded ``appconfig.update_llm`` chokepoint."""
    from throughlog.llm import local as L
    md = models_dir or L.models_dir()
    llm = cfg.get("llm") or {}
    is_local = str(llm.get("provider", "")).lower() == "local"
    choices = local_model_choices(cfg, md, opener=opener)
    reg = L.load_registry()
    status = read_pull_status(md)

    parts = ['<section class="card"><h2 class="sech" style="margin-top:0">Local model '
             '<span class="empty" style="font-weight:400">(no key — nothing leaves your '
             'machine)</span></h2>'
             '<p class="empty" style="font-size:13px">Run the model on this computer instead '
             'of the cloud. Easiest: install <b>Ollama</b> and its models appear below. Or '
             'download a small model here (served by <code>tl local serve</code>).</p>']

    cur = (f' → <code>{html.escape(str(llm.get("local_model", "")))}</code> at '
           f'<code>{html.escape(str(llm.get("local_endpoint", "")))}</code>' if is_local else "")
    parts.append(f'<p class="meta">Backend now: '
                 f'<code>{html.escape(str(llm.get("provider", "openrouter")))}</code>{cur}</p>')

    if status and status.get("state") == "downloading":
        done, total = int(status.get("done", 0)), int(status.get("total", 0))
        pct = (done / total * 100) if total else 0
        parts.append(
            f'<p class="note">Downloading <code>{html.escape(str(status.get("ref", "")))}</code> — '
            f'{done / 1e9:.2f}/{(total / 1e9 if total else 0):.2f} GB ({pct:.0f}%)… '
            'this page refreshes automatically.'
            '<script>setTimeout(function(){location.href="/settings";},2500);</script></p>')
    elif status and status.get("state") == "error":
        parts.append(f'<p class="note">Last download failed: '
                     f'{html.escape(str(status.get("error", "")))}</p>')
    elif status and status.get("state") == "done":
        parts.append(f'<p class="ok">Downloaded <code>{html.escape(str(status.get("name", "")))}'
                     '</code> — select it below and click <b>Use</b>.</p>')

    if choices:
        opts = "".join(
            f'<option value="{html.escape(c["value"], quote=True)}"'
            f'{" selected" if c["active"] else ""}>'
            f'{html.escape(c["label"])}{"  ✓ active" if c["active"] else ""}</option>'
            for c in choices)
        parts.append(
            '<form method="post" action="/settings/local/use">' + _hidden_token(token) +
            '<span class="fld"><label for="choice">Detected models</label>'
            f'<select id="choice" name="choice">{opts}</select></span>'
            '<button class="btn primary">Use selected</button> '
            '<button class="btn" formaction="/settings/local/test">Test connection</button> '
            '<button class="btn" formaction="/settings/local/serve">Start local server</button>'
            '</form>')
    else:
        parts.append('<p class="empty" style="font-size:13px">No local models detected yet — '
                     'download one below, or install Ollama and pull a model.</p>')

    mopts = "".join(
        f'<option value="{html.escape(m["id"], quote=True)}">'
        f'{html.escape(m.get("name", m["id"]))} — ~{m.get("approx_gb", "?")} GB — '
        f'{html.escape(str(m.get("license", "")))}</option>'
        for m in reg.get("models", []))
    parts.append(
        '<hr><form method="post" action="/settings/local/pull">' + _hidden_token(token) +
        '<span class="fld"><label for="curated">Download a curated model</label>'
        f'<select id="curated" name="curated">{mopts}</select></span>'
        '<span class="fld"><label for="hf_ref">…or any Hugging Face GGUF (optional)</label>'
        '<input type="text" id="hf_ref" name="hf_ref" autocomplete="off" '
        'placeholder="hf.co/&lt;org&gt;/&lt;repo&gt;[:Q4_K_M]"></span>'
        '<button class="btn">Download</button>'
        '<span class="hint">Saves to ~/.throughlog/models and downloads in the background.</span>'
        '</form>'
        '<p class="empty" style="font-size:12px">Serving a downloaded model needs '
        '<code>pip install "throughlog[local]"</code>; Ollama models need no serve step.</p>'
        '</section>')
    return "".join(parts)


def settings_html(cfg: dict[str, Any], projects: list[dict[str, Any]], *,
                  token: str, key_set: bool = False,
                  automation: dict[str, Any] | None = None, saved: str = "",
                  added: str = "", error: str = "", note: str = "") -> str:
    """The settings screen: LLM, privacy toggles, automation (autostart + nightly
    synthesis), and a merge-only project adder. The API key is write-only — never
    rendered back, only a "set" hint."""
    llm = cfg.get("llm") or {}
    priv = cfg.get("privacy") or {}
    syn = cfg.get("synthesis") or {}
    ini = cfg.get("init") or {}

    def _t(v: Any) -> str:
        return html.escape("" if v is None else str(v), quote=True)

    def _checkbox(name: str, label: str, on: bool) -> str:
        ck = " checked" if on else ""
        return (f'<span class="fld row"><input type="checkbox" id="{name}" '
                f'name="{name}" value="1"{ck}><label for="{name}">{label}</label></span>')

    def _select(name: str, label: str, current: str,
                options: list[tuple[str, str]]) -> str:
        opts = "".join(
            f'<option value="{v}"{" selected" if v == current else ""}>{html.escape(lbl)}</option>'
            for v, lbl in options)
        return (f'<span class="fld"><label for="{name}">{label}</label>'
                f'<select id="{name}" name="{name}">{opts}</select></span>')

    banner = ""
    if error:
        banner = f'<p class="note">{html.escape(error)}</p>'
    elif note:
        banner = f'<p class="ok">{html.escape(note)}</p>'
    elif saved:
        banner = f'<p class="ok">Saved {html.escape(saved)} settings.</p>'
    elif added:
        banner = (f'<p class="ok">Added project '
                  f'<code>{html.escape(added)}</code> — now being observed.</p>')

    key_hint = ("•••• key is set (leave blank to keep it)"
                if key_set else "paste your OpenRouter / OpenAI-compatible key")

    parts = ["<h1>Settings</h1>", banner]

    # -- LLM (cloud / advanced) --
    prov_cur = str(llm.get("provider", "openrouter")).lower()
    parts.append(
        '<section class="card"><h2 class="sech" style="margin-top:0">Language model'
        '</h2>'
        '<p class="empty" style="font-size:13px">Pick <b>Cloud</b> for a hosted model (needs a '
        'key) or <b>Local</b> to run on this machine (set up in the next card — no key, no '
        'egress). For a <b>hybrid</b>, choose Local and set a cloud <i>fallback model</i> + key '
        'below.</p>'
        '<form method="post" action="/settings/llm">' + _hidden_token(token) +
        _select("provider", "Backend",
                "local" if prov_cur == "local" else "cloud",
                [("cloud", "Cloud — OpenRouter / OpenAI-compatible"),
                 ("local", "Local — a model on this machine")]) +
        f'<span class="fld"><label for="api_key">API key '
        f'<span class="empty" style="font-weight:400">(cloud / fallback only)</span></label>'
        f'<input type="password" id="api_key" name="api_key" autocomplete="off" '
        f'placeholder="{html.escape(key_hint, quote=True)}"></span>'
        f'<span class="fld"><label for="model">Model</label>'
        f'<input type="text" id="model" name="model" value="{_t(llm.get("model"))}"></span>'
        f'<span class="fld"><label for="base_url">Base URL</label>'
        f'<input type="text" id="base_url" name="base_url" '
        f'value="{_t(llm.get("base_url"))}"></span>'
        f'<span class="fld"><label for="model_fallback">Cloud fallback model '
        f'<span class="empty" style="font-weight:400">(optional; enables hybrid when local is '
        f'primary)</span></label>'
        f'<input type="text" id="model_fallback" name="model_fallback" '
        f'value="{_t(llm.get("model_fallback"))}"></span>'
        f'<span class="fld"><label for="max_requests_per_min">Max requests / min '
        f'<span class="empty" style="font-weight:400">(0 = no limit; paces calls to '
        f'stay under a free-tier rate — never drops one; ignored for local)</span></label>'
        f'<input type="number" min="0" id="max_requests_per_min" '
        f'name="max_requests_per_min" '
        f'value="{_t(llm.get("max_requests_per_min", 0))}"></span>'
        + _reasoning_select(llm.get("reasoning_effort")) +
        '<button class="btn primary">Save model settings</button></form></section>'
    )

    # -- Local model (download / pick / test) --
    parts.append(local_settings_card(cfg, token))

    # -- Privacy --
    parts.append(
        '<section class="card"><h2 class="sech" style="margin-top:0">Privacy</h2>'
        '<p class="empty" style="font-size:13px">All default OFF. Diffs/clipboard '
        'previews are scrubbed and size-capped, and never leave your machine.</p>'
        '<form method="post" action="/settings/privacy">' + _hidden_token(token) +
        _checkbox("capture_diffs", "Capture scrubbed file diffs", bool(priv.get("capture_diffs"))) +
        _checkbox("clipboard_preview", "Keep a scrubbed clipboard preview",
                  bool(priv.get("clipboard_preview"))) +
        f'<span class="fld"><label for="diff_max_lines">Max diff lines</label>'
        f'<input type="number" id="diff_max_lines" name="diff_max_lines" '
        f'value="{_t(priv.get("diff_max_lines", 400))}"></span>'
        f'<span class="fld"><label for="diff_max_bytes">Max diff bytes</label>'
        f'<input type="number" id="diff_max_bytes" name="diff_max_bytes" '
        f'value="{_t(priv.get("diff_max_bytes", 65536))}"></span>'
        '<button class="btn primary">Save privacy settings</button></form></section>'
    )

    # -- Journal & summaries --
    budget_presets = [("auto", "Auto — match my model (recommended)"),
                      ("6000", "Balanced — ~6k tokens (free models)"),
                      ("16000", "Large — ~16k tokens (capable models)"),
                      ("200000", "Maximum — feed everything raw (big-context models)"),
                      ("0", "Legacy — condense very large days")]
    budget_cur = str(syn.get("max_input_tokens", "auto")).strip().lower()
    if budget_cur not in {v for v, _ in budget_presets}:
        budget_cur = "auto"
    parts.append(
        '<section class="card"><h2 class="sech" style="margin-top:0">Journal &amp; '
        'summaries</h2>'
        '<p class="empty" style="font-size:13px">The detailed entries keep the specifics '
        '(values tried, results, decisions); the living overview stays high-level. A period '
        'summary distils a week or month across all projects (needs an LLM key).</p>'
        '<form method="post" action="/settings/synthesis">' + _hidden_token(token) +
        _checkbox("write_entries", "Write the detailed entries",
                  bool(syn.get("write_entries", True))) +
        _select("entry_batch", "How often to call the LLM for entries",
                str(syn.get("entry_batch", "day")).lower(),
                [("adaptive", "Smart — batch quiet days, split busy ones (recommended)"),
                 ("week", "Once a week per project (fewest calls)"),
                 ("day", "Every day (most calls)")]) +
        _select("max_input_tokens", "Detail packed into each LLM call", budget_cur,
                budget_presets) +
        f'<span class="fld"><label for="max_batch_days">Never wait longer than (days) '
        f'<span class="empty" style="font-weight:400">(caps how long smart/weekly '
        f'batching may hold a day before it is summarized)</span></label>'
        f'<input type="number" min="1" id="max_batch_days" name="max_batch_days" '
        f'value="{_t(syn.get("max_batch_days", 7))}"></span>'
        + _select("entry_period", "Journal file grouping",
                  str(syn.get("entry_period", "month")).lower(),
                  [("month", "Monthly  (entries/YYYY-MM.md)"),
                   ("week", "Weekly  (entries/YYYY-Www.md)")]) +
        _select("summary_cadence", "Period summary",
                str(syn.get("summary_cadence", "off")).lower(),
                [("off", "Off"), ("weekly", "Weekly"), ("monthly", "Monthly")]) +
        _checkbox("skip_unchanged", "Skip re-synthesizing unchanged projects "
                  "(saves LLM calls on re-runs)", bool(syn.get("skip_unchanged", False))) +
        '<button class="btn primary">Save journal settings</button></form></section>'
    )

    # -- Automation (no-admin autostart + in-app nightly synthesis) --
    auto = automation or {}
    cap_on = bool(auto.get("capture"))
    syn_on = bool(auto.get("synthesis"))
    syn_time = str(auto.get("synthesis_time") or "22:30")
    tok = _hidden_token(token)

    def _pill(on: bool, label_on: str) -> str:
        return (f'<span class="ok" style="font-size:13px">{label_on}</span>'
                if on else '<span class="meta">Off</span>')

    if cap_on:
        cap_ctl = (f'<form method="post" action="/settings/autostart">{tok}'
                   '<input type="hidden" name="action" value="disable">'
                   '<button class="btn warn">Turn off</button></form>')
    else:
        cap_ctl = (f'<form method="post" action="/settings/autostart">{tok}'
                   '<input type="hidden" name="action" value="enable">'
                   + _checkbox("tray", "Show a tray icon (otherwise it runs hidden)",
                               False) +
                   '<button class="btn primary">Turn on</button></form>')

    syn_off = (f'<form method="post" action="/settings/schedule">{tok}'
               '<input type="hidden" name="action" value="disable">'
               '<button class="btn warn">Turn off</button></form>') if syn_on else ""
    syn_ctl = (
        f'<form method="post" action="/settings/schedule">{tok}'
        '<input type="hidden" name="action" value="enable">'
        '<span class="fld"><label for="time">Time (HH:MM)</label>'
        f'<input type="text" id="time" name="time" value="{_t(syn_time)}" '
        'placeholder="22:30" style="max-width:7em"></span>'
        f'<button class="btn primary">{"Update time" if syn_on else "Turn on"}'
        '</button></form>')

    parts.append(
        '<section class="card"><h2 class="sech" style="margin-top:0">Automation</h2>'
        '<p class="empty" style="font-size:13px">Set it and forget it — no admin '
        'rights needed, no terminal stays open. <b>Start at logon</b> drops a shortcut '
        'in your Startup folder that launches the app hidden in the background. '
        '<b>Nightly synthesis</b> runs inside that always-on app at the time you pick '
        '(so it needs the app to be running — which it is, once start-at-logon is on).'
        '</p>'
        f'<div class="proj"><div><b>Start capturing at logon</b>'
        f'<div class="meta">{_pill(cap_on, "On — runs hidden in the background")}</div>'
        f'</div>{cap_ctl}</div>'
        f'<div class="proj"><div><b>Synthesize the journal every night</b>'
        f'<div class="meta">{_pill(syn_on, "On — runs nightly at " + html.escape(syn_time))}'
        f'</div></div>{syn_off}</div>{syn_ctl}</section>'
    )

    # -- Projects --
    _dow = [("daily", "Every day"), ("mon", "Mon"), ("tue", "Tue"), ("wed", "Wed"),
            ("thu", "Thu"), ("fri", "Fri"), ("sat", "Sat"), ("sun", "Sun")]

    def _day_picker(pid: str, cur: str) -> str:
        cur = cur if cur in {v for v, _ in _dow} else "daily"
        opts = "".join(f'<option value="{v}"{" selected" if v == cur else ""}>{lbl}</option>'
                       for v, lbl in _dow)
        pid_q = html.escape(pid, quote=True)
        return (f'<form method="post" action="/settings/project-synthesis" '
                f'style="display:flex;gap:6px;align-items:center;margin:0">'
                + _hidden_token(token) +
                f'<input type="hidden" name="project_id" value="{pid_q}">'
                f'<label for="day_{pid_q}" class="meta">Synthesize on</label>'
                f'<select id="day_{pid_q}" name="day">{opts}</select>'
                f'<button class="btn">Save</button></form>')

    rows = []
    for p in projects:
        sigs = p.get("signals", {}) or {}
        path0 = (sigs.get("paths") or [""])[0]
        cur_day = str((p.get("synthesis") or {}).get("day", "daily")).strip().lower()
        rows.append(
            f'<div class="proj"><div><b>{html.escape(p.get("name", p.get("id", "")))}</b>'
            f'<div class="meta">{html.escape(path0)}</div></div>'
            f'{_day_picker(p.get("id", ""), cur_day)}</div>')
    proj_list = "".join(rows) or '<p class="empty">No projects yet.</p>'
    parts.append(
        '<section class="card"><h2 class="sech" style="margin-top:0">Projects</h2>'
        '<p class="empty" style="font-size:13px">A project\'s folder is what makes it '
        'observable. Adding one widens the privacy allowlist — you\'ll confirm first. '
        '"Synthesize on" spreads projects\' LLM calls across the week — the deterministic '
        'archive still updates every night.</p>' + proj_list +
        '<form method="post" action="/settings/init" style="margin-top:14px">'
        + _hidden_token(token) +
        _checkbox("llm_enrich", "Use the LLM to fill in keywords &amp; description for new "
                  "projects — sends folder structure + README only, never file contents",
                  bool(ini.get("llm_enrich"))) +
        '<button class="btn">Save enrichment setting</button></form>'
        '<form method="post" action="/settings/project" style="margin-top:14px">'
        + _hidden_token(token) +
        '<span class="fld"><label for="folder">Add a project folder (a git repo)'
        '</label><input type="text" id="folder" name="folder" '
        'placeholder="C:\\Users\\you\\projects\\my-repo"></span>'
        '<button class="btn primary">Add project</button></form>'
        '<hr><p class="empty" style="font-size:13px">Or point at a folder that '
        '<i>contains</i> several repos and add them all at once (you\'ll confirm '
        'before anything is observed).</p>'
        '<form method="post" action="/settings/scan">'
        + _hidden_token(token) +
        '<span class="fld"><label for="root">Scan a folder for git repos</label>'
        '<input type="text" id="root" name="root" '
        'placeholder="C:\\Users\\you\\projects"></span>'
        '<button class="btn">Scan for projects</button></form></section>'
    )
    return "".join(parts)


def confirm_scan_html(root: str, entries: list[dict[str, Any]], token: str) -> str:
    """Confirmation interstitial before a folder scan adds several projects at once."""
    if not entries:
        return ('<h1>Scan</h1><section class="card"><p class="empty">No new git repos '
                f'found under <code>{html.escape(root)}</code> (already-tracked repos '
                'are skipped).</p><p><a class="btn" href="/settings">Back</a></p>'
                '</section>')
    items = "".join(
        f'<li><b>{html.escape(e.get("name", e.get("id", "")))}</b> — '
        f'<code>{html.escape((e.get("signals", {}) or {}).get("paths", [""])[0])}</code>'
        '</li>' for e in entries)
    return (
        '<h1>Confirm — add these projects?</h1>'
        f'<section class="card"><p>Scanning <code>{html.escape(root)}</code> found '
        f'{len(entries)} new repo(s). Adding them makes their folders observable by '
        'capture (subject to the privacy gate):</p>'
        f'<ul>{items}</ul>'
        '<form method="post" action="/settings/scan">'
        f'{_hidden_token(token)}'
        f'<input type="hidden" name="root" value="{html.escape(root, quote=True)}">'
        '<input type="hidden" name="confirm" value="1">'
        '<button class="btn primary">Yes, add them</button> '
        '<a class="btn" href="/settings">Cancel</a></form></section>'
    )


def confirm_widen_html(folder: str, delta: list[str], token: str) -> str:
    """Confirmation interstitial shown before a project add widens the allowlist."""
    items = "".join(f"<li><code>{html.escape(d)}</code></li>" for d in delta)
    return (
        '<h1>Confirm — this widens what is observed</h1>'
        '<section class="card"><p>Adding this project will make the following '
        'directory observable by capture (files there can be recorded, subject to '
        'the privacy gate):</p>'
        f'<ul>{items}</ul>'
        '<form method="post" action="/settings/project">'
        f'{_hidden_token(token)}'
        f'<input type="hidden" name="folder" value="{html.escape(folder, quote=True)}">'
        '<input type="hidden" name="confirm" value="1">'
        '<button class="btn primary">Yes, add it</button> '
        '<a class="btn" href="/settings">Cancel</a></form></section>'
    )


# --------------------------------------------------------------------------- #
# Controller + request guards (used by the thin HTTP driver)
# --------------------------------------------------------------------------- #
class Controller:
    """Optional live-capture handle the dashboard acts through. ``tl up`` passes one
    bound to the running supervisor; plain ``tl serve`` gets a capture-less default
    (Pause/Resume hidden, but Synthesize still works via a subprocess)."""

    def __init__(self, *, supervisor: Any = None,
                 on_synthesize: Any = None) -> None:
        self._sup = supervisor
        self._on_synthesize = on_synthesize
        self._httpd: Any = None

    def bind_server(self, httpd: Any) -> None:
        """``serve`` hands the live HTTPServer here so the dashboard can shut the app
        down from a request (the fool-proof Quit when there's no console/tray)."""
        self._httpd = httpd

    @property
    def has_capture(self) -> bool:
        return self._sup is not None

    @property
    def can_quit(self) -> bool:
        return self._httpd is not None

    def quit(self) -> None:
        """Stop capture and shut the server down. The actual shutdown runs on a side
        thread because ``HTTPServer.shutdown`` blocks until ``serve_forever`` returns —
        which can't happen from inside the request thread that's calling us."""
        sup, httpd = self._sup, self._httpd

        def _shutdown() -> None:
            try:
                if sup is not None:
                    sup.stop()
            except Exception:
                pass
            try:
                if httpd is not None:
                    httpd.shutdown()
            except Exception:
                pass

        import threading
        threading.Thread(target=_shutdown, name="tl-quit", daemon=True).start()

    def paused(self) -> bool:
        return bool(self._sup and self._sup.paused.is_set())

    def toggle_pause(self) -> None:
        if not self._sup:
            return
        self._sup.toggle_pause()
        try:
            self._sup.write_status()       # update the badge immediately
        except Exception:
            pass

    def synthesize(self) -> None:
        if self._on_synthesize is not None:
            self._on_synthesize()
            return
        import subprocess
        import sys
        try:                               # detached; never blocks the HTTP request
            subprocess.Popen([sys.executable, "-m", "throughlog.cli", "synthesize"],
                             cwd=str(BASE_DIR))
        except Exception:
            pass


def csrf_ok(headers: Any, form: dict[str, list[str]], token: str) -> bool:
    """Reject forged cross-site POSTs to our localhost server. Two independent
    checks: (1) if an ``Origin`` is present it must match our own host:port; and
    (2) the form must carry our per-process secret token (a cross-site page can't
    read our same-origin HTML, so it can't learn it)."""
    origin = headers.get("Origin")
    if origin:
        host = headers.get("Host", "")
        if origin != f"http://{host}":
            return False
    submitted = (form.get("_token", [""]) or [""])[0]
    return bool(token) and secrets.compare_digest(submitted, token)


def _safe_load_projects() -> list[dict[str, Any]]:
    try:
        return load_projects()
    except Exception:
        return []


def _llm_client(cfg: dict[str, Any]) -> Any:
    """Build an LLM client for the read-only `ask` path, or None when no key
    resolves. Egress is still re-scrubbed inside ``client.chat``."""
    try:
        from throughlog.llm.client import LLMConfig, LLMClient
        c = LLMConfig.from_config(cfg)
        if c.resolve_key() or c.is_local:     # a local endpoint needs no key
            return LLMClient(c)
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# Automation — OS-level "set it and forget it" tasks (capture at logon, nightly
# synthesis). Thin wrappers over ``throughlog.deploy`` so the settings page can toggle
# them and tests can patch them without touching the real scheduler.
# --------------------------------------------------------------------------- #
_HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")   # 24h HH:MM, leading 0 optional


def automation_state() -> dict[str, Any]:
    """Current automation settings for the settings page. Read-only and fully guarded
    — any error degrades to 'off' so the page always renders.

    ``capture`` is whether the no-admin at-logon launcher is installed (a Startup-folder
    shortcut on Windows; launchd/cron elsewhere). ``synthesis``/``synthesis_time`` is
    the in-app nightly time from config (``schedule.synthesize_at``) — also no-admin,
    honored by the always-on ``tl up`` process."""
    from throughlog import deploy, appconfig
    state: dict[str, Any] = {"capture": False, "synthesis": False,
                             "synthesis_time": "22:30"}
    try:
        state["capture"] = deploy.task_status(deploy.CAPTURE_TASK)[0]
    except Exception:
        pass
    try:
        t = appconfig.nightly_time()
        if t:
            state["synthesis"] = True
            state["synthesis_time"] = t
    except Exception:
        pass
    return state


def _set_autostart(enable: bool, *, tray: bool = False) -> tuple[bool, str]:
    """Enable/disable capture-at-logon (no admin — Startup folder on Windows).
    Returns ``(ok, message)``."""
    from throughlog import deploy
    return (deploy.enable_autostart(tray=tray) if enable
            else deploy.disable_autostart())


def _set_nightly(enable: bool, *, time_hhmm: str = "22:30") -> tuple[bool, str]:
    """Set/clear the in-app nightly-synthesis time in config (no admin; runs while the
    app is open). Returns ``(ok, message)``."""
    from throughlog import appconfig
    try:
        appconfig.update_schedule(time_hhmm if enable else None)
        return True, ""
    except Exception as exc:
        return False, str(exc)


# --------------------------------------------------------------------------- #
# HTTP driver (thin)
# --------------------------------------------------------------------------- #
def build_handler(journal_dir: Path, data_dir_path: Path, registry: dict[str, str],
                  *, projects: list[dict[str, Any]] | None = None,
                  controller: Controller | None = None,
                  csrf_token: str | None = None):
    from http.server import BaseHTTPRequestHandler

    ctrl = controller or Controller()
    token = csrf_token or secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        # -- low-level senders ---------------------------------------------- #
        def _send(self, body: str, *, code: int = 200,
                  ctype: str = "text/html; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_font(self) -> None:
            # Self-host the bundled Space Grotesk (OFL) — no external CDN, so the
            # dashboard renders its wordmark/headings offline and never phones home.
            # Missing file (e.g. a non-editable install without assets/) => 404, and
            # the CSS font stack falls back to system fonts; the page still works.
            font_path = BASE_DIR / "assets" / "fonts" / "SpaceGrotesk-Variable.ttf"
            try:
                data = font_path.read_bytes()
            except OSError:
                self._send("not found", code=404, ctype="text/plain; charset=utf-8")
                return
            self.send_response(200)
            self.send_header("Content-Type", "font/ttf")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)

        def _page(self, title: str, body: str, *, active: str = "",
                  code: int = 200) -> None:
            projects_nav = discover_projects(journal_dir, registry)
            status = read_status(data_dir_path)
            self._send(render_page(title, body, projects=projects_nav, active=active,
                                   status=status), code=code)

        def _redirect(self, location: str) -> None:
            self.send_response(303)            # POST -> GET, so reloads don't re-POST
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        # -- shared builders ------------------------------------------------ #
        def _cfg(self) -> dict[str, Any]:
            try:
                return load_config()
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return {}

        def _chart_svg(self) -> str:
            try:
                date_key = datetime.now().strftime("%Y%m%d")
                events = timeline_events(data_dir_path, date_key)
                plist = projects if projects is not None else _safe_load_projects()
                return project_time_svg(project_durations(events, plist))
            except Exception:
                return ""

        def _render_overview(self, *, question: str = "", ans_html: str = "") -> None:
            projects_nav = discover_projects(journal_dir, registry)
            controls = control_bar_html(token, has_capture=ctrl.has_capture,
                                        paused=ctrl.paused(), can_quit=ctrl.can_quit)
            ask = ask_box_html(token, question=question, answer_html=ans_html)
            body = overview_html(journal_dir, projects_nav, controls_html=controls,
                                 chart_svg=self._chart_svg(), ask_html=ask)
            self._page("Overview", body, active="overview")

        def _render_settings(self, *, saved: str = "", added: str = "",
                             error: str = "", note: str = "", code: int = 200) -> None:
            from throughlog import appconfig
            cfg = self._cfg()
            self._page("Settings",
                       settings_html(cfg, _safe_load_projects(), token=token,
                                     key_set=appconfig.key_is_set(cfg),
                                     automation=automation_state(),
                                     saved=saved, added=added, error=error, note=note),
                       active="settings", code=code)

        # -- GET ------------------------------------------------------------ #
        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            path = u.path
            try:
                if path == "/":
                    self._render_overview()
                elif path == "/timeline":
                    qs = parse_qs(u.query)
                    date_key = (qs.get("date", [""])[0]
                                or datetime.now().strftime("%Y%m%d"))
                    self._page("Timeline",
                               timeline_html(timeline_events(data_dir_path, date_key),
                                             data_dir_path),
                               active="timeline")
                elif path == "/settings":
                    qs = parse_qs(u.query)
                    self._render_settings(saved=qs.get("saved", [""])[0],
                                          added=qs.get("added", [""])[0])
                elif path.startswith("/project/"):
                    pid = unquote(path[len("/project/"):])
                    name = registry.get(pid, pid)
                    self._page(name, project_html(journal_dir, pid), active=pid)
                elif path.startswith("/archive/"):
                    pid = unquote(path[len("/archive/"):])
                    self._page("Archive", archive_html(journal_dir, pid), active=pid)
                elif path.startswith("/entries/"):
                    parts = [unquote(p) for p in path[len("/entries/"):].split("/") if p]
                    pid = parts[0] if parts else ""
                    month = parts[1] if len(parts) > 1 else None
                    self._page(registry.get(pid, pid),
                               entries_html(journal_dir, pid, month), active=pid)
                elif path == "/summaries":
                    self._page("Summaries", summary_html(journal_dir), active="summaries")
                elif path.startswith("/summary/"):
                    period = unquote(path[len("/summary/"):].strip("/").split("/")[0])
                    self._page("Summaries", summary_html(journal_dir, period),
                               active="summaries")
                elif path == "/api/status":
                    self._send(json.dumps(read_status(data_dir_path) or {}),
                               ctype="application/json")
                elif path == "/healthz":
                    self._send("ok", ctype="text/plain; charset=utf-8")
                elif path == "/static/space-grotesk.ttf":
                    self._send_font()
                else:
                    self._page("Not found", _error_card("404 — no such page."),
                               code=404)
            except Exception as exc:  # never 500 with a stack trace to the browser
                self._page("Error", _error_card(str(exc)), code=500)

        # -- POST (all mutating routes; CSRF-guarded) ----------------------- #
        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length > 0 else ""
            form = parse_qs(raw)

            if not csrf_ok(self.headers, form, token):
                self._page("Forbidden",
                           _error_card("Bad or missing form token — reload the page "
                                       "and try again."), code=403)
                return

            def field(key: str, default: str = "") -> str:
                return (form.get(key, [default]) or [default])[0]

            try:
                from throughlog import appconfig
                if path == "/ask":
                    self._handle_ask(field("q").strip())
                elif path in ("/action/pause", "/action/resume"):
                    ctrl.toggle_pause()
                    self._redirect("/")
                elif path == "/action/synthesize":
                    ctrl.synthesize()
                    self._redirect("/")
                elif path == "/action/quit":
                    if ctrl.can_quit:
                        self._page("Stopped", _error_card(
                            "The app is shutting down — capture stopped. You can "
                            "close this tab."))
                        ctrl.quit()
                    else:
                        self._redirect("/")
                elif path == "/settings/llm":
                    patch: dict[str, Any] = {
                        "model": field("model").strip() or None,
                        "base_url": field("base_url").strip() or None,
                        "model_fallback": field("model_fallback").strip(),
                    }
                    mode = field("provider").strip().lower()
                    if mode == "local":
                        patch["provider"] = "local"
                    elif mode == "cloud":          # leave a non-openrouter cloud provider intact
                        cur = str((self._cfg().get("llm") or {}).get("provider", "")).lower()
                        patch["provider"] = "openrouter" if cur in ("", "local") else cur
                    effort = field("reasoning_effort").strip().lower()
                    if effort in ("", "low", "medium", "high"):
                        patch["reasoning_effort"] = effort     # "" clears -> default
                    rpm = field("max_requests_per_min").strip()
                    if rpm.isdigit():                          # 0 = no limit
                        patch["max_requests_per_min"] = int(rpm)
                    key = field("api_key").strip()
                    if key:                       # blank -> keep the existing key
                        patch["api_key"] = key
                    appconfig.update_llm(patch)
                    self._redirect("/settings?saved=model")
                elif path == "/settings/local/use":
                    self._handle_local_use(field("choice").strip())
                elif path == "/settings/local/pull":
                    ref = field("hf_ref").strip() or field("curated").strip()
                    self._handle_local_pull(ref, field("quant").strip() or None)
                elif path == "/settings/local/test":
                    self._handle_local_test()
                elif path == "/settings/local/serve":
                    self._handle_local_serve()
                elif path == "/settings/privacy":
                    patch = {
                        "capture_diffs": "capture_diffs" in form,
                        "clipboard_preview": "clipboard_preview" in form,
                    }
                    for k in ("diff_max_lines", "diff_max_bytes"):
                        v = field(k).strip()
                        if v.isdigit():
                            patch[k] = int(v)
                    appconfig.update_privacy(patch)
                    self._redirect("/settings?saved=privacy")
                elif path == "/settings/synthesis":
                    period = field("entry_period").strip().lower()
                    cadence = field("summary_cadence").strip().lower()
                    batch = field("entry_batch").strip().lower()
                    budget = field("max_input_tokens").strip().lower()
                    days = field("max_batch_days").strip()
                    patch = {"write_entries": "write_entries" in form,
                             "skip_unchanged": "skip_unchanged" in form}
                    if period in ("month", "week"):
                        patch["entry_period"] = period
                    if cadence in ("off", "weekly", "monthly"):
                        patch["summary_cadence"] = cadence
                    if batch in ("day", "week", "adaptive"):
                        patch["entry_batch"] = batch
                    if budget == "auto":
                        patch["max_input_tokens"] = "auto"
                    elif budget.isdigit():
                        patch["max_input_tokens"] = int(budget)
                    if days.isdigit() and int(days) >= 1:
                        patch["max_batch_days"] = int(days)
                    appconfig.update_synthesis(patch)
                    self._redirect("/settings?saved=journal")
                elif path == "/settings/project-synthesis":
                    appconfig.update_project_synthesis(
                        field("project_id").strip(), field("day").strip().lower())
                    self._redirect("/settings?saved=schedule")
                elif path == "/settings/init":
                    appconfig.update_init({"llm_enrich": "llm_enrich" in form})
                    self._redirect("/settings?saved=enrichment")
                elif path == "/settings/autostart":
                    enable = field("action") == "enable"
                    ok, msg = _set_autostart(enable, tray=("tray" in form))
                    if ok:
                        self._redirect("/settings?saved=automation")
                    else:
                        self._render_settings(
                            error=msg or "Could not update autostart.", code=400)
                elif path == "/settings/schedule":
                    if field("action") == "disable":
                        ok, msg = _set_nightly(False)
                    else:
                        t = field("time").strip() or "22:30"
                        if not _HHMM.match(t):
                            self._render_settings(
                                error=f"Invalid time: {t!r} — use HH:MM (e.g. 22:30).",
                                code=400)
                            return
                        ok, msg = _set_nightly(True, time_hhmm=t)
                    if ok:
                        self._redirect("/settings?saved=automation")
                    else:
                        self._render_settings(
                            error=msg or "Could not update the schedule.", code=400)
                elif path == "/settings/project":
                    self._handle_add_project(field("folder").strip(),
                                             confirmed=field("confirm") == "1")
                elif path == "/settings/scan":
                    self._handle_scan(field("root").strip(),
                                      confirmed=field("confirm") == "1")
                else:
                    self._page("Not found", _error_card("404 — no such action."),
                               code=404)
            except ValueError as exc:             # validation -> show on settings
                self._render_settings(error=str(exc), code=400)
            except Exception as exc:
                self._page("Error", _error_card(str(exc)), code=500)

        # -- POST helpers (local model) ------------------------------------- #
        def _handle_local_use(self, choice: str) -> None:
            """Point config at a detected model. ``choice`` is ``kind::ref`` from the picker;
            we translate it to provider=local + endpoint + model via the guarded chokepoint."""
            from throughlog.llm import local as L
            kind, _, ref = choice.partition("::")
            if kind == "ollama" and ref:
                L.configure(endpoint=L.DEFAULT_OLLAMA, model=ref)
            elif kind == "bundled" and ref:
                L.configure(endpoint=_BUNDLED_ENDPOINT, model=L.DEFAULT_ALIAS)
            else:
                self._render_settings(error="Pick a model to use.", code=400)
                return
            self._redirect("/settings?saved=model")

        def _handle_local_pull(self, ref: str, quant: str | None) -> None:
            if not ref:
                self._render_settings(error="Choose a curated model or enter a "
                                            "Hugging Face reference.", code=400)
                return
            from throughlog.llm import local as L
            started = start_background_pull(ref, quant=quant, models_dir=L.models_dir())
            if not started:
                self._render_settings(error="A download is already running — let it finish.",
                                      code=409)
                return
            self._redirect("/settings")

        def _handle_local_test(self) -> None:
            from throughlog.llm.client import (LLMConfig, LLMClient, LLMError,
                                               _classify_error)
            c = LLMConfig.from_config(self._cfg())
            client = LLMClient(c)
            targets = client._targets()
            if not targets:
                self._render_settings(error="No model configured to test.", code=400)
                return
            try:
                out = client.probe(targets[0])
                self._render_settings(
                    note=f"Local model reachable ✓ — replied {out.strip()[:40]!r}")
            except LLMError as exc:
                verdict, _ = _classify_error(str(exc))
                self._render_settings(error=f"Test failed — {verdict}", code=200)

        def _handle_local_serve(self) -> None:
            """Best-effort launch of the bundled server over the first downloaded model, so a
            non-technical user can close the loop without a terminal."""
            from throughlog.llm import local as L
            inst = L.installed_models()
            if not inst:
                self._render_settings(error="No downloaded model to serve — download one first "
                                            "(Ollama models need no serve step).", code=400)
                return
            try:
                L.serve(inst[0]["file"], detach=True)
                L.configure(endpoint=_BUNDLED_ENDPOINT, model=L.DEFAULT_ALIAS)
                self._render_settings(
                    note=f"Starting local server for {Path(inst[0]['file']).name} at "
                         f"{_BUNDLED_ENDPOINT}/v1 — give it a moment, then Test connection.")
            except L.LocalError as exc:
                self._render_settings(error=str(exc), code=400)

        # -- POST helpers --------------------------------------------------- #
        def _handle_ask(self, question: str) -> None:
            ans_html = ""
            if question:
                from throughlog import ask as askmod
                corpus = askmod.load_corpus(journal_dir)
                client = _llm_client(self._cfg())
                ans = askmod.answer(question, corpus, client)
                ans_html = answer_html(ans)
            self._render_overview(question=question, ans_html=ans_html)

        def _handle_add_project(self, folder: str, *, confirmed: bool) -> None:
            from throughlog import appconfig
            if not folder:
                self._render_settings(error="Enter a folder path to add.", code=400)
                return
            if not Path(folder).expanduser().is_dir():
                self._render_settings(error=f"Not a folder: {folder}", code=400)
                return
            if not confirmed:
                delta = appconfig.allowlist_delta(folder)
                if delta:                          # widens the allowlist -> confirm
                    self._page("Confirm", confirm_widen_html(folder, delta, token),
                               active="settings")
                    return
            # Opt-in, metadata-only LLM enrichment — only when the setting is on AND a key
            # resolves. The model never sets paths, so the allowlist stays deterministic.
            cfg = self._cfg()
            client = _llm_client(cfg) if appconfig.init_enrich_enabled(cfg) else None
            entry = appconfig.add_project(folder, client=client)
            self._redirect(f"/settings?added={quote(entry['id'])}")

        def _handle_scan(self, root: str, *, confirmed: bool) -> None:
            from throughlog import appconfig
            if not root:
                self._render_settings(error="Enter a folder to scan.", code=400)
                return
            if not Path(root).expanduser().is_dir():
                self._render_settings(error=f"Not a folder: {root}", code=400)
                return
            if not confirmed:                      # preview, then confirm-before-widening
                entries = appconfig.scan_projects(root)
                self._page("Scan", confirm_scan_html(root, entries, token),
                           active="settings")
                return
            added = appconfig.add_scanned_projects(root)
            self._redirect(f"/settings?saved=scan%20({len(added)}%20added)")

        def log_message(self, *args) -> None:   # silence default stderr logging
            pass

    return Handler


def make_server(host: str, port: int, *, journal_dir: Path, data_dir_path: Path,
                registry: dict[str, str],
                projects: list[dict[str, Any]] | None = None,
                controller: Controller | None = None,
                csrf_token: str | None = None):
    """Construct (but do not start) the HTTPServer. Pass port 0 for an ephemeral
    port (used by tests)."""
    from http.server import HTTPServer
    return HTTPServer((host, port),
                      build_handler(journal_dir, data_dir_path, registry,
                                    projects=projects, controller=controller,
                                    csrf_token=csrf_token))


def serve(*, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          journal_dir: str | Path | None = None,
          data_dir_path: str | Path | None = None,
          registry: dict[str, str] | None = None,
          projects: list[dict[str, Any]] | None = None,
          controller: Controller | None = None,
          open_browser: bool = True) -> None:
    """Run the dashboard until Ctrl+C, resolving journal/data dirs from config.

    `registry` (project id -> display name) and `projects` (full registry entries,
    needed for the time-per-project chart) may be supplied to override what is
    derived from projects.json — used by `tl demo` to label the built-in day.
    `controller` wires the live-capture supervisor in (passed by `tl up`)."""
    cfg = load_config() if (BASE_DIR / "config.json").exists() else {}
    ddir = Path(journal_dir) if journal_dir else \
        BASE_DIR / cfg.get("paths", {}).get("journal_dir", "journal")
    datadir = Path(data_dir_path) if data_dir_path else data_dir(cfg)
    if projects is None:
        try:
            projects = load_projects()
        except FileNotFoundError:
            projects = []
    if registry is None:
        registry = {p["id"]: p.get("name", p["id"]) for p in projects}

    httpd = make_server(host, port, journal_dir=ddir, data_dir_path=datadir,
                        registry=registry, projects=projects, controller=controller)
    if controller is not None:
        controller.bind_server(httpd)   # enables the dashboard Quit button
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"[tl] dashboard at {url}  (journal: {ddir})  —  Ctrl+C to stop")
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[tl] dashboard stopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    serve()
