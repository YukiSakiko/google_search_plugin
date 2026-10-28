import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from .base import ApiKeyMixin, BaseSearchEngine, SearchResult, mask_api_key

logger = logging.getLogger(__name__)


class ExaEngine(BaseSearchEngine, ApiKeyMixin):
    """Return Exa search excerpts without local crawling or synthesis."""

    BASE_URL = "https://api.exa.ai"
    SEARCH_ENDPOINT = "/search"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self._init_api_keys(self.config)
        self.search_type = self.config.get("search_type") or "auto"
        self.include_highlights = self.config.get("include_highlights", True)
        self.include_text = self.config.get("include_text", False)
        self.highlights_max_characters = int(self.config.get("highlights_max_characters", 0))
        self.text_max_characters = int(self.config.get("text_max_characters", 0))
        self.max_age_hours = int(self.config.get("max_age_hours", -2))
        self.livecrawl_timeout_ms = int(self.config.get("livecrawl_timeout_ms", 0))
        self.additional_queries = self._string_list("additional_queries")
        if self.search_type not in ("auto", "lite", "deep-lite", "deep"):
            raise ValueError("Exa search_type must be auto, lite, deep-lite, or deep.")
        if not 0 <= self.highlights_max_characters <= 1_000_000:
            raise ValueError("Exa highlights_max_characters must be between 0 and 1000000.")
        if not 0 <= self.text_max_characters <= 1_000_000:
            raise ValueError("Exa text_max_characters must be between 0 and 1000000.")
        if not -2 <= self.max_age_hours <= 720:
            raise ValueError("Exa max_age_hours must be -2 (unset), -1 (cached), or 0 through 720.")
        if not 0 <= self.livecrawl_timeout_ms <= 90_000:
            raise ValueError("Exa livecrawl_timeout_ms must be between 0 and 90000.")
        if len(self.additional_queries) > 10:
            raise ValueError("Exa additional_queries accepts at most 10 queries.")
        if self.additional_queries and self.search_type not in ("deep-lite", "deep"):
            raise ValueError("Exa additional_queries requires deep-lite or deep search_type.")
        if self.search_type == "lite" and (self.max_age_hours != -2 or self.livecrawl_timeout_ms != 0):
            raise ValueError("Exa lite does not support max_age_hours or livecrawl_timeout_ms.")

    def _string_list(self, field: str) -> List[str]:
        value = self.config.get(field) or []
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"Exa {field} must be a list of strings.")
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    def _build_payload(self, query: str, limit: int) -> Dict[str, Any]:
        contents: Dict[str, Any] = {"highlights": self.include_highlights}
        if self.include_highlights:
            highlight_options: Dict[str, Any] = {}
            highlights_query = str(self.config.get("highlights_query") or "").strip()
            if highlights_query:
                highlight_options["query"] = highlights_query
            if self.highlights_max_characters:
                highlight_options["maxCharacters"] = self.highlights_max_characters
            if highlight_options:
                contents["highlights"] = highlight_options
        if self.include_text:
            contents["text"] = {"maxCharacters": self.text_max_characters} if self.text_max_characters else True
        if self.max_age_hours != -2:
            contents["maxAgeHours"] = self.max_age_hours
        if self.livecrawl_timeout_ms:
            contents["livecrawlTimeout"] = self.livecrawl_timeout_ms

        payload: Dict[str, Any] = {"query": query, "numResults": limit, "type": self.search_type, "contents": contents}
        for config_field, api_field in (
            ("category", "category"),
            ("start_published_date", "startPublishedDate"),
            ("end_published_date", "endPublishedDate"),
            ("user_location", "userLocation"),
            ("objective", "objective"),
        ):
            value = self.config.get(config_field)
            if isinstance(value, str) and value.strip():
                payload[api_field] = value.strip()
        for config_field, api_field in (("include_domains", "includeDomains"), ("exclude_domains", "excludeDomains")):
            domains = self._string_list(config_field)
            if len(domains) > 1200:
                raise ValueError(f"Exa {config_field} accepts at most 1200 entries.")
            if domains:
                payload[api_field] = domains
        if self.config.get("moderation", False):
            payload["moderation"] = True
        if self.additional_queries:
            payload["additionalQueries"] = self.additional_queries
        return payload

    async def search(self, query: str, num_results: int) -> List[SearchResult]:
        api_keys = self._iter_api_keys()
        if not api_keys:
            logger.warning("Exa API key is not configured; skip Exa search.")
            return []

        configured_limit = max(1, min(int(self.max_results), 100))
        requested_limit = num_results if num_results > 0 else configured_limit
        limit = max(1, min(requested_limit, configured_limit))
        payload = self._build_payload(query, limit)
        timeout = aiohttp.ClientTimeout(total=self.TIMEOUT)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for api_key in api_keys:
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-api-key": api_key,
                }
                try:
                    async with session.post(
                        f"{self.BASE_URL}{self.SEARCH_ENDPOINT}",
                        json=payload,
                        headers=headers,
                        proxy=self.proxy,
                    ) as response:
                        if response.status >= 400:
                            logger.warning(
                                "Exa request failed: status=%s, key=%s",
                                response.status,
                                mask_api_key(api_key),
                            )
                            continue
                        response_text = await response.text()
                        data = json.loads(response_text)
                except Exception as exc:
                    logger.warning(
                        "Exa request failed: exception=%s, key=%s",
                        type(exc).__name__,
                        mask_api_key(api_key),
                    )
                    continue

                if not isinstance(data, dict) or data.get("error"):
                    continue
                items = data.get("results")
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
                    highlights = item.get("highlights")
                    excerpts = (
                        [text.strip() for text in highlights if isinstance(text, str) and text.strip()]
                        if isinstance(highlights, list)
                        else []
                    )
                    raw_text = item.get("text")
                    text = raw_text.strip() if isinstance(raw_text, str) else ""
                    content = text if self.include_text and text else "\n".join(excerpts) or text
                    seen_urls.add(url)
                    results.append(
                        SearchResult(
                            title=title or url,
                            url=url,
                            snippet=self.tidy_text(content),
                            abstract=self.tidy_text(content),
                            rank=len(results),
                            content=content,
                        )
                    )
                    if len(results) == limit:
                        break
                if results:
                    return results

        return []
