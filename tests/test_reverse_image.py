"""Offline tests for Issue #56 (image_search @Tool migration) and reverse_image_search."""

from __future__ import annotations

import base64
import json
import sys
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from plugins.google_search_plugin.config import (  # noqa: E402
    GoogleSearchPluginConfig,
    ReverseImageSection,
    SearchBackendSection,
)
from plugins.google_search_plugin.pipelines.reverse_image_pipeline import (  # noqa: E402
    ReverseImagePipeline,
    format_reverse_image_output,
)
from plugins.google_search_plugin.plugin import GoogleSearchPlugin  # noqa: E402
from plugins.google_search_plugin.search_engines.reverse_image.base import (  # noqa: E402
    ReverseImageMatch,
    ReverseImageOutput,
    deduplicate_matches,
    detect_image_mime_and_ext,
    normalize_match_url,
    parse_cookie_header,
)
from plugins.google_search_plugin.search_engines.reverse_image.bing_visual import BingVisualEngine  # noqa: E402
from plugins.google_search_plugin.search_engines.reverse_image.google_lens import (  # noqa: E402
    GoogleLensReverseEngine,
    _clean_ai_overview_text,
    _ensure_hl_param,
)
from plugins.google_search_plugin.search_engines.reverse_image.yandex_visual import YandexVisualEngine  # noqa: E402

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_FAKE_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC")


class Issue56MigrationTests(unittest.IsolatedAsyncioTestCase):
    def test_manifest_urls_and_capabilities_are_updated(self) -> None:
        manifest = json.loads((_PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["author"]["url"], "https://github.com/YukiSakiko")
        for key in ("repository", "homepage", "documentation", "issues"):
            self.assertIn("YukiSakiko/google_search_plugin", manifest["urls"][key])
            self.assertNotIn("XXXxx7258", manifest["urls"][key])
        self.assertIn("maisaka.context.resolve_image", manifest["capabilities"])

    def test_image_search_and_reverse_image_search_are_registered_as_tools_without_deprecation(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            plugin = GoogleSearchPlugin()
            plugin.set_plugin_config(GoogleSearchPluginConfig().model_dump())
            components = plugin.get_components()

        deprecation_msgs = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
        self.assertFalse(any("Action" in msg for msg in deprecation_msgs), deprecation_msgs)

        by_name = {c["name"]: c for c in components}
        self.assertIn("image_search", by_name)
        self.assertIn("reverse_image_search", by_name)

        img_comp = by_name["image_search"]
        self.assertEqual(img_comp["type"], "TOOL")
        self.assertNotEqual(img_comp["metadata"].get("invoke_method"), "plugin.invoke_action")
        self.assertFalse(img_comp["metadata"].get("legacy_action", False))
        self.assertFalse(img_comp["metadata"]["enabled"])

        rev_comp = by_name["reverse_image_search"]
        self.assertEqual(rev_comp["type"], "TOOL")
        self.assertTrue(rev_comp["metadata"]["enabled"])
        self.assertGreaterEqual(rev_comp["metadata"]["timeout_ms"], 120_000)

        states = plugin._configured_component_states()
        self.assertEqual(states["image_search"], ("tool", False))
        self.assertEqual(states["reverse_image_search"], ("tool", True))

    async def test_image_search_tool_returns_maisaka_content_items_instead_of_direct_send(self) -> None:
        plugin = GoogleSearchPlugin()
        plugin.set_plugin_config(GoogleSearchPluginConfig(actions={"image_search_enabled": True}).model_dump())  # type: ignore[arg-type]
        plugin._set_context(
            SimpleNamespace(  # type: ignore[arg-type]
                logger=MagicMock(),
                send=SimpleNamespace(image=AsyncMock(), text=AsyncMock()),
            )
        )
        plugin._engine_chain = object()  # type: ignore[assignment]
        plugin._content_fetcher = object()  # type: ignore[assignment]
        plugin._llm_runner = object()  # type: ignore[assignment]
        plugin._search_pipeline = object()  # type: ignore[assignment]
        plugin._url_pipeline = object()  # type: ignore[assignment]
        plugin._translator = object()  # type: ignore[assignment]
        plugin._reverse_image_pipeline = object()  # type: ignore[assignment]
        b64_png = base64.b64encode(_FAKE_PNG).decode("ascii")
        plugin._image_pipeline = SimpleNamespace(  # type: ignore[assignment]
            find_unique_image_media=AsyncMock(
                return_value=("ok", b64_png, "https://example.com/cat.png", "image/png")
            )
        )

        result = await plugin.handle_image_search(query="可爱猫咪")
        self.assertTrue(result["success"])
        self.assertEqual(result["name"], "image_search")
        self.assertIn("attach_pic", result["content"])
        self.assertEqual(len(result["content_items"]), 1)
        item = result["content_items"][0]
        self.assertEqual(item["content_type"], "image")
        self.assertEqual(item["data"], b64_png)
        self.assertEqual(item["mime_type"], "image/png")
        self.assertEqual(item["metadata"]["source_url"], "https://example.com/cat.png")
        plugin.ctx.send.image.assert_not_awaited()
        plugin.ctx.send.text.assert_not_awaited()


class ReverseImageParserTests(unittest.TestCase):
    def test_helpers_mime_cookie_url_and_dedup(self) -> None:
        self.assertEqual(detect_image_mime_and_ext(_FAKE_PNG), ("image/png", "png"))
        self.assertEqual(detect_image_mime_and_ext(b"\xff\xd8\xff\xe0"), ("image/jpeg", "jpg"))
        self.assertEqual(detect_image_mime_and_ext(b"RIFF1234WEBP"), ("image/webp", "webp"))

        cookies = parse_cookie_header("Cookie: SID=abc; __Secure-1PSID=xyz; badpart")
        self.assertEqual(len(cookies), 2)
        self.assertEqual(cookies[0]["name"], "SID")
        self.assertFalse(cookies[0]["secure"])
        self.assertEqual(cookies[1]["name"], "__Secure-1PSID")
        self.assertTrue(cookies[1]["secure"])

        cleaned = normalize_match_url("https://danbooru.donmai.us/posts/6604314?q=mygo&utm_source=yandexsmartcamera")
        self.assertEqual(cleaned, "https://danbooru.donmai.us/posts/6604314?q=mygo")

        deduped = deduplicate_matches(
            [
                ReverseImageMatch(title="Post 1", url="https://example.com/a?utm_source=x"),
                ReverseImageMatch(title="Post 1 dup", url="https://example.com/a"),
                ReverseImageMatch(title="Post 2", url="https://example.com/b"),
            ],
            limit=5,
        )
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0].url, "https://example.com/a")

    def test_yandex_parse_sites_html_extracts_sites_and_tags(self) -> None:
        state = {
            "initialState": {
                "cbirSites": {
                    "sites": [
                        {
                            "title": "chihaya anon drawn by noshimurin Danbooru",
                            "description": "Original illustration",
                            "url": "https://danbooru.donmai.us/posts/6604314?utm_source=yandexsmartcamera",
                            "domain": "danbooru.donmai.us",
                        },
                        {
                            "title": "Internal yandex link",
                            "url": "https://yandex.com/images/foo",
                            "domain": "yandex.com",
                        },
                        {
                            "title": "Mygo 剧 场 版 - 刺 激 影 片",
                            "url": "https://180gadgets.com/mygo",
                            "domain": "180gadgets.com",
                        },
                        {
                            "title": "Anonv Fappening Photos",
                            "url": "https://cabinpressure.tv/anonv",
                            "domain": "cabinpressure.tv",
                        },
                    ]
                },
                "cbirTags": {
                    "tags": [
                        {"text": "bang dream"},
                        {"text": "chihaya anon"},
                    ]
                },
            }
        }
        html_doc = f"<div class='Root' data-state='{json.dumps(state)}'></div>"
        matches, tags = YandexVisualEngine.parse_sites_html(html_doc, max_results=5)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].url, "https://danbooru.donmai.us/posts/6604314")
        self.assertEqual(matches[0].source, "danbooru.donmai.us")
        self.assertEqual(tags, ["bang dream", "chihaya anon"])

    def test_bing_parse_sbi_html_extracts_m_json_and_skips_internal(self) -> None:
        m1 = json.dumps({"t": "肥鱼造反证据存档_bilibili", "dm": "bilibili.com", "purl": "https://www.bilibili.com/video/BV1xx/"})
        m2 = json.dumps({"t": "Bing Internal", "dm": "bing.com", "purl": "https://www.bing.com/images/abc"})
        html_doc = f"<a m='{m1}'></a><a m='{m2}'></a>"
        matches = BingVisualEngine.parse_sbi_html(html_doc, max_results=5)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].title, "肥鱼造反证据存档_bilibili")
        self.assertEqual(matches[0].url, "https://www.bilibili.com/video/BV1xx/")

    def test_google_lens_helpers(self) -> None:
        self.assertTrue(GoogleLensReverseEngine._is_sorry_page("https://www.google.com/sorry/index?continue=...", ""))
        self.assertTrue(GoogleLensReverseEngine._is_sorry_page("https://www.google.com/search", "<div class='g-recaptcha'>"))
        self.assertFalse(GoogleLensReverseEngine._is_sorry_page("https://www.google.com/search?vsrid=1", "<div>ok</div>"))

        self.assertIn("hl=zh-CN", _ensure_hl_param("https://www.google.com/search?vsrid=abc&udm=26", "zh-CN"))
        cleaned_aio = _clean_ai_overview_text("AI 概览\n这是 DeepSeek 拟人角色鲸鱼娘。\n全部显示\n答得好\n")
        self.assertEqual(cleaned_aio, "这是 DeepSeek 拟人角色鲸鱼娘。")


class ReverseImagePipelineTests(unittest.IsolatedAsyncioTestCase):
    def _build_pipeline(self, **reverse_overrides: object) -> ReverseImagePipeline:
        cfg = ReverseImageSection(**reverse_overrides)  # type: ignore[arg-type]
        backend = SearchBackendSection(proxy="http://127.0.0.1:7890", max_content_length=500)
        return ReverseImagePipeline(reverse_cfg=cfg, backend_cfg=backend)

    async def test_google_lens_captcha_falls_back_to_yandex_and_bing_and_caches(self) -> None:
        pipeline = self._build_pipeline(cache_ttl_seconds=3600)
        pipeline.google_lens.search = AsyncMock(
            return_value=ReverseImageOutput(engine="google_lens", captcha_triggered=True, error="captcha")
        )
        pipeline.yandex.search = AsyncMock(
            return_value=ReverseImageOutput(
                engine="yandex",
                matches=[
                    ReverseImageMatch(
                        title="DeepSeek 鲸鱼娘表情包",
                        url="https://aigengtu.com/zh/meme/2293",
                        source="aigengtu.com",
                    )
                ],
                tags=["DeepSeek", "鲸鱼娘"],
                uploaded_image_url="https://avatars.mds.yandex.net/get-images-cbir/1/orig",
            )
        )
        pipeline.bing.search = AsyncMock(
            return_value=ReverseImageOutput(
                engine="bing",
                matches=[
                    ReverseImageMatch(
                        title="肥鱼造反证据存档_bilibili",
                        url="https://www.bilibili.com/video/BV1Mua663EKp/",
                        source="bilibili.com",
                    )
                ],
            )
        )

        text1 = await pipeline.run(image_bytes=_FAKE_PNG, image_hash="hash-whale")
        self.assertIn("Yandex Images + Bing Visual Search", text1)
        self.assertIn("DeepSeek 鲸鱼娘表情包", text1)
        self.assertIn("肥鱼造反证据存档_bilibili", text1)
        self.assertIn("DeepSeek, 鲸鱼娘", text1)
        pipeline.bing.search.assert_awaited_once_with(
            image_url="https://avatars.mds.yandex.net/get-images-cbir/1/orig",
            max_results=8,
        )

        # Second call with same hash must hit cache without re-invoking engines
        text2 = await pipeline.run(image_bytes=_FAKE_PNG, image_hash="hash-whale")
        self.assertEqual(text1, text2)
        self.assertEqual(pipeline.google_lens.search.await_count, 1)
        self.assertEqual(pipeline.yandex.search.await_count, 1)

    async def test_google_lens_success_short_circuits_and_includes_ai_overview(self) -> None:
        pipeline = self._build_pipeline()
        pipeline.google_lens.search = AsyncMock(
            return_value=ReverseImageOutput(
                engine="google_lens",
                ai_overview="这似乎是 DeepSeek 拟人角色（鲸鱼娘）。",
                matches=[
                    ReverseImageMatch(
                        title="梗鲸 DeepSeek表情包",
                        url="https://aigengtu.com/zh/meme/2293",
                        source="aigengtu.com",
                    )
                ],
            )
        )
        pipeline.yandex.search = AsyncMock()
        pipeline.bing.search = AsyncMock()

        text = await pipeline.run(image_bytes=_FAKE_PNG)
        self.assertIn("Google Lens", text)
        self.assertIn("\n### Google AI 概览\n", text)
        self.assertIn("DeepSeek 拟人角色（鲸鱼娘）", text)
        self.assertIn("https://aigengtu.com/zh/meme/2293", text)
        pipeline.yandex.search.assert_not_awaited()
        pipeline.bing.search.assert_not_awaited()

    async def test_optional_content_fetcher_enriches_top_matches(self) -> None:
        fetcher = SimpleNamespace(fetch_public_excerpt=AsyncMock(return_value="这是抓取到的网页正文介绍。"))
        pipeline = ReverseImagePipeline(
            reverse_cfg=ReverseImageSection(fetch_content=True, google_lens_enabled=False, bing_enabled=False),
            backend_cfg=SearchBackendSection(),
            content_fetcher=fetcher,  # type: ignore[arg-type]
        )
        pipeline.yandex.search = AsyncMock(
            return_value=ReverseImageOutput(
                engine="yandex",
                matches=[
                    ReverseImageMatch(title="梗鲸", url="https://aigengtu.com/zh/meme/2293", source="aigengtu.com")
                ],
            )
        )

        text = await pipeline.run(image_bytes=_FAKE_PNG)
        fetcher.fetch_public_excerpt.assert_awaited_once_with("https://aigengtu.com/zh/meme/2293")
        self.assertIn("这是抓取到的网页正文介绍。", text)

    async def test_plugin_handle_reverse_image_search_resolves_maisaka_context_image(self) -> None:
        plugin = GoogleSearchPlugin()
        plugin.set_plugin_config(GoogleSearchPluginConfig().model_dump())
        b64_png = base64.b64encode(_FAKE_PNG).decode("ascii")
        resolve_mock = AsyncMock(
            return_value={"success": True, "image": {"binary_data_base64": b64_png, "binary_hash": "abc123"}}
        )
        plugin._set_context(
            SimpleNamespace(  # type: ignore[arg-type]
                logger=MagicMock(),
                maisaka=SimpleNamespace(context=SimpleNamespace(resolve_image=resolve_mock)),
            )
        )
        plugin._engine_chain = object()  # type: ignore[assignment]
        plugin._content_fetcher = object()  # type: ignore[assignment]
        plugin._llm_runner = object()  # type: ignore[assignment]
        plugin._search_pipeline = object()  # type: ignore[assignment]
        plugin._url_pipeline = object()  # type: ignore[assignment]
        plugin._translator = object()  # type: ignore[assignment]
        plugin._image_pipeline = object()  # type: ignore[assignment]
        plugin._reverse_image_pipeline = SimpleNamespace(  # type: ignore[assignment]
            run=AsyncMock(return_value="【以图搜图结果】测试成功"),
            aclose=AsyncMock(),
        )

        res = await plugin.handle_reverse_image_search(msg_id="m42", image_index=1, stream_id="stream-1")
        self.assertTrue(res["success"])
        self.assertEqual(res["content"], "【以图搜图结果】测试成功")
        resolve_mock.assert_awaited_once_with("stream-1", "m42", index=1)
        plugin._reverse_image_pipeline.run.assert_awaited_once_with(
            image_bytes=_FAKE_PNG,
            image_url="",
            image_hash="abc123",
        )

        await plugin.on_unload()
        plugin._reverse_image_pipeline.aclose.assert_awaited_once()

    def test_format_output_keeps_links_clean(self) -> None:
        out = format_reverse_image_output(
            ReverseImageOutput(
                engine="yandex",
                matches=[ReverseImageMatch(title="Test", url="https://example.com/1", source="example.com")],
            )
        )
        self.assertIn("1. [Test](https://example.com/1)（example.com）", out)


class GoogleLensWebUIWidgetTests(unittest.IsolatedAsyncioTestCase):
    def _make_plugin(self, **reverse_overrides: object) -> tuple[GoogleSearchPlugin, GoogleLensReverseEngine]:
        plugin = GoogleSearchPlugin()
        cfg = GoogleSearchPluginConfig(reverse_image=ReverseImageSection(**reverse_overrides))  # type: ignore[arg-type]
        plugin.set_plugin_config(cfg.model_dump())
        plugin._set_context(SimpleNamespace(logger=MagicMock()))  # type: ignore[arg-type]
        lens = GoogleLensReverseEngine(
            enabled=cfg.reverse_image.google_lens_enabled,
            cookies=cfg.reverse_image.google_lens_cookies,
            profile_dir=cfg.reverse_image.google_lens_profile_dir,
            default_profile_dir="C:/nonexistent-google-lens-profile-test",
        )
        plugin._engine_chain = object()  # type: ignore[assignment]
        plugin._content_fetcher = object()  # type: ignore[assignment]
        plugin._llm_runner = object()  # type: ignore[assignment]
        plugin._search_pipeline = object()  # type: ignore[assignment]
        plugin._url_pipeline = object()  # type: ignore[assignment]
        plugin._translator = object()  # type: ignore[assignment]
        plugin._image_pipeline = object()  # type: ignore[assignment]
        plugin._reverse_image_pipeline = SimpleNamespace(google_lens=lens, aclose=AsyncMock())  # type: ignore[assignment]
        return plugin, lens

    async def test_webui_config_widgets_schema_and_host_protocol_validation(self) -> None:
        from src.plugin_runtime.config_widgets import invoke_widget_action, read_widget_snapshot

        plugin, lens = self._make_plugin(enabled=True, google_lens_enabled=True)
        schema = plugin.get_webui_config_schema(
            plugin_id="xxxxx7258.google-search-plugin",
            plugin_name="google_search",
            plugin_version="4.1.1",
        )
        widgets = schema.get("runtime_widgets")
        self.assertIsInstance(widgets, list)
        self.assertEqual(
            [w["id"] for w in widgets],
            [
                "google_lens_status",
                "google_lens_login_start",
                "google_lens_login_finish",
                "google_lens_login_reset",
            ],
        )

        # Idle state: start button enabled, finish hidden
        snap = await read_widget_snapshot(plugin, widgets)
        self.assertFalse(snap.states["google_lens_login_start"].disabled)
        self.assertFalse(snap.states["google_lens_login_finish"].visible)

        # Simulate clicking start -> enters waiting state
        async def _fake_start() -> None:
            lens._login_state = "waiting"
            lens._login_message = "登录窗口已在本机弹出"

        lens.start_interactive_login = AsyncMock(side_effect=_fake_start)  # type: ignore[method-assign]
        await invoke_widget_action(plugin, snap, "google_lens_login_start")
        lens.start_interactive_login.assert_awaited_once()

        # While waiting: start disabled, finish visible, search short-circuits cleanly
        snap2 = await read_widget_snapshot(plugin, widgets)
        self.assertEqual(snap2.states["google_lens_status"].tone, "warning")
        self.assertTrue(snap2.states["google_lens_login_start"].disabled)
        self.assertTrue(snap2.states["google_lens_login_finish"].visible)

        search_out = await lens.search(image_bytes=_FAKE_PNG)
        self.assertFalse(search_out.has_useful_data)
        self.assertIn("登录", search_out.error)

        # Simulate clicking finish -> logged_in state
        async def _fake_finish() -> None:
            lens._login_state = "logged_in"
            lens._login_message = "已检测到 Google 账号登录态并保存至本地目录"

        lens.finish_interactive_login = AsyncMock(side_effect=_fake_finish)  # type: ignore[method-assign]
        await invoke_widget_action(plugin, snap2, "google_lens_login_finish")
        lens.finish_interactive_login.assert_awaited_once()

        snap3 = await read_widget_snapshot(plugin, widgets)
        self.assertEqual(snap3.states["google_lens_status"].tone, "success")
        self.assertFalse(snap3.states["google_lens_login_finish"].visible)

    async def test_webui_widgets_disabled_and_headless_error_message(self) -> None:
        plugin, _ = self._make_plugin(enabled=True, google_lens_enabled=False)
        states = await plugin.get_config_widget_state()
        self.assertTrue(states["google_lens_login_start"].disabled)
        self.assertFalse(states["google_lens_login_finish"].visible)
        self.assertIn("未启用", states["google_lens_status"].text)

        msg = GoogleLensReverseEngine._format_headed_launch_error(
            RuntimeError("Looks like you launched a headed browser without having a XServer running (missing X server)")
        )
        self.assertIn("无图形界面", msg)
        self.assertIn("Cookie", msg)


if __name__ == "__main__":
    unittest.main()
