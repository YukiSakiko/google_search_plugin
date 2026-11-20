import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from plugins.google_search_plugin.config import ReverseImageSection, SearchBackendSection
from plugins.google_search_plugin.pipelines import reverse_image_pipeline as reverse
from plugins.google_search_plugin.plugin import GoogleSearchPlugin
from plugins.google_search_plugin.search_engines.reverse_image import ReverseImageMatch, ReverseImageOutput


class ReverseBudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_registered_budget_includes_enabled_stages(self):
        backend = SearchBackendSection(timeout=40, content_timeout=12)
        cfg = ReverseImageSection(fetch_content=True)
        total = reverse.reverse_rpc_budget(cfg, backend)
        self.assertGreater(total, sum(reverse._stage_limits(40)) + 2 * 12)
        cfg.google_lens_enabled = False
        self.assertLess(reverse.reverse_rpc_budget(cfg, backend), total)
        self.assertGreaterEqual(GoogleSearchPlugin()._reverse_image_rpc_timeout_ms(), 120_000)

    async def test_slow_lens_leaves_time_for_fallback(self):
        pipeline = reverse.ReverseImagePipeline(reverse_cfg=ReverseImageSection(), backend_cfg=SearchBackendSection())
        started = asyncio.Event()

        async def slow(**kwargs):
            started.set()
            await asyncio.Future()

        pipeline.google_lens.search = slow
        pipeline.yandex.search = AsyncMock(
            return_value=ReverseImageOutput(
                engine="yandex", matches=[ReverseImageMatch(title="match", url="https://example.org")]
            )
        )
        with patch.object(reverse, "_stage_limits", return_value=(0, 2, 2)):
            result = await pipeline.search_raw(image_bytes=b"fixture")
        self.assertTrue(started.is_set())
        self.assertEqual(result.engine, "yandex")
        pipeline.yandex.search.assert_awaited_once()
