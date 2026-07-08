"""Diff-capture policy + sanitizers — the opt-in, default-OFF machinery that lets
the privacy gate keep a scrubbed, size-capped file diff instead of stripping it.

Pure, stdlib-only, deterministic, simulator-testable. Imported by the gate, the
fs/git adapter, config, the simulator and tests. The git shell-out itself lives in
``sources/fs_git`` (the thin live driver); everything here is dependency-free logic.

Three layers of "never diff this":
  1. git's own ``.gitignore`` — free, because diffs come from ``git diff``, which
     never shows ignored or untracked files;
  2. a hardcoded **secrets-file denylist** (:func:`is_secret_file`), enforced even
     for *tracked* files because git will happily diff a committed ``.env``;
  3. **user globs** — per-project ``signals.ignore_globs`` and/or a repo-root
     ``.tlignore`` (:func:`path_ignored` / :func:`parse_tlignore`).

The sanitizer (:func:`scrub_diff`) caps size *first* (so a huge diff can't DoS the
regexes), suppresses binary blobs and private-key blocks whole, scrubs every line
through :mod:`throughlog.privacy.redactors`, drops any line that still looks credential-
shaped, and returns the cleaned diff with a set of audit codes. It NEVER raises —
any failure degrades to ``(None, [...])`` ("no diff"), so a malformed diff can never
crash the gate (the system's never-drop/never-crash invariant).
"""

from __future__ import annotations

import fnmatch
import math
import re
from dataclasses import dataclass
from pathlib import PureWindowsPath

from throughlog.privacy import redactors

# --- Tunables (defaults; overridable via config -> DiffPolicy) ---------------
DEFAULT_MAX_LINES = 400
DEFAULT_MAX_BYTES = 65536
DEFAULT_PREVIEW_CHARS = 256

# Audit/redaction codes stamped onto Privacy.redactions (sorted at the gate).
DIFF_CAPTURED = "diff_captured"
DIFF_SCRUBBED = "diff_scrubbed"
DIFF_TRUNCATED = "diff_truncated"
DIFF_BINARY_SUPPRESSED = "diff_binary_suppressed"
DIFF_SUPPRESSED_IGNORED = "diff_suppressed_ignored"
DIFF_ERROR_SUPPRESSED = "diff_error_suppressed"
COMMIT_BODY_CAPTURED = "commit_body_captured"
DIFFSTAT_CAPTURED = "diffstat_captured"
CLIPBOARD_PREVIEW = "clipboard_preview"


@dataclass(frozen=True)
class DiffPolicy:
    """Immutable policy threaded through the one chokepoint (bus -> gate).

    Frozen because capture is multi-threaded (``ThreadSafeEmitter``); a shared
    immutable policy is safe to read from every source thread. The default
    instance (:data:`DEFAULT_POLICY`) captures nothing — so the gate strips diffs
    exactly as it did before this feature existed.
    """
    capture_diffs: bool = False
    max_lines: int = DEFAULT_MAX_LINES
    max_bytes: int = DEFAULT_MAX_BYTES
    ignore_globs: tuple[str, ...] = ()
    clipboard_preview: bool = False
    clipboard_preview_chars: int = DEFAULT_PREVIEW_CHARS


DEFAULT_POLICY = DiffPolicy()


# --------------------------------------------------------------------------- #
# Layer 2 — hardcoded secrets-file denylist (basename match, both separators)
# --------------------------------------------------------------------------- #
_SECRET_FILE_GLOBS = (
    ".env", "*.env", ".env.*", "*.pem", "*.key",
    "id_rsa", "id_rsa.*", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", "credentials.*", "*.secret", "*.pfx", "*.p12",
    "*.keystore", ".npmrc", ".pypirc",
)


def _basename(path: str) -> str:
    """Last path segment, tolerant of either separator (mirrors the fs_git idiom:
    ``PureWindowsPath`` treats both ``/`` and ``\\`` as separators on any OS)."""
    return PureWindowsPath(str(path or "")).name


def is_secret_file(path: str) -> bool:
    """True if the file's *basename* matches a known secret-bearing file. Enforced
    even for tracked files, so a committed ``.env``/``*.key`` never yields a diff.

    Basename-scoped (not substring), so ``prevent.txt`` does NOT match ``.env`` and
    ``my.key.txt`` does NOT match ``*.key`` — unlike the categorizer's naive
    substring matching, this respects the boundary like the allowlist does.
    """
    name = _basename(path).lower()
    if not name:
        return False
    return any(fnmatch.fnmatchcase(name, g.lower()) for g in _SECRET_FILE_GLOBS)


# --------------------------------------------------------------------------- #
# Layer 3 — user globs (config ignore_globs + repo-root .tlignore)
# --------------------------------------------------------------------------- #
def _posix_rel(path: str) -> str:
    """Lowercased POSIX-style relative path for glob matching."""
    return str(path or "").replace("\\", "/").lower().lstrip("/")


def path_ignored(rel_path: str, globs: tuple[str, ...]) -> bool:
    """True if ``rel_path`` matches any ignore glob (gitignore-ish, stdlib fnmatch).

    Supported forms: ``*.sql`` (basename or full-path), ``secrets/*`` (prefix —
    boundary-respecting, so it does NOT match ``mysecrets/x``), ``build/`` (whole
    subtree), ``**/*.key`` (basename anywhere), and a bare ``node_modules`` segment
    (matches that directory/file anywhere in the path). No negation/anchoring — git's
    real ``.gitignore`` (layer 1) covers the common case; this is the additive
    user-glob layer.
    """
    if not rel_path or not globs:
        return False
    full = _posix_rel(rel_path)
    base = full.rsplit("/", 1)[-1]
    for raw in globs:
        g = (raw or "").strip().replace("\\", "/").lower()
        if not g or g.startswith("#"):
            continue
        if g.endswith("/"):                              # "dir/" -> whole subtree
            d = g.rstrip("/")
            if full == d or full.startswith(d + "/"):
                return True
            continue
        if g.startswith("**/"):                          # "**/x" -> basename anywhere
            if fnmatch.fnmatchcase(base, g[3:]):
                return True
        if fnmatch.fnmatchcase(full, g) or fnmatch.fnmatchcase(base, g):
            return True
        if "/" not in g and g in full.split("/"):        # bare segment anywhere
            return True
    return False


def parse_tlignore(text: str, *, max_lines: int = 1000) -> tuple[str, ...]:
    """Parse a ``.tlignore`` (gitignore-style) into a tuple of globs. One glob per
    line; ``#`` comments and blank lines skipped. Capped at ``max_lines`` so a
    hostile file can't blow up matching. Additive-only — it can never *un*-ignore."""
    globs: list[str] = []
    for i, line in enumerate(text.splitlines()):
        if i >= max_lines:
            break
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        globs.append(s)
    return tuple(globs)


# --------------------------------------------------------------------------- #
# Per-file decomposition of a (possibly multi-file) unified diff
# --------------------------------------------------------------------------- #
_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")


def split_diff_by_file(diff: str) -> list[tuple[str, str]]:
    """Split a unified diff into ``(file_rel, hunk_text)`` pairs, one per file.

    A ``git diff``/``git show`` emits a ``diff --git a/<f> b/<f>`` header per file;
    we key each chunk on the ``b/`` path. This is what lets the gate apply the
    secrets denylist per-file, so a committed ``.env`` hunk inside an otherwise
    allowed multi-file commit diff is dropped (V-01). A diff with no header
    (defensive) yields a single ``("", diff)`` pair.
    """
    chunks: list[tuple[str, list[str]]] = []
    cur_rel = ""
    cur: list[str] = []
    started = False
    for line in diff.splitlines(keepends=True):
        m = _DIFF_HEADER.match(line.rstrip("\n"))
        if m:
            if started:
                chunks.append((cur_rel, cur))
            cur_rel = m.group(2) or m.group(1)
            cur = [line]
            started = True
        elif started:
            cur.append(line)
    if started:
        chunks.append((cur_rel, cur))
    if not chunks:
        return [("", diff)]
    return [(rel, "".join(c)) for rel, c in chunks]


# --------------------------------------------------------------------------- #
# Diff sanitizer
# --------------------------------------------------------------------------- #
_PEM_BEGIN = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
# A long unbroken token. redactors already catches digit+alpha tokens; here we add
# a diff-local backstop for the digit-FREE high-entropy case (e.g. wrapped base64
# key material with no digits on a line) that redactors deliberately leaves alone.
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{40,}")
_REDACTED_TOKEN = "[REDACTED:high_entropy_token]"


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _redact_keylike(text: str) -> str:
    """Diff-local backstop: redact a long (>=40) high-entropy token even when it
    carries no digit (the only gap left by ``redactors``). The high length+entropy
    bar keeps ordinary code identifiers untouched."""
    def _sub(m: re.Match[str]) -> str:
        tok = m.group(0)
        if any(c.isdigit() for c in tok):
            return tok                       # redactors.scrub already handled this
        if _shannon_entropy(tok) >= 4.0:
            return _REDACTED_TOKEN
        return tok
    return _LONG_TOKEN.sub(_sub, text)


def _looks_binary(text: str) -> bool:
    if "Binary files " in text or "\x00" in text:
        return True
    if not text:
        return False
    printable = sum(1 for c in text if c.isprintable() or c in "\n\r\t ")
    return printable / len(text) < 0.85


def _cap(diff: str, policy: DiffPolicy) -> tuple[str, bool]:
    """Apply the byte cap then the line cap. Returns ``(text, truncated)``."""
    truncated = False
    b = diff.encode("utf-8", "replace")
    if len(b) > policy.max_bytes:
        diff = b[: policy.max_bytes].decode("utf-8", "ignore")
        truncated = True
    lines = diff.splitlines()
    if len(lines) > policy.max_lines:
        lines = lines[: policy.max_lines]
        truncated = True
    text = "\n".join(lines)
    if truncated:
        text += "\n[diff truncated]"
    return text, truncated


def scrub_diff(diff: str, policy: DiffPolicy) -> tuple[str | None, list[str]]:
    """Sanitize one file's unified diff. Returns ``(clean_or_None, sorted_codes)``.

    Never raises — any unexpected failure returns ``(None, [DIFF_ERROR_SUPPRESSED])``
    so a malformed diff can never crash the gate.
    """
    try:
        if not diff or not isinstance(diff, str):
            return None, []
        if _looks_binary(diff):
            return None, [DIFF_BINARY_SUPPRESSED]

        codes: set[str] = set()
        text, truncated = _cap(diff, policy)
        if truncated:
            codes.add(DIFF_TRUNCATED)

        out: list[str] = []
        in_pem = False
        changed = False
        for line in text.split("\n"):
            if _PEM_BEGIN.search(line):                   # V-05: suppress the whole block
                in_pem = True
                changed = True
                out.append("[REDACTED:private_key_block]")
                continue
            if in_pem:
                changed = True
                if _PEM_END.search(line):
                    in_pem = False
                continue                                  # drop every body line

            # Scrub known secrets IN PLACE (placeholders), then the digit-free
            # high-entropy backstop. We keep the line — the context is the point of
            # a diff — and rely on the scrubbers to have removed any real secret.
            cleaned, reds = redactors.scrub(line)
            keylike = _redact_keylike(cleaned)
            if reds or keylike != cleaned:
                changed = True
            out.append(keylike)

        result = "\n".join(out)
        if not result.strip():
            return None, sorted(codes)

        codes.add(DIFF_CAPTURED)
        if changed:
            codes.add(DIFF_SCRUBBED)
        return result, sorted(codes)
    except Exception:
        return None, [DIFF_ERROR_SUPPRESSED]


# --------------------------------------------------------------------------- #
# Clipboard preview (Feature D) — capped, scrubbed, secret-safe
# --------------------------------------------------------------------------- #
def make_clipboard_preview(raw: str, policy: DiffPolicy) -> tuple[str, set[str]]:
    """A capped, scrubbed clipboard preview — or ``("", set())`` if it would risk a
    secret. V-04: secret detection runs over the WHOLE clipboard (not just the
    window), and if anything secret-shaped is present the preview is dropped
    entirely — so a token straddling the cut can never leave a fragment behind.
    """
    if not raw:
        return "", set()
    _, secrets = redactors.redact_secrets(raw)
    if secrets or redactors.is_credential_shaped(raw):
        return "", set()
    window = raw[: max(0, policy.clipboard_preview_chars)]
    cleaned, _ = redactors.scrub(window)
    if not cleaned.strip():
        return "", set()
    return cleaned, {CLIPBOARD_PREVIEW}
