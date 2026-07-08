"""Deterministic redactors — secret/credential scrubbing and path normalization.

Pure functions, regex + entropy based, no LLM. Used by the gate (before
persistence) and by egress_check (before any remote send). Conservative by
design: it is fine to over-redact a token; it is never acceptable to leak one.
"""

from __future__ import annotations

import math
import re

# --- Secret patterns ---------------------------------------------------------
# (name, compiled regex). Order matters only for reporting; all are applied.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z\-_]{30,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("private_key_block", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    # key=value style secrets in configs/URLs. The key alternation includes
    # COMPOUND names (secret_key, client_secret, private_key, refresh_token, …):
    # a bare `\bsecret\b` would miss `secret_key=` because `_` is a word char, so
    # the old pattern leaked any value behind a compound secret key (risk #4).
    ("kv_secret", re.compile(
        r"(?i)\b(?:"
        r"client[_\-]?secret"
        r"|(?:api|access|auth|refresh|session|secret|private|app)[_\-]?(?:key|token|secret)"
        r"|password|passwd|passphrase|pwd|secret|token"
        r")\s*[=:]\s*['\"]?([^\s'\";,&]+)")),
    # password embedded in a URL/connection string:  scheme://user:PASS@host
    ("url_password", re.compile(r"(?i)://[^/\s:@]+:([^/\s:@]{1,})@")),
]

_REDACTED = "[REDACTED:{}]"

# Generic high-entropy token: a run of base64/hex-ish chars. Backstop for unknown
# secret shapes. The floor was lowered 24 -> 16 to narrow the "medium-length token
# with no key= prefix" leak band (risk #4); the digit+alpha AND entropy>=3.5 gates
# below still keep ordinary words/paths/identifiers from being redacted. (Extremely
# low-entropy unnamed strings remain a deliberate non-target — catching them would
# mean redacting normal prose.)
_TOKEN_CANDIDATE = re.compile(r"\b[A-Za-z0-9+/=_\-]{16,}\b")


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Return (scrubbed_text, sorted list of secret-type names found)."""
    if not text:
        return text, []
    found: set[str] = set()

    for name, pat in _SECRET_PATTERNS:
        if pat.search(text):
            found.add(name)
            # For kv_secret / url_password we only blank the captured value group.
            if name in ("kv_secret", "url_password"):
                def _sub(m: re.Match[str]) -> str:
                    # Replace the captured value by position, not str.replace, which
                    # could hit an identical short substring inside the key name.
                    s, e = m.start(1) - m.start(), m.end(1) - m.start()
                    g = m.group(0)
                    return g[:s] + _REDACTED.format(name) + g[e:]
                text = pat.sub(_sub, text)
            else:
                text = pat.sub(_REDACTED.format(name), text)

    # Entropy backstop for unknown high-entropy tokens.
    def _maybe_token(m: re.Match[str]) -> str:
        tok = m.group(0)
        if _shannon_entropy(tok) >= 3.5 and any(c.isdigit() for c in tok) \
                and any(c.isalpha() for c in tok):
            found.add("high_entropy_token")
            return _REDACTED.format("high_entropy_token")
        return tok

    text = _TOKEN_CANDIDATE.sub(_maybe_token, text)
    return text, sorted(found)


# --- Path normalization ------------------------------------------------------
# Strip the user-identifying prefix `<drive>:\Users\<name>` -> `~`, keeping the
# structure below it (useful for project attribution, no PII).
_USER_PREFIX = re.compile(r"(?i)\b[A-Za-z]:[\\/]Users[\\/][^\\/]+")
_POSIX_HOME = re.compile(r"(?i)/home/[^/]+|/Users/[^/]+")


def normalize_home_paths(text: str) -> str:
    if not text:
        return text
    text = _USER_PREFIX.sub("~", text)
    text = _POSIX_HOME.sub("~", text)
    return text


def scrub(text: str) -> tuple[str, list[str]]:
    """Full scrub: home-path normalization + secret redaction.

    Returns (clean_text, redaction_types). 'path' is included in the redaction
    list whenever a home prefix was normalized away.
    """
    if not text:
        return text, []
    normalized = normalize_home_paths(text)
    reds: list[str] = []
    if normalized != text:
        reds.append("path")
    cleaned, secrets = redact_secrets(normalized)
    reds.extend(secrets)
    return cleaned, reds


# --- Clipboard classification ------------------------------------------------
_URL_RE = re.compile(r"https?://([^/\s]+)")
_CODE_HINT = re.compile(r"[{};=]|def |class |function |=>|</")


def is_credential_shaped(text: str) -> bool:
    """True if clipboard text looks like a secret/credential that must be dropped."""
    if not text:
        return False
    _, found = redact_secrets(text)
    if found:
        return True
    stripped = text.strip()
    # A lone high-entropy blob with no spaces is very likely a token/password.
    if " " not in stripped and 12 <= len(stripped) <= 256 \
            and _shannon_entropy(stripped) >= 3.5 \
            and any(c.isdigit() for c in stripped) and any(c.isalpha() for c in stripped):
        return True
    return False


def classify_clipboard(text: str) -> dict[str, object]:
    """Return a TYPED SUMMARY of clipboard content — never the raw content.

    kind: credential | url | code_snippet | text
    Credential-shaped content carries no preview and is meant to be dropped.
    """
    raw = text or ""
    length = len(raw)

    if is_credential_shaped(raw):
        return {"kind": "credential", "length": length}

    m = _URL_RE.search(raw)
    if m:
        return {"kind": "url", "length": length, "host": m.group(1)}

    if "\n" in raw or _CODE_HINT.search(raw):
        return {"kind": "code_snippet", "length": length}

    return {"kind": "text", "length": length}
