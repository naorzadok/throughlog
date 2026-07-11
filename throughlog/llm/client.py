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
import urllib.parse
import urllib.request
from dataclasses import dataclass
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
    # Local backend (a fully in-machine OpenAI-compatible server: Ollama, llama.cpp,
    # llama-cpp-python, LM Studio, …). Only consulted when provider == "local", where the
    # *primary* target becomes {local_endpoint (normalized to /v1), local_model}, no API
    # key is required, and pacing is disabled. `model_fallback` (+ a resolvable cloud key)
    # then acts as an optional CLOUD fallback — that is the "hybrid" mode. Defaults keep
    # the wire byte-identical for every non-local provider.
    local_endpoint: str = "http://localhost:11434"
    local_model: str = ""

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "LLMConfig":
        llm = (config or {}).get("llm", {}) or {}
        provider = str(llm.get("provider", "openrouter") or "openrouter")
        effort = str(llm.get("reasoning_effort", "") or "").strip().lower()
        try:
            rpm = int(llm.get("max_requests_per_min", 0) or 0)
        except (TypeError, ValueError):
            rpm = 0
        # A local endpoint has no shared per-minute quota, so client-side pacing is
        # pointless there — disable it so a copied config's cloud default (18/min) does
        # not needlessly slow local inference.
        if provider == "local":
            rpm = 0
        return cls(
            provider=provider,
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
            local_endpoint=str(llm.get("local_endpoint", "http://localhost:11434")
                               or "http://localhost:11434"),
            local_model=str(llm.get("local_model", "") or "").strip(),
        )

    def resolve_key(self) -> str:
        """Inline key wins (local, gitignored); else the named environment var."""
        return self.api_key or os.environ.get(self.api_key_env, "")

    @property
    def is_local(self) -> bool:
        """True when the PRIMARY target is an in-machine endpoint — provider is explicitly
        'local', or the configured base_url points at loopback. Such a target is built
        keyless and without the OpenRouter attribution headers. (A hybrid's cloud fallback
        is judged separately, per target.)"""
        return self.provider == "local" or _loopback_host(self.base_url)


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
# Request targets (endpoint routing) — one per model in the try chain
# --------------------------------------------------------------------------- #
def _loopback_host(url: str) -> bool:
    """True when ``url`` points at this machine (localhost / 127.0.0.1 / ::1 / *.localhost).
    A loopback target is treated as local: keyless-OK, no OpenRouter headers."""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".localhost")


def _ensure_v1(endpoint: str) -> str:
    """Normalize a local endpoint to its OpenAI-compatible ``/v1`` root (Ollama, llama.cpp,
    LM Studio all serve there), so a bare ``http://localhost:11434`` works out of the box."""
    base = (endpoint or "").rstrip("/")
    if not base:
        return base
    return base if base.endswith("/v1") else base + "/v1"


@dataclass
class _Target:
    """One endpoint the try chain may hit: its base_url, the model to request, the provider
    (for attribution-header gating), the resolved key, and whether it is in-machine (keyless-
    OK). The chain is a *list* of these so a local primary and a cloud fallback can live on
    different servers — the whole of "hybrid"."""
    base_url: str
    model: str
    provider: str
    key: str
    is_local: bool


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
        targets = self._targets()
        primary = targets[0].model if targets else ""
        for target in self._live_targets(targets):
            try:
                text, usage, tries = self._chat_one(target, messages, temperature, max_tokens)
                attempts += tries
                pt, ct, tt = _usage_tuple(usage)
                self.calls.append(CallRecord(
                    label=label, model=target.model, prompt_tokens=pt, completion_tokens=ct,
                    total_tokens=tt, latency_sec=round(self._mono() - t0, 4),
                    attempts=attempts, fallback_used=target.model != primary, ok=True))
                return text
            except LLMError as exc:
                attempts += max(1, self.cfg.max_retries)
                last = f"{target.model}: {exc}"
                if self.cfg.circuit_breaker and _is_rate_limit(str(exc)):
                    self._dead_models.add(target.model)   # trip the breaker for the rest of the run
                continue
        self.calls.append(CallRecord(
            label=label, model=primary, latency_sec=round(self._mono() - t0, 4),
            attempts=attempts, fallback_used=len(targets) > 1, ok=False, error=last))
        raise LLMError(last or "no model configured")

    def _live_targets(self, targets: list[_Target]) -> list[_Target]:
        """The targets to attempt this call: the full chain, minus any whose model the
        breaker has proven dead this run — but never empty (if every one is dead we still
        try the full chain, because refusing a call the pipeline needs is not allowed)."""
        if not self.cfg.circuit_breaker or not self._dead_models:
            return targets
        live = [t for t in targets if t.model not in self._dead_models]
        return live or targets

    def _targets(self) -> list[_Target]:
        """The ordered endpoints to try. For a cloud/OpenAI-compatible provider this is one
        target per model in ``_model_chain()``, all on the one ``base_url``. For
        ``provider == "local"`` the primary is the in-machine {local_endpoint, local_model}
        target (keyless), optionally followed by a CLOUD fallback ({base_url, model_fallback}
        with a resolved key) — that second target is what makes "hybrid" cross two servers."""
        cfg = self.cfg
        if cfg.provider == "local":
            targets = [_Target(_ensure_v1(cfg.local_endpoint), cfg.local_model,
                               "local", "", True)]
            cloud_key = cfg.resolve_key()
            if cfg.model_fallback and cloud_key and cfg.base_url:
                prov = "openrouter" if "openrouter.ai" in cfg.base_url else "openai"
                targets.append(_Target(cfg.base_url, cfg.model_fallback, prov, cloud_key,
                                       _loopback_host(cfg.base_url)))
            return [t for t in targets if t.model]
        key = cfg.resolve_key()
        local = _loopback_host(cfg.base_url)
        return [_Target(cfg.base_url, m, cfg.provider, key, local)
                for m in self._model_chain()]

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

    def probe(self, target: _Target, *, max_tokens: int = 16) -> str:
        """One-shot ping to a single target with no fallback — used by the connectivity
        smoke so it can report exactly which link in the chain works."""
        sys_clean, _ = egress.egress_check(
            "You are a terse echo. Reply with exactly one word.")
        usr_clean, _ = egress.egress_check("Reply with the single word: pong")
        messages = [{"role": "system", "content": sys_clean},
                    {"role": "user", "content": usr_clean}]
        text, _usage, _tries = self._chat_one(target, messages, 0.0, max_tokens)
        return text

    # -- internals ---------------------------------------------------------- #
    def _chat_one(self, target: _Target, messages: list[dict[str, str]],
                  temperature: float, max_tokens: int) -> tuple[str, dict[str, Any], int]:
        """One target: POST with capped-backoff retries on transient failures. Returns
        (text, usage, attempts_made) on success; raises LLMError once retries are
        exhausted (the caller may then fall back to the next target)."""
        payload: dict[str, Any] = {
            "model": target.model,
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
                text, usage = self._post(body, target)
                return text, usage, attempt
            except _Retryable as exc:
                last = str(exc)
                if attempt < self.cfg.max_retries:
                    self._sleep(min(2.0 ** attempt, 30.0))
                    continue
                raise LLMError(f"exhausted {self.cfg.max_retries} retries: {last}") from exc
        raise LLMError(last or "no attempt made")

    def _post(self, body: bytes, target: _Target) -> tuple[str, dict[str, Any]]:
        key = target.key
        if not key:
            if target.is_local:
                key = "sk-local"      # local servers ignore Authorization; send a placeholder
            else:
                raise LLMError(
                    f"no API key — set ${self.cfg.api_key_env} or llm.api_key in config.json")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        if target.provider == "openrouter":       # attribution — OpenRouter only, dropped for local
            headers["HTTP-Referer"] = "https://github.com/naorzadok/throughlog"
            headers["X-Title"] = "throughlog"
        req = urllib.request.Request(
            f"{target.base_url}/chat/completions",
            data=body, method="POST", headers=headers,
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
    if "network" in low or "urlopen" in low or "refused" in low or "timed out" in low:
        return ("UNREACHABLE - could not connect. If this is a local model, is the server "
                "running (Ollama, or `tl local serve`)? Otherwise check llm.base_url.",
                SMOKE_HARD)
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
    client = LLMClient(cfg)
    targets = client._targets()
    print(f"provider={cfg.provider}  base_url={cfg.base_url}"
          f"  reasoning={cfg.reasoning_effort or 'default'}")
    chain_desc = " -> ".join(f"{t.model}@{t.base_url}" for t in targets) or "(none configured)"
    print(f"model chain : {chain_desc}")
    key_state = ("present" if cfg.resolve_key()
                 else ("not required (local endpoint)" if cfg.is_local else "MISSING"))
    print(f"api key     : {key_state}")

    if not targets:
        print("\nRESULT: FAILED — no model configured (set llm.model / llm.local_model "
              "in config.json)")
        return SMOKE_HARD

    # Probe each target on its own (no fallback) so the report says exactly which
    # link in the chain works — e.g. primary rate-limited but fallback healthy.
    codes: list[int] = []
    for target in targets:
        try:
            out = client.probe(target)
            print(f"\n  {target.model} @ {target.base_url}\n    OK  <- {out.strip()[:80]!r}")
            codes.append(SMOKE_OK)
        except LLMError as exc:
            verdict, code = _classify_error(str(exc))
            print(f"\n  {target.model} @ {target.base_url}\n    {verdict}")
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
