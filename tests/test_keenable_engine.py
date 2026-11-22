"""Offline tests for the opt-in Keenable engine."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_HOST_ROOT = Path(__file__).resolve().parents[3]
if str(_HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(_HOST_ROOT))

from plugins.google_search_plugin.config import EnginesSection, SearchBackendSection  # noqa: E402
from plugins.google_search_plugin.pipelines.engine_chain import _build_common_cfg, _build_engine_dict  # noqa: E402
from plugins.google_search_plugin.search_engines.keenable import KeenableEngine  # noqa: E402


def response_context(body: object = None, status: int = 200) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=json.dumps(body))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def session_context(*responses: MagicMock | Exception) -> tuple[MagicMock, MagicMock]:
    session = MagicMock()
    session.post.side_effect = responses
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    return context, session


ROWS = {
    "results": [
        {"title": "First", "url": "https://example.com/a", "snippet": "alpha\nbeta"},
        {"title": "", "url": "https://example.com/b", "description": "from description"},
        {"title": "Dup", "url": "https://example.com/a", "snippet": "dup"},
        {"title": "Bad", "url": "not a url"},
        "not a dict",
    ]
}


class KeenableEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyless_request_uses_public_endpoint_with_title_header(self) -> None:
        context, session = session_context(response_context(ROWS))
        engine = KeenableEngine({"timeout": 12, "proxy": "http://localhost:8080", "max_results": 10})
        with patch("aiohttp.ClientSession", return_value=context):
            results = await engine.search("test query", 4)
        args, kwargs = session.post.call_args
        self.assertEqual(args, ("https://api.keenable.ai/v1/search/public",))
        self.assertEqual(kwargs["json"], {"query": "test query", "max_results": 4})
        self.assertEqual(kwargs["headers"]["X-Keenable-Title"], "google_search_plugin")
        self.assertNotIn("X-API-Key", kwargs["headers"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["proxy"], "http://localhost:8080")
        self.assertEqual([r.url for r in results], ["https://example.com/a", "https://example.com/b"])
        self.assertEqual(results[0].title, "First")
        self.assertEqual(results[0].snippet, "alpha\nbeta")
        self.assertEqual(results[1].title, "https://example.com/b")
        self.assertEqual(results[1].snippet, "from description")
        self.assertEqual([r.rank for r in results], [0, 1])

    async def test_key_uses_authenticated_endpoint_and_published_after(self) -> None:
        context, session = session_context(response_context(ROWS))
        engine = KeenableEngine({"api_keys": ["fixture-only-keenable-key"], "published_after": " 7d "})
        with patch("aiohttp.ClientSession", return_value=context):
            await engine.search("q", 0)
        args, kwargs = session.post.call_args
        self.assertEqual(args, ("https://api.keenable.ai/v1/search",))
        self.assertEqual(kwargs["headers"]["X-API-Key"], "fixture-only-keenable-key")
        self.assertEqual(kwargs["json"], {"query": "q", "max_results": 10, "published_after": "7d"})

    async def test_failed_key_tries_next_key_but_never_falls_back_to_keyless(self) -> None:
        context, session = session_context(response_context({}, status=401), response_context({}, status=401))
        engine = KeenableEngine({"api_keys": ["key-one-123456", "key-two-123456"]})
        with patch("aiohttp.ClientSession", return_value=context):
            results = await engine.search("q", 3)
        self.assertEqual(results, [])
        self.assertEqual(session.post.call_count, 2)
        for call in session.post.call_args_list:
            self.assertEqual(call.args, ("https://api.keenable.ai/v1/search",))
            self.assertFalse(call.kwargs["allow_redirects"])
        used_keys = {call.kwargs["headers"]["X-API-Key"] for call in session.post.call_args_list}
        self.assertEqual(used_keys, set(engine.api_keys))

    async def test_second_key_can_succeed_after_authentication_failure(self) -> None:
        context, session = session_context(response_context(status=401), response_context(ROWS))
        engine = KeenableEngine({"api_keys": ["first", "second"]})
        with patch("aiohttp.ClientSession", return_value=context):
            results = await engine.search("q", 3)
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual([r.title for r in results], ["First", "https://example.com/b"])

    async def test_non_key_failures_do_not_repeat_the_request(self) -> None:
        cases = {
            "redirect": response_context(status=307),
            "bad query": response_context(status=400),
            "organization rate limit": response_context(status=429),
            "service unavailable": response_context(status=503),
            "invalid result schema": response_context(["not", "a", "dict"]),
            "unusable results": response_context({"results": [{"url": ""}]}),
            "transport error": OSError("connection failed"),
        }
        engine = KeenableEngine({"api_keys": ["first", "second"]})
        for name, response in cases.items():
            with self.subTest(case=name):
                context, session = session_context(response, response_context(ROWS))
                with patch("aiohttp.ClientSession", return_value=context):
                    self.assertEqual(await engine.search("q", 3), [])
                session.post.assert_called_once()

    async def test_key_rotation_shares_one_deadline(self) -> None:
        stalled = response_context()
        stalled.__aenter__.side_effect = asyncio.Event().wait
        context, session = session_context(response_context(status=401), stalled, response_context(ROWS))
        engine = KeenableEngine({"api_keys": ["first", "second", "third"], "timeout": 0.05})
        with (
            patch("aiohttp.ClientSession", return_value=context),
            patch("asyncio.timeout", wraps=asyncio.timeout) as deadline,
        ):
            results = await asyncio.wait_for(engine.search("q", 3), timeout=2)
        deadline.assert_called_once_with(0.05)
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(results, [])

    async def test_nonpositive_timeout_keeps_deadline_disabled(self) -> None:
        async def read_body() -> str:
            await asyncio.sleep(0)
            return json.dumps(ROWS)

        for timeout in (0, -1):
            with self.subTest(timeout=timeout):
                response = response_context()
                response.__aenter__.return_value.text.side_effect = read_body
                context, _ = session_context(response)
                with patch("aiohttp.ClientSession", return_value=context) as factory:
                    results = await KeenableEngine({"timeout": timeout}).search("q", 1)
                self.assertEqual([r.title for r in results], ["First"])
                self.assertIsNone(factory.call_args.kwargs["timeout"].total)

    async def test_configured_result_count_does_not_truncate_snippets(self) -> None:
        snippet = "text " * 1200 + "end"
        rows = {"results": [{"url": f"https://example.com/{i}", "snippet": snippet} for i in range(4)]}
        context, session = session_context(response_context(rows))
        engine = KeenableEngine({"max_results": 3})
        with patch("aiohttp.ClientSession", return_value=context):
            results = await engine.search("q", 0)
        self.assertEqual(session.post.call_args.kwargs["json"]["max_results"], 3)
        self.assertEqual([r.url for r in results], [f"https://example.com/{i}" for i in range(3)])
        self.assertEqual([r.abstract for r in results], [snippet] * 3)

    async def test_markdown_is_preserved_with_description_as_fallback(self) -> None:
        markdown = "Paragraph\n\n```python\nif ok:\n    run()\n```"
        rows = {
            "results": [
                {"url": "https://example.com/a", "snippet": f"  {markdown}\n", "description": "unused"},
                {"url": "https://example.com/b", "snippet": "  \n", "description": f"  {markdown}\n"},
                {"url": "https://example.com/c", "snippet": ["unexpected"], "description": markdown},
            ]
        }
        context, _ = session_context(response_context(rows))
        with patch("aiohttp.ClientSession", return_value=context):
            results = await KeenableEngine({}).search("q", 3)
        self.assertEqual([(r.snippet, r.abstract) for r in results], [(markdown, markdown)] * 3)

    async def test_limit_is_clamped_to_50(self) -> None:
        context, session = session_context(response_context({"results": []}))
        with patch("aiohttp.ClientSession", return_value=context):
            await KeenableEngine({"max_results": 200}).search("q", 120)
        self.assertEqual(session.post.call_args.kwargs["json"]["max_results"], 50)


class KeenableConfigTests(unittest.TestCase):
    def test_keenable_is_off_by_default(self) -> None:
        engines = EnginesSection()
        self.assertFalse(engines.keenable_enabled)
        self.assertEqual(SearchBackendSection().default_engine, "bing")
        common = _build_common_cfg(SearchBackendSection())
        self.assertEqual(
            _build_engine_dict("keenable", engines, common),
            {**common, "enabled": False, "api_keys": [], "published_after": ""},
        )

    def test_keenable_options_are_passed(self) -> None:
        engines = EnginesSection(keenable_enabled=True, keenable_api_keys=["k"], keenable_published_after="1mo")
        cfg = _build_engine_dict("keenable", engines, _build_common_cfg(SearchBackendSection()))
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["api_keys"], ["k"])
        self.assertEqual(cfg["published_after"], "1mo")


if __name__ == "__main__":
    unittest.main()
