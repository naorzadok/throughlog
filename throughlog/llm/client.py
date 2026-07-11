"""Pluggable LLM client — stdlib `urllib` to an OpenAI-compatible chat endpoint.

Design constraints (from the determinism boundary):
  * stdlib only — no SDK, no new dependency. A raw REST POST to
    `{base_url}/chat/completions` with a Bearer key. OpenRouter is the default
    target; the same shape works for Ollama (`/v1`) or any OpenAI-compatible API.
  * This is the single egress door to a remote model. `chat()` re-runs the
    egress gate (`throughlog.privacy.egress`) on the system+user prompt before sending,
    so already-gated data is scrubbed a second time — a gate bug cannot leak.
  * Robust to weak/free models and flaky free tiers: transport errors, HTTP 429,
    and 5xx retry with capped backoff; a terminal failure raises `LLMError`, which
    callers MUST turn into `needs_review` (never a crash, never a dropped event).

The client returns the raw assistant *text*. Parsing structured answers (the
categorization JSON) is the caller's job — kept separate so transport retries and
content-parse retries don't entangle.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Callable

from throughlog.privacy import egress
from throughlog.llm.ratelimit import RateLimiter

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_KEY_ENV = "OPENROUTER_API_KEY"


class LLMError(RuntimeError):
    """Terminal failure: the model could not be reached or gave no usable content
    after retries. Phase 1/2 must degrade gracefully (needs_review), never crash."""


class _Retryable(Exception):
    """Internal: a transient transport failure worth retrying."""


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class LLMConfig:
    provider: str = "openrouter"
    base_url: str = DEFAULT_BASE_URL
    model: str = ""
    model_fallback: str = ""          # tried when `model` is exhausted/unreachable (e.g. a
                                      # rate-limited free model); empty = no fallback
    api_key: str = ""                 # inline key (gitignored config.json) — optional
    api_key_env: str = DEFAULT_KEY_ENV
    timeout_sec: float = 600.0
    max_retries: int = 3
    # Client-side pacing: at most this many physical requests per rolling minute
    # (0 = disabled, byte-identical to no gate). The "timing manager" that keeps a
    # free-tier key under its per-minute limit; it delays a call, never drops one.
    max_requests_per_min: int = 0
    # OpenRouter's unified reasoning knob ("low"/"medium"/"high"). Empty = provider
    # default (the param is omitted entirely). Models that don't support reasoning
    # safely ignore it, so this needs no per-model capability detection.
    reasoning_effort: str = ""
    # Per-run circuit-breaker (default OFF, byte-identical when off): once a model
    # terminally rate-limits (429) this run, stop re-attempting it on later calls and go
    # straight to the fallback — so the pacer isn't fighting a known-dead primary call
    # after call. It only ever SKIPS a model already proven dead; it never refuses a call
    # (if every model is dead it still tries the full chain).
    circuit_breaker: bool = False

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "LLMConfig":
        llm = (config or {}).get("llm", {}) or {}
        effort = str(llm.get("reasoning_effort", "") or "").strip().lower()
        try:
            rpm = int(llm.get("max_requests_per_min", 0) or 0)
        except (TypeError, ValueError):
            rpm = 0
        return cls(
            provider=llm.get("provider", "openrouter"),
            base_url=str(llm.get("base_url", DEFAULT_BASE_URL)).rstrip("/"),
            model=llm.get("model", ""),
            model_fallback=str(llm.get("model_fallback", "") or "").strip(),
            api_key=llm.get("api_key", ""),
            api_key_env=llm.get("api_key_env", DEFAULT_KEY_ENV),
            timeout_sec=float(llm.get("timeout_sec", 600.0)),
            max_retries=int(llm.get("max_retries", 3)),
            max_requests_per_min=max(0, rpm),
            reasoning_effort=effort if effort in ("low", "medium", "high") else "",
            circuit_breaker=bool(llm.get("circuit_breaker", False)),
        )

    def resolve_key(self) -> str:
        """Inline key wins (local, gitignored); else the named environment var."""
        return self.api_key or os.environ.get(self.api_key_env, "")


# --------------------------------------------------------------------------- #
# Call metering (observability only — never touches the wire or the pipeline)
# --------------------------------------------------------------------------- #
@dataclass
class CallRecord:
    """One logical ``chat()`` invocation: which call site, which model finally
    answered, tokens (from the provider ``usage`` block), wall latency, physical
    attempts across the model chain, whether the fallback model was used, and whether
    it terminally failed. Purely descriptive — collected so the flow can be stress-tested
    and optimized; it changes no bytes sent and no pipeline behavior."""
    label: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_sec: float = 0.0
    attempts: int = 0
    fallback_used: bool = False
    ok: bool = True
    error: str = ""


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _is_rate_limit(msg: str) -> bool:
    """True when a terminal LLMError message reflects provider throttling (HTTP 429),
    the signal the circuit-breaker trips on. Matched on text because that is all a
    terminal LLMError carries."""
    low = msg.lower()
    return "429" in low or "rate-limit" in low or "rate limit" in low


def _usage_tuple(usage: dict[str, Any]) -> tuple[int, int, int]:
    """(prompt, completion, total) from an OpenAI-style ``usage`` block; total falls
    back to prompt+completion when the provider omits it."""
    pt = _int((usage or {}).get("prompt_tokens"))
    ct = _int((usage or {}).get("completion_tokens"))
    tt = _int((usage or {}).get("total_tokens")) or (pt + ct)
    return pt, ct, tt


# --------------------------------------------------------------------------- #
# Response extraction (tolerant of provider quirks)
# --------------------------------------------------------------------------- #
def _extract_content(data: dict[str, Any]) -> str:
    """Pull assistant text from an OpenAI-compatible response.

    Tolerates: a top-level provider `error`; `content` as a list of parts; and
    the reasoning/content split some models (e.g. gpt-oss) emit, where `content`
    is empty but the answer is in a `reasoning` field.
    """
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise LLMError(f"provider error: {msg}")
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise _Retryable(f"no choices in response: {str(data)[:200]}") from exc

    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):                      # parts -> concatenate text
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not content and isinstance(msg, dict):          # gpt-oss reasoning fallback
        content = msg.get("reasoning") or ""
    if not content or not str(content).strip():
        raise _Retryable("empty completion")
    return str(content)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class LLMClient:
    """Minimal OpenAI-compatible chat client over stdlib urllib.

    `opener` and `sleep` are injectable so tests exercise retries/egress with no
    network and no real delay.
    """

    def __init__(self, config: LLMConfig, *,
                 opener: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.cfg = config
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._mono = monotonic
        # Per-call metering log (observability only). Appended one row per chat();
        # read via `metrics_summary()` or directly by the stress harness. Empty and
        # inert unless something reads it — it never influences a request.
        self.calls: list[CallRecord] = []
        # Circuit-breaker state (only consulted when cfg.circuit_breaker): models that
        # terminally rate-limited this run and should be skipped on later calls.
        self._dead_models: set[str] = set()
        # Client-side pacing gate. Shares the injected `sleep`, so tests never really
        # wait; disabled (rpm<=0) it is a no-op, keeping the wire byte-identical.
        self._limiter = RateLimiter(config.max_requests_per_min, sleep=sleep)

    # -- public ------------------------------------------------------------- #
    def chat(self, system: str, user: str, *, temperature: float = 0.0,
             max_tokens: int = 1500, label: str = "") -> str:
        """Send system+user, return assistant text. Egress-scrubs the outbound
        prompt, retries transient failures with backoff per model, and — when the
        primary model is exhausted/unreachable (e.g. a rate-limited free model) —
        falls through to ``model_fallback`` before giving up. Raises LLMError only
        once every configured model has failed, so Phase 1/2 degrade, never crash.

        ``label`` names the call site (e.g. "categorize"/"entry"/"overview") for the
        metering log only; it never reaches the wire."""
        sys_clean, _ = egress.egress_check(system)
        usr_clean, _ = egress.egress_check(user)
        messages = [
            {"role": "system", "content": sys_clean},
            {"role": "user", "content": usr_clean},
        ]
        t0 = self._mono()
        last = ""
        attempts = 0
        for model in self._try_chain():
            try:
                text, usage, tries = self._chat_one(model, messages, temperature, max_tokens)
                attempts += tries
                pt, ct, tt = _usage_tuple(usage)
                self.calls.append(CallRecord(
                    label=label, model=model, prompt_tokens=pt, completion_tokens=ct,
                    total_tokens=tt, latency_sec=round(self._mono() - t0, 4),
                    attempts=attempts, fallback_used=model != self.cfg.model, ok=True))
                return text
            except LLMError as exc:
                attempts += max(1, self.cfg.max_retries)
                last = f"{model}: {exc}"
                if self.cfg.circuit_breaker and _is_rate_limit(str(exc)):
                    self._dead_models.add(model)     # trip the breaker for the rest of the run
                continue
        chain = self._model_chain()
        self.calls.append(CallRecord(
            label=label, model=chain[0] if chain else "", latency_sec=round(self._mono() - t0, 4),
            attempts=attempts, fallback_used=len(chain) > 1, ok=False, error=last))
        raise LLMError(last or "no model configured")

    def _try_chain(self) -> list[str]:
        """The models to attempt this call: the full chain, minus any the breaker has
        proven dead this run — but never empty (if every model is dead we still try the
        full chain, because refusing a call the pipeline needs is not allowed)."""
        chain = self._model_chain()
        if not self.cfg.circuit_breaker or not self._dead_models:
            return chain
        live = [m for m in chain if m not in self._dead_models]
        return live or chain

    def metrics_summary(self) -> dict[str, Any]:
        """Aggregate the metering log for a run: call/degrade/fallback counts, token
        totals, and summed wall latency. Read by the CLI and the stress harness."""
        c = self.calls
        return {
            "calls": len(c),
            "ok": sum(1 for r in c if r.ok),
            "degraded": sum(1 for r in c if not r.ok),
            "fallbacks": sum(1 for r in c if r.fallback_used),
            "prompt_tokens": sum(r.prompt_tokens for r in c),
            "completion_tokens": sum(r.completion_tokens for r in c),
            "total_tokens": sum(r.total_tokens for r in c),
            "latency_sec": round(sum(r.latency_sec for r in c), 3),
        }

    def _model_chain(self) -> list[str]:
        """The models to try, in order: the primary then the (distinct) fallback."""
        chain = [self.cfg.model]
        fb = self.cfg.model_fallback
        if fb and fb not in chain:
            chain.append(fb)
        return [m for m in chain if m]

    # -- internals ---------------------------------------------------------- #
    def _chat_one(self, model: str, messages: list[dict[str, str]],
                  temperature: float, max_tokens: int) -> tuple[str, dict[str, Any], int]:
        """One model: POST with capped-backoff retries on transient failures. Returns
        (text, usage, attempts_made) on success; raises LLMError once retries are
        exhausted (the caller may then fall back)."""
        payload: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if self.cfg.reasoning_effort:     # omit entirely when default -> unchanged wire
            payload["reasoning"] = {"effort": self.cfg.reasoning_effort}
        body = json.dumps(payload).encode("utf-8")

        last = ""
        for attempt in range(1, max(1, self.cfg.max_retries) + 1):
            try:
                text, usage = self._post(body)
                return text, usage, attempt
            except _Retryable as exc:
                last = str(exc)
                if attempt < self.cfg.max_retries:
                    self._sleep(min(2.0 ** attempt, 30.0))
                    continue
                raise LLMError(f"exhausted {self.cfg.max_retries} retries: {last}") from exc
        raise LLMError(last or "no attempt made")
    def _post(self, body: bytes) -> tuple[str, dict[str, Any]]:
        key = self.cfg.resolve_key()
        if not key:
            raise LLMError(
                f"no API key — set ${self.cfg.api_key_env} or llm.api_key in config.json")
        req = urllib.request.Request(
            f"{self.cfg.base_url}/chat/completions",
            data=body, method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                # OpenRouter attribution (optional, harmless elsewhere).
                "HTTP-Referer": "https://github.com/naorzadok/throughlog",
                "X-Title": "throughlog",
            },
        )
        # Pace before we actually send — this is the per-request layer, so retries
        # and fallback-model requests are throttled too (they all count against the
        # provider's per-minute limit). Delays only; never skips the send.
        self._limiter.acquire()
        try:
            with self._opener(req, timeout=self.cfg.timeout_sec) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if exc.code == 429 or 500 <= exc.code < 600:
                raise _Retryable(f"HTTP {exc.code}: {detail}") from exc
            raise LLMError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _Retryable(f"network: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _Retryable(f"non-JSON response: {raw[:200]}") from exc
        usage = data.get("usage") if isinstance(data, dict) else None
        return _extract_content(data), (usage if isinstance(usage, dict) else {})


def from_config(config: dict[str, Any], *, opener: Callable[..., Any] | None = None,
                sleep: Callable[[float], None] = time.sleep) -> LLMClient:
    return LLMClient(LLMConfig.from_config(config), opener=opener, sleep=sleep)


# --------------------------------------------------------------------------- #
# Connectivity smoke test (live) — `python -m throughlog.llm.client --smoke`
# --------------------------------------------------------------------------- #
# Distinct exit codes so a script/CI can branch on *why* a ping failed.
SMOKE_OK = 0          # reachable and answered
SMOKE_HARD = 1        # a real problem — wrong key / base_url / model id; won't self-heal
SMOKE_RATELIMIT = 2   # reachable but throttled (HTTP 429) — transient, not your fault
SMOKE_NO_KEY = 3      # no key configured


def _classify_error(msg: str) -> tuple[str, int]:
    """Map a terminal LLMError message to a one-line verdict + exit code.

    The whole point of the smoke: a *failed* ping must distinguish transient
    free-tier rate-limiting (your account is fine — retry later or add credits)
    from a real auth/config problem (a wrong key, base_url, or model id) that
    will never fix itself. We classify on the message because that is all a
    terminal LLMError carries; unknown failures stay "hard" so a genuine problem
    is never masked as merely transient."""
    low = msg.lower()
    if "no api key" in low:
        return ("NO KEY - set $OPENROUTER_API_KEY or llm.api_key in config.json", SMOKE_NO_KEY)
    if "http 429" in low or "rate-limit" in low or "rate limit" in low:
        return ("RATE-LIMITED (429) - reachable but throttled; transient. "
                "Retry later, or add OpenRouter credits to draw on your own quota.",
                SMOKE_RATELIMIT)
    if "http 401" in low or "http 403" in low:
        return ("AUTH ERROR - key rejected. Check llm.api_key / the API-key env var.",
                SMOKE_HARD)
    if "http 404" in low:
        return ("CONFIG ERROR (404) - check llm.base_url and the model id.", SMOKE_HARD)
    return (f"FAILED - {msg}", SMOKE_HARD)


def _overall_exit(codes: list[int]) -> int:
    """Collapse per-model probe codes into one process exit code. Success wins
    (any model answered ⇒ the chain works); otherwise report the most
    fundamental problem first: no key > hard error > transient rate-limit."""
    if SMOKE_OK in codes:
        return SMOKE_OK
    if SMOKE_NO_KEY in codes:
        return SMOKE_NO_KEY
    if SMOKE_HARD in codes:
        return SMOKE_HARD
    if SMOKE_RATELIMIT in codes:
        return SMOKE_RATELIMIT
    return SMOKE_HARD


def _smoke(argv: list[str] | None = None) -> int:
    import argparse
    from throughlog.config import load_config

    ap = argparse.ArgumentParser(
        description="Ping each configured model (live) and classify the outcome.")
    ap.add_argument("--smoke", action="store_true")
    ap.parse_args(argv)

    cfg = LLMConfig.from_config(load_config())
    chain = LLMClient(cfg)._model_chain()
    print(f"provider={cfg.provider}  base_url={cfg.base_url}"
          f"  reasoning={cfg.reasoning_effort or 'default'}")
    print(f"model chain : {' -> '.join(chain) if chain else '(none configured)'}")
    print(f"api key     : {'present' if cfg.resolve_key() else 'MISSING'}")

    if not chain:
        print("\nRESULT: FAILED — no model configured (set llm.model in config.json)")
        return SMOKE_HARD

    # Probe each model on its own (no fallback) so the report says exactly which
    # link in the chain works — e.g. primary rate-limited but fallback healthy.
    codes: list[int] = []
    for model in chain:
        probe = replace(cfg, model=model, model_fallback="")
        try:
            out = LLMClient(probe).chat(
                "You are a terse echo. Reply with exactly one word.",
                "Reply with the single word: pong")
            print(f"\n  {model}\n    OK  <- {out.strip()[:80]!r}")
            codes.append(SMOKE_OK)
        except LLMError as exc:
            verdict, code = _classify_error(str(exc))
            print(f"\n  {model}\n    {verdict}")
            codes.append(code)

    rc = _overall_exit(codes)
    summary = {
        SMOKE_OK: "OK - the model chain works.",
        SMOKE_NO_KEY: "NO KEY - add a key to config.json or the API-key env var.",
        SMOKE_HARD: "FAILED - a real auth/config problem (see above); won't self-heal.",
        SMOKE_RATELIMIT: "RATE-LIMITED - every model is throttled right now. "
                         "Transient: retry later, or add OpenRouter credits.",
    }[rc]
    print(f"\nRESULT: {summary}")
    return rc


if __name__ == "__main__":
    import sys
    sys.exit(_smoke())
