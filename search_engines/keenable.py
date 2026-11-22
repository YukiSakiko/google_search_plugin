import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from .base import ApiKeyMixin, BaseSearchEngine, SearchResult, mask_api_key

logger = logging.getLogger(__name__)


class KeenableEngine(BaseSearchEngine, ApiKeyMixin):
    """Keenable web search. Works without a key through the public endpoint."""

    BASE_URL = "https://api.keenable.ai"
    SEARCH_ENDPOINT = "/v1/search"
    PUBLIC_ENDPOINT = "/v1/search/public"
    CLIENT_TITLE = "google_search_plugin"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self._init_api_keys(self.config)
        self.published_after = str(self.config.get("published_after") or "").strip()

    async def search(self, query: str, num_results: int) -> List[SearchResult]:
        configured_limit = max(1, min(int(self.max_results), 50))
        requested_limit = num_results if num_results > 0 else configured_limit
        limit = max(1, min(requested_limit, configured_limit))
        payload: Dict[str, Any] = {"query": query, "max_results": limit}
        if self.published_after:
            payload["published_after"] = self.published_after
        items = await self._request_items(payload)
        results: List[SearchResult] = []
        seen_urls = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_url = item.get("url")
            url = raw_url.strip() if isinstance(raw_url, str) else ""
            if not self._is_valid_url(url) or any(char.isspace() for char in url) or url in seen_urls:
                continue
            raw_title = item.get("title")
            title = self.tidy_text(raw_title) if isinstance(raw_title, str) else ""
            snippet = ""
            for field in ("snippet", "description"):
                value = item.get(field)
                if isinstance(value, str):
                    snippet = value.strip()
                    if snippet:
                        break
            seen_urls.add(url)
            results.append(
                SearchResult(title=title or url, url=url, snippet=snippet, abstract=snippet, rank=len(results))
            )
            if len(results) == limit:
                break
        return results

    async def _request_items(self, payload: Dict[str, Any]) -> List[Any]:
        # A configured key never falls back to the keyless endpoint.
        api_keys: List[Optional[str]] = list(self._iter_api_keys()) or [None]
        # The outer deadline is shared by all keys; avoid a second, per-request timer.
        deadline = self.TIMEOUT if self.TIMEOUT > 0 else None
        try:
            async with (
                asyncio.timeout(deadline),
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session,
            ):
                for api_key in api_keys:
                    headers = {
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "X-Keenable-Title": self.CLIENT_TITLE,
                    }
                    if api_key:
                        headers["X-API-Key"] = api_key
                    endpoint = self.SEARCH_ENDPOINT if api_key else self.PUBLIC_ENDPOINT
                    async with session.post(
                        f"{self.BASE_URL}{endpoint}",
                        json=payload,
                        headers=headers,
                        proxy=self.proxy,
                        allow_redirects=False,
                    ) as response:
                        if response.status >= 300:
                            logger.warning(
                                "Keenable request failed: status=%s, key=%s",
                                response.status,
                                mask_api_key(api_key),
                            )
                            # Only an invalid credential can be fixed with a different key.
                            if response.status == 401 and api_key:
                                continue
                            return []
                        data = json.loads(await response.text())
                        items = data.get("results") if isinstance(data, dict) else None
                        return items if isinstance(items, list) else []
        except (aiohttp.ClientError, OSError, ValueError) as exc:
            logger.warning("Keenable request failed: exception=%s", type(exc).__name__)
        return []
