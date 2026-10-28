import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from .base import ApiKeyMixin, BaseSearchEngine, SearchResult, mask_api_key

logger = logging.getLogger(__name__)


class AnySearchEngine(BaseSearchEngine, ApiKeyMixin):
    """Search AnySearch's unified API without local crawling or synthesis."""

    BASE_URL = "https://api.anysearch.com"
    SEARCH_ENDPOINT = "/v1/search"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self._init_api_keys(self.config)
        self.tag = str(self.config.get("tag") or "").strip()
        self.zone = str(self.config.get("zone") or "").strip()
        self.language = str(self.config.get("language") or "").strip()
        self.params = self.config.get("params") or {}
        self.format = self.config.get("format") or "json"
        if self.zone not in ("", "cn", "intl"):
            raise ValueError("AnySearch zone must be empty, cn, or intl.")
        if not isinstance(self.params, dict):
            raise ValueError("AnySearch params must be a JSON object.")
        if self.format not in ("json", "markdown"):
            raise ValueError("AnySearch format must be json or markdown.")

    async def search(self, query: str, num_results: int) -> List[SearchResult]:
        configured_limit = max(1, min(int(self.max_results), 10))
        requested_limit = num_results if num_results > 0 else configured_limit
        limit = max(1, min(requested_limit, configured_limit))
        payload = {"query": query, "max_results": limit, "format": self.format}
        for field, value in (
            ("tag", self.tag),
            ("zone", self.zone),
            ("language", self.language),
            ("params", self.params),
        ):
            if value:
                payload[field] = value
        timeout = aiohttp.ClientTimeout(total=self.TIMEOUT)
        api_keys = self._iter_api_keys() or [None]

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for api_key in api_keys:
                headers = {"Content-Type": "application/json", "Accept": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"

                try:
                    async with session.post(
                        f"{self.BASE_URL}{self.SEARCH_ENDPOINT}",
                        json=payload,
                        headers=headers,
                        proxy=self.proxy,
                    ) as response:
                        if response.status >= 400:
                            # Anonymous quota errors can contain generated credentials.
                            logger.warning(
                                "AnySearch request failed: status=%s, key=%s",
                                response.status,
                                mask_api_key(api_key),
                            )
                            continue
                        response_text = await response.text()
                        data = json.loads(response_text)
                except Exception as exc:
                    logger.warning(
                        "AnySearch request failed: exception=%s, key=%s",
                        type(exc).__name__,
                        mask_api_key(api_key),
                    )
                    continue

                if not isinstance(data, dict):
                    continue
                code = data.get("code")
                if type(code) is not int or code != 0:
                    logger.warning(
                        "AnySearch response failed: code=%s, key=%s",
                        code if type(code) is int else "invalid",
                        mask_api_key(api_key),
                    )
                    continue
                response_data = data.get("data")
                if not isinstance(response_data, dict):
                    continue
                items = response_data.get("results")
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
                    raw_snippet = item.get("snippet")
                    snippet = raw_snippet.strip() if isinstance(raw_snippet, str) else ""
                    raw_content = item.get("content")
                    content = raw_content.strip() if isinstance(raw_content, str) else ""
                    seen_urls.add(url)
                    results.append(
                        SearchResult(
                            title=title or url,
                            url=url,
                            snippet=self.tidy_text(snippet or content),
                            abstract=self.tidy_text(snippet or content),
                            rank=len(results),
                            content=content or snippet,
                        )
                    )
                    if len(results) == limit:
                        break
                if results:
                    return results

        return []
