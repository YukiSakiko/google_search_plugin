"""Offline regression tests for list-only API key configuration."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import os
import sys
import unittest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from plugins.google_search_plugin.config import EnginesSection, SearchBackendSection  # noqa: E402
from plugins.google_search_plugin.pipelines.engine_chain import (  # noqa: E402
    EngineChain,
    _build_common_cfg,
    _build_engine_dict,
)
from plugins.google_search_plugin.pipelines.image_search_pipeline import ImageSearchPipeline  # noqa: E402
from plugins.google_search_plugin.plugin import GoogleSearchPlugin  # noqa: E402
from plugins.google_search_plugin.search_engines.base import load_api_keys  # noqa: E402
from plugins.google_search_plugin.search_engines.anysearch import AnySearchEngine  # noqa: E402
from plugins.google_search_plugin.search_engines.deepseek import DeepSeekEngine  # noqa: E402
from plugins.google_search_plugin.search_engines.exa import ExaEngine  # noqa: E402
from plugins.google_search_plugin.search_engines.tavily import TavilyEngine  # noqa: E402
from plugins.google_search_plugin.search_engines.you import (  # noqa: E402
    YouContentsClient,
    YouImagesEngine,
    YouLiveNewsEngine,
    YouSearchEngine,
)

_CLIENTS = (
    TavilyEngine,
    AnySearchEngine,
    ExaEngine,
    DeepSeekEngine,
    YouSearchEngine,
    YouLiveNewsEngine,
    YouContentsClient,
    YouImagesEngine,
)
_ENV_KEYS = {
    "TAVILY_API_KEY": "ignored-tavily-env-key",
    "YOU_API_KEY": "ignored-you-env-key",
    "DEEPSEEK_API_KEY": "ignored-deepseek-env-key",
    "ANYSEARCH_API_KEY": "ignored-anysearch-env-key",
    "EXA_API_KEY": "ignored-exa-env-key",
}


def _configured_engines() -> EnginesSection:
    return EnginesSection(
        tavily_enabled=True,
        tavily_api_keys=["tavily-first", "tavily-second"],
        anysearch_enabled=True,
        anysearch_api_keys=["anysearch-first", "anysearch-second"],
        exa_enabled=True,
        exa_api_keys=["exa-first", "exa-second"],
        deepseek_enabled=True,
        deepseek_api_keys=["deepseek-first"],
        you_enabled=True,
        you_news_enabled=True,
        you_contents_enabled=True,
        you_images_enabled=True,
        you_api_keys=["you-first", "you-second"],
    )


class ApiKeyConfigTests(unittest.TestCase):
    def test_models_only_expose_api_key_lists(self) -> None:
        fields = EnginesSection.model_fields
        for provider in ("tavily", "anysearch", "exa", "you", "deepseek"):
            with self.subTest(provider=provider):
                self.assertIn(f"{provider}_api_keys", fields)
                self.assertNotIn(f"{provider}_api_key", fields)
                self.assertEqual(EnginesSection().model_dump()[f"{provider}_api_keys"], [])

    def test_key_lists_are_trimmed_deduplicated_and_do_not_read_other_sources(self) -> None:
        configured = [" first ", "", "second", "first", "  "]
        with patch.dict(os.environ, _ENV_KEYS):
            self.assertEqual(
                load_api_keys({"api_keys": configured, "api_key": "ignored-single-key"}), ["first", "second"]
            )
            self.assertEqual(load_api_keys({"api_key": "ignored-single-key"}), [])
            self.assertEqual(load_api_keys({"api_keys": []}), [])
            self.assertEqual(load_api_keys({}), [])
        self.assertEqual(configured, [" first ", "", "second", "first", "  "])

    def test_all_api_clients_use_only_the_list_and_keep_key_rotation(self) -> None:
        with patch.dict(os.environ, _ENV_KEYS):
            for client_type in _CLIENTS:
                with self.subTest(client=client_type.__name__):
                    client = client_type({"api_keys": [" first ", "second", "first"], "api_key": "ignored-single-key"})
                    self.assertEqual(client.api_keys, ["first", "second"])
                    self.assertTrue(client.has_api_keys())
                    self.assertIn(client._pick_api_key(), client.api_keys)
                    attempts = client._iter_api_keys()
                    self.assertCountEqual(attempts, ["first", "second"])
                    self.assertEqual(len(attempts), 2)
                    empty_client = client_type({"api_key": "ignored-single-key"})
                    self.assertFalse(empty_client.has_api_keys())
                    self.assertEqual(empty_client._iter_api_keys(), [])
                    self.assertIsNone(empty_client._pick_api_key())

    def test_engine_builders_pass_independent_lists_to_all_api_services(self) -> None:
        engines = _configured_engines()
        common = _build_common_cfg(SearchBackendSection())
        providers = {
            "tavily": engines.tavily_api_keys,
            "anysearch": engines.anysearch_api_keys,
            "exa": engines.exa_api_keys,
            "deepseek": engines.deepseek_api_keys,
        }
        providers.update({name: engines.you_api_keys for name in ("you", "you_news", "you_contents", "you_images")})
        for engine_name, expected in providers.items():
            with self.subTest(engine=engine_name):
                config = _build_engine_dict(engine_name, engines, common)
                self.assertEqual(config["api_keys"], expected)
                self.assertIsNot(config["api_keys"], expected)
                self.assertNotIn("api_key", config)
                self.assertTrue(config["enabled"])

    def test_search_and_image_pipelines_share_the_configured_you_keys(self) -> None:
        engines = _configured_engines()
        backend = SearchBackendSection()
        chain = EngineChain(engines, backend)
        images = ImageSearchPipeline(engines_cfg=engines, backend_cfg=backend)
        self.assertEqual(chain.tavily.api_keys, engines.tavily_api_keys)
        self.assertEqual(chain.anysearch.api_keys, engines.anysearch_api_keys)
        self.assertEqual(chain.exa.api_keys, engines.exa_api_keys)
        self.assertEqual(chain.deepseek.api_keys, engines.deepseek_api_keys)
        for client in (chain.you, chain.you_news, chain.you_contents, images.you_images):
            with self.subTest(client=type(client).__name__):
                self.assertEqual(client.api_keys, engines.you_api_keys)
                self.assertTrue(client.has_api_keys())


class EmptyApiKeyRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_keys_do_not_trigger_network_requests(self) -> None:
        config = {"api_key": "ignored-single-key"}
        with (
            patch.dict(os.environ, _ENV_KEYS),
            patch(
                "aiohttp.ClientSession", side_effect=AssertionError("Empty key lists must not issue network requests")
            ),
        ):
            for client_type in (TavilyEngine, ExaEngine, DeepSeekEngine, YouSearchEngine, YouLiveNewsEngine):
                with self.subTest(client=client_type.__name__):
                    self.assertEqual(await client_type(config).search("example", 1), [])
            self.assertEqual(await YouImagesEngine(config).search_images("example", 1), [])
            self.assertEqual(await YouContentsClient(config).fetch_contents(["https://example.com"]), {})


class LegacyApiKeyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = GoogleSearchPlugin()

    def test_old_keys_merge_without_duplicates_or_mutating_input(self) -> None:
        raw = self.plugin.build_default_config()
        raw["engines"].update(
            tavily_api_keys=["same-key"],
            tavily_api_key=" same-key ",
            you_api_keys=["existing-key"],
            you_api_key=" added-key ",
            deepseek_api_key="deepseek-old-key",
        )
        snapshot = deepcopy(raw)
        normalized, changed = self.plugin.normalize_plugin_config(raw)
        self.assertTrue(changed)
        self.assertEqual(raw, snapshot)
        self.assertEqual(normalized["engines"]["tavily_api_keys"], ["same-key"])
        self.assertEqual(normalized["engines"]["you_api_keys"], ["existing-key", "added-key"])
        self.assertEqual(normalized["engines"]["deepseek_api_keys"], ["deepseek-old-key"])
        for provider in ("tavily", "you", "deepseek"):
            self.assertNotIn(f"{provider}_api_key", normalized["engines"])

    def test_host_upgrade_template_keeps_old_keys_until_normalization(self) -> None:
        upgrade_template = self.plugin.get_default_config()
        for provider in ("tavily", "you", "deepseek"):
            with self.subTest(provider=provider):
                self.assertEqual(upgrade_template["engines"][f"{provider}_api_key"], "")
                upgrade_template["engines"][f"{provider}_api_key"] = f"{provider}-old-key"
        normalized, changed = self.plugin.normalize_plugin_config(upgrade_template)
        self.assertTrue(changed)
        for provider in ("tavily", "you", "deepseek"):
            self.assertEqual(normalized["engines"][f"{provider}_api_keys"], [f"{provider}-old-key"])
            self.assertNotIn(f"{provider}_api_key", self.plugin.build_default_config()["engines"])

    def test_new_config_and_repeated_normalization_only_contain_lists(self) -> None:
        with patch.dict(os.environ, _ENV_KEYS):
            normalized, _ = self.plugin.normalize_plugin_config(self.plugin.get_default_config())
        self.assertEqual(normalized, self.plugin.build_default_config())
        repeated, changed = self.plugin.normalize_plugin_config(normalized)
        self.assertEqual(repeated, normalized)
        self.assertFalse(changed)

    def test_config_injection_exposes_migrated_keys_not_old_fields(self) -> None:
        raw = self.plugin.build_default_config()
        raw["engines"]["you_api_key"] = "old-you-key"
        self.plugin.set_plugin_config(raw)
        self.assertEqual(self.plugin.config.engines.you_api_keys, ["old-you-key"])
        self.assertNotIn("you_api_key", self.plugin.get_plugin_config_data()["engines"])
        schema = self.plugin.get_webui_config_schema()
        self.assertIn("you_api_keys", schema["sections"]["you"]["fields"])
        self.assertNotIn("you_api_key", schema["sections"]["you"]["fields"])

    def test_invalid_old_key_or_list_type_is_not_silently_discarded(self) -> None:
        for fields in ({"you_api_key": 123}, {"you_api_key": "old-key", "you_api_keys": "not-a-list"}):
            raw = self.plugin.build_default_config()
            raw["engines"].update(fields)
            with self.subTest(fields=tuple(fields)), self.assertRaises(ValueError):
                self.plugin.normalize_plugin_config(raw)


if __name__ == "__main__":
    unittest.main()
