"""Offline protocol tests for AnySearch and Exa."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from search_engines.anysearch import AnySearchEngine  # noqa: E402
from search_engines.exa import ExaEngine  # noqa: E402


def response_context(body: object = None, status: int = 200, *, raw: str | None = None) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=json.dumps(body) if raw is None else raw)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def session_context(*responses: MagicMock) -> tuple[MagicMock, MagicMock]:
    session = MagicMock()
    session.post.side_effect = responses
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    return context, session


def anysearch_body(results: list[object]) -> dict:
    return {"code": 0, "message": "success", "data": {"results": results}}


class SearchApiProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_anysearch_anonymous_request_and_content(self) -> None:
        context, session = session_context(
            response_context(
                anysearch_body(
                    [
                        {
                            "title": "",
                            "url": "https://example.com/a",
                            "snippet": "short",
                            "content": "line one\nline two",
                        },
                    ]
                )
            )
        )
        client = AnySearchEngine({"timeout": 17, "proxy": "http://localhost:8080"})
        with patch("aiohttp.ClientSession", return_value=context) as factory:
            results = await client.search("test query", 3)
        args, kwargs = session.post.call_args
        self.assertEqual(args, ("https://api.anysearch.com/v1/search",))
        self.assertEqual(kwargs["json"], {"query": "test query", "max_results": 3, "format": "json"})
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertEqual(kwargs["proxy"], "http://localhost:8080")
        self.assertEqual(factory.call_args.kwargs["timeout"].total, 17)
        self.assertEqual(results[0].title, "https://example.com/a")
        self.assertEqual(results[0].snippet, "short")
        self.assertEqual(results[0].content, "line one\nline two")

    async def test_anysearch_bearer_and_snippet_fallback(self) -> None:
        context, session = session_context(
            response_context(
                anysearch_body(
                    [
                        {"title": "Title", "url": "https://example.com/a", "snippet": "first\nsecond"},
                    ]
                )
            )
        )
        with patch("aiohttp.ClientSession", return_value=context):
            results = await AnySearchEngine({"api_keys": ["test-key"]}).search("query", 1)
        self.assertEqual(session.post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(results[0].content, "first\nsecond")

    async def test_anysearch_advanced_parameters_and_markdown_response(self) -> None:
        context, session = session_context(
            response_context(
                anysearch_body(
                    [
                        {"title": "Guide", "url": "https://example.com/a", "content": "# Heading\n\n- First\n- Second"},
                    ]
                )
            )
        )
        config = {
            "tag": " code.doc ",
            "zone": "intl",
            "language": " en ",
            "params": {"library": "golang", "nested": {"values": [1, False]}},
            "format": "markdown",
        }
        with patch("aiohttp.ClientSession", return_value=context):
            results = await AnySearchEngine(config).search("query", 2)
        self.assertEqual(
            session.post.call_args.kwargs["json"],
            {
                "query": "query",
                "max_results": 2,
                "format": "markdown",
                "tag": "code.doc",
                "zone": "intl",
                "language": "en",
                "params": {"library": "golang", "nested": {"values": [1, False]}},
            },
        )
        self.assertEqual(results[0].content, "# Heading\n\n- First\n- Second")

    async def test_anysearch_empty_advanced_options_are_omitted(self) -> None:
        context, session = session_context(response_context(anysearch_body([])))
        with patch("aiohttp.ClientSession", return_value=context):
            await AnySearchEngine({"tag": " ", "zone": "", "language": " ", "params": {}}).search("query", 2)
        self.assertEqual(session.post.call_args.kwargs["json"], {"query": "query", "max_results": 2, "format": "json"})

    async def test_exa_key_is_required(self) -> None:
        with patch("aiohttp.ClientSession") as factory:
            self.assertEqual(await ExaEngine({}).search("query", 1), [])
        factory.assert_not_called()

    async def test_exa_request_and_highlights(self) -> None:
        context, session = session_context(
            response_context(
                {
                    "results": [
                        {
                            "title": " Title\nHere ",
                            "url": "https://example.com/a",
                            "highlights": ["first", None, "second\nthird"],
                        },
                    ]
                }
            )
        )
        client = ExaEngine({"api_keys": ["test-key"], "timeout": 23, "proxy": "http://localhost:8080"})
        with patch("aiohttp.ClientSession", return_value=context) as factory:
            results = await client.search("test query", 4)
        args, kwargs = session.post.call_args
        self.assertEqual(args, ("https://api.exa.ai/search",))
        self.assertEqual(
            kwargs["json"],
            {
                "query": "test query",
                "numResults": 4,
                "type": "auto",
                "contents": {"highlights": True},
            },
        )
        self.assertEqual(kwargs["headers"]["x-api-key"], "test-key")
        self.assertEqual(kwargs["proxy"], "http://localhost:8080")
        self.assertEqual(factory.call_args.kwargs["timeout"].total, 23)
        self.assertEqual(results[0].title, "Title Here")
        self.assertEqual(results[0].snippet, "first second third")
        self.assertEqual(results[0].content, "first\nsecond\nthird")

    async def test_exa_custom_search_type_and_text_fallback(self) -> None:
        context, session = session_context(
            response_context(
                {
                    "results": [
                        {
                            "title": None,
                            "url": "https://example.com/a",
                            "highlights": {"bad": "shape"},
                            "text": "first\nsecond",
                        },
                    ]
                }
            )
        )
        with patch("aiohttp.ClientSession", return_value=context):
            results = await ExaEngine({"api_keys": ["test-key"], "search_type": "lite"}).search("query", 1)
        self.assertEqual(session.post.call_args.kwargs["json"]["type"], "lite")
        self.assertEqual(results[0].title, "https://example.com/a")
        self.assertEqual(results[0].content, "first\nsecond")

    async def test_exa_advanced_parameters_map_exactly_and_full_text_wins(self) -> None:
        context, session = session_context(
            response_context(
                {
                    "results": [
                        {
                            "title": "Guide",
                            "url": "https://example.com/a",
                            "highlights": ["short excerpt"],
                            "text": "full\ntext",
                        },
                    ]
                }
            )
        )
        config = {
            "api_keys": ["test-key"],
            "search_type": "deep",
            "category": "news",
            "include_domains": [" example.com/docs ", "*.example.org", "example.com/docs"],
            "exclude_domains": ["example.net"],
            "start_published_date": "2026-01-01T00:00:00Z",
            "end_published_date": "2026-10-28T00:00:00Z",
            "user_location": "CN",
            "moderation": True,
            "objective": "prefer primary sources",
            "additional_queries": ["other perspective"],
            "include_highlights": True,
            "highlights_query": "focus",
            "highlights_max_characters": 3000,
            "include_text": True,
            "text_max_characters": 10000,
            "max_age_hours": 24,
            "livecrawl_timeout_ms": 7000,
        }
        with patch("aiohttp.ClientSession", return_value=context):
            results = await ExaEngine(config).search("query", 4)
        self.assertEqual(
            session.post.call_args.kwargs["json"],
            {
                "query": "query",
                "numResults": 4,
                "type": "deep",
                "category": "news",
                "includeDomains": ["example.com/docs", "*.example.org"],
                "excludeDomains": ["example.net"],
                "startPublishedDate": "2026-01-01T00:00:00Z",
                "endPublishedDate": "2026-10-28T00:00:00Z",
                "userLocation": "CN",
                "moderation": True,
                "objective": "prefer primary sources",
                "additionalQueries": ["other perspective"],
                "contents": {
                    "highlights": {"query": "focus", "maxCharacters": 3000},
                    "text": {"maxCharacters": 10000},
                    "maxAgeHours": 24,
                    "livecrawlTimeout": 7000,
                },
            },
        )
        self.assertEqual(results[0].content, "full\ntext")

    async def test_exa_empty_advanced_options_keep_default_payload(self) -> None:
        context, session = session_context(response_context({"results": []}))
        config = {
            "api_keys": ["test-key"],
            "category": " ",
            "include_domains": [" "],
            "exclude_domains": [],
            "start_published_date": "",
            "end_published_date": " ",
            "user_location": "",
            "moderation": False,
            "objective": " ",
            "additional_queries": [],
            "highlights_query": " ",
            "highlights_max_characters": 0,
            "include_text": False,
            "text_max_characters": 0,
            "max_age_hours": -2,
            "livecrawl_timeout_ms": 0,
        }
        with patch("aiohttp.ClientSession", return_value=context):
            await ExaEngine(config).search("query", 2)
        self.assertEqual(
            session.post.call_args.kwargs["json"],
            {
                "query": "query",
                "numResults": 2,
                "type": "auto",
                "contents": {"highlights": True},
            },
        )

    async def test_exa_lite_supports_filters_highlights_and_full_text(self) -> None:
        context, session = session_context(response_context({"results": []}))
        config = {
            "api_keys": ["test-key"],
            "search_type": "lite",
            "category": "news",
            "include_domains": ["example.com"],
            "start_published_date": "2026-01-01T00:00:00Z",
            "include_text": True,
            "highlights_query": "focus",
        }
        with patch("aiohttp.ClientSession", return_value=context):
            await ExaEngine(config).search("query", 2)
        self.assertEqual(
            session.post.call_args.kwargs["json"],
            {
                "query": "query",
                "numResults": 2,
                "type": "lite",
                "category": "news",
                "includeDomains": ["example.com"],
                "startPublishedDate": "2026-01-01T00:00:00Z",
                "contents": {"highlights": {"query": "focus"}, "text": True},
            },
        )

    async def test_exa_incompatible_lite_options_fail_before_network(self) -> None:
        for options in (
            {"max_age_hours": -1},
            {"max_age_hours": 0},
            {"max_age_hours": 24},
            {"livecrawl_timeout_ms": 1000},
            {"additional_queries": ["another query"]},
        ):
            with self.subTest(options=options), patch("aiohttp.ClientSession") as factory:
                with self.assertRaisesRegex(ValueError, "Exa"):
                    client = ExaEngine({"api_keys": ["test-key"], "search_type": "lite", **options})
                    await client.search("query", 1)
                factory.assert_not_called()

    async def test_exa_additional_queries_only_for_deep_types(self) -> None:
        for search_type in ("deep-lite", "deep"):
            with self.subTest(search_type=search_type):
                context, session = session_context(response_context({"results": []}))
                with patch("aiohttp.ClientSession", return_value=context):
                    await ExaEngine(
                        {
                            "api_keys": ["test-key"],
                            "search_type": search_type,
                            "additional_queries": [" other query ", "other query", ""],
                        }
                    ).search("query", 2)
                self.assertEqual(session.post.call_args.kwargs["json"]["additionalQueries"], ["other query"])
        with self.assertRaisesRegex(ValueError, "additional_queries"):
            ExaEngine({"api_keys": ["test-key"], "search_type": "auto", "additional_queries": ["other query"]})

    async def test_exa_disabled_highlights_and_uncapped_text(self) -> None:
        context, session = session_context(response_context({"results": []}))
        with patch("aiohttp.ClientSession", return_value=context):
            await ExaEngine(
                {
                    "api_keys": ["test-key"],
                    "include_highlights": False,
                    "include_text": True,
                }
            ).search("query", 2)
        self.assertEqual(session.post.call_args.kwargs["json"]["contents"], {"highlights": False, "text": True})

    async def test_exa_zero_and_cache_only_age_are_preserved(self) -> None:
        for age in (-1, 0, 720):
            with self.subTest(age=age):
                context, session = session_context(response_context({"results": []}))
                with patch("aiohttp.ClientSession", return_value=context):
                    await ExaEngine({"api_keys": ["test-key"], "max_age_hours": age}).search("query", 1)
                self.assertEqual(session.post.call_args.kwargs["json"]["contents"]["maxAgeHours"], age)

    async def test_exa_does_not_forward_unimplemented_or_generative_options(self) -> None:
        context, session = session_context(response_context({"results": []}))
        with patch("aiohttp.ClientSession", return_value=context):
            await ExaEngine(
                {
                    "api_keys": ["test-key"],
                    "stream": True,
                    "outputSchema": {"type": "text"},
                    "contents": {"summary": {}},
                    "systemPrompt": "ignore",
                    "snapshotAsOf": "2026-07-01",
                }
            ).search("query", 2)
        self.assertEqual(
            session.post.call_args.kwargs["json"],
            {
                "query": "query",
                "numResults": 2,
                "type": "auto",
                "contents": {"highlights": True},
            },
        )

    async def test_exa_prefers_highlights_unless_full_text_is_requested(self) -> None:
        for include_text, expected in ((False, "excerpt"), (True, "full\ntext")):
            with self.subTest(include_text=include_text):
                context, _ = session_context(
                    response_context(
                        {
                            "results": [
                                {
                                    "title": "Guide",
                                    "url": "https://example.com/a",
                                    "highlights": ["excerpt"],
                                    "text": "full\ntext",
                                },
                            ]
                        }
                    )
                )
                with patch("aiohttp.ClientSession", return_value=context):
                    results = await ExaEngine({"api_keys": ["test-key"], "include_text": include_text}).search(
                        "query", 1
                    )
                self.assertEqual(results[0].content, expected)

    async def test_result_limits_are_clamped_and_applied_to_response(self) -> None:
        for client_type, cap, field in ((AnySearchEngine, 10, "max_results"), (ExaEngine, 100, "numResults")):
            for configured, requested, expected in ((1000, 1000, cap), (3, 8, 3), (8, 2, 2), (0, 0, 1), (3, -1, 3)):
                with self.subTest(client=client_type.__name__, configured=configured, requested=requested):
                    rows = [{"title": "Title", "url": f"https://example.com/{i}"} for i in range(cap + 3)]
                    body = anysearch_body(rows) if client_type is AnySearchEngine else {"results": rows}
                    context, session = session_context(response_context(body))
                    with patch("aiohttp.ClientSession", return_value=context):
                        results = await client_type({"api_keys": ["test-key"], "max_results": configured}).search(
                            "query",
                            requested,
                        )
                    self.assertEqual(session.post.call_args.kwargs["json"][field], expected)
                    self.assertEqual(len(results), expected)

    async def test_invalid_rows_and_duplicate_urls_are_handled(self) -> None:
        rows = [
            None,
            "bad",
            {"url": 123},
            {"url": "javascript:alert(1)"},
            {"url": "https://bad host.example/a"},
            {"title": {"bad": "title"}, "url": "https://example.com/a", "snippet": [1], "content": {"bad": 1}},
            {"title": "Duplicate", "url": "https://example.com/a"},
            {"title": "Good", "url": "https://example.com/b", "text": 42, "highlights": [None, {}, "excerpt"]},
        ]
        for client_type in (AnySearchEngine, ExaEngine):
            with self.subTest(client=client_type.__name__):
                body = anysearch_body(rows) if client_type is AnySearchEngine else {"results": rows}
                context, _ = session_context(response_context(body))
                with patch("aiohttp.ClientSession", return_value=context):
                    results = await client_type({"api_keys": ["test-key"]}).search("query", 10)
                self.assertEqual([result.url for result in results], ["https://example.com/a", "https://example.com/b"])
                self.assertEqual([result.rank for result in results], [0, 1])
                self.assertEqual(results[0].content, "")

    async def test_bad_responses_retry_next_key(self) -> None:
        malformed_bodies = [None, [], {}, {"results": {}}, {"data": []}, {"code": -1}, {"code": "0"}]
        for client_type in (AnySearchEngine, ExaEngine):
            for bad_body in malformed_bodies:
                with self.subTest(client=client_type.__name__, body=bad_body):
                    rows = [{"url": "https://example.com/a", "title": "Good", "content": "text"}]
                    body = anysearch_body(rows) if client_type is AnySearchEngine else {"results": rows}
                    context, session = session_context(response_context(bad_body), response_context(body))
                    client = client_type({"api_keys": ["first-key", "second-key"]})
                    with (
                        patch("aiohttp.ClientSession", return_value=context),
                        patch.object(
                            client,
                            "_iter_api_keys",
                            return_value=["first-key", "second-key"],
                        ),
                    ):
                        results = await client.search("query", 1)
                    self.assertEqual(len(results), 1)
                    self.assertEqual(session.post.call_count, 2)
                    auth_header = "Authorization" if client_type is AnySearchEngine else "x-api-key"
                    auth_prefix = "Bearer " if client_type is AnySearchEngine else ""
                    self.assertEqual(
                        [call.kwargs["headers"][auth_header] for call in session.post.call_args_list],
                        [f"{auth_prefix}first-key", f"{auth_prefix}second-key"],
                    )

    async def test_http_empty_json_and_exception_failures_retry(self) -> None:
        for client_type in (AnySearchEngine, ExaEngine):
            for bad_response in (
                response_context(status=429),
                response_context(raw=""),
                response_context(raw="not-json"),
            ):
                with self.subTest(client=client_type.__name__):
                    rows = [{"url": "https://example.com/a", "title": "Good"}]
                    body = anysearch_body(rows) if client_type is AnySearchEngine else {"results": rows}
                    context, session = session_context(bad_response, response_context(body))
                    client = client_type({"api_keys": ["first-key", "second-key"]})
                    with (
                        patch("aiohttp.ClientSession", return_value=context),
                        patch.object(
                            client,
                            "_iter_api_keys",
                            return_value=["first-key", "second-key"],
                        ),
                    ):
                        self.assertEqual(len(await client.search("query", 1)), 1)
                    self.assertEqual(session.post.call_count, 2)

    async def test_exception_and_all_invalid_rows_retry_next_key(self) -> None:
        for client_type in (AnySearchEngine, ExaEngine):
            exception = response_context()
            exception.__aenter__.side_effect = TimeoutError("do not log this message")
            bad_rows = [None, {"url": "invalid"}]
            bad_body = anysearch_body(bad_rows) if client_type is AnySearchEngine else {"results": bad_rows}
            for failed in (exception, response_context(bad_body)):
                with self.subTest(client=client_type.__name__):
                    rows = [{"url": "https://example.com/a", "title": "Good"}]
                    body = anysearch_body(rows) if client_type is AnySearchEngine else {"results": rows}
                    context, session = session_context(failed, response_context(body))
                    client = client_type({"api_keys": ["first-key", "second-key"]})
                    with (
                        patch("aiohttp.ClientSession", return_value=context),
                        patch.object(
                            client,
                            "_iter_api_keys",
                            return_value=["first-key", "second-key"],
                        ),
                    ):
                        self.assertEqual(len(await client.search("query", 1)), 1)
                    self.assertEqual(session.post.call_count, 2)

    async def test_anysearch_invalid_configured_key_never_falls_back_to_anonymous(self) -> None:
        context, session = session_context(response_context({"code": -1}, status=401))
        with patch("aiohttp.ClientSession", return_value=context):
            self.assertEqual(await AnySearchEngine({"api_keys": ["bad-test-key"]}).search("query", 1), [])
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(session.post.call_args.kwargs["headers"]["Authorization"], "Bearer bad-test-key")

    async def test_empty_result_list_does_not_retry_keys(self) -> None:
        for client_type in (AnySearchEngine, ExaEngine):
            body = anysearch_body([]) if client_type is AnySearchEngine else {"results": []}
            context, session = session_context(response_context(body))
            with patch("aiohttp.ClientSession", return_value=context):
                self.assertEqual(await client_type({"api_keys": ["first-key", "second-key"]}).search("query", 1), [])
            self.assertEqual(session.post.call_count, 1)

    async def test_anysearch_errors_never_log_credentials_or_exception_text(self) -> None:
        secret = "username=private-user password=private-password api_key=private-secret"
        http_error = response_context({"code": -1, "message": secret}, status=402)
        body_error = response_context({"code": -1, "message": secret})
        invalid_code = response_context({"code": secret, "message": secret})
        bad_json = response_context(raw=secret)
        exception_response = response_context()
        exception_response.__aenter__.side_effect = RuntimeError(secret)
        for response in (http_error, body_error, invalid_code, bad_json, exception_response):
            with self.subTest(response=response):
                context, _ = session_context(response)
                with (
                    patch("aiohttp.ClientSession", return_value=context),
                    self.assertLogs(
                        "search_engines.anysearch",
                        level="WARNING",
                    ) as captured,
                ):
                    self.assertEqual(await AnySearchEngine({}).search("query", 1), [])
                output = "\n".join(captured.output)
                for value in ("private-user", "private-password", "private-secret"):
                    self.assertNotIn(value, output)
        http_error.__aenter__.return_value.text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
