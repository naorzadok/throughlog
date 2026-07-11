"""Local-backend tests — the no-key, in-machine LLM path (client wiring + `tl local` core +
dashboard model management). All offline: a fake `opener` stands in for Hugging Face / Ollama /
the model server, so nothing here touches the network."""

import io
import json
import os
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from throughlog.llm.client import LLMConfig, LLMClient, LLMError
from throughlog.llm import local as L
from throughlog import server as S


# --------------------------------------------------------------------------- #
# Fake HTTP plumbing
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, body: bytes, headers: dict | None = None, status: int = 200):
        self._b = body
        self.headers = headers or {}
        self.status = status

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            b, self._b = self._b, b""
            return b
        b, self._b = self._b[:n], self._b[n:]
        return b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _chat_opener(capture: list | None = None, reply: str = "pong"):
    """A fake urlopen for the chat endpoint that records each request and echoes a reply."""
    body = json.dumps({"choices": [{"message": {"content": reply}}]}).encode()

    def opener(req, timeout=None):
        if capture is not None:
            capture.append({
                "url": req.full_url,
                "headers": {k.lower(): v for k, v in req.header_items()},
                "body": json.loads(req.data.decode()),
            })
        return _Resp(body)
    return opener


def _cfg(**llm) -> dict:
    return {"llm": llm}


# --------------------------------------------------------------------------- #
# Client wiring
# --------------------------------------------------------------------------- #
class LocalClientWiring(unittest.TestCase):
    def test_provider_local_reads_endpoint_model_and_zeroes_pacing(self):
        c = LLMConfig.from_config(_cfg(provider="local",
                                       local_endpoint="http://localhost:11434",
                                       local_model="nano", max_requests_per_min=18))
        self.assertTrue(c.is_local)
        self.assertEqual(c.local_model, "nano")
        self.assertEqual(c.max_requests_per_min, 0)          # pacing disabled for local

    def test_loopback_base_url_is_local(self):
        self.assertTrue(LLMConfig.from_config(_cfg(base_url="http://127.0.0.1:1234/v1")).is_local)
        self.assertTrue(LLMConfig.from_config(_cfg(base_url="http://localhost:8080/v1")).is_local)
        self.assertFalse(LLMConfig.from_config(_cfg(base_url="https://openrouter.ai/api/v1")).is_local)

    def test_local_target_keyless_no_referer_and_v1_normalized(self):
        cap = []
        c = LLMClient(LLMConfig.from_config(_cfg(provider="local",
                                                 local_endpoint="http://localhost:11434",
                                                 local_model="nano")),
                      opener=_chat_opener(cap), sleep=lambda _: None)
        self.assertEqual(c.chat("s", "u"), "pong")
        self.assertEqual(cap[0]["url"], "http://localhost:11434/v1/chat/completions")
        self.assertEqual(cap[0]["body"]["model"], "nano")
        self.assertEqual(cap[0]["headers"]["authorization"], "Bearer sk-local")
        self.assertNotIn("http-referer", cap[0]["headers"])   # OpenRouter attribution dropped

    def test_cloud_wire_keeps_referer_and_real_key(self):
        cap = []
        c = LLMClient(LLMConfig.from_config(_cfg(model="m", api_key="real-key")),
                      opener=_chat_opener(cap), sleep=lambda _: None)
        c.chat("s", "u")
        self.assertEqual(cap[0]["headers"]["authorization"], "Bearer real-key")
        self.assertIn("http-referer", cap[0]["headers"])      # unchanged for openrouter

    def test_cloud_requires_key_local_does_not(self):
        with self.assertRaises(LLMError):
            LLMClient(LLMConfig.from_config(_cfg(model="m")),   # cloud, no key
                      opener=_chat_opener(), sleep=lambda _: None).chat("s", "u")
        # local, no key -> fine
        out = LLMClient(LLMConfig.from_config(_cfg(provider="local", local_model="nano")),
                        opener=_chat_opener(), sleep=lambda _: None).chat("s", "u")
        self.assertEqual(out, "pong")

    def test_hybrid_local_primary_cloud_fallback(self):
        seen = []
        good = json.dumps({"choices": [{"message": {"content": "cloud"}}]}).encode()

        def opener(req, timeout=None):
            seen.append(req.full_url)
            if "11434" in req.full_url:               # local primary is down
                raise urllib.error.URLError("refused")
            return _Resp(good)

        c = LLMClient(LLMConfig.from_config(_cfg(
            provider="local", local_endpoint="http://localhost:11434", local_model="nano",
            model_fallback="cloud/model", api_key="k", base_url="https://openrouter.ai/api/v1")),
            opener=opener, sleep=lambda _: None)
        self.assertEqual(c.chat("s", "u"), "cloud")
        self.assertIn("11434", seen[0])               # tried local first
        self.assertIn("openrouter.ai", seen[-1])      # fell back to cloud
        self.assertTrue(c.calls[-1].fallback_used)

    def test_pure_local_has_no_cloud_fallback_without_key(self):
        c = LLMClient(LLMConfig.from_config(_cfg(provider="local", local_model="nano",
                                                 model_fallback="cloud/model")))
        targets = c._targets()
        self.assertEqual(len(targets), 1)             # no key -> no cloud fallback target
        self.assertTrue(targets[0].is_local)


class BuildClientKeylessLocal(unittest.TestCase):
    def test_cli_build_client_builds_local_without_key(self):
        from throughlog.cli import build_client
        client = build_client(_cfg(provider="local", local_model="nano"), enable=True)
        self.assertIsNotNone(client)

    def test_cli_build_client_none_for_cloud_without_key(self):
        from throughlog.cli import build_client
        self.assertIsNone(build_client(_cfg(model="m"), enable=True))

    def test_server_llm_client_builds_local_without_key(self):
        self.assertIsNotNone(S._llm_client(_cfg(provider="local", local_model="nano")))


# --------------------------------------------------------------------------- #
# Registry + reference resolution (pure)
# --------------------------------------------------------------------------- #
class ResolveSpec(unittest.TestCase):
    def test_curated_alias(self):
        spec = L.resolve_spec("nemotron-3-nano-4b")
        self.assertEqual(spec.repo, "nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF")
        self.assertIn("q4_k_m", spec.gguf_pattern.lower())

    def test_escape_hatch_with_and_without_prefix_and_quant(self):
        self.assertEqual(L.resolve_spec("hf.co/foo/bar:Q5_K_M").repo, "foo/bar")
        self.assertEqual(L.resolve_spec("hf.co/foo/bar:Q5_K_M").gguf_pattern, "*q5_k_m*.gguf")
        self.assertEqual(L.resolve_spec("foo/bar").repo, "foo/bar")
        self.assertEqual(L.resolve_spec("https://huggingface.co/a/b").repo, "a/b")

    def test_explicit_quant_overrides_curated_pattern(self):
        self.assertEqual(L.resolve_spec("nemotron-3-nano-4b", quant="Q8_0").gguf_pattern,
                         "*q8_0*.gguf")

    def test_bad_ref_raises(self):
        with self.assertRaises(L.LocalError):
            L.resolve_spec("not-an-alias-and-no-slash")

    def test_pick_gguf_prefers_pattern_and_keeps_split_set(self):
        files = ["README.md", "m-Q4_K_M.gguf", "m-Q8_0.gguf",
                 "big-Q4_K_M-00001-of-00002.gguf", "big-Q4_K_M-00002-of-00002.gguf"]
        picks = L._pick_gguf(files, "*q4_k_m*.gguf")
        self.assertIn("m-Q4_K_M.gguf", picks)
        self.assertIn("big-Q4_K_M-00001-of-00002.gguf", picks)
        self.assertNotIn("m-Q8_0.gguf", picks)

    def test_pick_gguf_falls_back_to_any_gguf(self):
        self.assertEqual(L._pick_gguf(["only.gguf", "x.txt"], "*nomatch*.gguf"), ["only.gguf"])


# --------------------------------------------------------------------------- #
# Download + Ollama detection (stdlib, injected opener)
# --------------------------------------------------------------------------- #
class DownloadAndDetect(unittest.TestCase):
    def _hf_opener(self, files: list[str], blob: bytes = b"GGUF" * 8):
        def opener(req, timeout=None):
            if "/api/models/" in req.full_url:
                return _Resp(json.dumps(
                    {"siblings": [{"rfilename": f} for f in files]}).encode())
            if req.full_url.endswith(".gguf"):
                return _Resp(blob, headers={"Content-Length": str(len(blob))})
            raise AssertionError(req.full_url)
        return opener

    def test_pull_downloads_matching_gguf_with_progress(self):
        with TemporaryDirectory() as d:
            seen = []
            p = L.pull("foo/bar", dest_dir=Path(d),
                       opener=self._hf_opener(["m-Q4_K_M.gguf", "x.md"]),
                       progress=lambda a, b: seen.append((a, b)))
            self.assertEqual(Path(p).name, "m-Q4_K_M.gguf")
            self.assertTrue(Path(p).is_file())
            self.assertEqual(seen[-1][0], seen[-1][1])       # progress reached total

    def test_pull_no_matching_file_raises(self):
        with TemporaryDirectory() as d:
            with self.assertRaises(L.LocalError):
                L.pull("foo/bar", dest_dir=Path(d), opener=self._hf_opener(["only.txt"]))

    def test_list_repo_files_network_error_raises_localerror(self):
        def opener(req, timeout=None):
            raise urllib.error.URLError("down")
        with self.assertRaises(L.LocalError):
            L.list_repo_files("foo/bar", opener=opener)

    def test_ollama_models_parses(self):
        def opener(req, timeout=None):
            return _Resp(json.dumps({"models": [{"name": "qwen2.5:3b", "size": 2_000_000_000},
                                                {"name": "nano:latest"}]}).encode())
        got = L.ollama_models(opener=opener)
        self.assertEqual([m["name"] for m in got], ["qwen2.5:3b", "nano:latest"])

    def test_ollama_models_tolerates_failure(self):
        def opener(req, timeout=None):
            raise urllib.error.URLError("refused")
        self.assertEqual(L.ollama_models(opener=opener), [])

    def test_installed_models_folds_split_parts(self):
        with TemporaryDirectory() as d:
            for name in ("a-00001-of-00002.gguf", "a-00002-of-00002.gguf", "solo.gguf"):
                (Path(d) / name).write_bytes(b"x" * 10)
            names = {m["name"] for m in L.installed_models(Path(d))}
            self.assertEqual(names, {"a-00001-of-00002.gguf", "solo.gguf"})

    def test_configure_writes_provider_local(self):
        with TemporaryDirectory() as d:
            cfgp = Path(d) / "config.json"
            cfgp.write_text(json.dumps({"llm": {"provider": "openrouter"}}), encoding="utf-8")
            L.configure(endpoint="http://127.0.0.1:8080", model="local", config_path=cfgp)
            llm = json.loads(cfgp.read_text())["llm"]
            self.assertEqual(llm["provider"], "local")
            self.assertEqual(llm["local_endpoint"], "http://127.0.0.1:8080")
            self.assertEqual(llm["local_model"], "local")


# --------------------------------------------------------------------------- #
# Dashboard model management
# --------------------------------------------------------------------------- #
class DashboardLocal(unittest.TestCase):
    def _ollama_opener(self, tags):
        def opener(req, timeout=None):
            return _Resp(json.dumps({"models": [{"name": t} for t in tags]}).encode())
        return opener

    def test_local_model_choices_lists_downloaded_and_ollama(self):
        with TemporaryDirectory() as d:
            (Path(d) / "nano-Q4_K_M.gguf").write_bytes(b"x" * 100)
            cfg = _cfg(provider="local", local_model="local",
                       local_endpoint="http://127.0.0.1:8080")
            choices = S.local_model_choices(cfg, Path(d), opener=self._ollama_opener(["q:3b"]))
            kinds = {c["kind"] for c in choices}
            self.assertEqual(kinds, {"bundled", "ollama"})
            bundled = next(c for c in choices if c["kind"] == "bundled")
            self.assertTrue(bundled["active"])               # matches current config
            self.assertTrue(bundled["value"].startswith("bundled::"))

    def test_local_settings_card_renders_download_options(self):
        with TemporaryDirectory() as d:
            card = S.local_settings_card(_cfg(provider="openrouter"), "tok",
                                         models_dir=Path(d), opener=self._ollama_opener([]))
            self.assertIn("Local model", card)
            self.assertIn("nemotron-3-nano-4b", card)        # curated option present
            self.assertIn("/settings/local/pull", card)

    def test_background_pull_writes_status_transitions(self):
        orig = L.pull
        try:
            def fake_pull(ref, *, quant=None, dest_dir=None, progress=None, **kw):
                if progress:
                    progress(5, 10)
                p = Path(dest_dir) / "m.gguf"
                p.write_bytes(b"x")
                return p
            L.pull = fake_pull
            with TemporaryDirectory() as d:
                md = Path(d)
                self.assertTrue(S.start_background_pull("foo/bar", quant=None, models_dir=md))
                for _ in range(200):                          # wait for the daemon thread
                    st = S.read_pull_status(md)
                    if st and st.get("state") == "done":
                        break
                    time.sleep(0.01)
                self.assertEqual(S.read_pull_status(md)["state"], "done")
                self.assertEqual(S.read_pull_status(md)["name"], "m.gguf")
        finally:
            L.pull = orig

    def test_background_pull_single_flight(self):
        # Hold the lock to simulate an in-flight pull; a second start is refused.
        acquired = S._pull_lock.acquire(blocking=False)
        try:
            with TemporaryDirectory() as d:
                self.assertFalse(S.start_background_pull("x/y", quant=None, models_dir=Path(d)))
        finally:
            if acquired:
                S._pull_lock.release()


if __name__ == "__main__":
    unittest.main()
