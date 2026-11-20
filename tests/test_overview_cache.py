import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from plugins.google_search_plugin.config import ReverseImageSection, SearchBackendSection
from plugins.google_search_plugin.pipelines import reverse_image_pipeline as reverse
from plugins.google_search_plugin.search_engines.reverse_image import ReverseImageMatch, ReverseImageOutput

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC")
MATCH = ReverseImageMatch(title="Result", url="https://example.org/result")


class LensOverviewCacheTests(unittest.IsolatedAsyncioTestCase):
    def make_pipeline(self, ttl=3600):
        pipe = reverse.ReverseImagePipeline(
            reverse_cfg=ReverseImageSection(cache_ttl_seconds=ttl), backend_cfg=SearchBackendSection()
        )
        pipe.download_image_url = AsyncMock(return_value=PNG)
        return pipe

    async def test_overview_wait_reaches_lens_and_time_budgets(self):
        cfg = ReverseImageSection(google_lens_overview_wait_seconds=30)
        backend = SearchBackendSection(timeout=40)
        pipe = reverse.ReverseImagePipeline(reverse_cfg=cfg, backend_cfg=backend)
        self.assertEqual(pipe.google_lens.overview_wait_seconds, 30)
        pipe.google_lens.search = AsyncMock(return_value=ReverseImageOutput(engine="google_lens", matches=[MATCH]))
        with patch.object(reverse, "_run_stage", wraps=reverse._run_stage) as stage:
            await pipe.search_raw(image_bytes=PNG)
        self.assertEqual(stage.await_args.args[1], reverse._stage_limits(40, 0)[0] + 30)
        budget = reverse.reverse_rpc_budget(cfg, backend)
        cfg.google_lens_overview_wait_seconds = 0
        self.assertEqual(budget, reverse.reverse_rpc_budget(cfg, backend) + 30)

    async def test_links_only_url_alias_cannot_extend_the_next_attempt(self):
        pipe = self.make_pipeline()
        pipe.search_raw = AsyncMock(
            side_effect=[
                ReverseImageOutput(engine="google_lens", matches=[MATCH], ai_overview=" "),
                ReverseImageOutput(engine="google_lens", ai_overview="New overview"),
            ]
        )
        clock = SimpleNamespace(monotonic=MagicMock(return_value=100.0))
        with patch.object(reverse, "time", clock):
            links = await pipe.run(image_url="https://example.org/a")
            self.assertIn("未读取到 Google AI 概览", links)
            self.assertIn(MATCH.url, links)
            clock.monotonic.return_value = 114.0
            self.assertEqual(await pipe.run(image_url="https://example.org/b"), links)
            self.assertEqual(await pipe.run(image_bytes=PNG), links)
            pipe.search_raw.assert_awaited_once()
            clock.monotonic.return_value = 115.0
            full = await pipe.run(image_url="https://example.org/b")
            self.assertIn("New overview", full)
            self.assertNotIn("未读取到", full)
            self.assertEqual(pipe.search_raw.await_count, 2)
            clock.monotonic.return_value = 190.0
            self.assertEqual(await pipe.run(image_url="https://example.org/b"), full)
            self.assertEqual(pipe.download_image_url.await_count, 4)
            self.assertEqual(pipe.search_raw.await_count, 2)

    async def test_short_configured_ttl_and_disabled_cache_are_respected(self):
        for ttl in (0, 4):
            with self.subTest(ttl=ttl):
                pipe = self.make_pipeline(ttl)
                pipe.search_raw = AsyncMock(return_value=ReverseImageOutput(engine="google_lens", matches=[MATCH]))
                clock = SimpleNamespace(monotonic=MagicMock(return_value=50.0))
                with patch.object(reverse, "time", clock):
                    await pipe.run(image_url="https://example.org/photo")
                    clock.monotonic.return_value = 50.0 + ttl
                    await pipe.run(image_url="https://example.org/photo")
                    self.assertEqual(pipe.search_raw.await_count, 2)
                    self.assertEqual(pipe.download_image_url.await_count, 2)

    async def test_non_google_results_do_not_claim_a_missing_google_overview(self):
        pipe = self.make_pipeline()
        pipe.search_raw = AsyncMock(return_value=ReverseImageOutput(engine="yandex", matches=[MATCH]))
        clock = SimpleNamespace(monotonic=MagicMock(return_value=0.0))
        with patch.object(reverse, "time", clock):
            text = await pipe.run(image_bytes=PNG)
            self.assertNotIn("Google", text)
            clock.monotonic.return_value = 20.0
            self.assertEqual(await pipe.run(image_bytes=PNG), text)
            pipe.search_raw.assert_awaited_once()
