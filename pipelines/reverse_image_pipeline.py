"""反向以图搜图流水线（Google Lens → Yandex → Bing 自动降级）。

职责:
- 串联 Google Lens（Playwright 持久化目录 / Cookie 注入）与纯 HTTP 引擎（Yandex / Bing）
- 基于图片内容哈希做 TTL 缓存，避免群聊重复发同一表情包时反复请求搜索引擎
- 可选复用 ContentFetcher 对头部匹配页面补充网页正文
- 直接输出结构化 Markdown 结果给 MaiBot Planner，由 Planner 结合对话上下文总结，不额外消耗插件内部 LLM
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, NamedTuple, Optional

from .public_fetch import MAX_IMAGE_BYTES, fetch_public_resource
from ..search_engines.reverse_image.base import verify_image_data
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

_MAX_CACHE_ENTRIES = 128
_URL_CACHE_MAX_SECONDS = 60
_LENS_LINKS_ONLY_CACHE_SECONDS = 15
_DEFAULT_PROFILE_DIR = Path(__file__).resolve().parent.parent / ".google_lens_profile"


class _CacheEntry(NamedTuple):
    text: str
    expires_at: float


def _lens_links_only(output: ReverseImageOutput) -> bool:
    return output.engine == "google_lens" and bool(output.matches) and not output.ai_overview.strip()


def _stage_limits(search_timeout: int, overview_wait_seconds: int = 6) -> tuple[int, int, int]:
    timeout = max(search_timeout, 10)
    # Lens: upload + navigation, up to 10s for first results, then the overview grace period.
    return 2 * max(timeout, 25) + 10 + overview_wait_seconds + 15, 2 * timeout + 3, timeout + 3


def reverse_rpc_budget(cfg: "ReverseImageSection", backend: "SearchBackendSection") -> int:
    lens, yandex, bing = _stage_limits(backend.timeout, cfg.google_lens_overview_wait_seconds)
    download = max(backend.content_timeout, 10)
    stages = (
        (lens if cfg.google_lens_enabled else 0)
        + (yandex if cfg.yandex_enabled else 0)
        + (bing if cfg.bing_enabled else 0)
    )
    return max(120, 15 + download + stages + (download if cfg.fetch_content else 0))


async def _run_stage(engine: str, seconds: int, call: Awaitable[ReverseImageOutput]) -> ReverseImageOutput:
    started = time.monotonic()
    try:
        async with asyncio.timeout(seconds):
            return await call
    except TimeoutError:
        return ReverseImageOutput(engine=engine, error=f"{engine} 超过 {seconds} 秒仍未完成")
    finally:
        logger.info("以图搜图阶段 engine=%s elapsed_ms=%.0f", engine, (time.monotonic() - started) * 1000)

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
        lines.append("### Google AI 概览" if output.engine == "google_lens" else "### 参考概览")
        lines.append(overview)
    elif _lens_links_only(output):
        lines.extend(("", "本次未读取到 Google AI 概览，以下是关联网页。"))

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
            overview_wait_seconds=reverse_cfg.google_lens_overview_wait_seconds,
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

        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self.last_engine: str = ""

    @staticmethod
    def _normalize_hl(lang: str) -> str:
        cleaned = (lang or "zh-cn").strip()
        if "-" in cleaned:
            primary, region = cleaned.split("-", 1)
            return f"{primary.strip().lower()}-{region.strip().upper()}"
        return cleaned or "zh-CN"

    def _get_cached(self, cache_key: str) -> _CacheEntry | None:
        entry = self._cache.get(cache_key)
        if entry is None:
            return None
        if time.monotonic() >= entry.expires_at or self._cfg.cache_ttl_seconds <= 0:
            self._cache.pop(cache_key, None)
            return None
        self._cache.move_to_end(cache_key)
        return entry

    def _set_cached(self, cache_key: str, entry: _CacheEntry, *, max_age: int | None = None) -> None:
        if not cache_key:
            return
        if max_age is not None:
            # A URL alias must not keep the original image result alive past its expiry.
            entry = _CacheEntry(entry.text, min(entry.expires_at, time.monotonic() + max_age))
        self._cache[cache_key] = entry
        self._cache.move_to_end(cache_key)
        while len(self._cache) > _MAX_CACHE_ENTRIES:
            self._cache.popitem(last=False)

    async def download_image_url(self, image_url: str) -> bytes:
        response = await fetch_public_resource(
            image_url, proxy=self._backend.proxy, timeout=max(int(self._backend.content_timeout or 10), 10),
            max_bytes=MAX_IMAGE_BYTES,
        )
        return response.body

    async def _enrich_with_content_fetcher(self, output: ReverseImageOutput) -> None:
        """可选：抓取前 2 个关联页面的正文片段补充到结果中。"""
        if not self._cfg.fetch_content or self._fetcher is None or not output.matches:
            return
        top_matches = output.matches[:2]
        excerpts = await asyncio.gather(
            *(self._fetcher.fetch_public_excerpt(m.url) for m in top_matches), return_exceptions=True
        )
        for match, excerpt in zip(top_matches, excerpts, strict=True):
            if isinstance(excerpt, str) and excerpt:
                match.content = excerpt

    async def search_raw(
        self,
        *,
        image_bytes: Optional[bytes] = None,
        image_url: str = "",
    ) -> ReverseImageOutput:
        """按 Google Lens → Yandex → Bing 顺序执行以图搜图并返回原始结构。"""
        max_results = max(int(self._cfg.max_results or 8), 1)
        errors: list[str] = []
        lens_limit, yandex_limit, bing_limit = _stage_limits(
            self._backend.timeout, self._cfg.google_lens_overview_wait_seconds
        )
        attempted = 0

        # 1. Google Lens（优先）
        if self.google_lens.enabled and image_bytes:
            attempted += 1
            lens_out = await _run_stage(
                "google_lens", lens_limit, self.google_lens.search(image_bytes=image_bytes, max_results=max_results)
            )
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
            attempted += 1
            yandex_out = await _run_stage(
                "yandex", yandex_limit,
                self.yandex.search(image_bytes=image_bytes, image_url=shared_image_url, max_results=max_results),
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
            attempted += 1
            bing_out = await _run_stage(
                "bing", bing_limit, self.bing.search(image_url=shared_image_url, max_results=max_results)
            )
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

        if combined_matches or tags:
            merged_engine = "+".join(engines_used)
            self.last_engine = merged_engine
            return ReverseImageOutput(
                engine=merged_engine,
                matches=deduplicate_matches(combined_matches, max_results),
                tags=tags,
                uploaded_image_url=shared_image_url,
            )

        self.last_engine = ""
        if attempted == 0:
            message = "没有可用于该图片的识图引擎。"
            if self.bing.enabled and not shared_image_url:
                message += "必应需要图片链接，聊天图片需先启用 Yandex 上传。"
            return ReverseImageOutput(engine="", error=message)
        return ReverseImageOutput(engine="", error="；".join(errors) if len(errors) == attempted else "")

    async def run(
        self,
        *,
        image_bytes: Optional[bytes] = None,
        image_url: str = "",
        image_hash: str = "",
    ) -> str:
        """执行完整以图搜图并返回可直接交给 Planner 的文本结果。"""
        cleaned_url = (image_url or "").strip()
        if image_bytes is not None and cleaned_url:
            raise ValueError("聊天图片和图片链接只能选一个")
        url_key = ""
        if cleaned_url:
            url_key = "url:" + compute_image_hash(cleaned_url.encode())
            cached = self._get_cached(url_key)
            if cached is not None:
                logger.info("命中以图搜图链接缓存")
                return cached.text
            image_bytes = await self.download_image_url(cleaned_url)
        if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
            raise ValueError("图片为空或超过 15 MiB")
        cache_key = "image:" + ((image_hash or "").strip() or compute_image_hash(image_bytes))

        cached = self._get_cached(cache_key)
        if cached is not None:
            logger.info("命中以图搜图缓存 (key=%s)", cache_key[:16])
            self._set_cached(url_key, cached, max_age=_URL_CACHE_MAX_SECONDS)
            return cached.text
        await asyncio.to_thread(verify_image_data, image_bytes)
        output = await self.search_raw(image_bytes=image_bytes, image_url=cleaned_url)
        if not output.has_useful_data:
            if output.error:
                raise ValueError(output.error)
            return "已完成以图搜图，但没有找到关联网页或识别信息。"

        await self._enrich_with_content_fetcher(output)
        formatted = format_reverse_image_output(
            output,
            max_content_length=min(int(self._backend.max_content_length or 800), 1200),
        )
        ttl = self._cfg.cache_ttl_seconds
        if _lens_links_only(output):
            ttl = min(ttl, _LENS_LINKS_ONLY_CACHE_SECONDS)
        if ttl > 0:
            entry = _CacheEntry(formatted, time.monotonic() + ttl)
            self._set_cached(cache_key, entry)
            self._set_cached(url_key, entry, max_age=_URL_CACHE_MAX_SECONDS)
        return formatted

    async def aclose(self) -> None:
        """清理底层浏览器资源。"""
        await self.google_lens.aclose()
