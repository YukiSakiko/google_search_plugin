"""反向以图搜图引擎共用数据结构与工具函数。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from PIL import Image

# 明确过滤的追踪查询参数（保持返回给 Planner / ContentFetcher 的 URL 干净）
_TRACKING_QUERY_KEYS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "yclid",
    }
)

# 搜索结果中需要剔除的引擎自有内部域名
_GOOGLE_INTERNAL_HOSTS = (
    "google.com",
    "google.co.jp",
    "google.com.hk",
    "google.com.tw",
    "google.co.uk",
    "google.co.kr",
    "google.de",
    "google.fr",
    "gstatic.com",
    "googleapis.com",
    "googleusercontent.com",
    "g.co",
)

_YANDEX_INTERNAL_HOSTS = (
    "yandex.com",
    "yandex.ru",
    "yandex.net",
    "ya.ru",
    "yastatic.net",
)

_BING_INTERNAL_HOSTS = (
    "bing.com",
    "bing.net",
    "msn.com",
    "microsoft.com",
    "live.com",
)


@dataclass
class ReverseImageMatch:
    """单条以图搜图关联页面结果。"""

    title: str
    url: str
    source: str = ""
    snippet: str = ""
    content: str = ""


@dataclass
class ReverseImageOutput:
    """单个以图搜图引擎的标准化输出。"""

    engine: str
    matches: list[ReverseImageMatch] = field(default_factory=list)
    ai_overview: str = ""
    tags: list[str] = field(default_factory=list)
    uploaded_image_url: str = ""
    captcha_triggered: bool = False
    error: str = ""

    @property
    def has_useful_data(self) -> bool:
        return bool(self.matches or self.ai_overview.strip() or self.tags)


def compute_image_hash(image_bytes: bytes) -> str:
    """计算图片内容的 SHA-256 摘要，用于缓存去重。"""
    return hashlib.sha256(image_bytes).hexdigest()


def detect_image_mime_and_ext(image_bytes: bytes) -> tuple[str, str]:
    """根据文件头识别格式；不相信服务器对未知内容的类型声明。"""
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp", "webp"
    if image_bytes.startswith(b"BM"):
        return "image/bmp", "bmp"

    if image_bytes[4:8] == b"ftyp" and image_bytes[8:12] in {b"avif", b"avis"}:
        return "image/avif", "avif"
    return "", ""


def verify_image_data(data: bytes) -> str:
    """读取受支持的栅格图片，拒绝错误页面、损坏的文件和超大画布。"""
    mime, _ = detect_image_mime_and_ext(data)
    if not mime:
        raise ValueError("不是支持的图片格式")
    try:
        with Image.open(BytesIO(data)) as picture:
            if picture.width * picture.height > 64_000_000:
                raise ValueError("图片像素过多")
            picture.verify()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("无法读取图片文件") from exc
    return mime


def normalize_match_url(url: str) -> str:
    """清洗匹配结果 URL，剥离常见追踪参数。"""
    raw = (url or "").strip()
    if not raw.startswith(("http://", "https://")):
        return ""
    try:
        parsed = urlparse(raw)
        if not parsed.netloc:
            return ""
        if not parsed.query:
            return raw
        kept_pairs = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_QUERY_KEYS
        ]
        cleaned_query = urlencode(kept_pairs, doseq=True)
        return urlunparse(parsed._replace(query=cleaned_query))
    except Exception:
        return raw


def extract_domain(url: str) -> str:
    """提取 URL 的主域名（去掉 www. 前缀）。"""
    try:
        host = urlparse(url).netloc.lower().split(":", 1)[0]
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def is_host_in(url: str, blocked_hosts: tuple[str, ...]) -> bool:
    """判断 URL 主机是否属于指定域名集合及其子域。"""
    try:
        netloc = urlparse(url).netloc.lower().split(":", 1)[0]
    except Exception:
        return True
    if not netloc:
        return True
    return any(netloc == host or netloc.endswith("." + host) for host in blocked_hosts)


def is_google_internal_url(url: str) -> bool:
    return is_host_in(url, _GOOGLE_INTERNAL_HOSTS)


def is_yandex_internal_url(url: str) -> bool:
    return is_host_in(url, _YANDEX_INTERNAL_HOSTS)


def is_bing_internal_url(url: str) -> bool:
    return is_host_in(url, _BING_INTERNAL_HOSTS)


def parse_cookie_header(cookie_str: str, *, domain: str = ".google.com") -> list[dict[str, Any]]:
    """将浏览器复制的 Cookie 头字符串解析为 Playwright add_cookies 结构。"""
    raw = (cookie_str or "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[7:].strip()
    if not raw:
        return []

    cookies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for part in raw.split(";"):
        chunk = part.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
                "secure": name.startswith(("__Secure-", "__Host-")),
            }
        )
    return cookies


def deduplicate_matches(matches: list[ReverseImageMatch], limit: int) -> list[ReverseImageMatch]:
    """按归一化 URL 去重并截断至指定条数。"""
    result: list[ReverseImageMatch] = []
    seen_urls: set[str] = set()
    for item in matches:
        cleaned_url = normalize_match_url(item.url)
        if not cleaned_url or cleaned_url in seen_urls:
            continue
        seen_urls.add(cleaned_url)
        title = " ".join((item.title or "").split()).strip()
        if not title:
            title = item.source or extract_domain(cleaned_url) or cleaned_url
        source = (item.source or "").strip() or extract_domain(cleaned_url)
        snippet = " ".join((item.snippet or "").split()).strip()
        result.append(
            ReverseImageMatch(
                title=title,
                url=cleaned_url,
                source=source,
                snippet=snippet,
                content=item.content,
            )
        )
        if limit > 0 and len(result) >= limit:
            break
    return result
