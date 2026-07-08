"""Push outputs — deliver the journal where work happens (`tl report`).

A journal in a folder is passive. This turns the already-synthesized output
(``journal/daily.md`` + ``journal/executive_summary.md``) into a daily standup
message and pushes it to stdout, Slack, or a GitHub issue/PR comment.

These are integration adapters layered *on top of* ``synthesize.py`` output — they
read finished markdown and never touch the deterministic pipeline or an LLM. The
formatters are pure (testable offline); the transports are thin ``urllib`` POSTs
with an injectable ``opener`` so tests run with no network.

    python -m throughlog.cli report                       # today's standup -> stdout
    python -m throughlog.cli report --weekly              # last 7 days
    python -m throughlog.cli report --slack               # -> Slack incoming webhook
    python -m throughlog.cli report --github me/repo#42   # -> a GitHub issue/PR comment
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# --------------------------------------------------------------------------- #
# Parsing the synthesized output
# --------------------------------------------------------------------------- #
@dataclass
class DailySection:
    date: str
    projects: dict[str, str] = field(default_factory=dict)   # project_id -> paragraph


def parse_daily(daily_md: str) -> list[DailySection]:
    """Parse ``daily.md`` into sections (newest first, as written). Each ``## DATE``
    block holds ``**project** — paragraph`` lines terminated by a ``---`` rule."""
    sections: list[DailySection] = []
    cur: DailySection | None = None
    line_re = re.compile(r"\*\*(.+?)\*\*\s*[—–-]\s*(.*)")
    for line in (daily_md or "").splitlines():
        s = line.strip()
        if s.startswith("## "):
            cur = DailySection(date=s[3:].strip())
        elif s == "---":
            if cur is not None:
                sections.append(cur)
            cur = None
        elif cur is not None:
            m = line_re.match(s)
            if m:
                cur.projects[m.group(1)] = m.group(2).strip()
    if cur is not None:
        sections.append(cur)
    return sections


def exec_summary_body(exec_md: str) -> str:
    """The body of ``executive_summary.md`` minus its ``# Executive Summary…`` title."""
    text = (exec_md or "").strip()
    if text.startswith("#"):
        nl = text.find("\n")
        return text[nl + 1:].strip() if nl != -1 else ""
    return text


def select_sections(sections: list[DailySection], *, date: str | None,
                    weekly: bool) -> list[DailySection]:
    """Pick the section(s) to report: a specific date, the newest, or the last 7."""
    if not sections:
        return []
    if date:
        return [s for s in sections if s.date == date]
    if weekly:
        return sections[:7]
    return sections[:1]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
@dataclass
class ReportInputs:
    sections: list[DailySection]
    exec_body: str
    period_summary: str = ""        # synthesized weekly/monthly retrospective body, if any
    period_label: str = ""          # e.g. "2026-W26" / "2026-06"


def _newest_period_summary(journal_dir: Path, granularity: str) -> tuple[str, str]:
    """``(body, period_label)`` of the newest ``summaries/<stem>.md`` matching the
    granularity ('week' stems carry '-W', 'month' stems don't), or ``("", "")``. Lets a
    weekly/monthly report push the real synthesized retrospective instead of regluing
    the daily paragraphs."""
    sdir = Path(journal_dir) / "summaries"
    if not sdir.is_dir():
        return "", ""
    want_week = granularity == "week"
    for stem in sorted((p.stem for p in sdir.glob("*.md")), reverse=True):
        if ("-W" in stem) == want_week:
            try:
                text = (sdir / f"{stem}.md").read_text(encoding="utf-8")
            except OSError:
                continue
            return exec_summary_body(text), stem
    return "", ""


def load_inputs(journal_dir: str | Path, *, date: str | None = None,
                weekly: bool = False, monthly: bool = False) -> ReportInputs:
    d = Path(journal_dir)
    daily = (d / "daily.md").read_text(encoding="utf-8") if (d / "daily.md").exists() else ""
    exec_md = ""
    ep = d / "executive_summary.md"
    if ep.exists():
        exec_md = ep.read_text(encoding="utf-8")
    rollup = weekly or monthly
    sections = select_sections(parse_daily(daily), date=date, weekly=rollup)
    period_summary, period_label = "", ""
    if rollup:                       # prefer the synthesized retrospective when present
        period_summary, period_label = _newest_period_summary(
            d, "month" if monthly else "week")
    return ReportInputs(sections=sections, exec_body=exec_summary_body(exec_md),
                        period_summary=period_summary, period_label=period_label)


# --------------------------------------------------------------------------- #
# Pure formatters
# --------------------------------------------------------------------------- #
def _title(sections: list[DailySection], weekly: bool) -> str:
    if weekly and sections:
        return f"Work summary — {sections[-1].date} → {sections[0].date}"
    if sections:
        return f"Daily standup — {sections[0].date}"
    return "Work summary"


def standup_markdown(inp: ReportInputs, *, weekly: bool = False,
                     include_exec: bool = True) -> str:
    """GitHub-flavored markdown standup/summary from the parsed inputs. When a synthesized
    period summary is present (weekly/monthly), it is used as the body verbatim; otherwise
    the per-day paragraphs are reglued as before."""
    if inp.period_summary:
        title = f"Work summary — {inp.period_label}" if inp.period_label else "Work summary"
        return f"## {title}\n\n{inp.period_summary.strip()}\n"
    out: list[str] = [f"## {_title(inp.sections, weekly)}", ""]
    if not inp.sections:
        out.append("_No journal entries found for the requested period._")
        return "\n".join(out) + "\n"
    for sec in inp.sections:
        if weekly:
            out.append(f"### {sec.date}")
        for pid, para in sec.projects.items():
            out.append(f"- **{pid}** — {para}")
        out.append("")
    if include_exec and not weekly and inp.exec_body:
        out.append("### Executive summary")
        out.append(inp.exec_body)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _md_to_slack(md: str) -> str:
    """Markdown -> Slack mrkdwn: ``**b**``->``*b*``, headings/bullets to plain."""
    lines: list[str] = []
    for line in md.splitlines():
        s = line
        s = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", s)        # heading -> bold
        s = re.sub(r"^\s*[-*]\s+", "• ", s)               # bullet -> •
        s = re.sub(r"\*\*(.+?)\*\*", r"*\1*", s)          # bold -> slack bold
        lines.append(s)
    return "\n".join(lines).strip()


def slack_payload(inp: ReportInputs, *, weekly: bool = False) -> dict[str, Any]:
    """A Slack incoming-webhook payload (``{"text": mrkdwn}``)."""
    return {"text": _md_to_slack(standup_markdown(inp, weekly=weekly))}


def github_markdown(inp: ReportInputs, *, weekly: bool = False) -> str:
    """Markdown body for a GitHub issue/PR comment, with an attribution footer."""
    body = standup_markdown(inp, weekly=weekly)
    return body + "\n<sub>🛰️ posted by [ThroughLog](https://github.com/naorzadok/throughlog)</sub>\n"


def stdout_text(inp: ReportInputs, *, weekly: bool = False) -> str:
    return standup_markdown(inp, weekly=weekly)


# --------------------------------------------------------------------------- #
# Thin transports (injectable opener; never raise into the caller's flow)
# --------------------------------------------------------------------------- #
@dataclass
class PostResult:
    ok: bool
    status: int | None = None
    error: str = ""


def _post(url: str, *, body: bytes, headers: dict[str, str],
          opener: Callable[..., Any] | None, timeout: float = 10.0) -> PostResult:
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with opener(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
        return PostResult(ok=200 <= int(status) < 300, status=int(status))
    except urllib.error.HTTPError as exc:
        return PostResult(ok=False, status=exc.code, error=f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return PostResult(ok=False, error=f"{type(exc).__name__}: {exc}")


def post_slack(webhook_url: str, payload: dict[str, Any], *,
               opener: Callable[..., Any] | None = None) -> PostResult:
    return _post(webhook_url, body=json.dumps(payload).encode("utf-8"),
                 headers={"Content-Type": "application/json"}, opener=opener)


def parse_github_target(target: str) -> tuple[str, str]:
    """``owner/repo#42`` -> (``owner/repo``, ``42``). Raises ValueError if malformed."""
    m = re.match(r"^([^/\s]+/[^#\s]+)#(\d+)$", target.strip())
    if not m:
        raise ValueError(f"github target must be owner/repo#number, got {target!r}")
    return m.group(1), m.group(2)


def post_github_comment(target: str, body_md: str, token: str, *,
                        opener: Callable[..., Any] | None = None) -> PostResult:
    """Post ``body_md`` as a comment on a GitHub issue/PR (``owner/repo#number``)."""
    repo, number = parse_github_target(target)
    url = f"https://api.github.com/repos/{repo}/issues/{number}/comments"
    return _post(
        url, body=json.dumps({"body": body_md}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "throughlog"},
        opener=opener)
