"""Tests for configuration assembly shared by the CLI and the HTTP API."""
from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import config as config_mod
from core import models
from core.config import Presets


class DeriveContextFieldsTests(unittest.TestCase):
    def test_budgets_derive_from_probed_window(self):
        fields = config_mod.derive_context_fields(8000)
        self.assertEqual(fields["n_ctx"], 8000)
        self.assertEqual(fields["max_gen_tokens"], 4000)
        self.assertEqual(fields["compact_trigger"], 4000)
        self.assertEqual(fields["compact_target"], 2000)


class ProbeCacheTests(unittest.TestCase):
    """The probe cache stops front-end refresh storms from re-probing the server."""

    def setUp(self):
        config_mod.reset_probe_cache()
        self.addCleanup(config_mod.reset_probe_cache)

    def test_repeated_lookups_hit_the_cache(self):
        with mock.patch.object(config_mod, "probe_context_window", return_value=64000) as probe:
            first = config_mod._probe_window("http://cache.test/v1", model="m")
            second = config_mod._probe_window("http://cache.test/v1", model="m")
        self.assertEqual(first, 64000)
        self.assertEqual(second, 64000)
        self.assertEqual(probe.call_count, 1)

    def test_model_changes_use_distinct_cache_keys(self):
        with mock.patch.object(config_mod, "probe_context_window", return_value=64000) as probe:
            config_mod._probe_window("http://cache.test/v1", model="a")
            config_mod._probe_window("http://cache.test/v1", model="b")
        self.assertEqual(probe.call_count, 2)

    def test_hot_reload_resets_the_cache(self):
        with mock.patch.object(config_mod, "probe_context_window", return_value=64000) as probe:
            config_mod._probe_window("http://cache.test/v1", model="m")
            config_mod.reset_probe_cache()
            config_mod._probe_window("http://cache.test/v1", model="m")
        self.assertEqual(probe.call_count, 2)

    def test_save_providers_file_clears_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_mod._PROBED_WINDOW_CACHE["stale|key"] = 1
            config_mod.save_providers_file(config_mod.DEFAULT_PROVIDERS_TEMPLATE, providers_path=Path(tmp) / "p.json")
        self.assertEqual(config_mod._PROBED_WINDOW_CACHE, {})


class SlotAffinityTests(unittest.TestCase):
    """Dual-slot affinity: main -> id_slot 0; subagent/compactor -> id_slot 1."""

    def test_role_chat_extra_carries_id_slot(self):
        main = config_mod._resolve_role_endpoint("main", providers_data=None)
        sub = config_mod._resolve_role_endpoint("subagent", providers_data=None)
        comp = config_mod._resolve_role_endpoint("compactor", providers_data=None)
        self.assertEqual(main["chat_extra"]["id_slot"], 0)
        self.assertEqual(sub["chat_extra"]["id_slot"], 1)
        self.assertEqual(comp["chat_extra"]["id_slot"], 1)

    def test_explicit_id_slot_is_respected(self):
        providers = {
            "active_profiles": {"main": "custom"},
            "profiles": {
                "custom": {
                    "base_url": "http://slot.test/v1",
                    "model": "m",
                    "chat_extra": {"id_slot": 5},
                }
            },
        }
        resolved = config_mod._resolve_role_endpoint("main", providers_data=providers)
        self.assertEqual(resolved["chat_extra"]["id_slot"], 5)


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        config_mod.reset_probe_cache()
        self.addCleanup(config_mod.reset_probe_cache)

    def test_load_config_uses_probed_window(self):
        with mock.patch.object(config_mod, "probe_context_window", return_value=12345):
            cfg = config_mod.load_config()
        self.assertEqual(cfg.n_ctx, 12345)
        self.assertEqual(cfg.max_gen_tokens, 6172)
        self.assertEqual(cfg.model, models.MODEL)
        self.assertEqual(cfg.base_url, models.BASE_URL)


class ProbeContextWindowTests(unittest.TestCase):
    """The context window comes from the server; probing never raises."""

    @staticmethod
    def _urlopen(payload):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__ = mock.MagicMock(return_value=response)
        response.__exit__ = mock.MagicMock(return_value=False)
        return mock.MagicMock(return_value=response)

    def test_llamacpp_meta_n_ctx(self):
        payload = {"data": [{"id": "m", "meta": {"n_ctx": 262144}}]}
        with mock.patch("urllib.request.urlopen", self._urlopen(payload)):
            self.assertEqual(models.probe_context_window("http://x:8080/v1"), 262144)

    def test_openrouter_context_length_field(self):
        payload = {"data": [{"id": "m", "context_length": 8192}]}
        with mock.patch("urllib.request.urlopen", self._urlopen(payload)):
            self.assertEqual(models.probe_context_window("http://x/v1"), 8192)

    def test_unreachable_server_falls_back(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
            self.assertEqual(models.probe_context_window("http://x/v1"), models.FALLBACK_N_CTX)

    def test_malformed_payload_falls_back(self):
        with mock.patch("urllib.request.urlopen", self._urlopen({"data": [{"id": "m"}]})):
            self.assertEqual(models.probe_context_window("http://x/v1"), models.FALLBACK_N_CTX)


class EndpointConfigurationTests(unittest.TestCase):
    """The endpoint is environment-configured; no private address is hardcoded."""

    def test_env_overrides_base_url_and_model(self):
        env = {"GEMMA_BASE_URL": "http://example.test:8080/v1", "GEMMA_MODEL": "example-model"}
        with mock.patch.dict(os.environ, env):
            reloaded = importlib.reload(models)
            try:
                self.assertEqual(reloaded.BASE_URL, "http://example.test:8080/v1")
                self.assertEqual(reloaded.MODEL, "example-model")
            finally:
                importlib.reload(models)  # restore defaults for the rest of the suite

    def test_neutral_defaults(self):
        neutral = {"GEMMA_BASE_URL": "", "GEMMA_MODEL": ""}
        with mock.patch.dict(os.environ, neutral):
            reloaded = importlib.reload(models)
            try:
                self.assertEqual(reloaded.BASE_URL, "http://127.0.0.1:8080/v1")
                self.assertEqual(reloaded.MODEL, "local-model")
            finally:
                importlib.reload(models)  # restore the caller's environment values


class MultiApiTieredRoutingTests(unittest.TestCase):
    """Tests for Multi-API role-tiered subscription routing (GATE-FUNC-05)."""

    def setUp(self):
        config_mod.reset_probe_cache()
        self.addCleanup(config_mod.reset_probe_cache)

    def test_providers_json_routes_main_subagent_and_compactor_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            prov_path = Path(tmp) / "providers.json"
            config_mod.save_providers_file(config_mod.DEFAULT_PROVIDERS_TEMPLATE, providers_path=prov_path)

            with mock.patch.dict(os.environ, {"FLAGSHIP_API_KEY": "k_flag", "LOCAL_API_KEY": "k_local"}), \
                    mock.patch.object(config_mod, "probe_context_window", side_effect=lambda url, **kw: kw.get("fallback", 32768)):
                main_cfg = config_mod.load_config(role="main", providers_path=prov_path)
                sub_cfg = config_mod.load_config(role="subagent", providers_path=prov_path)
                comp_cfg = config_mod.load_config(role="compactor", providers_path=prov_path)

            self.assertEqual(main_cfg.model, "claude-3-7-sonnet-or-gemma4-pro")
            self.assertEqual(main_cfg.api_key, "k_flag")
            self.assertEqual(main_cfg.n_ctx, 128000)
            self.assertEqual(main_cfg.price_per_1m_input, 3.00)

            self.assertEqual(sub_cfg.model, "gemma-4-9b-instruct")
            self.assertEqual(sub_cfg.api_key, "k_local")
            self.assertEqual(sub_cfg.n_ctx, 32768)

            self.assertEqual(comp_cfg.model, "local-model")
            self.assertEqual(comp_cfg.price_per_1m_input, 0.0)

    def test_role_env_overrides_and_gemma_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_prov = Path(tmp) / "nonexistent.json"
            env = {
                "GEMMA_BASE_URL": "http://127.0.0.1:8080/v1",
                "GEMMA_MODEL": "base-gemma",
                "SUBAGENT_BASE_URL": "http://127.0.0.1:9090/v1",
                "SUBAGENT_MODEL": "fast-sub-model",
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(config_mod, "probe_context_window", return_value=16384):
                main_cfg = config_mod.load_config(role="main", providers_path=missing_prov)
                sub_cfg = config_mod.load_config(role="subagent", providers_path=missing_prov)

            self.assertEqual(main_cfg.model, "base-gemma")
            self.assertEqual(sub_cfg.model, "fast-sub-model")
            self.assertEqual(sub_cfg.base_url, "http://127.0.0.1:9090/v1")


class NoDefaultSystemPromptTests(unittest.TestCase):
    """The system prompt lives in .agent/AGENTS.md; nothing is preset in code."""

    def test_no_default_prompt_constant(self):
        self.assertFalse(hasattr(models, "DEFAULT_SYSTEM"))
        self.assertFalse(hasattr(Presets, "DEFAULT_SYSTEM"))

    def test_only_safety_presets_remain(self):
        self.assertTrue(Presets.DANGEROUS_SHELL_REGEX)


if __name__ == "__main__":
    unittest.main()