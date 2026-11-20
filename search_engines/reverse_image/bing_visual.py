"""Bing Visual Search 纯 HTTP 反向以图搜图引擎。

通过 ``https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:...``
获取图片关联页面（免 Cookie、免浏览器）。支持直接传入公网图片 URL，或由上层
流水线复用 Yandex 上传阶段返回的临时图片 URL。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .base import (
    ReverseImageMatch,
    ReverseImageOutput,
    deduplicate_matches,
    extract_domain,
    is_bing_internal_url,
    normalize_match_url,
)

logger = logging.getLogger(__name__)

_BING_SBI_URL = "https://www.bing.com/images/search"


class BingVisualEngine:
    """Bing Visual Search 纯 HTTP 以图搜图引擎。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        proxy: Optional[str] = None,
        timeout: int = 20,
        language: str = "zh-CN",
    ) -> None:
        self.enabled = enabled
        self.proxy = (proxy or "").strip() or None
        self.timeout = max(int(timeout or 20), 5)
        self.language = (language or "zh-CN").strip()

    def _proxies(self) -> Optional[dict[str, str]]:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    @staticmethod
    def parse_sbi_html(html_text: str, max_results: int = 8) -> list[ReverseImageMatch]:
        """从 Bing SBI 页面元素 ``[m]`` JSON 属性中提取关联页面。"""
        soup = BeautifulSoup(html_text, "lxml")
        matches: list[ReverseImageMatch] = []

        for el in soup.select("[m]"):
            raw_m = el.get("m")
            if not isinstance(raw_m, str) or "purl" not in raw_m:
                continue
            try:
                data = json.loads(raw_m)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            purl = normalize_match_url(str(data.get("purl") or data.get("hostPageUrl") or ""))
            if not purl or is_bing_internal_url(purl):
                continue
            title = str(data.get("t") or data.get("name") or "").strip()
            domain = str(data.get("dm") or "").strip() or extract_domain(purl)
            snippet = str(data.get("desc") or "").strip()
            matches.append(
                ReverseImageMatch(
                    title=title,
                    url=purl,
                    source=domain,
                    snippet=snippet,
                )
            )

        return deduplicate_matches(matches, max_results)

    def _search_sync(self, *, image_url: str, max_results: int = 8) -> ReverseImageOutput:
        cleaned_url = (image_url or "").strip()
        if not cleaned_url.startswith(("http://", "https://")):
            return ReverseImageOutput(engine="bing", error="Bing 视觉搜索需要可访问的图片 URL")

        resp = curl_requests.get(
            _BING_SBI_URL,
            params={
                "view": "detailv2",
                "iss": "sbi",
                "form": "SBIVSP",
                "q": f"imgurl:{cleaned_url}",
            },
            impersonate="chrome",
            proxies=self._proxies(),
            headers={
                "Accept-Language": f"{self.language},zh;q=0.9,en;q=0.8",
                "Referer": "https://www.bing.com/visualsearch",
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            return ReverseImageOutput(engine="bing", error=f"Bing 视觉搜索失败 (HTTP {resp.status_code})")

        matches = self.parse_sbi_html(resp.text, max_results=max_results)
        return ReverseImageOutput(engine="bing", matches=matches)

    async def search(self, *, image_url: str, max_results: int = 8) -> ReverseImageOutput:
        """异步执行 Bing 反向以图搜图。"""
        if not self.enabled:
            return ReverseImageOutput(engine="bing", error="Bing 识图未启用")
        try:
            return await asyncio.to_thread(
                self._search_sync,
                image_url=image_url,
                max_results=max_results,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bing 反向以图搜图异常: %s", exc)
            return ReverseImageOutput(engine="bing", error=str(exc))
