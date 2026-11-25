"""Plugin-boundary regressions for migrating and displaying engine order."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, Mock

_HOST_ROOT = Path(__file__).resolve().parents[3]
if str(_HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(_HOST_ROOT))

from plugins.google_search_plugin.config import LEGACY_ENGINE_ORDER, SearchBackendSection  # noqa: E402
from plugins.google_search_plugin.plugin import GoogleSearchPlugin  # noqa: E402


def _config(backend: dict) -> dict:
    return {"plugin": GoogleSearchPlugin.build_default_config()["plugin"], "search_backend": backend}


class EngineOrderPluginTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_order_is_migrated_before_sdk_defaults_even_without_engines_section(self) -> None:
        plugin = GoogleSearchPlugin()
        raw = _config({"default_engine": "exa", "max_results": 4})
        snapshot = deepcopy(raw)
        normalized, changed = plugin.normalize_plugin_config(raw)
        self.assertTrue(changed)
        self.assertEqual(raw, snapshot)
        rows = normalized["search_backend"]["engine_order"]
        self.assertEqual(rows[0], {"engine": "Exa"})
        self.assertTrue(all(isinstance(row, dict) and set(row) == {"engine"} for row in rows))
        self.assertEqual(
            SearchBackendSection.model_validate(normalized["search_backend"]).engine_ids,
            [
                "exa",
                "deepseek",
                "tavily",
                "webz",
                "anysearch",
                "keenable",
                "you",
                "you_news",
                "google",
                "bing",
                "duckduckgo",
                "sogou",
            ],
        )
        self.assertEqual(normalized["search_backend"]["max_results"], 4)

    def test_explicit_order_and_empty_order_survive_sdk_normalization(self) -> None:
        plugin = GoogleSearchPlugin()
        canonical = [{"engine": "Webz.io"}, {"engine": "Exa"}]
        for order, expected in ((["webz", "exa"], canonical), (canonical, canonical), ([], [])):
            with self.subTest(order=order):
                raw = _config({"default_engine": "google", "engine_order": order})
                normalized, _ = plugin.normalize_plugin_config(raw)
                self.assertEqual(normalized["search_backend"]["engine_order"], expected)
                second, changed = plugin.normalize_plugin_config(normalized)
                self.assertEqual(second, normalized)
                self.assertFalse(changed)

    async def test_status_reports_the_order_instead_of_the_legacy_default(self) -> None:
        plugin = GoogleSearchPlugin()
        plugin.set_plugin_config(_config({"default_engine": "google", "engine_order": ["webz", "exa"]}))
        ok, message, _ = await plugin.handle_status()
        self.assertTrue(ok)
        self.assertIn("搜索顺序: webz → exa", message)
        self.assertNotIn("默认引擎:", message)

    async def test_status_explains_an_empty_order_without_sending_a_message(self) -> None:
        plugin = GoogleSearchPlugin()
        plugin.set_plugin_config(_config({"engine_order": []}))
        ok, message, _ = await plugin.handle_status()
        self.assertTrue(ok)
        self.assertIn("不发起网页搜索", message)

    async def test_reordered_config_rebuilds_the_live_pipeline(self) -> None:
        plugin = GoogleSearchPlugin()
        plugin._set_context(
            SimpleNamespace(
                logger=Mock(),
                config=SimpleNamespace(get=AsyncMock(return_value="Test bot")),
                component=SimpleNamespace(
                    enable_component=AsyncMock(return_value={"success": True}),
                    disable_component=AsyncMock(return_value={"success": True}),
                ),
            )
        )
        plugin.set_plugin_config(_config({"engine_order": ["bing", "sogou"]}))
        await plugin.on_load()
        try:
            previous = plugin._engine_chain
            updated = _config({"default_engine": "bing", "engine_order": [{"engine": "搜狗"}, {"engine": "Bing"}]})
            plugin.set_plugin_config(updated)
            await plugin.on_config_update("plugin", updated, "test")
            chain = plugin._engine_chain
            self.assertIsNot(chain, previous)
            attempts = Mock()
            for engine_id in LEGACY_ENGINE_ORDER:
                engine = getattr(chain, engine_id)
                engine.search = AsyncMock(return_value=[])
                attempts.attach_mock(engine.search, engine_id)
            self.assertEqual(await chain.search_with_fallback("public test", 1), ([], ""))
            self.assertEqual([call[0] for call in attempts.mock_calls], ["sogou", "bing"])
        finally:
            await plugin.on_unload()


if __name__ == "__main__":
    unittest.main()
