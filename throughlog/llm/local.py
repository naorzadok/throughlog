"""Local, in-machine LLM backend — the self-contained alternative to a cloud key.

`tl local` downloads a small GGUF model and serves it over an OpenAI-compatible endpoint,
so the whole pipeline runs with NO API key and NO network egress. Two on-ramps compose here:

  * Ollama (external app) — already serves an OpenAI API at http://localhost:11434/v1; we
    only *detect* its models and point config at them. No download here.
  * Bundled llama-cpp-python (the `throughlog[local]` extra) — we download a GGUF into
    ~/.throughlog/models/ and run `python -m llama_cpp.server` over it.

Design mirrors the rest of the repo: a **pure core** (registry + reference resolution +
filesystem/HTTP-metadata helpers — dependency-free, testable with an injected `opener`) and a
thin **live driver** (the actual download stream + the served subprocess). The GGUF download is
deliberately pure-stdlib (Hugging Face's public API + a streaming `urllib` GET), so it needs no
extra installed and yields clean byte-progress for the dashboard; only *serving* needs the
optional `llama-cpp-python`. LLM failure is never fatal upstream: a missing model / dead server
surfaces as an `LLMError` and the pipeline degrades deterministically, exactly as with the cloud.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

HF_ENDPOINT = "https://huggingface.co"
# 127.0.0.1, not "localhost": where Ollama binds, and it dodges the Windows IPv6-first
# ("::1") connect delay that makes a refused "localhost" probe take ~1s per call.
DEFAULT_OLLAMA = "http://127.0.0.1:11434"
DEFAULT_PORT = 8080
DEFAULT_HOST = "127.0.0.1"
DEFAULT_ALIAS = "local"          # the model name our bundled server reports
DEFAULT_QUANT = "Q4_K_M"


class LocalError(RuntimeError):
    """A local-backend setup failure (bad ref, missing dep, unreachable server). Surfaced to
    the user as a message; never raised into the pipeline (which sees only LLMError)."""


# --------------------------------------------------------------------------- #
# Curated registry (data, not code) + reference resolution — PURE
# --------------------------------------------------------------------------- #
_REGISTRY_PATH = Path(__file__).with_name("models.json")


def load_registry(path: Path | None = None) -> dict[str, Any]:
    """The shipped curated shortlist. Never raises — a missing/broken file yields an empty
    registry so the escape hatch (`hf.co/<org>/<repo>`) still works."""
    try:
        return json.loads((path or _REGISTRY_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"default": "", "default_quant": DEFAULT_QUANT, "models": []}


@dataclass
class ModelSpec:
    """A resolved model reference: which HF repo, which quant file(s) to select, and the
    descriptive bits for the UI. `id` is the alias or `org/repo` used as a display handle."""
    id: str
    repo: str
    gguf_pattern: str
    name: str = ""
    license: str = ""
    approx_gb: float = 0.0


def _quant_glob(quant: str) -> str:
    return f"*{quant.lower()}*.gguf"


def _strip_hf_prefix(ref: str) -> str:
    low = ref.lower()
    for prefix in ("https://hf.co/", "https://huggingface.co/", "hf.co/", "huggingface.co/"):
        if low.startswith(prefix):
            return ref[len(prefix):]
    return ref


def resolve_spec(ref: str, registry: dict[str, Any] | None = None, *,
                 quant: str | None = None) -> ModelSpec:
    """Resolve a model reference to a ``ModelSpec``. Accepts a curated alias
    (``nemotron-3-nano-4b``) or the escape hatch ``[hf.co/]<org>/<repo>[:quant]`` — so any
    GGUF repo, including one released after this code shipped, works with no code change.
    Raises ``LocalError`` on an unknown alias / malformed ref."""
    registry = registry or load_registry()
    default_quant = quant or registry.get("default_quant", DEFAULT_QUANT)
    raw = (ref or "").strip() or registry.get("default", "")
    if not raw:
        raise LocalError("no model specified and no default in the registry")

    for m in registry.get("models", []):        # curated alias?
        if m.get("id") == raw:
            pattern = _quant_glob(quant) if quant else (m.get("gguf_pattern") or _quant_glob(default_quant))
            return ModelSpec(id=m["id"], repo=m["hf_repo"], gguf_pattern=pattern,
                             name=m.get("name", ""), license=m.get("license", ""),
                             approx_gb=float(m.get("approx_gb") or 0.0))

    body = _strip_hf_prefix(raw)                 # escape hatch: <org>/<repo>[:quant]
    q = quant
    if ":" in body:
        body, q2 = body.rsplit(":", 1)
        q = q or q2
    body = body.strip("/")
    if body.count("/") < 1:
        raise LocalError(
            f"unknown model alias or malformed HF ref: {ref!r} — use an alias from "
            f"`tl local list`, or hf.co/<org>/<repo>[:quant]")
    org_repo = "/".join(body.split("/")[:2])
    return ModelSpec(id=org_repo, repo=org_repo, gguf_pattern=_quant_glob(q or default_quant),
                     name=org_repo)


# --------------------------------------------------------------------------- #
# Filesystem — PURE (over a directory)
# --------------------------------------------------------------------------- #
def models_dir() -> Path:
    """Where downloaded GGUFs live — ``$TL_MODELS_DIR`` or ``~/.throughlog/models`` (kept out
    of the repo and out of ``data/``, so nothing captured or large lands in either)."""
    env = os.environ.get("TL_MODELS_DIR")
    return Path(env) if env else Path.home() / ".throughlog" / "models"


_SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.IGNORECASE)


def installed_models(directory: Path | None = None) -> list[dict[str, Any]]:
    """Scan a models dir for downloaded GGUFs, one row per model (a split set is represented by
    its first part). Pure over the filesystem — used by `tl local status` and the dashboard."""
    d = directory or models_dir()
    out: list[dict[str, Any]] = []
    if not d.exists():
        return out
    for f in sorted(d.glob("*.gguf")):
        m = _SPLIT_RE.search(f.name)
        if m and m.group(1) != "00001":          # secondary split part — folded into part 1
            continue
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        out.append({"file": str(f), "name": f.name, "size_bytes": size})
    return out


def _pick_gguf(files: list[str], pattern: str) -> list[str]:
    """From a repo's file list choose the GGUF(s) for the wanted quant: everything matching the
    glob (a split set matches together); fall back to any Q4_K_M, then any GGUF. Sorted so a
    split set downloads part 1..N in order."""
    ggufs = [f for f in files if f.lower().endswith(".gguf")]
    matched = [f for f in ggufs if fnmatch.fnmatch(f.lower(), pattern.lower())]
    if not matched:
        matched = [f for f in ggufs if "q4_k_m" in f.lower()] or ggufs
    return sorted(matched)


# --------------------------------------------------------------------------- #
# Hugging Face metadata + streaming download — stdlib (injectable opener)
# --------------------------------------------------------------------------- #
def _auth_headers(token: str | None) -> dict[str, str]:
    token = token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def list_repo_files(repo: str, *, token: str | None = None,
                    opener: Callable[..., Any] | None = None) -> list[str]:
    """List a model repo's files via the public HF API (no dependency). Raises LocalError on a
    network/API failure so callers can show a clean message."""
    opener = opener or urllib.request.urlopen
    url = f"{HF_ENDPOINT}/api/models/{repo}"
    req = urllib.request.Request(url, headers=_auth_headers(token))
    try:
        with opener(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise LocalError(f"Hugging Face API HTTP {exc.code} for {repo} "
                         f"(private/gated model? set $HF_TOKEN)") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise LocalError(f"could not reach Hugging Face for {repo}: {exc}") from exc
    sibs = data.get("siblings") if isinstance(data, dict) else None
    return [s.get("rfilename", "") for s in (sibs or []) if s.get("rfilename")]


def _download_one(repo: str, rfilename: str, dest: Path, *, token: str | None = None,
                  opener: Callable[..., Any] | None = None,
                  progress: Callable[[int, int], None] | None = None,
                  chunk: int = 1 << 20) -> Path:
    """Stream a single file to ``dest`` (resumable via a ``.part`` sidecar + HTTP Range),
    reporting (downloaded, total) bytes. Returns the final path."""
    opener = opener or urllib.request.urlopen
    url = f"{HF_ENDPOINT}/{repo}/resolve/main/{urllib.parse.quote(rfilename)}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = _auth_headers(token)
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0) + have
            mode = "ab" if have and resp.status == 206 else "wb"
            if mode == "wb":
                have = 0
            done = have
            with open(part, mode) as fh:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    fh.write(buf)
                    done += len(buf)
                    if progress:
                        progress(done, total)
    except urllib.error.HTTPError as exc:
        raise LocalError(f"download HTTP {exc.code} for {rfilename} "
                         f"(gated model? set $HF_TOKEN)") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LocalError(f"download failed for {rfilename}: {exc}") from exc
    part.replace(dest)
    return dest


def pull(ref: str, *, quant: str | None = None, dest_dir: Path | None = None,
         token: str | None = None, opener: Callable[..., Any] | None = None,
         progress: Callable[[int, int], None] | None = None,
         registry: dict[str, Any] | None = None) -> Path:
    """Download the GGUF for ``ref`` into the models dir and return the primary file's path.

    ``ref`` may be a curated alias, an HF ref (``hf.co/org/repo[:quant]``), or a direct local
    ``.gguf`` path / ``http(s)`` ``.gguf`` URL. A split (multi-part) model downloads all parts;
    the first part is returned (llama.cpp auto-loads the rest). Raises ``LocalError`` on failure
    — never partial-success silently."""
    d = dest_dir or models_dir()
    raw = (ref or "").strip()

    # Direct local file already on disk.
    if raw.lower().endswith(".gguf") and Path(raw).expanduser().is_file():
        return Path(raw).expanduser()
    # Direct GGUF URL.
    if raw.lower().startswith(("http://", "https://")) and raw.lower().endswith(".gguf"):
        name = urllib.parse.unquote(raw.rsplit("/", 1)[-1])
        # Reuse the streamer with an absolute URL by faking repo/rfilename split.
        return _download_direct(raw, d / name, token=token, opener=opener, progress=progress)

    spec = resolve_spec(raw, registry, quant=quant)
    files = list_repo_files(spec.repo, token=token, opener=opener)
    picks = _pick_gguf(files, spec.gguf_pattern)
    if not picks:
        raise LocalError(f"no .gguf file matching {spec.gguf_pattern!r} in {spec.repo}")
    primary: Path | None = None
    for i, rf in enumerate(picks):
        target = d / Path(rf).name
        if target.exists():                       # already downloaded — skip
            if primary is None:
                primary = target
            continue
        got = _download_one(spec.repo, rf, target, token=token, opener=opener,
                            progress=progress)
        if primary is None:
            primary = got
    assert primary is not None
    return primary


def _download_direct(url: str, dest: Path, *, token: str | None = None,
                     opener: Callable[..., Any] | None = None,
                     progress: Callable[[int, int], None] | None = None,
                     chunk: int = 1 << 20) -> Path:
    """Stream an absolute GGUF URL to ``dest`` (no HF repo resolution)."""
    opener = opener or urllib.request.urlopen
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers=_auth_headers(token))
    try:
        with opener(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(part, "wb") as fh:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    fh.write(buf)
                    done += len(buf)
                    if progress:
                        progress(done, total)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        raise LocalError(f"download failed for {url}: {exc}") from exc
    part.replace(dest)
    return dest


# --------------------------------------------------------------------------- #
# Ollama detection — stdlib (the zero-download on-ramp)
# --------------------------------------------------------------------------- #
def ollama_models(endpoint: str = DEFAULT_OLLAMA, *,
                  opener: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
    """Models already pulled into a running Ollama, via ``GET /api/tags``. Returns [] when
    Ollama isn't running — never raises (it's a best-effort probe)."""
    opener = opener or urllib.request.urlopen
    try:
        with opener(f"{endpoint.rstrip('/')}/api/tags", timeout=1) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return [{"name": m.get("name", ""), "size_bytes": int(m.get("size") or 0)}
                for m in (data.get("models") or []) if m.get("name")]
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Serving the bundled model — thin live driver (needs the [local] extra)
# --------------------------------------------------------------------------- #
def have_llama_cpp() -> bool:
    return importlib.util.find_spec("llama_cpp") is not None


def serve_command(model_file: Path | str, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                  alias: str = DEFAULT_ALIAS) -> list[str]:
    """The argv to run llama-cpp-python's OpenAI-compatible server over ``model_file`` (pure —
    so the exact command is testable and printable)."""
    return [sys.executable, "-m", "llama_cpp.server", "--model", str(model_file),
            "--host", host, "--port", str(port), "--model_alias", alias]


def serve(model_file: Path | str, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          alias: str = DEFAULT_ALIAS, detach: bool = False) -> subprocess.Popen:
    """Launch the bundled server over ``model_file``. Raises ``LocalError`` when the optional
    dependency or the file is missing. ``detach`` starts it in the background (for the
    dashboard); otherwise it runs in the foreground (Ctrl+C to stop)."""
    if not have_llama_cpp():
        raise LocalError('llama-cpp-python is not installed — run: pip install "throughlog[local]" '
                         "(no C/C++ compiler? add: --extra-index-url "
                         "https://abetlen.github.io/llama-cpp-python/whl/cpu). "
                         "Or use Ollama and point local_endpoint at http://localhost:11434.")
    if not Path(model_file).is_file():
        raise LocalError(f"model file not found: {model_file} — run `tl local pull` first.")
    cmd = serve_command(model_file, host=host, port=port, alias=alias)
    kwargs: dict[str, Any] = {}
    if detach and os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS|NEW_PROCESS_GROUP
    elif detach:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def endpoint_url(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    """The base endpoint config should point at for the bundled server (from_config appends
    /v1). Loopback host keeps the client keyless."""
    return f"http://{host}:{port}"


# --------------------------------------------------------------------------- #
# Config write — reuse the guarded chokepoint (no new config path)
# --------------------------------------------------------------------------- #
def configure(*, endpoint: str, model: str, config_path: str | Path | None = None) -> dict[str, Any]:
    """Point config at a local model: provider=local + local_endpoint + local_model, written
    through the same known-keys-only ``appconfig.update_llm`` chokepoint the dashboard uses."""
    from throughlog import appconfig
    return appconfig.update_llm(
        {"provider": "local", "local_endpoint": endpoint, "local_model": model},
        config_path=config_path)
