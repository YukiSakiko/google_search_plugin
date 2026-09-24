from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp

from .base import ApiKeyMixin, BaseSearchEngine, SearchResult, mask_api_key

logger = logging.getLogger(__name__)


class DeepSeekEngine(BaseSearchEngine, ApiKeyMixin):
    """DeepSeek 官方服务端联网搜索客户端。

    通过 DeepSeek 的 Anthropic 兼容接口 (/anthropic/v1/messages) 调用
    服务端的 web_search_20250305 检索工具，直接获取实时网页索引与生成的总结。
    """

    base_url: str
    model: str
    max_tokens: int

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self._init_api_keys(self.config, "DEEPSEEK_API_KEY")
        self.base_url = self._resolve_endpoint_url(self.config.get("base_url") or "https://api.deepseek.com")
        self.model = self.config.get("model") or "deepseek-chat"
        self.max_tokens = int(self.config.get("max_tokens") or 2048)

    @staticmethod
    def _resolve_endpoint_url(base: str) -> str:
        """规范化 DeepSeek Anthropic 端点地址。"""
        url = str(base or "").strip().rstrip("/")
        if url.endswith("/messages"):
            return url
        if url.endswith("/anthropic/v1"):
            return f"{url}/messages"
        if url.endswith("/anthropic"):
            return f"{url}/v1/messages"
        return f"{url}/anthropic/v1/messages"

    async def search(self, query: str, num_results: int) -> List[SearchResult]:
        """通过 DeepSeek Anthropic 协议执行联网搜索。"""
        api_keys = self._iter_api_keys()
        if not api_keys:
            logger.warning("DeepSeek API key 未配置,跳过 DeepSeek 搜索。")
            return []

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": f"当前现实时间为：{now_str}。当搜索需要时效性时，请据此判断最新的信息。",
            "messages": [{"role": "user", "content": query}],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        }

        timeout = aiohttp.ClientTimeout(total=self.TIMEOUT)
        headers = {
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for api_key in api_keys:
                headers_with_key = dict(headers)
                headers_with_key["x-api-key"] = api_key

                try:
                    async with session.post(
                        self.base_url,
                        json=payload,
                        headers=headers_with_key,
                        proxy=self.proxy,
                    ) as response:
                        response_text = await response.text()
                        if response.status >= 400:
                            logger.error(
                                "DeepSeek 搜索请求失败, status %s, key %s: %s",
                                response.status,
                                mask_api_key(api_key),
                                response_text,
                            )
                            continue

                        if not response_text:
                            logger.error("DeepSeek 返回空响应, key %s", mask_api_key(api_key))
                            continue

                        try:
                            data = json.loads(response_text)
                        except json.JSONDecodeError:
                            logger.error(
                                "解析 DeepSeek 响应失败, key %s: %s",
                                mask_api_key(api_key),
                                response_text,
                            )
                            continue

                except Exception as exc:
                    logger.error(
                        "DeepSeek 搜索异常, key %s: %s",
                        mask_api_key(api_key),
                        exc,
                        exc_info=True,
                    )
                    continue

                if not isinstance(data, dict):
                    continue

                texts: List[str] = []
                results: List[SearchResult] = []
                seen_urls = set()

                for block in data.get("content", []):
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_val = block.get("text", "")
                        if text_val:
                            texts.append(text_val)
                    elif btype == "web_search_tool_result":
                        raw_items = block.get("content", [])
                        if isinstance(raw_items, list):
                            for idx, item in enumerate(raw_items):
                                if not isinstance(item, dict):
                                    continue
                                # 兼容顶层条目或嵌套在 web_search_result 下的结构
                                entry = (
                                    item.get("web_search_result")
                                    if isinstance(item.get("web_search_result"), dict)
                                    else item
                                )
                                title = self.tidy_text(entry.get("title", ""))
                                url = entry.get("url", "")
                                if not title or not self._is_valid_url(url):
                                    continue
                                if url in seen_urls:
                                    continue
                                seen_urls.add(url)

                                snippet = self.tidy_text(
                                    entry.get("snippet") or entry.get("content") or title
                                )
                                results.append(
                                    SearchResult(
                                        title=title,
                                        url=url,
                                        snippet=snippet,
                                        abstract=snippet,
                                        rank=idx,
                                        content=snippet,
                                    )
                                )

                answer = "\n".join(texts).strip()

                # 请求级作用域保存答案（rank=-1，url="" 绝不包含 API 端点，防止泄漏和并发污染）
                final_results = results[: min(len(results), num_results)]
                if answer:
                    final_results.insert(
                        0,
                        SearchResult(
                            title="DeepSeek Summary",
                            url="",
                            snippet=answer,
                            abstract=answer,
                            rank=-1,
                            content=answer,
                        ),
                    )

                return final_results

        return []
