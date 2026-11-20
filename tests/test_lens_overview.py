from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from plugins.google_search_plugin.search_engines.reverse_image.google_lens import GoogleLensReverseEngine  # noqa: E402


def _snapshot(*urls: str, overview: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        json_value=AsyncMock(
            return_value={"links": [{"title": url, "url": url} for url in urls], "aio": overview, "source": "turn"}
        ),
        dispose=AsyncMock(),
    )


class LensOverviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_overview_finishes_without_waiting_for_links(self) -> None:
        handle = _snapshot(overview="这是一朵玫瑰。")
        page = SimpleNamespace(wait_for_function=AsyncMock(return_value=handle))
        matches, overview = await GoogleLensReverseEngine()._extract_from_page(page, 8)
        self.assertEqual((matches, overview), ([], "这是一朵玫瑰。"))
        page.wait_for_function.assert_awaited_once()
        handle.dispose.assert_awaited_once()

    async def test_zero_wait_keeps_the_first_snapshot(self) -> None:
        for answer in ("", "这是一朵玫瑰。"):
            with self.subTest(answer=answer):
                page = SimpleNamespace(
                    wait_for_function=AsyncMock(return_value=_snapshot("https://example.org", overview=answer))
                )
                matches, overview = await GoogleLensReverseEngine(overview_wait_seconds=0)._extract_from_page(page, 8)
                self.assertEqual(overview, answer)
                self.assertEqual([item.url for item in matches], ["https://example.org"])
                page.wait_for_function.assert_awaited_once()

    async def test_link_starts_an_independent_window_and_refreshes_links_on_timeout(self) -> None:
        first, later = "https://example.org/first", "https://example.org/later"
        for latest, expected in (({"links": [{"title": "Later", "url": later}]}, [later, first]), (None, [first])):
            with self.subTest(expected=expected):
                page = SimpleNamespace(
                    wait_for_function=AsyncMock(side_effect=[_snapshot(first), PlaywrightTimeoutError("timeout")]),
                    evaluate=AsyncMock(return_value=latest),
                )
                matches, overview = await GoogleLensReverseEngine()._extract_from_page(page, 8)
                self.assertEqual([item.url for item in matches], expected)
                self.assertEqual(overview, "")
                calls = page.wait_for_function.await_args_list
                self.assertEqual([c.kwargs["timeout"] for c in calls], [10000, 6000])
                self.assertEqual([c.kwargs["arg"]["overviewOnly"] for c in calls], [False, True])

    async def test_empty_page_does_not_start_another_window(self) -> None:
        page = SimpleNamespace(wait_for_function=AsyncMock(side_effect=PlaywrightTimeoutError("timeout")))
        self.assertEqual(await GoogleLensReverseEngine()._extract_from_page(page, 8), ([], ""))
        page.wait_for_function.assert_awaited_once()

    async def test_browser_extracts_visible_answer_not_related_pages_or_loading_ui(self) -> None:
        async with async_playwright() as pw:
            for channel in (None, "chrome", "msedge"):
                try:
                    browser = await pw.chromium.launch(channel=channel)
                    break
                except PlaywrightError as exc:
                    if not any(token in str(exc).lower() for token in ("doesn't exist", "not found")):
                        raise
            else:
                self.skipTest("未安装可用的 Chromium 浏览器")
            try:
                page = await browser.new_page()
                lens = GoogleLensReverseEngine(overview_wait_seconds=0)
                await page.set_content(
                    '<div data-scope-id="turn" hidden>旧概览</div>'
                    '<div data-scope-id="turn" aria-busy="true">旧回答</div>'
                    '<h2 id="title">AI 概览</h2><section aria-labelledby="title">这是一朵玫瑰。</section>'
                )
                matches, overview = await asyncio.wait_for(lens._extract_from_page(page, 8), 3)
                self.assertEqual((matches, overview), ([], "这是一朵玫瑰。"))

                await page.set_content(
                    "<h2>AI Overview</h2><p>这是普通网页摘要，不是概览。</p>"
                    '<div data-scope-id="turn"><h3>AI Overview</h3>Loading…'
                    '<a href="https://example.org/source">引用链接</a><button>显示更多</button></div>'
                )
                matches, overview = await lens._extract_from_page(page, 8)
                self.assertEqual(overview, "")
                self.assertEqual([item.title for item in matches], ["引用链接"])

                await page.evaluate(
                    "() => { setTimeout(() => { document.querySelector('[data-scope-id]').innerText = '一只海鸥。'; }, 600); }"
                )
                matches, overview = await GoogleLensReverseEngine(overview_wait_seconds=3)._extract_from_page(page, 8)
                self.assertEqual(overview, "一只海鸥。")
                self.assertEqual([item.title for item in matches], ["引用链接"])
            finally:
                await browser.close()
