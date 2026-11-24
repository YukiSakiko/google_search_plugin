import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp

from .base import ApiKeyMixin, BaseSearchEngine, SearchResult, mask_api_key

logger = logging.getLogger(__name__)

_MAX_QUERY_CHARS = 750
_MAX_QUERY_WORDS = 100
_MAX_K = 100
_MAX_DAYS = 30
_TOKEN_ENV_NAME = "WEBZ_API_TOKEN"
_SENTIMENTS = frozenset({"positive", "negative", "neutral"})


class WebzEngine(BaseSearchEngine, ApiKeyMixin):
    """Search recent news through the Webz.io News Search API.

    The request is ``POST https://api.webz.io/api/news/context``. There is no
    ``days`` field; a day window is sent as ``filters.published_from``.
    """

    BASE_URL = "https://api.webz.io"
    SEARCH_ENDPOINT = "/api/news/context"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self._init_api_keys(self.config)
        self.days = self._clamp_days(self.config.get("days", _MAX_DAYS))
        self.language = self._clean_token(self.config.get("language"), lower=True)
        self.country = self._clean_country(self.config.get("country"))
        self.sentiment = self._clean_sentiment(self.config.get("sentiment"))
        self.domains = self._string_list("domains")
        self.tickers = list(dict.fromkeys(item.upper() for item in self._string_list("tickers")))

    async def search(self, query: str, num_results: int) -> List[SearchResult]:
        """Execute a news search. Returns an empty list when the call cannot be made."""
        tokens = self._candidate_tokens()
        if not tokens:
            logger.warning("Webz API token is not configured; skip Webz search.")
            return []

        endpoint = self._https_endpoint()
        if endpoint is None:
            logger.error("Webz search endpoint must be HTTPS; skip Webz search.")
            return []

        normalized = self._normalize_query(query)
        if normalized is None:
            return []

        payload = self._build_payload(normalized, num_results)
        timeout = aiohttp.ClientTimeout(total=self.TIMEOUT)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for api_key in tokens:
                request_headers = dict(headers)
                request_headers["Authorization"] = f"Bearer {api_key}"
                try:
                    async with session.post(
                        endpoint,
                        json=payload,
                        headers=request_headers,
                        proxy=self.proxy,
                        allow_redirects=False,
                    ) as response:
                        response_text = await response.text()
                        if response.status >= 400:
                            logger.error(
                                "Webz search request failed with status %s for key %s.",
                                response.status,
                                mask_api_key(api_key),
                            )
                            continue
                        if not response_text:
                            logger.error("Webz returned an empty response for key %s.", mask_api_key(api_key))
                            continue
                        try:
                            data = json.loads(response_text)
                        except json.JSONDecodeError:
                            logger.error(
                                "Failed to parse Webz response as JSON for key %s.",
                                mask_api_key(api_key),
                            )
                            continue
                except Exception as exc:
                    logger.error(
                        "Webz search raised an exception for key %s: %s",
                        mask_api_key(api_key),
                        type(exc).__name__,
                    )
                    continue

                return self._parse_results(data, payload["k"])

        return []

    def has_api_keys(self) -> bool:
        """Return True when a configured key or WEBZ_API_TOKEN is available."""
        if self.api_keys:
            return True
        return bool(self._env_token())

    def _candidate_tokens(self) -> List[str]:
        configured = self._iter_api_keys()
        if configured:
            return configured
        env_token = self._env_token()
        return [env_token] if env_token else []

    @staticmethod
    def _env_token() -> str:
        value = os.environ.get(_TOKEN_ENV_NAME, "")
        if not isinstance(value, str):
            return ""
        return value.strip()

    def _https_endpoint(self) -> Optional[str]:
        base = str(self.BASE_URL or "").strip().rstrip("/")
        endpoint = f"{base}{self.SEARCH_ENDPOINT}"
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        if parsed.username or parsed.password or parsed.query:
            return None
        return endpoint

    def _build_payload(self, query: str, num_results: int) -> Dict[str, Any]:
        configured_limit = max(1, min(int(self.max_results), _MAX_K))
        requested = num_results if isinstance(num_results, int) and num_results > 0 else configured_limit
        limit = max(1, min(requested, configured_limit, _MAX_K))
        filters: Dict[str, Any] = {"published_from": self._published_from()}
        if self.language:
            filters["language"] = [self.language]
        if self.country:
            filters["country"] = [self.country]
        if self.sentiment:
            filters["sentiment"] = [self.sentiment]
        if self.domains:
            filters["domain"] = list(self.domains)
        if self.tickers:
            filters["ticker"] = list(self.tickers)
        return {"query": query, "k": limit, "filters": filters}

    def _published_from(self) -> str:
        current = datetime.now(timezone.utc)
        return (current - timedelta(days=self.days)).date().isoformat()

    def _parse_results(self, data: Any, limit: int) -> List[SearchResult]:
        rows = data.get("results") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []

        results: List[SearchResult] = []
        seen_urls = set()
        for item in rows:
            if not isinstance(item, dict):
                continue
            article = item.get("article") if isinstance(item.get("article"), dict) else {}
            raw_url = article.get("url")
            url = raw_url.strip() if isinstance(raw_url, str) else ""
            if not self._is_valid_url(url) or url in seen_urls:
                continue
            raw_title = article.get("title")
            title = self.tidy_text(raw_title) if isinstance(raw_title, str) else ""
            chunk = item.get("chunk") if isinstance(item.get("chunk"), dict) else {}
            raw_text = chunk.get("text") if isinstance(chunk.get("text"), str) else ""
            if not raw_text.strip() and isinstance(article.get("summary"), str):
                raw_text = article["summary"]
            content = raw_text.strip()
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
        return results

    @staticmethod
    def _normalize_query(query: str) -> Optional[str]:
        text = query.strip() if isinstance(query, str) else ""
        if not text:
            logger.warning("Webz search query is empty; skip Webz search.")
            return None
        if len(text) > _MAX_QUERY_CHARS or len(text.split()) > _MAX_QUERY_WORDS:
            logger.warning("Webz search query is too long; skip Webz search.")
            return None
        return text

    def _string_list(self, field: str) -> List[str]:
        value = self.config.get(field) or []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        cleaned: List[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            token = item.strip()
            if token and token not in cleaned:
                cleaned.append(token)
        return cleaned

    @staticmethod
    def _clean_token(value: Any, *, lower: bool) -> str:
        if not isinstance(value, str):
            return ""
        token = value.strip()
        return token.lower() if lower else token

    @classmethod
    def _clean_country(cls, value: Any) -> str:
        token = cls._clean_token(value, lower=False).upper()
        if token and (len(token) != 2 or not token.isascii() or not token.isalpha()):
            return ""
        return token

    @classmethod
    def _clean_sentiment(cls, value: Any) -> str:
        token = cls._clean_token(value, lower=True)
        if token in {"", "auto"}:
            return ""
        return token if token in _SENTIMENTS else ""

    @staticmethod
    def _clamp_days(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return _MAX_DAYS
        return max(0, min(value, _MAX_DAYS))
