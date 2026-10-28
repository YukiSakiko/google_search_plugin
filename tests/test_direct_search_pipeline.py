"""Offline regressions for API search direct output and engine fallback."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import sys
import unittest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from plugins.google_search_plugin.config import EnginesSection, SearchBackendSection  # noqa: E402
from plugins.google_search_plugin.pipelines.engine_chain import EngineChain  # noqa: E402
from plugins.google_search_plugin.pipelines.search_pipeline import SearchPipeline  # noqa: E402
from plugins.google_search_plugin.search_engines.base import SearchResult  # noqa: E402

_ENGINE_NAMES = (
    "deepseek",
    "tavily",
    "anysearch",
    "exa",
    "you",
    "you_news",
    "google",
    "bing",
    "duckduckgo",
    "sogou",
)
_PIPELINE_MODULE = "plugins.google_search_plugin.pipelines.search_pipeline"


def _result(title: str = "Search title", **fields: object) -> SearchResult:
    values = {
        "title": title,
        "url": "https://example.com/article",
        "snippet": "Search snippet",
        "abstract": "Search abstract",
        "content": "Search content",
    }
    values.update(fields)
    return SearchResult(**values)


def _pipeline(
    engine_name: str,
    results: list[SearchResult],
    *,
    fetch_content: bool = True,
) -> tuple[SearchPipeline, SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    engines = SimpleNamespace(
        search_with_fallback=AsyncMock(return_value=(results, engine_name)),
        last_tavily_answer="Previous Tavily answer",
    )
    fetcher = SimpleNamespace(
        fetch_batch=AsyncMock(return_value=results),
        integrate_inline_content=Mock(),
    )
    llm = SimpleNamespace(generate=AsyncMock(return_value="Model-generated summary"))
    pipeline = SearchPipeline(
        backend_cfg=SearchBackendSection(max_results=7, fetch_content=fetch_content),
        engine_chain=engines,
        content_fetcher=fetcher,
        llm_runner=llm,
    )
    return pipeline, engines, fetcher, llm


class DirectSearchPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_engines_directly_return_content_and_links_without_fetch_or_llm(self) -> None:
        results = [
            _result("Content title", content="First line\nSecond line"),
            _result("Abstract title", url="https://example.com/abstract", content="", abstract="Abstract fallback"),
            _result(
                "Snippet title", url="https://example.com/snippet", content="", abstract="", snippet="Snippet fallback"
            ),
            _result("Title only", url="https://example.com/title", content="", abstract="", snippet=""),
        ]
        for engine_name in ("anysearch", "exa"):
            for fetch_content in (False, True):
                with self.subTest(engine=engine_name, fetch_content=fetch_content):
                    pipeline, engines, fetcher, llm = _pipeline(engine_name, results, fetch_content=fetch_content)
                    with patch(
                        f"{_PIPELINE_MODULE}.build_summarize_prompt",
                        side_effect=AssertionError("Direct API results must not build an LLM prompt"),
                    ):
                        answer = await pipeline.run("Original question", bot_name="Test bot")
                    for result in results:
                        self.assertIn(result.title, answer)
                        self.assertIn(result.url, answer)
                    self.assertIn("First line\nSecond line", answer)
                    self.assertIn("Abstract fallback", answer)
                    self.assertIn("Snippet fallback", answer)
                    self.assertNotIn("Previous Tavily answer", answer)
                    self.assertNotIn("Model-generated summary", answer)
                    engines.search_with_fallback.assert_awaited_once_with("Original question", 7, tavily_topic=None)
                    fetcher.fetch_batch.assert_not_awaited()
                    fetcher.integrate_inline_content.assert_not_called()
                    llm.generate.assert_not_awaited()

    async def test_no_results_returns_the_existing_message_without_processing(self) -> None:
        pipeline, _, fetcher, llm = _pipeline("", [])
        answer = await pipeline.run("Original question", bot_name="Test bot")
        self.assertIn("Original question", answer)
        self.assertIn("没有找到", answer)
        fetcher.fetch_batch.assert_not_awaited()
        fetcher.integrate_inline_content.assert_not_called()
        llm.generate.assert_not_awaited()

    async def test_direct_output_applies_per_result_content_limit_only_when_positive(self) -> None:
        for engine_name in ("anysearch", "exa"):
            for limit in (5, 0, -1):
                with self.subTest(engine=engine_name, limit=limit):
                    result = _result(content="1234567890")
                    pipeline, _, fetcher, llm = _pipeline(engine_name, [result])
                    pipeline._backend.max_content_length = limit
                    answer = await pipeline.run("question", bot_name="Test bot")
                    if limit > 0:
                        self.assertIn("12345...", answer)
                        self.assertNotIn("67890", answer)
                    else:
                        self.assertIn("1234567890", answer)
                    self.assertIn(result.title, answer)
                    self.assertIn(result.url, answer)
                    fetcher.fetch_batch.assert_not_awaited()
                    llm.generate.assert_not_awaited()

    async def test_deepseek_summary_still_returns_directly_with_external_references(self) -> None:
        summary = _result("DeepSeek Summary", url="", content="DeepSeek server answer", rank=-1)
        source = _result()
        pipeline, _, fetcher, llm = _pipeline("deepseek", [summary, source])
        answer = await pipeline.run("Original question", bot_name="Test bot")
        self.assertTrue(answer.startswith("DeepSeek server answer"))
        self.assertIn(source.title, answer)
        self.assertIn(source.url, answer)
        fetcher.fetch_batch.assert_not_awaited()
        fetcher.integrate_inline_content.assert_not_called()
        llm.generate.assert_not_awaited()

    async def test_tavily_keeps_inline_integration_and_existing_summary_path(self) -> None:
        results = [_result()]
        pipeline, engines, fetcher, llm = _pipeline("tavily", results)
        answer = await pipeline.run("Original question", bot_name="Test bot", tavily_topic_override="general")
        self.assertEqual(answer, "Model-generated summary")
        engines.search_with_fallback.assert_awaited_once_with("Original question", 7, tavily_topic="general")
        fetcher.integrate_inline_content.assert_called_once_with(
            results, "Previous Tavily answer", engine_name="Tavily"
        )
        fetcher.fetch_batch.assert_not_awaited()
        llm.generate.assert_awaited_once()

    async def test_existing_web_engines_keep_configured_fetch_and_summary_behavior(self) -> None:
        for engine_name in ("bing", "you"):
            for fetch_content in (False, True):
                with self.subTest(engine=engine_name, fetch_content=fetch_content):
                    results = [_result()]
                    pipeline, _, fetcher, llm = _pipeline(engine_name, results, fetch_content=fetch_content)
                    self.assertEqual(
                        await pipeline.run("Original question", bot_name="Test bot"),
                        "Model-generated summary",
                    )
                    if fetch_content:
                        fetcher.fetch_batch.assert_awaited_once_with(results, last_success_engine=engine_name)
                    else:
                        fetcher.fetch_batch.assert_not_awaited()
                    fetcher.integrate_inline_content.assert_not_called()
                    llm.generate.assert_awaited_once()


class NewEngineFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        network_guard = patch(
            "aiohttp.ClientSession",
            side_effect=AssertionError("Fallback regression tests must not issue network requests"),
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def make_chain(
        self,
        *,
        enabled: tuple[str, ...] = ("anysearch", "exa"),
        default: str = "anysearch",
        exa_keys: tuple[str, ...] = ("fake-exa-key",),
    ) -> EngineChain:
        values = {f"{name}_enabled": name in enabled for name in _ENGINE_NAMES}
        values.update(
            exa_api_keys=list(exa_keys),
            tavily_api_keys=["fake-tavily-key"],
            deepseek_api_keys=["fake-deepseek-key"],
            you_api_keys=["fake-you-key"],
        )
        chain = EngineChain(EnginesSection(**values), SearchBackendSection(default_engine=default))
        for name in _ENGINE_NAMES:
            getattr(chain, name).search = AsyncMock(return_value=[])
        return chain

    async def test_new_default_engine_runs_first_and_success_stops_fallback(self) -> None:
        for default in ("anysearch", "exa"):
            with self.subTest(default=default):
                chain = self.make_chain(default=default, enabled=_ENGINE_NAMES)
                results = [_result()]
                getattr(chain, default).search.return_value = results
                actual, name = await chain.search_with_fallback("Original question", 4)
                self.assertEqual((actual, name), (results, default))
                self.assertEqual(chain.last_success_engine, default)
                self.assertIsNone(chain.last_tavily_answer)
                for engine_name in _ENGINE_NAMES:
                    search = getattr(chain, engine_name).search
                    if engine_name == default:
                        search.assert_awaited_once_with("Original question", 4)
                    else:
                        search.assert_not_awaited()

    async def test_enabled_anysearch_can_run_anonymously(self) -> None:
        chain = self.make_chain(enabled=("anysearch",))
        self.assertFalse(chain.anysearch.has_api_keys())
        results = [_result()]
        chain.anysearch.search.return_value = results
        self.assertEqual(await chain.search_with_fallback("question", 3), (results, "anysearch"))
        chain.anysearch.search.assert_awaited_once_with("question", 3)

    async def test_exa_without_keys_is_skipped_and_falls_back_to_anysearch(self) -> None:
        chain = self.make_chain(default="exa", exa_keys=())
        results = [_result()]
        chain.anysearch.search.return_value = results
        self.assertEqual(await chain.search_with_fallback("question", 3), (results, "anysearch"))
        chain.exa.search.assert_not_awaited()

    async def test_disabled_new_engines_are_never_requested(self) -> None:
        chain = self.make_chain(enabled=("bing",), default="exa")
        results = [_result()]
        chain.bing.search.return_value = results
        self.assertEqual(await chain.search_with_fallback("question", 3), (results, "bing"))
        chain.anysearch.search.assert_not_awaited()
        chain.exa.search.assert_not_awaited()

    async def test_new_engines_fall_back_to_each_other_after_empty_results_or_exceptions(self) -> None:
        for default, fallback in (("anysearch", "exa"), ("exa", "anysearch")):
            for failure in ([], RuntimeError("Search unavailable")):
                with self.subTest(default=default, failure=type(failure).__name__):
                    chain = self.make_chain(default=default)
                    primary = getattr(chain, default).search
                    if isinstance(failure, Exception):
                        primary.side_effect = failure
                    else:
                        primary.return_value = failure
                    results = [_result()]
                    getattr(chain, fallback).search.return_value = results
                    self.assertEqual(await chain.search_with_fallback("question", 3), (results, fallback))
                    primary.assert_awaited_once_with("question", 3)
                    getattr(chain, fallback).search.assert_awaited_once_with("question", 3)

    async def test_new_engine_failure_still_reaches_existing_bing_fallback(self) -> None:
        chain = self.make_chain(enabled=("anysearch", "exa", "bing"))
        chain.anysearch.search.side_effect = RuntimeError("Search unavailable")
        results = [_result()]
        chain.bing.search.return_value = results
        self.assertEqual(await chain.search_with_fallback("question", 3), (results, "bing"))
        chain.anysearch.search.assert_awaited_once_with("question", 3)
        chain.exa.search.assert_awaited_once_with("question", 3)
        chain.bing.search.assert_awaited_once_with("question", 3)

    async def test_new_engine_failure_keeps_existing_api_fallback_and_tavily_topic(self) -> None:
        for fallback in ("deepseek", "tavily", "you"):
            with self.subTest(fallback=fallback):
                chain = self.make_chain(enabled=("anysearch", "exa", fallback))
                chain.anysearch.search.side_effect = RuntimeError("Search unavailable")
                chain.tavily.last_answer = "Tavily server answer"
                results = [_result()]
                getattr(chain, fallback).search.return_value = results
                self.assertEqual(
                    await chain.search_with_fallback("question", 3, tavily_topic="general"),
                    (results, fallback),
                )
                chain.anysearch.search.assert_awaited_once_with("question", 3)
                if fallback == "tavily":
                    chain.tavily.search.assert_awaited_once_with("question", 3, topic="general")
                    self.assertEqual(chain.last_tavily_answer, "Tavily server answer")
                else:
                    getattr(chain, fallback).search.assert_awaited_once_with("question", 3)
                    self.assertIsNone(chain.last_tavily_answer)

    async def test_all_empty_engines_preserve_the_empty_chain_contract(self) -> None:
        chain = self.make_chain()
        self.assertEqual(await chain.search_with_fallback("question", 3), ([], ""))


if __name__ == "__main__":
    unittest.main()
