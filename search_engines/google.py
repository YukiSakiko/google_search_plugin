"""Google 搜索引擎实现。

基于 aiohttp + bs4 自实现,无第三方库依赖。参考 SearXNG 的 google 引擎设计:
- CONSENT=YES+ cookie 绕过 GDPR consent 横幅(没这个会被 redirect 到 consent 页)
- sorry.google.com / /sorry/ 路径 / 短 HTML 含 /sorry/ → 视为 CAPTCHA, 返空让上层切引擎
- 直接使用 Opera Mini 兼容模式,避免 Chrome-like 请求拿到 JS/noscript 壳页
- 按 language 选 subdomain (没 google.cn, 中文走 google.com.hk)
- 参数集: q + hl + lr + client=ms-opera-mini + filter=0 + ie/oe=utf8, 不传 num (实测无效)
"""

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
from bs4 import BeautifulSoup

from .base import BaseSearchEngine, SearchResult

logger = logging.getLogger(__name__)


# 语言代码 → Google subdomain。无 google.cn, 中国大陆走 com.hk。
_SUBDOMAIN_BY_LANG: Dict[str, str] = {
    "zh-cn": "www.google.com.hk",
    "zh-tw": "www.google.com.tw",
    "zh-hk": "www.google.com.hk",
    "ja": "www.google.co.jp",
    "ja-jp": "www.google.co.jp",
    "ko": "www.google.co.kr",
    "ko-kr": "www.google.co.kr",
    "en": "www.google.com",
    "en-us": "www.google.com",
    "en-gb": "www.google.co.uk",
}

# 内部域名: 过滤 google 自家的导航/购物/相关搜索链接
_GOOGLE_HOSTS = (
    "google.com", "google.co.jp", "google.com.hk", "google.com.tw",
    "google.co.uk", "google.co.kr", "google.de", "google.fr", "googleusercontent.com",
)


_OPERA_MINI_USER_AGENT = (
    "Opera/9.80 (J2ME/MIDP; Opera Mini/8.0.35626/191.249; U; en) "
    "Presto/2.12.423 Version/12.16"
)

# 摘要容器选择器。当前 Google 对 Opera Mini UA 返回的是精简版 SERP:
# 结果卡片是 div.Gx5Zad, 摘要节点是 div.ilUpNd.H66NU(标题是 ilUpNd UFvD1,
# 域名是 ilUpNd BamJPe, 所以必须带 H66NU 才不误取)。后面几个
# VwiC3b / IsZvec / aCOpRe / data-sncf 是桌面版和历史版本的 class,
# 保留下来兼容 Google 随时可能切回的另一种变体。
_SNIPPET_SELECTOR = (
    "div.ilUpNd.H66NU, div.VwiC3b, div.IsZvec, span.aCOpRe, div[data-sncf]"
)

# Google 偶发返回 uoh=3 的加密跳转令牌变体: /url?q=CAESbgHrOzAV... 是
# 二进制 base64 令牌而非明文 URL, 服务端跟随重定向只会落到
# "Redirect Notice - The previous page is sending you to an invalid url",
# 必须重试一次换取明文 /url?q=https://... 变体。
#
# 判定只看 SERP 里 /url? 跳转链接的目标, 不做整页子串匹配: 查询词本身
# 含 "uoh=" / "q=CAES" 时会被回显进 <title> 等无关位置, 整页匹配会误判成
# 加密变体并丢弃本来可解析的结果。
_REDIRECT_ANCHOR_SELECTOR = 'a[href*="/url?"]'


class GoogleEngine(BaseSearchEngine):
    """Google 搜索引擎实现"""

    language: str

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self.language = str(self.config.get("language") or "zh-cn").strip().lower()

    def _subdomain(self) -> str:
        return _SUBDOMAIN_BY_LANG.get(self.language, "www.google.com")

    def _hl(self) -> str:
        """界面语言, e.g. zh-cn → zh-CN, en → en。"""
        if "-" in self.language:
            primary, region = self.language.split("-", 1)
            return f"{primary.strip()}-{region.strip().upper()}"
        return self.language

    def _lr(self) -> str:
        """限定结果语言, e.g. lang_zh-CN / lang_en。"""
        if self.language in {"zh-cn", "zh-hans"}:
            return "lang_zh-CN"
        if self.language in {"zh-tw", "zh-hk", "zh-hant"}:
            return "lang_zh-TW"
        primary = self.language.split("-", 1)[0].strip().lower()
        return f"lang_{primary}" if primary else ""

    def _build_url(self, query: str, start: int = 0) -> str:
        params: Dict[str, str] = {
            "q": query,
            "hl": self._hl(),
            "ie": "utf8",
            "oe": "utf8",
            "filter": "0",
            "safe": "off",
            "start": str(start),
            "pws": "0",
            "client": "ms-opera-mini",
        }
        lr = self._lr()
        if lr:
            params["lr"] = lr
        return f"https://{self._subdomain()}/search?{urlencode(params)}"

    def _is_captcha(self, status: int, final_url: str, html: str) -> bool:
        """SearXNG 风格的 sorry 检测 + reCAPTCHA 识别。命中任一即视为被风控。"""
        try:
            parsed = urlparse(final_url)
            if parsed.netloc == "sorry.google.com":
                return True
            if parsed.path.startswith("/sorry"):
                return True
        except Exception:
            pass
        if status == 302:
            return True
        # 经典短 HTML sorry 跳转
        if len(html) < 2000 and "/sorry/" in html:
            return True
        # reCAPTCHA 拦截页(短 HTML 含 recaptcha sitekey / solveSimpleChallenge)
        if len(html) < 5000 and (
            "g-recaptcha" in html
            or "recaptcha/enterprise" in html
            or "solveSimpleChallenge" in html
        ):
            return True
        return False

    def _is_javascript_wall(self, status: int, html: str) -> bool:
        """识别 Google 只返回 JavaScript/noscript 壳页的情况。

        这类响应通常是 90KB 左右的 200 页面,标题为 Google Search,但没有
        h3/data-ved/url?q 等可解析结果,正文主要是混淆 JS 和 noscript 提示。
        """
        if status != 200:
            return False
        low = html.lower()
        if "<h3" in low or "data-ved" in low or "/url?q=" in low:
            return False
        return (
            "<noscript>" in low
            and "window.google" in low
            and len(html) > 50_000
        )

    def _is_opaque_redirect(self, html: str) -> bool:
        """识别结果外链被换成加密跳转令牌的变体。

        Google 有概率(实测约 1/15)把结果外链换成 ``/url?q=CAES...&uoh=3``
        这种二进制 base64 令牌。这类链接既不是 http(s) 也无法在无 JS 环境
        下解出真实目标: 服务端跟随重定向只会拿到
        "Redirect Notice - The previous page is sending you to an invalid url"。

        判定只看 SERP 里实际的 /url? 跳转链接目标, 不扫整页字符串。整页
        匹配会被查询词回显(<title>uoh=3 xxx - Google 搜索</title>)误触发,
        那种页面结果其实是好的。同时要求"页面确实存在跳转链接", 避免在
        没有任何 /url? 链接的页面上凭空判定。
        """
        soup = BeautifulSoup(html, "html.parser")
        anchors = soup.select(_REDIRECT_ANCHOR_SELECTOR)
        if not anchors:
            return False
        for anchor in anchors:
            href = str(anchor.get("href") or "")
            try:
                target = parse_qs(urlparse(href).query).get("q", [""])[0]
            except ValueError:
                continue
            # 明文跳转是 /url?q=https://..., 加密变体是 /url?q=CAES... 这种
            # 不以 scheme 开头的 base64 令牌。
            if target and not target.startswith(("http://", "https://")):
                return True
        return False

    async def _fetch(self, url: str) -> tuple[int, str, str]:
        """请求 Google, 返回 (status, final_url, html)。

        per-request headers, 不污染 self.headers (与 bing._fetch 同模式)。
        cookie 必须带 CONSENT=YES+, 否则 GDPR consent 拦截会让 Google 返横幅页。
        """
        headers = dict(self.headers)
        headers["User-Agent"] = _OPERA_MINI_USER_AGENT
        hl = self._hl()
        primary_lang = hl.split("-")[0]
        headers["Accept-Language"] = f"{hl},{primary_lang};q=0.9"
        headers["Referer"] = f"https://{self._subdomain()}/"
        cookies = {"CONSENT": "YES+"}
        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.TIMEOUT),
                proxy=self.proxy,
                allow_redirects=True,
            ) as resp:
                html = await resp.text()
                return resp.status, str(resp.url), html

    def _clean_url(self, raw: str) -> str:
        """Google 把外链包成 /url?q=https://target&sa=U&ved=...&usg=...
        其余直接返回, _is_google_internal 在外层过滤。
        """
        if not raw:
            return ""
        if raw.startswith("/url?") or raw.startswith("https://www.google.com/url?"):
            try:
                qs = parse_qs(urlparse(raw).query)
                target = qs.get("q", [""])[0] or qs.get("url", [""])[0]
                if target:
                    return target
            except Exception:
                pass
        return raw

    def _is_google_internal(self, url: str) -> bool:
        """过滤 google 自家域名(/search?... /maps /shopping 等导航链接)。"""
        try:
            netloc = urlparse(url).netloc.lower()
        except Exception:
            return False
        return any(netloc == h or netloc.endswith("." + h) for h in _GOOGLE_HOSTS)

    def _extract_snippet(self, anchor: Any) -> str:
        """从结果 anchor 上溯找 snippet 容器, 兼容多版本 class。

        Opera Mini 精简版 SERP 的层级是:
            div.Gx5Zad
              div.sHTlR.lQigmf > a > div.rdSCGb > div.pQyidf > h3   (标题)
              div.lQigmf > div.ilUpNd.H66NU                          (摘要)
        摘要和标题不在同一个子树里, 所以必须从 anchor 往上逐层找,
        直到某一层的 select_one 命中 _SNIPPET_SELECTOR 为止。
        命中失败就返空, snippet 不是必需(title + url 够下游 ContentFetcher 抓正文)。
        """
        outer: Any = anchor
        for _ in range(4):
            outer = outer.find_parent("div")
            if outer is None:
                break
            snippet_node = outer.select_one(_SNIPPET_SELECTOR)
            if snippet_node:
                return self.tidy_text(snippet_node.get_text(" ", strip=True))
        return ""

    def _parse(self, html: str) -> List[SearchResult]:
        """提取搜索结果。

        策略: 找所有 h3, 顺着 parent 拿最近的 <a href>。Google SERP 历史上最稳定的
        结构就是 "h3 永远在结果卡片里, 标题外面包一层 <a>"。class 跨版本变化大,
        不依赖 class。
        """
        soup = BeautifulSoup(html, "html.parser")
        results: List[SearchResult] = []
        seen_urls: set[str] = set()

        for idx, h3 in enumerate(soup.select("h3")):
            anchor = h3.find_parent("a")
            if anchor is None or not anchor.get("href"):
                continue
            title = self.tidy_text(h3.get_text())
            url = self._clean_url(anchor.get("href"))
            if not title or not url:
                continue
            if not url.startswith(("http://", "https://")):
                continue
            if self._is_google_internal(url):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            snippet = self._extract_snippet(anchor)
            results.append(
                SearchResult(title=title, url=url, snippet=snippet, abstract=snippet, rank=idx)
            )
        return results

    async def search(self, query: str, num_results: int) -> List[SearchResult]:
        try:
            url = self._build_url(query)
            logger.info("Requesting Google search URL: %s", url)
            status, final_url, html = await self._fetch(url)

            if self._is_captcha(status, final_url, html):
                logger.warning(
                    "Google CAPTCHA/sorry detected for query '%s' (status=%d, final_url=%s)",
                    query, status, final_url,
                )
                return []

            if status != 200:
                logger.warning("Google returned status %d for query '%s'", status, query)
                return []

            if self._is_javascript_wall(status, html):
                logger.warning(
                    "Google JavaScript wall detected for query '%s' (html_len=%d); "
                    "pure HTTP parsing cannot extract results from this response.",
                    query,
                    len(html),
                )
                return []

            # 先解析。有结果就直接返回, 不做任何加密跳转判定 —— 解析器本身
            # 已经过滤掉了非 http(s) 的链接, 能解析出结果就说明页面是可用的。
            results = self._parse(html)

            # 只有解析为空时才考虑加密跳转变体: 此时再看结果外链是否被换成
            # 了 base64 令牌, 是则同 query 重试一次换取明文变体
            # (实测 15 次里 14 次能拿到明文)。
            if not results and self._is_opaque_redirect(html):
                logger.warning(
                    "Google returned opaque redirect tokens for query '%s'; retrying once",
                    query,
                )
                status, final_url, html = await self._fetch(url)
                if status != 200 or self._is_captcha(status, final_url, html):
                    logger.warning(
                        "Google retry failed for query '%s' (status=%d)", query, status,
                    )
                    return []
                if self._is_javascript_wall(status, html):
                    logger.warning(
                        "Google retry hit JavaScript wall for query '%s' (html_len=%d)",
                        query,
                        len(html),
                    )
                    return []
                results = self._parse(html)

            if not results:
                logger.warning(
                    "Google parsed 0 results for query '%s' (html_len=%d)", query, len(html),
                )
            logger.info(
                "Returning %d Google results for query '%s'", len(results[:num_results]), query,
            )
            return results[:num_results]
        except Exception as exc:  # noqa: BLE001
            logger.error("Google search error for query '%s': %s", query, exc, exc_info=True)
            return []
