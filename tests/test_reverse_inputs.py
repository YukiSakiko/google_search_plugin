"""Offline checks for public media downloads and reverse search input handling."""

import asyncio
import base64
import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from yarl import URL

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from plugins.google_search_plugin.config import GoogleSearchPluginConfig, ReverseImageSection, SearchBackendSection
from plugins.google_search_plugin.pipelines import public_fetch
from plugins.google_search_plugin.pipelines.image_search_pipeline import ImageSearchPipeline
from plugins.google_search_plugin.pipelines.reverse_image_pipeline import ReverseImagePipeline
from plugins.google_search_plugin.plugin import GoogleSearchPlugin
from plugins.google_search_plugin.search_engines.reverse_image.base import (
    ReverseImageOutput,
    verify_image_data,
)

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC")


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolution_rejects_mixed_public_and_nonpublic_answers(self):
        records = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 443)) for ip in ("1.1.1.1", "127.0.0.1")]
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=records)):
            with self.assertRaisesRegex(public_fetch.PublicFetchError, "不是公网"):
                await public_fetch._public_addresses(URL("https://example.org/image"))

    async def test_redirect_revalidated_before_any_followup_request(self):
        target = "https://images.example.org/sample.png"
        response = MagicMock(status=302, headers={"Location": "/next"})
        with (
            patch.object(public_fetch.aiohttp, "ClientSession") as session_factory,
            patch.object(
                public_fetch,
                "_public_addresses",
                AsyncMock(side_effect=[["1.1.1.1"], public_fetch.PublicFetchError("blocked")]),
            ) as resolve,
        ):
            session = MagicMock()
            session_factory.return_value.__aenter__.return_value = session
            session.get.return_value.__aenter__.return_value = response
            with self.assertRaisesRegex(public_fetch.PublicFetchError, "blocked"):
                await public_fetch.fetch_public_resource(
                    target, proxy="http://proxy.example", timeout=10, max_bytes=1024
                )
            self.assertEqual(resolve.await_count, 2)
            session.get.assert_called_once()
            args, kwargs = session.get.call_args
            self.assertEqual(args[0].raw_host, "1.1.1.1")
            self.assertEqual(kwargs["headers"]["Host"], "images.example.org")
            self.assertEqual(kwargs["server_hostname"], "images.example.org")
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["proxy"], "http://proxy.example")
            self.assertFalse(session_factory.call_args.kwargs["trust_env"])

    async def test_content_length_limit_applies_before_read(self):
        response = MagicMock(status=200, headers={}, content_length=17)
        with (
            patch.object(public_fetch.aiohttp, "ClientSession") as factory,
            patch.object(public_fetch, "_public_addresses", AsyncMock(return_value=["1.1.1.1"])),
        ):
            session = MagicMock()
            factory.return_value.__aenter__.return_value = session
            session.get.return_value.__aenter__.return_value = response
            with self.assertRaisesRegex(public_fetch.PublicFetchError, "过大"):
                await public_fetch.fetch_public_resource(
                    "https://example.org/image", proxy=None, timeout=1, max_bytes=16
                )
            response.content.iter_chunked.assert_not_called()


class ReverseInputsTests(unittest.IsolatedAsyncioTestCase):
    def pipeline(self, ttl=3600):
        return ReverseImagePipeline(
            reverse_cfg=ReverseImageSection(cache_ttl_seconds=ttl, google_lens_enabled=False, yandex_enabled=False),
            backend_cfg=SearchBackendSection(),
        )

    async def test_invalid_inputs_do_not_reach_engines(self):
        pipeline = self.pipeline()
        pipeline.search_raw = AsyncMock()
        pipeline.download_image_url = AsyncMock(return_value=b"not an image")
        with self.assertRaises(ValueError):
            await pipeline.run(image_url="https://example.org/image")
        with self.assertRaises(ValueError):
            await pipeline.run(image_bytes=PNG, image_url="https://example.org/image")
        self.assertEqual(pipeline.download_image_url.await_count, 1)
        pipeline.search_raw.assert_not_awaited()
        self.assertFalse(pipeline._cache)
        self.assertEqual(verify_image_data(PNG), "image/png")
        with self.assertRaises(ValueError):
            verify_image_data(PNG[:20])

    async def test_handler_rejects_both_sources_before_fetch_and_exposes_errors(self):
        plugin = GoogleSearchPlugin()
        plugin.set_plugin_config(GoogleSearchPluginConfig().model_dump())
        plugin._set_context(SimpleNamespace(logger=MagicMock()))
        pipeline = SimpleNamespace(run=AsyncMock(side_effect=ValueError("无法读取图片")))
        plugin._reverse_image_pipeline = pipeline
        with patch.object(plugin, "_ensure_pipelines_ready", return_value=True):
            both = await plugin.handle_reverse_image_search(msg_id="m1", image_url="https://example.org/image")
            self.assertFalse(both["success"])
            pipeline.run.assert_not_awaited()
            failure = await plugin.handle_reverse_image_search(image_url="https://example.org/image")
            self.assertFalse(failure["success"])
            self.assertIn("无法读取图片", failure["error"])

    async def test_regular_search_skips_non_image_even_with_image_content_type(self):
        cfg = GoogleSearchPluginConfig()
        pipeline = ImageSearchPipeline(engines_cfg=cfg.engines, backend_cfg=cfg.search_backend)
        urls = ["https://example.org/a", "https://example.org/b"]
        pipeline._search_with_fallback = AsyncMock(return_value=[{"image": url} for url in urls])
        resources = [
            public_fetch.PublicResource(b"not a picture", "image/jpeg", None),
            public_fetch.PublicResource(PNG, "application/octet-stream", None),
        ]
        with (
            patch(f"{ImageSearchPipeline.__module__}.random.shuffle"),
            patch(f"{ImageSearchPipeline.__module__}.fetch_public_resource", AsyncMock(side_effect=resources)),
        ):
            status, _, url, mime = await pipeline.find_unique_image_media("cat")
        self.assertEqual((status, url, mime), ("ok", urls[1], "image/png"))

    async def test_unavailable_bing_and_empty_search_are_distinct(self):
        pipeline = self.pipeline()
        pipeline.bing.search = AsyncMock(return_value=ReverseImageOutput(engine="bing"))
        pipeline.download_image_url = AsyncMock(return_value=PNG)
        with self.assertRaisesRegex(ValueError, "必应需要图片链接"):
            await pipeline.run(image_bytes=PNG)
        pipeline.bing.search.assert_not_awaited()
        self.assertIn("没有找到", await pipeline.run(image_url="https://example.org/photo"))
        pipeline.bing.search.return_value = ReverseImageOutput(engine="bing", error="服务暂不可用")
        with self.assertRaisesRegex(ValueError, "服务暂不可用"):
            await pipeline.run(image_url="https://example.org/photo")

    async def test_url_cache_is_short_lived_and_respects_config(self):
        for ttl in (0, 15, 3600):
            with self.subTest(ttl=ttl):
                pipeline = self.pipeline(ttl)
                pipeline.download_image_url = AsyncMock(return_value=PNG)
                pipeline.search_raw = AsyncMock(return_value=ReverseImageOutput(engine="bing", ai_overview="ok"))
                clock = SimpleNamespace(monotonic=MagicMock(return_value=0))
                with patch(f"{ReverseImagePipeline.__module__}.time", clock):
                    await pipeline.run(image_url="https://example.org/a")
                    clock.monotonic.return_value = 1
                    await pipeline.run(image_url="https://example.org/a")
                    self.assertEqual(pipeline.download_image_url.await_count, 1 if ttl else 2)
                    self.assertEqual(pipeline.search_raw.await_count, 1 if ttl else 2)
                    clock.monotonic.return_value = min(ttl or 60, 60) + 1
                    await pipeline.run(image_url="https://example.org/a")
                    self.assertEqual(pipeline.download_image_url.await_count, 2 if ttl else 3)
                    self.assertEqual(pipeline.search_raw.await_count, 1 if ttl > 60 else (2 if ttl else 3))
