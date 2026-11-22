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
        timeout = aiohttp.ClientTimeout(total=self.TIMEOUT)
        # A configured key never falls back to the keyless endpoint.
        api_keys: List[Optional[str]] = list(self._iter_api_keys()) or [None]

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for api_key in api_keys:
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Keenable-Title": self.CLIENT_TITLE,
                }
                if api_key:
                    headers["X-API-Key"] = api_key
                endpoint = self.SEARCH_ENDPOINT if api_key else self.PUBLIC_ENDPOINT
                try:
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
                            continue
                        data = json.loads(await response.text())
                except Exception as exc:
                    logger.warning(
                        "Keenable request failed: exception=%s, key=%s",
                        type(exc).__name__,
                        mask_api_key(api_key),
                    )
                    continue

                items = data.get("results") if isinstance(data, dict) else None
                if not isinstance(items, list):
                    continue
                if not items:
                    return []

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
                    raw_snippet = item.get("snippet") or item.get("description")
                    snippet = self.tidy_text(raw_snippet) if isinstance(raw_snippet, str) else ""
                    seen_urls.add(url)
                    results.append(
                        SearchResult(
                            title=title or url,
                            url=url,
                            snippet=snippet,
                            abstract=snippet,
                            rank=len(results),
                        )
                    )
                    if len(results) == limit:
                        break
                if results:
                    return results

        return []
