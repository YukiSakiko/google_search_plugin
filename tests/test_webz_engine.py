"""Offline protocol tests for the optional Webz.io News Search engine."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from search_engines.webz import WebzEngine  # noqa: E402

_TOKEN = "fixture-webz-token"
_ENV_TOKEN = "fixture-env-webz-token"


def response_context(body: object = None, status: int = 200, *, raw: str | None = None) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=json.dumps(body) if raw is None else raw)
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


def article(
    url: str = "https://example.com/a",
    title: str = "Headline",
    text: str = "Relevant excerpt",
    summary: str = "Short summary",
) -> dict:
    return {
        "score": 7.1,
        "article": {
            "url": url,
            "title": title,
            "summary": summary,
            "published_at": "2026-11-20T00:00:00Z",
        },
        "chunk": {"text": text},
    }


class WebzEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("WEBZ_API_TOKEN", None)
        self.addCleanup(self._env.stop)

    async def test_missing_token_does_not_call_the_network(self) -> None:
        engine = WebzEngine({})
        self.assertFalse(engine.has_api_keys())
        with patch("aiohttp.ClientSession", side_effect=AssertionError("no token")):
            self.assertEqual(await engine.search("news", 3), [])

    async def test_configured_key_posts_news_context_without_a_days_field(self) -> None:
        context, session = session_context(response_context({"results": [article()]}))
        engine = WebzEngine(
            {
                "api_keys": [f" {_TOKEN} "],
                "timeout": 12,
                "proxy": "http://127.0.0.1:8080",
                "max_results": 10,
                "days": 7,
                "language": " English ",
                "country": " us ",
                "sentiment": "positive",
                "domains": [" example.com ", "example.com", ""],
                "tickers": [" nvda ", "NVDA"],
            }
        )
        fixed = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
        with (
            patch.dict(os.environ, {"WEBZ_API_TOKEN": _ENV_TOKEN}),
            patch("search_engines.webz.datetime") as clock,
            patch("aiohttp.ClientSession", return_value=context),
        ):
            clock.now.return_value = fixed
            results = await engine.search("  nvidia earnings  ", 4)
        args, kwargs = session.post.call_args
        self.assertEqual(args, ("https://api.webz.io/api/news/context",))
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {_TOKEN}")
        self.assertNotIn(_ENV_TOKEN, json.dumps(kwargs["json"]))
        self.assertNotIn(_TOKEN, json.dumps(kwargs["json"]))
        self.assertNotIn("days", kwargs["json"])
        self.assertNotIn("days", kwargs["json"]["filters"])
        self.assertEqual(
            kwargs["json"],
            {
                "query": "nvidia earnings",
                "k": 4,
                "filters": {
                    "published_from": "2026-11-17",
                    "language": ["english"],
                    "country": ["US"],
                    "sentiment": ["positive"],
                    "domain": ["example.com"],
                    "ticker": ["NVDA"],
                },
            },
        )
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["proxy"], "http://127.0.0.1:8080")
        self.assertEqual(results[0].title, "Headline")
        self.assertEqual(results[0].url, "https://example.com/a")
        self.assertEqual(results[0].content, "Relevant excerpt")
        self.assertEqual(engine.api_keys, [_TOKEN])

    async def test_env_token_is_used_only_when_the_key_list_is_empty(self) -> None:
        context, session = session_context(response_context({"results": []}))
        engine = WebzEngine({"days": 0})
        self.assertEqual(engine.api_keys, [])
        self.assertTrue(engine.has_api_keys() is False)
        with patch.dict(os.environ, {"WEBZ_API_TOKEN": f"  {_ENV_TOKEN}  "}):
            self.assertTrue(engine.has_api_keys())
            fixed = datetime(2026, 11, 24, tzinfo=timezone.utc)
            with (
                patch("search_engines.webz.datetime") as clock,
                patch("aiohttp.ClientSession", return_value=context),
            ):
                clock.now.return_value = fixed
                self.assertEqual(await engine.search("query", 0), [])
        self.assertEqual(session.post.call_args.kwargs["headers"]["Authorization"], f"Bearer {_ENV_TOKEN}")
        self.assertEqual(session.post.call_args.kwargs["json"]["filters"]["published_from"], "2026-11-24")
        self.assertEqual(session.post.call_args.kwargs["json"]["k"], 10)
        self.assertEqual(engine.api_keys, [])

    async def test_non_https_endpoint_is_rejected_before_the_token_is_sent(self) -> None:
        for base_url in ("http://api.webz.io", "https://user:secret@api.webz.io", "https://api.webz.io?token=secret"):
            with self.subTest(base_url=base_url):
                engine = WebzEngine({"api_keys": [_TOKEN]})
                engine.BASE_URL = base_url
                with patch("aiohttp.ClientSession", side_effect=AssertionError(base_url)):
                    self.assertEqual(await engine.search("query", 1), [])

    async def test_empty_or_oversized_query_does_not_send_the_token(self) -> None:
        engine = WebzEngine({"api_keys": [_TOKEN]})
        with patch("aiohttp.ClientSession", side_effect=AssertionError("query")):
            self.assertEqual(await engine.search("   ", 1), [])
            self.assertEqual(await engine.search("x" * 751, 1), [])
            self.assertEqual(await engine.search(" ".join(["word"] * 101), 1), [])

    async def test_failed_key_tries_the_next_key(self) -> None:
        context, session = session_context(
            response_context({"detail": "unauthorized"}, status=401),
            response_context({"results": [article(title="Second key")]}),
        )
        engine = WebzEngine({"api_keys": ["first-token-1234", "second-token-5678"]})
        with (
            patch("search_engines.base.random.sample", side_effect=lambda population, k: list(population)),
            patch("aiohttp.ClientSession", return_value=context),
        ):
            results = await engine.search("query", 2)
        self.assertEqual([item.title for item in results], ["Second key"])
        self.assertEqual(session.post.call_count, 2)
        headers = [call.kwargs["headers"]["Authorization"] for call in session.post.call_args_list]
        self.assertEqual(headers, ["Bearer first-token-1234", "Bearer second-token-5678"])
        for call in session.post.call_args_list:
            self.assertNotIn("first-token-1234", json.dumps(call.kwargs["json"]))
            self.assertFalse(call.kwargs["allow_redirects"])

    async def test_results_skip_invalid_rows_and_duplicate_urls(self) -> None:
        payload = {
            "results": [
                article(url="https://example.com/a", title="", text="from chunk", summary="unused summary"),
                article(url="https://example.com/a", title="Duplicate"),
                article(url="https://example.com/b", title="Summary only", text="  ", summary="from summary"),
                article(url="ftp://example.com/c", title="Bad scheme"),
                {"article": "nope"},
                "not a dict",
            ]
        }
        context, _session = session_context(response_context(payload))
        engine = WebzEngine({"api_keys": [_TOKEN], "max_results": 10})
        with patch("aiohttp.ClientSession", return_value=context):
            results = await engine.search("query", 5)
        self.assertEqual([item.url for item in results], ["https://example.com/a", "https://example.com/b"])
        self.assertEqual(results[0].title, "https://example.com/a")
        self.assertEqual(results[0].content, "from chunk")
        self.assertEqual(results[1].content, "from summary")
        self.assertEqual([item.rank for item in results], [0, 1])

    async def test_request_and_service_errors_never_try_another_token(self) -> None:
        body = {"results": [article()]}
        cases = {
            "redirect with data": response_context(body, 307),
            "bad query": response_context(body, 400),
            "invalid filters": response_context(body, 422),
            "service unavailable": response_context(body, 503),
            "invalid JSON": response_context(raw="invalid"),
            "empty response": response_context(raw=""),
            "connection failure": OSError("disconnected"),
        }
        engine = WebzEngine({"api_keys": ["fixture-a", "fixture-b", "fixture-c"]})
        for reason, failure in cases.items():
            with self.subTest(reason=reason):
                context, session = session_context(failure, response_context(body))
                with patch("aiohttp.ClientSession", return_value=context):
                    self.assertEqual(await engine.search("query", 2), [])
                session.post.assert_called_once()

    async def test_key_scoped_statuses_try_each_configured_token_once_without_using_env(self) -> None:
        engine = WebzEngine({"api_keys": ["fixture-a", "fixture-a", "fixture-b"]})
        for status in (401, 402, 403, 429):
            with self.subTest(status=status):
                ctx, session = session_context(
                    response_context(status=status),
                    response_context(status=status),
                    response_context({"results": [article()]}),
                )
                with patch.dict(os.environ, {"WEBZ_API_TOKEN": _ENV_TOKEN}), patch(
                    "aiohttp.ClientSession", return_value=ctx
                ):
                    self.assertEqual(await engine.search("query", 1), [])
                headers = [call.kwargs["headers"]["Authorization"] for call in session.post.call_args_list]
                self.assertCountEqual(headers, ["Bearer fixture-a", "Bearer fixture-b"])

    async def test_timeout_budget_survives_key_rotation(self) -> None:
        # Spend 6 of 10 seconds on the first key; a 5-second second request is too late.
        loop = asyncio.get_running_loop()
        clock = loop.time
        elapsed = 0
        first = response_context(status=401)
        unauthorized = first.__aenter__.return_value

        async def first_response():
            nonlocal elapsed
            elapsed += 6
            await asyncio.sleep(0)
            return unauthorized

        async def second_body() -> str:
            nonlocal elapsed
            elapsed += 5
            await asyncio.sleep(0.01)
            return json.dumps({"results": [article()]})

        first.__aenter__.side_effect = first_response
        second = response_context()
        second.__aenter__.return_value.text.side_effect = second_body
        context, session = session_context(first, second, response_context({"results": [article()]}))
        engine = WebzEngine({"api_keys": ["fixture-a", "fixture-b", "fixture-c"], "timeout": 10})
        with (
            patch.object(loop, "slow_callback_duration", 60),
            patch.object(loop, "time", side_effect=lambda: clock() + elapsed),
            patch("aiohttp.ClientSession", return_value=context),
        ):
            self.assertEqual(await engine.search("query", 1), [])
        self.assertEqual(session.post.call_count, 2)

    async def test_nonpositive_timeouts_do_not_set_a_session_or_search_deadline(self) -> None:
        async def read_body() -> str:
            await asyncio.sleep(0)
            return json.dumps({"results": [article()]})

        for timeout in (0, -1):
            with self.subTest(timeout=timeout):
                response = response_context()
                response.__aenter__.return_value.text.side_effect = read_body
                context, _ = session_context(response)
                with patch("aiohttp.ClientSession", return_value=context) as factory:
                    results = await WebzEngine({"api_keys": [_TOKEN], "timeout": timeout}).search("query", 2)
                self.assertEqual([r.title for r in results], ["Headline"])
                self.assertIsNone(factory.call_args.kwargs["timeout"].total)

    async def test_external_cancellation_is_not_treated_as_a_failed_key(self) -> None:
        started = asyncio.Event()

        async def blocked_body():
            started.set()
            await asyncio.Future()

        response = response_context()
        response.__aenter__.return_value.text.side_effect = blocked_body
        context, session = session_context(response, response_context({"results": [article()]}))
        engine = WebzEngine({"api_keys": ["fixture-a", "fixture-b"]})
        with patch("aiohttp.ClientSession", return_value=context):
            task = asyncio.create_task(engine.search("query", 1))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        session.post.assert_called_once()

    async def test_only_provided_dates_are_kept_and_long_chunks_are_preserved(self) -> None:
        chunk = "A" * 5200 + "\n\n    tail"
        rows = [article(url=f"https://example.com/{i}", text=chunk) for i in range(4)]
        rows[0]["article"]["published_at"] = " 2026-11-20T07:15:00+08:00 "
        rows[1]["article"].pop("published_at")
        rows[2]["article"]["published_at"] = 12345
        rows[3]["article"]["published_at"] = None
        context, _ = session_context(response_context({"results": rows}))
        with patch("aiohttp.ClientSession", return_value=context):
            results = await WebzEngine({"api_keys": [_TOKEN]}).search("query", 4)
        self.assertEqual([item.published_at for item in results], ["2026-11-20T07:15:00+08:00", "", "", ""])
        self.assertEqual([(item.snippet, item.abstract, item.content) for item in results], [(chunk, chunk, chunk)] * 4)

    async def test_k_is_capped_at_100(self) -> None:
        context, session = session_context(response_context({"results": []}))
        engine = WebzEngine({"api_keys": [_TOKEN], "max_results": 500})
        with patch("aiohttp.ClientSession", return_value=context):
            await engine.search("query", 250)
        self.assertEqual(session.post.call_args.kwargs["json"]["k"], 100)


if __name__ == "__main__":
    unittest.main()
