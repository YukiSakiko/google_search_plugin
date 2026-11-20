"""反向以图搜图流水线（Google Lens → Yandex → Bing 自动降级）。

职责:
- 串联 Google Lens（Playwright 持久化目录 / Cookie 注入）与纯 HTTP 引擎（Yandex / Bing）
- 基于图片内容哈希做 TTL 缓存，避免群聊重复发同一表情包时反复请求搜索引擎
- 可选复用 ContentFetcher 对头部匹配页面补充网页正文
- 直接输出结构化 Markdown 结果给 MaiBot Planner，由 Planner 结合对话上下文总结，不额外消耗插件内部 LLM
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import aiohttp

from ..search_engines.base import SearchResult
from ..search_engines.reverse_image import (
    BingVisualEngine,
    GoogleLensReverseEngine,
    ReverseImageMatch,
    ReverseImageOutput,
    YandexVisualEngine,
    compute_image_hash,
    deduplicate_matches,
)

if TYPE_CHECKING:
    from ..config import ReverseImageSection, SearchBackendSection
    from .content_fetcher import ContentFetcher

logger = logging.getLogger(__name__)

_MAX_IMAGE_DOWNLOAD_BYTES = 15 * 1024 * 1024  # 15 MiB
_MAX_CACHE_ENTRIES = 128
_DEFAULT_PROFILE_DIR = Path(__file__).resolve().parent.parent / ".google_lens_profile"

_ENGINE_LABELS = {
    "google_lens": "Google Lens",
    "yandex": "Yandex Images",
    "bing": "Bing Visual Search",
    "yandex+bing": "Yandex Images + Bing Visual Search",
}


def _resolve_profile_dir(raw_profile_dir: str, cookies: str) -> str:
    """解析 Google Lens 持久化目录。

    - 若用户显式配置了 ``google_lens_profile_dir``，直接使用该路径；
    - 若用户仅配置了 ``google_lens_cookies``（典型 Docker/无头服务器用法）且未填目录，
      返回空字符串，使用纯 Cookie 注入的无盘上下文；
    - 若两者皆为空，默认使用插件目录下的 ``.google_lens_profile`` 方便桌面端首次登录后持久化复用。
    """
    cleaned = (raw_profile_dir or "").strip()
    if cleaned:
        return str(Path(cleaned).expanduser())
    if (cookies or "").strip():
        return ""
    return str(_DEFAULT_PROFILE_DIR)


def format_reverse_image_output(
    output: ReverseImageOutput,
    *,
    max_content_length: int = 800,
) -> str:
    """将反向搜图结果格式化为适合 MaiBot Planner 阅读的精简 Markdown。"""
    engine_label = _ENGINE_LABELS.get(output.engine, output.engine or "未知引擎")
    lines: list[str] = [f"【以图搜图结果】（来源：{engine_label}）"]

    if output.ai_overview.strip():
        overview = output.ai_overview.strip()
        if max_content_length > 0 and len(overview) > max_content_length * 2:
            overview = overview[: max_content_length * 2] + "..."
        lines.append("")
        lines.append("### 参考概览")
        lines.append(overview)

    if output.tags:
        lines.append("")
        lines.append(f"### 识别标签\n{', '.join(output.tags[:8])}")

    if output.matches:
        lines.append("")
        lines.append("### 关联网页")
        for idx, match in enumerate(output.matches, start=1):
            source_suffix = f"（{match.source}）" if match.source else ""
            lines.append(f"{idx}. [{match.title}]({match.url}){source_suffix}")
            detail = (match.content or match.snippet or "").strip()
            if detail:
                if max_content_length > 0 and len(detail) > max_content_length:
                    detail = detail[:max_content_length] + "..."
                lines.append(f"   {detail}")

    lines.append("")
    lines.append("提示：如需深入了解某个关联网页的完整详情，可调用 web_search 并直接传入对应 URL。")
    return "\n".join(lines).strip()


class ReverseImagePipeline:
    """反向以图搜图流水线。"""

    def __init__(
        self,
        *,
        reverse_cfg: "ReverseImageSection",
        backend_cfg: "SearchBackendSection",
        content_fetcher: Optional["ContentFetcher"] = None,
        google_language: str = "zh-cn",
    ) -> None:
        self._cfg = reverse_cfg
        self._backend = backend_cfg
        self._fetcher = content_fetcher

        hl = self._normalize_hl(google_language)
        proxy = (backend_cfg.proxy or "").strip() or None
        timeout = max(int(backend_cfg.timeout or 20), 10)
        profile_dir = _resolve_profile_dir(
            reverse_cfg.google_lens_profile_dir,
            reverse_cfg.google_lens_cookies,
        )

        self.google_lens = GoogleLensReverseEngine(
            enabled=bool(reverse_cfg.google_lens_enabled),
            proxy=proxy,
            timeout=max(timeout, 25),
            language=hl,
            cookies=reverse_cfg.google_lens_cookies,
            profile_dir=profile_dir,
            default_profile_dir=str(_DEFAULT_PROFILE_DIR),
            headless=bool(reverse_cfg.google_lens_headless),
            browser_channel=reverse_cfg.google_lens_browser_channel,
        )
        self.yandex = YandexVisualEngine(
            enabled=bool(reverse_cfg.yandex_enabled),
            proxy=proxy,
            timeout=timeout,
            language=hl,
        )
        self.bing = BingVisualEngine(
            enabled=bool(reverse_cfg.bing_enabled),
            proxy=proxy,
            timeout=timeout,
            language=hl,
        )

        # OrderedDict 实现简单 LRU + TTL 缓存: image_hash -> (ts, formatted_text)
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self.last_engine: str = ""

    @staticmethod
    def _normalize_hl(lang: str) -> str:
        cleaned = (lang or "zh-cn").strip()
        if "-" in cleaned:
            primary, region = cleaned.split("-", 1)
            return f"{primary.strip().lower()}-{region.strip().upper()}"
        return cleaned or "zh-CN"

    def _get_cached(self, cache_key: str) -> Optional[str]:
        ttl = int(self._cfg.cache_ttl_seconds or 0)
        if ttl <= 0 or not cache_key:
            return None
        entry = self._cache.get(cache_key)
        if entry is None:
            return None
        ts, text = entry
        if time.time() - ts > ttl:
            self._cache.pop(cache_key, None)
            return None
        self._cache.move_to_end(cache_key)
        return text

    def _set_cached(self, cache_key: str, text: str) -> None:
        ttl = int(self._cfg.cache_ttl_seconds or 0)
        if ttl <= 0 or not cache_key or not text:
            return
        self._cache[cache_key] = (time.time(), text)
        self._cache.move_to_end(cache_key)
        while len(self._cache) > _MAX_CACHE_ENTRIES:
            self._cache.popitem(last=False)

    async def download_image_url(self, image_url: str) -> Optional[bytes]:
        """下载公网图片 URL 的字节数据（受体积上限与超时保护）。"""
        url = (image_url or "").strip()
        if not url.startswith(("http://", "https://")):
            return None
        timeout = aiohttp.ClientTimeout(total=max(int(self._backend.content_timeout or 10), 10))
        proxy = (self._backend.proxy or "").strip() or None
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        }
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(url, headers=headers, timeout=timeout, proxy=proxy) as resp:
                    if resp.status != 200:
                        logger.warning("下载待识图片失败 %s (HTTP %d)", url, resp.status)
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > _MAX_IMAGE_DOWNLOAD_BYTES:
                            logger.warning("待识图片超过 15 MiB 上限，放弃下载: %s", url)
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks) if chunks else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载待识图片异常 %s: %s", url, exc)
            return None

    async def _enrich_with_content_fetcher(self, output: ReverseImageOutput) -> None:
        """可选：抓取前 2 个关联页面的正文片段补充到结果中。"""
        if not self._cfg.fetch_content or self._fetcher is None or not output.matches:
            return
        top_matches = output.matches[:2]
        search_results = [
            SearchResult(title=m.title, url=m.url, snippet=m.snippet, rank=idx)
            for idx, m in enumerate(top_matches)
        ]
        try:
            enriched = await self._fetcher.fetch_batch(search_results, last_success_engine=output.engine)
            for match, sr in zip(top_matches, enriched):
                if sr.content:
                    match.content = sr.content
                elif sr.abstract and not match.snippet:
                    match.snippet = sr.abstract
        except Exception as exc:  # noqa: BLE001
            logger.warning("以图搜图正文补充失败（忽略并继续返回链接列表）: %s", exc)

    async def search_raw(
        self,
        *,
        image_bytes: Optional[bytes] = None,
        image_url: str = "",
    ) -> ReverseImageOutput:
        """按 Google Lens → Yandex → Bing 顺序执行以图搜图并返回原始结构。"""
        max_results = max(int(self._cfg.max_results or 8), 1)
        errors: list[str] = []

        # 1. Google Lens（优先）
        if self.google_lens.enabled and image_bytes:
            lens_out = await self.google_lens.search(image_bytes=image_bytes, max_results=max_results)
            if lens_out.has_useful_data:
                self.last_engine = "google_lens"
                return lens_out
            if lens_out.error:
                errors.append(f"Google Lens: {lens_out.error}")
            logger.info("Google Lens 未返回有效结果（captcha=%s），准备降级至纯 HTTP 引擎", lens_out.captcha_triggered)

        # 2. Yandex（纯 HTTP，免 Cookie；同时可产出临时公网图 URL 供 Bing 复用）
        yandex_out: Optional[ReverseImageOutput] = None
        shared_image_url = (image_url or "").strip()
        if self.yandex.enabled and (image_bytes or shared_image_url):
            yandex_out = await self.yandex.search(
                image_bytes=image_bytes,
                image_url=shared_image_url,
                max_results=max_results,
            )
            if yandex_out.uploaded_image_url and not shared_image_url:
                shared_image_url = yandex_out.uploaded_image_url
            if yandex_out.has_useful_data and len(yandex_out.matches) >= 3:
                self.last_engine = "yandex"
                return yandex_out
            if yandex_out.error:
                errors.append(f"Yandex: {yandex_out.error}")

        # 3. Bing Visual Search（纯 HTTP 兜底或补充，当 Yandex 结果不足 3 条时触发）
        bing_out: Optional[ReverseImageOutput] = None
        if self.bing.enabled and shared_image_url:
            bing_out = await self.bing.search(image_url=shared_image_url, max_results=max_results)
            if bing_out.error:
                errors.append(f"Bing: {bing_out.error}")

        # 合并 Yandex + Bing（保留 Yandex 高精度匹配在前，Bing 结果补充在后）
        combined_matches: list[ReverseImageMatch] = []
        tags: list[str] = []
        engines_used: list[str] = []
        if yandex_out and yandex_out.has_useful_data:
            combined_matches.extend(yandex_out.matches)
            tags.extend(yandex_out.tags)
            engines_used.append("yandex")
        if bing_out and bing_out.has_useful_data:
            combined_matches.extend(bing_out.matches)
            engines_used.append("bing")

        if combined_matches:
            merged_engine = "+".join(engines_used)
            self.last_engine = merged_engine
            return ReverseImageOutput(
                engine=merged_engine,
                matches=deduplicate_matches(combined_matches, max_results),
                tags=tags,
                uploaded_image_url=shared_image_url,
            )

        self.last_engine = ""
        return ReverseImageOutput(
            engine="",
            error="；".join(errors) if errors else "所有已启用的以图搜图引擎均未返回有效结果",
        )

    async def run(
        self,
        *,
        image_bytes: Optional[bytes] = None,
        image_url: str = "",
        image_hash: str = "",
    ) -> str:
        """执行完整以图搜图并返回可直接交给 Planner 的文本结果。"""
        cleaned_url = (image_url or "").strip()
        if not image_bytes and cleaned_url:
            image_bytes = await self.download_image_url(cleaned_url)

        if not image_bytes and not cleaned_url:
            return "未获取到有效的图片数据，无法执行以图搜图。"

        cache_key = (image_hash or "").strip()
        if not cache_key and image_bytes:
            cache_key = compute_image_hash(image_bytes)
        elif not cache_key and cleaned_url:
            cache_key = f"url:{cleaned_url}"

        cached = self._get_cached(cache_key)
        if cached is not None:
            logger.info("命中以图搜图缓存 (key=%s)", cache_key[:16])
            return cached

        output = await self.search_raw(image_bytes=image_bytes, image_url=cleaned_url)
        if not output.has_useful_data:
            detail = f"（{output.error}）" if output.error else ""
            return f"未能通过以图搜图找到该图片的关联网页信息{detail}。"

        await self._enrich_with_content_fetcher(output)
        formatted = format_reverse_image_output(
            output,
            max_content_length=min(int(self._backend.max_content_length or 800), 1200),
        )
        self._set_cached(cache_key, formatted)
        return formatted

    async def aclose(self) -> None:
        """清理底层浏览器资源。"""
        await self.google_lens.aclose()
