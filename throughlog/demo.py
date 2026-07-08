"""Built-in demo day — a zero-config, keyless first-run experience.

A fresh clone has no `config.json`, no API key, and no captured corpus (the live
thin-log under `data/` is gitignored runtime output). Without something to look
at, the dashboard is empty and the project does not earn its screenshot. This
module is the fix: a small, deterministic, **synthetic** workday that ships in
the source tree and showcases the product end-to-end.

It is the analogue of the bundled replay corpus, but generated rather than
committed — so nothing personal and nothing large ever lands in the repo. The
events are hand-built to exercise the full surface:

  * two projects (a web app + an ML model),
  * focus sessions, file changes, human commits, idle, opaque-app deep work,
    a long-running compute job, and narration, and crucially
  * an **AI-agent thread** (`AGENT_REPORT` + a bot-authored commit) — the wedge:
    "here is what my agent did, in my diary, attributed to the right project."

Every event is pre-stamped with a `Privacy` audit record (exactly the shape the
gate produces) so the demo store is indistinguishable from a real gated store:
`python -m throughlog.privacy.gate --audit` on it reports CLEAN, and categorization resolves every
event deterministically via the path/git signal stack — no LLM, no key.

Determinism boundary: this is fixture data, pure stdlib, no LLM. It plugs into
the *real* load -> reconcile -> categorize -> synthesize -> serve path; it does
not add a pipeline branch.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from throughlog.schema import (
    NormalizedEvent, Source, Attribution, Privacy,
    FOCUS_SESSION, DEEP_WORK, LONG_RUN, FILE_CHANGE, GIT_COMMIT,
    NARRATION, IDLE_START, IDLE_END, AGENT_REPORT,
)

# The demo "day". Fixed so the output is byte-stable across runs and machines.
DEMO_DAY = "20260624"            # data/demo/<DEMO_DAY>.jsonl
DEMO_TODAY = "2026-06-24"        # diary date label

GATE_VERSION = "2"

# --------------------------------------------------------------------------- #
# Demo project registry — generic, path-anchored, no personal data.
# The events below live under these paths / remotes, so the deterministic signal
# stack (path 0.95, git-remote 0.82, window-pattern 0.75) attributes every one of
# them above threshold (0.51) with no LLM call.
# --------------------------------------------------------------------------- #
DEMO_PROJECTS: list[dict[str, Any]] = [
    {
        "id": "acme-checkout",
        "name": "Acme Checkout",
        "status": "active",
        "description": "The web checkout service — coupons, payments, currency.",
        "signals": {
            "paths": ["~/projects/acme-checkout"],
            "git_remotes": ["github.com/acme/checkout"],
            "jira_prefixes": ["CHK"],
            "keywords": ["checkout", "coupon", "discount", "payment",
                         "cart", "currency", "rounding"],
            "apps": ["Code.exe", "chrome.exe"],
            "domains": ["github.com/acme/checkout"],
            "window_patterns": [".*[Cc]heckout.*", ".*[Cc]oupon.*"],
        },
    },
    {
        "id": "pricing-model",
        "name": "Pricing Model",
        "status": "active",
        "description": "The dynamic-pricing ML model — features, training, eval.",
        "signals": {
            "paths": ["~/projects/pricing-model"],
            "git_remotes": ["github.com/acme/pricing-model"],
            "jira_prefixes": ["PRI"],
            "keywords": ["pricing", "model", "auc", "seasonality",
                         "feature", "holdout", "training"],
            "apps": ["Code.exe", "python.exe"],
            "domains": ["github.com/acme/pricing-model"],
            "window_patterns": [".*[Pp]ricing.*", ".*[Mm]odel.*"],
        },
    },
]


# --------------------------------------------------------------------------- #
# Event construction
# --------------------------------------------------------------------------- #
def _ts(hhmmss: str) -> str:
    """A demo wall-clock string in the corpus's `YYYY-MM-DD HH:MM:SS` shape."""
    return f"{DEMO_TODAY} {hhmmss}"


def _ev(type: str, hhmmss: str, *, kind: str, adapter: str,
        payload: dict[str, Any], identity: str = "host:demo",
        session_id: str = "", redactions: list[str] | None = None,
        trust: str = "validated") -> NormalizedEvent:
    """Build one pre-gated demo event (Privacy stamp included)."""
    ts = _ts(hhmmss)
    # Deterministic id derived from content, so the demo store is byte-stable
    # across runs and machines (reproducible screenshots, stable de-dup).
    digest = hashlib.sha1(
        json.dumps([type, ts, adapter, identity, payload], sort_keys=True,
                   ensure_ascii=False).encode("utf-8")).hexdigest()
    return NormalizedEvent(
        type=type,
        source=Source(kind=kind, adapter=adapter, identity=identity,
                      session_id=session_id),
        ts_wall=ts,
        recv_ts=ts,
        payload=payload,
        attribution=Attribution(),
        privacy=Privacy(gate_version=GATE_VERSION,
                        redactions=list(redactions or []), passed_at=ts),
        trust=trust,
        event_id=digest[:32],
    )


def build_demo_events() -> list[NormalizedEvent]:
    """A believable single day across two projects, including an agent thread.

    Returned in arrival order (slightly out of wall order in one place, to show
    the reconciler doing its job); `timeline.reconcile` puts them right.
    """
    A = "~/projects/acme-checkout"
    P = "~/projects/pricing-model"
    return [
        # --- morning: a focused checkout bugfix -----------------------------
        _ev(FOCUS_SESSION, "09:05:00", kind="os", adapter="os_focus",
            payload={
                "anchor": "validate.ts — Acme Checkout — VS Code",
                "process": "Code.exe", "duration_sec": 1500, "mode": "focused",
                "active_file": f"{A}/src/checkout/validate.ts",
                "intent": {"label": "Fix coupon stacking on expired codes",
                           "rung": "uia_text"},
            }, redactions=["path"]),
        _ev(FILE_CHANGE, "09:32:00", kind="fs", adapter="fs_git",
            payload={"path": f"{A}/src/checkout/validate.ts", "action": "modified"},
            redactions=["path"]),
        _ev(FILE_CHANGE, "09:41:00", kind="fs", adapter="fs_git",
            payload={"path": f"{A}/tests/checkout/validate.test.ts",
                     "action": "modified"}, redactions=["path"]),
        _ev(GIT_COMMIT, "10:06:00", kind="git", adapter="fs_git",
            payload={
                "repo": "github.com/acme/checkout",
                "message": "Fix coupon stacking on expired codes (CHK-241)",
                "actor": "naor", "files": ["src/checkout/validate.ts",
                                           "tests/checkout/validate.test.ts"],
            }),
        _ev(NARRATION, "10:18:00", kind="intent", adapter="narration",
            payload={"note": "Coupon validation was double-counting stacked "
                             "discounts at checkout; added a guard and a "
                             "regression test."}),

        # --- a break -------------------------------------------------------
        _ev(IDLE_START, "10:45:00", kind="os", adapter="os_focus",
            payload={"reason": "no input"}),
        _ev(IDLE_END, "11:09:00", kind="os", adapter="os_focus",
            payload={"idle_sec": 1440}),

        # --- opaque-app deep work (the OS is blind, we still see it) --------
        _ev(DEEP_WORK, "11:15:00", kind="os", adapter="os_focus",
            payload={
                "anchor": "Checkout redesign — Figma",
                "process": "figma.exe", "duration_sec": 1800, "mode": "opaque",
                "satellites": [{"title": "Checkout flow v3"},
                               {"title": "Coupon field states"}],
            }),

        # --- midday: pricing model work ------------------------------------
        _ev(LONG_RUN, "12:30:00", kind="os", adapter="proc_monitor",
            payload={
                "anchor": "pytest — pricing-model", "process": "python.exe",
                "duration_sec": 2400, "mode": "compute",
                "cmdline": "pytest -q tests/", "cwd": P,
            }, redactions=["path"]),
        _ev(FOCUS_SESSION, "13:10:00", kind="os", adapter="os_focus",
            payload={
                "anchor": "eval.ipynb — Pricing Model — VS Code",
                "process": "Code.exe", "duration_sec": 2000, "mode": "focused",
                "active_file": f"{P}/notebooks/eval.ipynb",
                "intent": {"label": "Evaluate v2 pricing model AUC",
                           "rung": "active_file"},
            }, redactions=["path"]),

        # --- the wedge: an AI agent worked on checkout in the cloud ---------
        # Reported late / slightly out of order on purpose; reconcile fixes it.
        _ev(AGENT_REPORT, "14:12:00", kind="agent", adapter="agent_ingest",
            identity="agent:claude-code", session_id="cc-7f3a",
            payload={
                "repo": "github.com/acme/checkout",
                "tool": "claude-code",
                "summary": "Added multi-currency rounding to the checkout total "
                           "and unit tests; opened PR #482.",
                "files": ["src/checkout/currency.ts",
                          "tests/checkout/currency.test.ts"],
                "message": "PR #482: multi-currency rounding",
            }),
        _ev(GIT_COMMIT, "15:02:00", kind="git", adapter="fs_git",
            payload={
                "repo": "github.com/acme/checkout",
                "message": "Add multi-currency rounding to checkout total (#482)",
                "actor": "claude-code[bot]",
                "files": ["src/checkout/currency.ts",
                          "tests/checkout/currency.test.ts"],
            }),

        # --- afternoon: back to the model ----------------------------------
        _ev(FOCUS_SESSION, "16:20:00", kind="os", adapter="os_focus",
            payload={
                "anchor": "features.py — Pricing Model — VS Code",
                "process": "Code.exe", "duration_sec": 1600, "mode": "focused",
                "active_file": f"{P}/src/features.py",
                "intent": {"label": "Add seasonality features", "rung": "active_file"},
            }, redactions=["path"]),
        _ev(GIT_COMMIT, "16:58:00", kind="git", adapter="fs_git",
            payload={
                "repo": "github.com/acme/pricing-model",
                "message": "Add seasonality features; AUC 0.88 -> 0.91 (PRI-77)",
                "actor": "naor", "files": ["src/features.py", "src/train.py"],
            }),
        _ev(NARRATION, "17:05:00", kind="intent", adapter="narration",
            payload={"note": "Pricing model AUC up to 0.91 after seasonality "
                             "features; still need to validate on the holdout set."}),
    ]


# --------------------------------------------------------------------------- #
# Illustrative LLM tiers (hand-authored fixtures)
#
# The demo runs keyless, so the two LLM-produced tiers — the living `diary.md` and the
# detailed `journal/<YYYY-MM>.md` — would otherwise come out empty (a stub diary, no
# journal at all) and the tour could not show the product's headline feature. These
# fixtures are *synthetic and illustrative*: hand-written to look like what the model
# emits from the demo events above, so the guided tour shows all three tiers AND the
# central contrast — the journal preserves the specifics (CHK-241, PR #482, AUC
# 0.88 → 0.91, the exact features) while the living diary deliberately rolls them up
# ("a few points", "the specifics are in the detailed journal"). Nothing here is
# generated; it is fixture data on the same footing as the demo events.
# --------------------------------------------------------------------------- #
DEMO_DIARIES: dict[str, str] = {
    "acme-checkout": f"""# Acme Checkout
**Status:** active | **Last Updated:** {DEMO_TODAY}

## Current State
Coupon correctness is the active area: a discount-stacking bug on expired codes was
fixed and locked down with a regression test. In parallel, an AI agent contributed
multi-currency rounding for the checkout total — the PR is open and awaiting a human
review. A redesign of the checkout flow is in early design exploration.

## Ongoing Threads
- Review the agent's multi-currency rounding PR before merge.
- Checkout flow v3 redesign (coupon-field states) — design only so far.

## Chronological Narrative
The day centered on coupon edge cases and a parallel, agent-driven currency change.
Several stacked/expired-coupon cases were worked through; the exact codes, files, and
guard ordering live in the detailed journal rather than here.

## Key Artifacts
- CHK-241 — coupon stacking fix
- PR #482 — multi-currency rounding (agent)
""",
    "pricing-model": f"""# Pricing Model
**Status:** active | **Last Updated:** {DEMO_TODAY}

## Current State
Iterating on model accuracy. A batch of seasonality features was added and lifted AUC
by a few points; validation on the true holdout set is still pending before the gain is
counted as real.

## Ongoing Threads
- Validate the latest AUC gain on the holdout set (not just the eval slice).
- Decide which of the new seasonality features actually generalize.

## Chronological Narrative
The period's work was seasonality feature engineering plus an evaluation pass. A few
feature variants were tried with measurable but not-yet-confirmed gains; the exact
features and before/after metrics are recorded in the detailed journal.

## Key Artifacts
- PRI-77 — seasonality features
- notebooks/eval.ipynb — v2 evaluation
""",
}

# Per project, per month (the journal/<YYYY-MM>.md partition). Each value is one or more
# dated sections framed exactly like a real journal entry (`---\n## <date>\n…\n---\n`), so
# it renders through the same `journal_html` path and would merge idempotently by date.
DEMO_JOURNALS: dict[str, dict[str, str]] = {
    "acme-checkout": {
        "2026-06": f"""---
## {DEMO_TODAY}

### Coupon stacking on expired codes (CHK-241)
Tracked down a bug where `validateCoupon` in `src/checkout/validate.ts` double-counted
stacked discounts when one of the codes had already expired: the `isExpired(code)` guard
ran *after* the stacking reducer, so an expired 15% code still contributed to the running
total. Moved the expiry check ahead of the reducer and added a regression test in
`tests/checkout/validate.test.ts` covering two stacked codes where the older one is
expired (expected total unchanged at the single-coupon price). Committed at 10:06.

### Multi-currency rounding (agent, PR #482)
The Claude Code agent added banker's rounding to the checkout total for non-USD
currencies in `src/checkout/currency.ts` (+ `currency.test.ts`), fixing half-cent drift
on EUR/GBP carts, and opened PR #482. Landed as a bot commit at 15:02 — needs a human
review pass before merge.

### Checkout redesign (Figma)
~30 min on the "Checkout redesign — Figma" board: flow v3 plus the coupon-field states
(empty / applied / error). No code yet — design exploration.

**Open threads:** review agent PR #482; decide whether the coupon-field error state needs
a new copy string.

---
""",
    },
    "pricing-model": {
        "2026-06": f"""---
## {DEMO_TODAY}

### v2 model evaluation
Ran the v2 evaluation notebook (`notebooks/eval.ipynb`) against the current eval slice.
Baseline AUC sat at **0.88**.

### Seasonality features (PRI-77)
Added three seasonality features in `src/features.py` (day-of-week, week-of-year, and a
month-end spike flag) and wired them into `src/train.py`. Retrained: **AUC 0.88 → 0.91**.
The month-end flag carried most of the lift; day-of-week was marginal. Committed at 16:58.

### Test run
`pytest -q tests/` (~40 min, green) before the feature work.

**Open threads:** validate the 0.91 on the *real* holdout set (not the eval slice) before
calling it a win; consider dropping day-of-week if it doesn't generalize.

---
""",
    },
}


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def write_demo_thinlog(path: str | Path) -> Path:
    """Write the demo events as a thin-log JSONL (the same on-disk shape the bus
    produces), so the rest of the pipeline loads it through the normal path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for ev in build_demo_events():
            f.write(ev.to_json() + "\n")
    return p


def seed_demo_diaries(diaries_dir: str | Path) -> None:
    """Write the illustrative living-diary + detailed-journal fixtures over the keyless
    synthesis output, so the demo dashboard shows all three tiers (the keyless run leaves
    `diary.md` a stub and produces no journal). Pure fixture write — no LLM, no events."""
    base = Path(diaries_dir)
    for pid, diary in DEMO_DIARIES.items():
        pdir = base / f"project_{pid}"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "diary.md").write_text(diary, encoding="utf-8")
        for month, section in DEMO_JOURNALS.get(pid, {}).items():
            jdir = pdir / "journal"
            jdir.mkdir(parents=True, exist_ok=True)
            (jdir / f"{month}.md").write_text(section, encoding="utf-8")
