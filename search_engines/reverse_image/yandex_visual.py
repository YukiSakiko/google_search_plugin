"""Yandex Images 纯 HTTP 反向以图搜图引擎。

两阶段纯 HTTP 流程（免 Cookie、免浏览器）:
1. POST https://yandex.com/images/search?rpt=imageview&format=json 上传图片，
   获得 ``cbirId`` 与临时公网图片 URL（``originalImageUrl``，可供下游 Bing 复用）。
2. GET https://yandex.com/images/search?rpt=imageview&cbir_id=...&cbir_page=sites
   解析页面 ``[data-state]`` 内的结构化 JSON（``cbirSites`` / ``cbirTags``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from curl_cffi import CurlMime, requests as curl_requests

from .base import (
    ReverseImageMatch,
    ReverseImageOutput,
    deduplicate_matches,
    detect_image_mime_and_ext,
    extract_domain,
    is_yandex_internal_url,
    normalize_match_url,
)

logger = logging.getLogger(__name__)

_YANDEX_SEARCH_URL = "https://yandex.com/images/search"
_UPLOAD_REQUEST_SPEC = json.dumps({"blocks": [{"block": "b-page_type_search-by-image__link"}]})

# 过滤 Yandex 常见的低质成人/SEO 采集垃圾站（保留正常图站与社区）
_SPAM_KEYWORDS = (
    "fappening",
    "hentai.services",
    "pornhub",
    "xvideos",
    "xnxx",
    "chaturbate",
    "onlyfans leak",
    "刺激影片",
)
# 典型垃圾采集站特征：连续 3 组以上“单汉字+空格+单汉字”
_SPACED_CJK_SPAM_RE = re.compile(r"(?:[\u4e00-\u9fff]\s+){3,}[\u4e00-\u9fff]")


def _is_low_quality_spam(title: str, domain: str, url: str) -> bool:
    combined = f"{title} {domain} {url}".lower()
    if any(kw in combined for kw in _SPAM_KEYWORDS):
        return True
    if _SPACED_CJK_SPAM_RE.search(title):
        return True
    return False


class YandexVisualEngine:
    """Yandex CBIR 纯 HTTP 以图搜图引擎。"""

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
    def _is_captcha_response(final_url: str, html_text: str) -> bool:
        low_url = (final_url or "").lower()
        if "showcaptcha" in low_url or "/captcha" in low_url:
            return True
        low_html = (html_text or "")[:12000].lower()
        return "smartcaptcha" in low_html or "are you not a robot" in low_html

    @staticmethod
    def _parse_upload_payload(payload: Any) -> tuple[str, str]:
        """从 Yandex JSON 上传响应中提取 (cbir_id, original_image_url)。"""
        if not isinstance(payload, dict):
            return "", ""
        for block in payload.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            params = block.get("params")
            if not isinstance(params, dict):
                continue
            cbir_id = str(params.get("cbirId") or "").strip()
            orig_url = str(params.get("originalImageUrl") or "").strip()
            if cbir_id or orig_url:
                return cbir_id, orig_url
        return "", ""

    @staticmethod
    def parse_sites_html(html_text: str, max_results: int = 8) -> tuple[list[ReverseImageMatch], list[str]]:
        """解析 Yandex cbir_page=sites 页面中的结构化 data-state JSON。"""
        soup = BeautifulSoup(html_text, "lxml")
        matches: list[ReverseImageMatch] = []
        tags: list[str] = []
        seen_tags: set[str] = set()

        for el in soup.select("[data-state]"):
            raw_state = el.get("data-state")
            if not isinstance(raw_state, str) or not raw_state.strip():
                continue
            if "cbirSites" not in raw_state and "cbirTags" not in raw_state and "cbirSimilar" not in raw_state:
                continue
            try:
                state = json.loads(raw_state)
            except ValueError:
                continue
            if not isinstance(state, dict):
                continue
            initial_state = state.get("initialState")
            if not isinstance(initial_state, dict):
                continue

            # 1. 优先解析 cbirSites（精确页面匹配）
            cbir_sites = initial_state.get("cbirSites")
            if isinstance(cbir_sites, dict):
                for site in cbir_sites.get("sites") or []:
                    if not isinstance(site, dict):
                        continue
                    url = normalize_match_url(str(site.get("url") or ""))
                    if not url or is_yandex_internal_url(url):
                        continue
                    title = str(site.get("title") or "").strip()
                    domain = str(site.get("domain") or "").strip() or extract_domain(url)
                    if _is_low_quality_spam(title, domain, url):
                        continue
                    desc = str(site.get("description") or "").strip()
                    matches.append(
                        ReverseImageMatch(
                            title=title,
                            url=url,
                            source=domain,
                            snippet=desc,
                        )
                    )

            # 2. 提取视觉识别标签
            cbir_tags = initial_state.get("cbirTags")
            if isinstance(cbir_tags, dict):
                for tag_item in cbir_tags.get("tags") or []:
                    if not isinstance(tag_item, dict):
                        continue
                    text = str(tag_item.get("text") or "").strip()
                    if text and text not in seen_tags:
                        seen_tags.add(text)
                        tags.append(text)
                        if len(tags) >= 8:
                            break

            # 3. cbirSites 为空时从 cbirSimilar 兜底提取来源 URL
            if not matches:
                cbir_similar = initial_state.get("cbirSimilar")
                if isinstance(cbir_similar, dict):
                    for thumb in cbir_similar.get("thumbs") or []:
                        if not isinstance(thumb, dict):
                            continue
                        link_url = str(thumb.get("linkUrl") or "")
                        qs = parse_qs(urlparse(link_url).query)
                        img_url = normalize_match_url((qs.get("img_url") or [""])[0])
                        if not img_url or is_yandex_internal_url(img_url):
                            continue
                        title = str(thumb.get("title") or "").strip()
                        matches.append(
                            ReverseImageMatch(
                                title=title,
                                url=img_url,
                                source=extract_domain(img_url),
                            )
                        )

        return deduplicate_matches(matches, max_results), tags

    def _search_sync(
        self,
        *,
        image_bytes: Optional[bytes] = None,
        image_url: str = "",
        max_results: int = 8,
    ) -> ReverseImageOutput:
        proxies = self._proxies()
        headers = {
            "Accept-Language": f"{self.language},zh;q=0.9,en;q=0.8",
            "Referer": "https://yandex.com/images/",
        }

        cbir_id = ""
        orig_url = (image_url or "").strip()

        if image_bytes:
            mime_type, ext = detect_image_mime_and_ext(image_bytes)
            mp = CurlMime()
            mp.addpart(
                name="upfile",
                content_type=mime_type,
                filename=f"image.{ext}",
                data=image_bytes,
            )
            upload_resp = curl_requests.post(
                _YANDEX_SEARCH_URL,
                params={
                    "rpt": "imageview",
                    "format": "json",
                    "request": _UPLOAD_REQUEST_SPEC,
                },
                multipart=mp,
                impersonate="chrome",
                proxies=proxies,
                headers=headers,
                timeout=self.timeout,
            )
            if self._is_captcha_response(str(upload_resp.url), upload_resp.text):
                logger.warning("Yandex 上传阶段触发验证码")
                return ReverseImageOutput(engine="yandex", captcha_triggered=True, error="Yandex 触发人机验证")
            if upload_resp.status_code != 200:
                return ReverseImageOutput(engine="yandex", error=f"Yandex 上传失败 (HTTP {upload_resp.status_code})")
            try:
                cbir_id, uploaded_url = self._parse_upload_payload(upload_resp.json())
            except ValueError as exc:
                return ReverseImageOutput(engine="yandex", error=f"Yandex 上传响应 JSON 解析失败: {exc}")
            if uploaded_url:
                orig_url = uploaded_url

        if not cbir_id and not orig_url:
            return ReverseImageOutput(engine="yandex", error="Yandex 未获得有效 cbirId 或图片 URL")

        query_params: dict[str, str] = {
            "rpt": "imageview",
            "cbir_page": "sites",
        }
        if orig_url:
            query_params["url"] = orig_url
        if cbir_id:
            query_params["cbir_id"] = cbir_id

        sites_resp = curl_requests.get(
            _YANDEX_SEARCH_URL,
            params=query_params,
            impersonate="chrome",
            proxies=proxies,
            headers=headers,
            timeout=self.timeout,
        )
        if self._is_captcha_response(str(sites_resp.url), sites_resp.text):
            logger.warning("Yandex 站点结果页触发验证码")
            return ReverseImageOutput(
                engine="yandex",
                uploaded_image_url=orig_url,
                captcha_triggered=True,
                error="Yandex 触发人机验证",
            )
        if sites_resp.status_code != 200:
            return ReverseImageOutput(
                engine="yandex",
                uploaded_image_url=orig_url,
                error=f"Yandex 结果页请求失败 (HTTP {sites_resp.status_code})",
            )

        matches, tags = self.parse_sites_html(sites_resp.text, max_results=max_results)
        return ReverseImageOutput(
            engine="yandex",
            matches=matches,
            tags=tags,
            uploaded_image_url=orig_url,
        )

    async def search(
        self,
        *,
        image_bytes: Optional[bytes] = None,
        image_url: str = "",
        max_results: int = 8,
    ) -> ReverseImageOutput:
        """异步执行 Yandex 反向以图搜图。"""
        if not self.enabled:
            return ReverseImageOutput(engine="yandex", error="Yandex 识图未启用")
        try:
            return await asyncio.to_thread(
                self._search_sync,
                image_bytes=image_bytes,
                image_url=image_url,
                max_results=max_results,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Yandex 反向以图搜图异常: %s", exc)
            return ReverseImageOutput(engine="yandex", error=str(exc))
