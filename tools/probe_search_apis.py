"""Probe live search pipelines; credentials are read from one JSON line on stdin."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys
import time
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from plugins.google_search_plugin.config import EnginesSection, SearchBackendSection  # noqa: E402
from plugins.google_search_plugin.pipelines.engine_chain import EngineChain  # noqa: E402
from plugins.google_search_plugin.pipelines.search_pipeline import SearchPipeline  # noqa: E402


async def probe(provider: str, key: str, args: argparse.Namespace) -> dict:
    engines = EnginesSection(
        google_enabled=False,
        bing_enabled=False,
        sogou_enabled=False,
        duckduckgo_enabled=False,
        **{
            f"{provider}_enabled": True,
            f"{provider}_api_keys": [key] if key else [],
            "exa_search_type": args.search_type,
            **json.loads(args.engine_options),
        },
    )
    backend = SearchBackendSection(
        default_engine=provider,
        timeout=args.timeout,
        max_results=args.num_results,
        fetch_content=True,
    )
    chain = EngineChain(engines, backend)
    fetcher = Mock()
    fetcher.fetch_batch = AsyncMock(side_effect=AssertionError("Unexpected local crawl"))
    fetcher.integrate_inline_content = Mock(side_effect=AssertionError("Unexpected inline integration"))
    llm = Mock()
    llm.generate = AsyncMock(side_effect=AssertionError("Unexpected local LLM call"))
    pipeline = SearchPipeline(backend_cfg=backend, engine_chain=chain, content_fetcher=fetcher, llm_runner=llm)
    found = []
    original_search = chain.search_with_fallback

    async def observed_search(*positional, **keywords):
        nonlocal found
        found, engine = await original_search(*positional, **keywords)
        return found, engine

    chain.search_with_fallback = observed_search
    requests = []
    trace = aiohttp.TraceConfig()

    async def on_request_end(session, context, params):
        requests.append({"status": params.response.status})

    trace.on_request_end.append(on_request_end)
    original_session = aiohttp.ClientSession

    def observed_session(*positional, **keywords):
        return original_session(*positional, **keywords, trace_configs=[trace])

    started = time.perf_counter()
    error_type = None
    answer = ""
    with patch("aiohttp.ClientSession", side_effect=observed_session):
        try:
            answer = await pipeline.run(args.query, bot_name="Probe")
        except Exception as exc:
            error_type = type(exc).__name__
    return {
        "provider": provider,
        "search_type": args.search_type if provider == "exa" else None,
        "success": bool(found) and error_type is None,
        "requests": requests,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "result_count": len(found),
        "content_characters": sum(len(result.content) for result in found),
        "answer_characters": len(answer),
        "local_crawl_calls": fetcher.fetch_batch.await_count,
        "local_llm_calls": llm.generate.await_count,
        "first_result": {"title": found[0].title, "url": found[0].url} if found else None,
        "error_type": error_type,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("anysearch", "exa", "both"), default="both")
    parser.add_argument("--query", default="Python asyncio documentation")
    parser.add_argument("--num-results", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--search-type", choices=("auto", "lite", "deep-lite", "deep"), default="auto")
    parser.add_argument("--engine-options", default="{}", help="Non-secret EnginesSection options as JSON")
    args = parser.parse_args()
    credentials = json.loads(sys.stdin.readline())
    providers = ("anysearch", "exa") if args.provider == "both" else (args.provider,)
    reports = []
    for provider in providers:
        try:
            reports.append(await probe(provider, credentials.get(provider, ""), args))
        except Exception as exc:
            reports.append({"provider": provider, "success": False, "error_type": type(exc).__name__})
    output = json.dumps(reports, ensure_ascii=False)
    for key in credentials.values():
        if isinstance(key, str) and key:
            output = output.replace(key, "<redacted>")
    print(output)
    return 0 if all(report["success"] for report in reports) else 1


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    raise SystemExit(asyncio.run(main()))
