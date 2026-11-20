"""Google Lens 反向以图搜图引擎（Playwright 同会话上传 + 结果提取）。

两阶段同会话架构:
1. 通过 Playwright 上下文自带的 ``context.request.post("https://lens.google.com/v3/upload")``
   携同登录态 Cookie 上传图片，快速拿到与当前账号绑定的 ``303 Location``
   （``https://www.google.com/search?vsrid=...&udm=26...``），无需模拟点击上传按钮。
2. 在同一 Playwright 上下文中打开该 ``vsrid`` 结果页，提取关联页面链接与可选 AI 概览；
   同时兼容本地持久化目录（WebUI 一键登录）与配置 Cookie 注入（Docker/无头服务器）。
3. 遇到 ``sorry.google.com`` 或 reCAPTCHA 时不做任何绕过，立即标记 ``captcha_triggered=True``
   并进入短期冷却，让上层流水线无缝降级到纯 HTTP 的 Yandex / Bing 引擎。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .base import (
    ReverseImageMatch,
    ReverseImageOutput,
    deduplicate_matches,
    detect_image_mime_and_ext,
    extract_domain,
    is_google_internal_url,
    normalize_match_url,
    parse_cookie_header,
)

logger = logging.getLogger(__name__)

_LENS_UPLOAD_URL = "https://lens.google.com/v3/upload"
_CONSENT_COOKIE = "YES+cb.20220419-08-p0.cs+FX+111"
_GOOGLE_AUTH_COOKIE_NAMES = frozenset({"SID", "HSID", "SSID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"})
_LOGIN_MAX_WAIT_SECONDS = 15 * 60

LoginState = Literal["idle", "starting", "waiting", "saving", "logged_in", "error"]

# AI 概览文本末尾常见的 UI 按钮噪声行
_AIO_NOISE_LINES = frozenset(
    {
        "AI 概览",
        "AI Overview",
        "全部显示",
        "显示更多",
        "Show more",
        "Show all",
        "了解详情",
        "Learn more",
        "答得好",
        "答得不好",
        "复制文字",
        "分享",
        "反馈",
        "关于此回答",
    }
)


def _ensure_hl_param(url: str, hl: str) -> str:
    """确保 Lens 结果页 URL 携带期望的 hl 语言参数。"""
    if not url or not hl:
        return url
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        qs["hl"] = [hl]
        return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
    except Exception:
        return url


_AIO_BADGE_COUNT_RE = re.compile(r"^\+\d+$")


def _clean_ai_overview_text(raw_text: str) -> str:
    """清理 AI 概览容器中的零散 UI 按钮文案与引用计数角标。"""
    if not raw_text:
        return ""
    cleaned_lines: list[str] = []
    for line in raw_text.splitlines():
        stripped = " ".join(line.split()).strip().strip("\u2060")
        if not stripped or stripped in _AIO_NOISE_LINES or _AIO_BADGE_COUNT_RE.match(stripped):
            continue
        cleaned_lines.append(stripped)
    return "\n".join(cleaned_lines).strip()


class GoogleLensReverseEngine:
    """Google Lens 混合引擎（纯 HTTP 上传换 vsrid + Playwright 解析）。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        proxy: Optional[str] = None,
        timeout: int = 25,
        language: str = "zh-CN",
        cookies: str = "",
        profile_dir: str = "",
        default_profile_dir: str = "",
        headless: bool = True,
        browser_channel: str = "auto",
        captcha_cooldown_seconds: int = 300,
    ) -> None:
        self.enabled = enabled
        self.proxy = (proxy or "").strip() or None
        self.timeout = max(int(timeout or 25), 10)
        self.language = (language or "zh-CN").strip()
        self.cookies = (cookies or "").strip()
        self.profile_dir = (profile_dir or "").strip()
        self._default_profile_dir = (default_profile_dir or "").strip()
        self.headless = bool(headless)
        self.browser_channel = (browser_channel or "auto").strip().lower()
        self.captcha_cooldown_seconds = max(int(captcha_cooldown_seconds or 0), 0)

        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._cookies_injected = False
        self._captcha_cooldown_until: float = 0.0

        # WebUI 交互式登录状态机
        self._login_state: LoginState = "idle"
        self._login_message: str = ""
        self._login_task: Optional[asyncio.Task[None]] = None
        self._login_started_event: Optional[asyncio.Event] = None
        self._login_done_event: Optional[asyncio.Event] = None
        self._login_playwright: Any = None
        self._login_context: Any = None

    async def _upload_for_vsrid_url(self, context: Any, image_bytes: bytes) -> tuple[str, str]:
        """阶段 1: 通过同一 Playwright 上下文携登录态 Cookie 上传图片，返回 (location_url, error)。"""
        mime_type, ext = detect_image_mime_and_ext(image_bytes)
        resp = await context.request.post(
            _LENS_UPLOAD_URL,
            params={
                "hl": self.language,
                "re": "df",
                "st": str(int(time.time() * 1000)),
                "vpw": "1280",
                "vph": "800",
                "ep": "gsbubb",
            },
            multipart={
                "encoded_image": {
                    "name": f"image.{ext}",
                    "mimeType": mime_type,
                    "buffer": image_bytes,
                }
            },
            max_redirects=0,
            timeout=self.timeout * 1000,
        )
        loc = str(resp.headers.get("location") or "").strip()
        if resp.status in (301, 302, 303, 307, 308) and loc:
            return _ensure_hl_param(loc, self.language), ""
        return "", f"Google Lens 上传未返回跳转地址 (HTTP {resp.status})"

    def _candidate_channels(self) -> list[Optional[str]]:
        if self.browser_channel == "chromium":
            return [None]
        if self.browser_channel in {"chrome", "msedge"}:
            return [self.browser_channel, None]
        # auto: 优先系统 Chrome，回退 Edge 与 Playwright 内置 Chromium
        return ["chrome", "msedge", None]

    async def _ensure_context(self) -> Any:
        """懒加载并复用 Playwright Context（支持持久化 Profile 与 Cookie 注入）。"""
        if self._context is not None:
            return self._context

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("当前 Python 环境未安装 playwright") from exc

        self._playwright = await async_playwright().start()
        proxy_cfg = {"server": self.proxy} if self.proxy else None
        launch_args = ["--disable-blink-features=AutomationControlled"]

        last_error: Optional[Exception] = None
        for channel in self._candidate_channels():
            common_kwargs: dict[str, Any] = {
                "headless": self.headless,
                "args": launch_args,
            }
            if proxy_cfg is not None:
                common_kwargs["proxy"] = proxy_cfg
            if channel:
                common_kwargs["channel"] = channel

            try:
                if self.profile_dir:
                    Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
                    self._context = await self._playwright.chromium.launch_persistent_context(
                        user_data_dir=self.profile_dir,
                        locale=self.language,
                        viewport={"width": 1280, "height": 800},
                        **common_kwargs,
                    )
                    self._browser = None
                else:
                    self._browser = await self._playwright.chromium.launch(**common_kwargs)
                    self._context = await self._browser.new_context(
                        locale=self.language,
                        viewport={"width": 1280, "height": 800},
                    )
                logger.info(
                    "Google Lens Playwright 已就绪 (channel=%s, persistent=%s, headless=%s)",
                    channel or "bundled-chromium",
                    bool(self.profile_dir),
                    self.headless,
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.debug("尝试启动 Playwright channel=%s 失败: %s", channel, exc)
                continue

        if self._context is None:
            await self.aclose()
            raise RuntimeError(f"无法启动 Playwright 浏览器: {last_error}")

        await self._inject_cookies_once()
        return self._context

    async def _inject_cookies_once(self) -> None:
        if self._cookies_injected or self._context is None:
            return
        cookies = [
            {
                "name": "CONSENT",
                "value": _CONSENT_COOKIE,
                "domain": ".google.com",
                "path": "/",
            }
        ]
        cookies.extend(parse_cookie_header(self.cookies, domain=".google.com"))
        if cookies:
            try:
                await self._context.add_cookies(cookies)
            except Exception as exc:  # noqa: BLE001
                logger.warning("向 Google Lens 浏览器注入 Cookie 失败: %s", exc)
        self._cookies_injected = True

    @staticmethod
    def _is_sorry_page(url: str, html_snippet: str) -> bool:
        low_url = (url or "").lower()
        if "sorry.google.com" in low_url or "/sorry/" in low_url:
            return True
        low_html = (html_snippet or "")[:12000].lower()
        return (
            "g-recaptcha" in low_html
            or "recaptcha/enterprise" in low_html
            or "solvesimplechallenge" in low_html
        )

    async def _extract_from_page(self, page: Any, max_results: int) -> tuple[list[ReverseImageMatch], str]:
        """从已加载的 Google Lens 结果页提取外链与可选 AI 概览。"""
        # 等待外链出现（最长 ~8s），允许页面异步完成渲染
        try:
            await page.wait_for_function(
                """() => {
                    const anchors = document.querySelectorAll('a[href^="http"]');
                    for (const a of anchors) {
                        const h = a.href || '';
                        if (h && !h.includes('google.') && !h.includes('gstatic.') && !h.includes('googleapis.')) {
                            return true;
                        }
                    }
                    return false;
                }""",
                timeout=min(self.timeout * 1000, 10000),
            )
        except Exception:
            pass

        # 给异步 AI 概览 (/async/folsrch) 一个短窗口完成首屏渲染
        await asyncio.sleep(1.5)

        payload = await page.evaluate(
            """(limit) => {
                const links = [];
                const seen = new Set();
                for (const a of document.querySelectorAll('a[href^="http"]')) {
                    const href = a.href || '';
                    if (!href) continue;
                    const text = (a.innerText || a.getAttribute('aria-label') || a.getAttribute('title') || '')
                        .replace(/\\s+/g, ' ')
                        .trim();
                    if (!text || text.length < 2 || seen.has(href)) continue;
                    seen.add(href);
                    links.push({ title: text.slice(0, 180), url: href });
                    if (links.length >= limit * 3) break;
                }

                let aio = '';
                const turn = document.querySelector('[data-scope-id="turn"]');
                if (turn && turn.innerText) {
                    aio = turn.innerText;
                }
                return { links, aio };
            }""",
            max(max_results, 5),
        )

        matches: list[ReverseImageMatch] = []
        for item in payload.get("links") or []:
            url = normalize_match_url(str(item.get("url") or ""))
            if not url or is_google_internal_url(url):
                continue
            title = str(item.get("title") or "").strip()
            matches.append(
                ReverseImageMatch(
                    title=title,
                    url=url,
                    source=extract_domain(url),
                )
            )
        cleaned_matches = deduplicate_matches(matches, max_results)
        ai_overview = _clean_ai_overview_text(str(payload.get("aio") or ""))
        return cleaned_matches, ai_overview

    def is_login_active(self) -> bool:
        return self._login_state in {"starting", "waiting", "saving"}

    def cooldown_remaining_seconds(self) -> int:
        remaining = int(self._captcha_cooldown_until - time.time())
        return max(remaining, 0)

    def login_target_profile_dir(self) -> str:
        return self.profile_dir or self._default_profile_dir

    def has_saved_profile(self) -> bool:
        target = self.login_target_profile_dir()
        if not target:
            return False
        root = Path(target)
        if not root.is_dir():
            return False
        candidates = (
            root / "Default" / "Network" / "Cookies",
            root / "Default" / "Cookies",
            root / "Local State",
        )
        return any(path.is_file() and path.stat().st_size > 0 for path in candidates)

    def get_login_snapshot(self) -> dict[str, Any]:
        return {
            "state": self._login_state,
            "message": self._login_message,
            "active": self.is_login_active(),
            "has_cookies_config": bool(self.cookies),
            "has_saved_profile": self.has_saved_profile(),
            "profile_dir": self.login_target_profile_dir(),
            "cooldown_remaining": self.cooldown_remaining_seconds(),
        }

    async def start_interactive_login(self) -> None:
        """启动本机有头浏览器窗口供用户登录 Google（8s 内立即返回，兼容 WebUI 超时限制）。"""
        if self.is_login_active():
            return

        target_dir = self.login_target_profile_dir()
        if not target_dir:
            self._login_state = "error"
            self._login_message = "未配置有效的本地持久化目录，无法启动本地登录窗口。"
            return

        # 释放可能占用同一 user_data_dir 的无头搜索上下文
        try:
            async with asyncio.timeout(2.5):
                async with self._lock:
                    await self._close_search_browser()
        except TimeoutError:
            self._login_state = "error"
            self._login_message = "当前有以图搜图请求正在执行，请稍后再点击打开登录窗口。"
            return

        self._login_state = "starting"
        self._login_message = "正在启动本机浏览器登录窗口..."
        self._login_started_event = asyncio.Event()
        self._login_done_event = asyncio.Event()
        self._login_task = asyncio.create_task(
            self._run_interactive_login(target_dir),
            name="google-lens-webui-login",
        )

        # 短暂等待启动结果（最多 2.5s），让 WebUI 首屏就能看到“窗口已打开”或明确的报错提示
        try:
            await asyncio.wait_for(self._login_started_event.wait(), timeout=2.5)
        except TimeoutError:
            pass

    async def _run_interactive_login(self, target_dir: str) -> None:
        pw: Any = None
        context: Any = None
        detected_auth = False
        try:
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError("当前 Python 环境未安装 playwright") from exc

            Path(target_dir).mkdir(parents=True, exist_ok=True)
            pw = await async_playwright().start()
            self._login_playwright = pw

            proxy_cfg = {"server": self.proxy} if self.proxy else None
            launch_args = ["--disable-blink-features=AutomationControlled"]
            last_error: Optional[Exception] = None
            for channel in self._candidate_channels():
                kwargs: dict[str, Any] = {
                    "headless": False,
                    "locale": self.language,
                    "viewport": {"width": 1280, "height": 800},
                    "args": launch_args,
                }
                if proxy_cfg is not None:
                    kwargs["proxy"] = proxy_cfg
                if channel:
                    kwargs["channel"] = channel
                try:
                    context = await pw.chromium.launch_persistent_context(user_data_dir=target_dir, **kwargs)
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    continue

            if context is None:
                raise RuntimeError(self._format_headed_launch_error(last_error))

            self._login_context = context
            done_event = self._login_done_event
            if done_event is not None:
                context.on("close", lambda *_: done_event.set())

            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(
                    f"https://www.google.com/?hl={self.language}",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
            except Exception as nav_exc:  # noqa: BLE001
                logger.warning("Google Lens 登录页首次导航失败（仍保留窗口供手动操作）: %s", nav_exc)

            self._login_state = "waiting"
            self._login_message = (
                "登录窗口已在本机弹出。请在浏览器中登录 Google 账号（可顺手试搜一次）；"
                "完成后直接关闭该窗口，或点击下方「完成并保存登录态」。"
            )
            if self._login_started_event is not None:
                self._login_started_event.set()

            if done_event is not None:
                try:
                    await asyncio.wait_for(done_event.wait(), timeout=_LOGIN_MAX_WAIT_SECONDS)
                except TimeoutError:
                    logger.info("Google Lens 交互式登录窗口超过 15 分钟未关闭，自动结束会话")

            self._login_state = "saving"
            self._login_message = "正在保存登录态并关闭窗口..."
            try:
                cookies = await context.cookies("https://www.google.com")
                cookie_names = {str(c.get("name") or "") for c in cookies if isinstance(c, dict)}
                detected_auth = bool(cookie_names & _GOOGLE_AUTH_COOKIE_NAMES)
            except Exception:
                detected_auth = False
        except asyncio.CancelledError:
            self._login_state = "idle"
            self._login_message = "登录会话已取消。"
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Google Lens WebUI 登录启动失败: %s", exc)
            self._login_state = "error"
            self._login_message = str(exc)
            if self._login_started_event is not None:
                self._login_started_event.set()
            return
        finally:
            ctx_to_close, self._login_context = self._login_context, None
            pw_to_stop, self._login_playwright = self._login_playwright, None
            if ctx_to_close is not None:
                try:
                    await ctx_to_close.close()
                except Exception:
                    pass
            if pw_to_stop is not None:
                try:
                    await pw_to_stop.stop()
                except Exception:
                    pass

        # 登录成功后确保搜索端启用该持久化目录并清空风控冷却
        self.profile_dir = target_dir
        self._captcha_cooldown_until = 0.0
        if detected_auth or self.has_saved_profile():
            self._login_state = "logged_in"
            if detected_auth:
                self._login_message = "已检测到 Google 账号登录态并保存至本地目录，Google Lens 已就绪。"
            else:
                self._login_message = "浏览器窗口已关闭，本地持久化目录已更新；如未完成登录可再次点击打开。"
        else:
            self._login_state = "idle"
            self._login_message = "浏览器窗口已关闭，尚未检测到保存的登录态。"

    @staticmethod
    def _format_headed_launch_error(exc: Optional[Exception]) -> str:
        raw = str(exc or "未知错误")
        low = raw.lower()
        if any(hint in low for hint in ("missing x server", "no display", "$display", "ozone_platform", "headless")):
            return (
                "当前运行环境无图形界面（如 Docker / 无桌面服务器），无法弹出本地登录窗口；"
                "请在「高级设置 → 以图搜图引擎参数」填写 Google Lens 登录 Cookie，或直接使用免登录的 Yandex + Bing。"
            )
        return f"启动本机浏览器失败：{raw}"

    async def finish_interactive_login(self) -> None:
        """结束交互式登录窗口并将登录态落盘。"""
        if self._login_done_event is not None:
            self._login_done_event.set()
        task = self._login_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except TimeoutError:
                pass

    async def reset_login_state(self) -> None:
        """清除本地持久化目录与风控冷却状态。"""
        await self._stop_interactive_login()
        async with self._lock:
            await self._close_search_browser()
        self._captcha_cooldown_until = 0.0
        target_dir = self.login_target_profile_dir()
        if target_dir and Path(target_dir).exists():
            await asyncio.to_thread(shutil.rmtree, target_dir, True)
        self._login_state = "idle"
        self._login_message = "已清除本地登录目录并重置风控冷却。"

    async def _stop_interactive_login(self) -> None:
        if self._login_done_event is not None:
            self._login_done_event.set()
        task, self._login_task = self._login_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass

    async def search(
        self,
        *,
        image_bytes: Optional[bytes] = None,
        max_results: int = 8,
    ) -> ReverseImageOutput:
        """执行 Google Lens 反向以图搜图。"""
        if not self.enabled:
            return ReverseImageOutput(engine="google_lens", error="Google Lens 未启用")
        if not image_bytes:
            return ReverseImageOutput(engine="google_lens", error="缺少图片数据")
        if self.is_login_active():
            return ReverseImageOutput(
                engine="google_lens",
                error="Google Lens 正在执行 WebUI 交互式登录，暂由备用引擎处理",
            )
        now = time.time()
        if self._captcha_cooldown_until > now:
            remaining = int(self._captcha_cooldown_until - now)
            return ReverseImageOutput(
                engine="google_lens",
                captcha_triggered=True,
                error=f"Google Lens 处于风控冷却期（剩余 {remaining}s），已直接交由备用引擎",
            )

        async with self._lock:
            page: Any = None
            try:
                context = await self._ensure_context()
                vsrid_url, upload_err = await self._upload_for_vsrid_url(context, image_bytes)
                if upload_err or not vsrid_url:
                    return ReverseImageOutput(engine="google_lens", error=upload_err or "上传未拿到结果地址")

                page = await context.new_page()
                await page.goto(vsrid_url, wait_until="domcontentloaded", timeout=self.timeout * 1000)

                current_url = str(page.url or "")
                html_head = (await page.content())[:12000]
                if self._is_sorry_page(current_url, html_head):
                    if self.captcha_cooldown_seconds > 0:
                        self._captcha_cooldown_until = time.time() + self.captcha_cooldown_seconds
                    logger.warning("Google Lens 命中风控/验证码页 (%s)，自动交由备用引擎降级", current_url[:100])
                    return ReverseImageOutput(
                        engine="google_lens",
                        captcha_triggered=True,
                        error="Google Lens 触发风控/人机验证，请检查登录态或稍后重试",
                    )

                matches, ai_overview = await self._extract_from_page(page, max_results=max_results)
                return ReverseImageOutput(
                    engine="google_lens",
                    matches=matches,
                    ai_overview=ai_overview,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Google Lens Playwright 提取异常: %s", exc)
                # 浏览器上下文异常断开时重置，确保下次调用能自动恢复
                if "closed" in str(exc).lower() or "target" in str(exc).lower():
                    await self._close_search_browser()
                return ReverseImageOutput(engine="google_lens", error=str(exc))
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass

    async def _close_search_browser(self) -> None:
        """仅关闭无头/搜索用 Playwright 上下文。"""
        ctx, self._context = self._context, None
        browser, self._browser = self._browser, None
        pw, self._playwright = self._playwright, None
        self._cookies_injected = False

        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass

    async def aclose(self) -> None:
        """释放所有 Playwright 浏览器资源（含搜索上下文与交互式登录窗口）。"""
        await self._stop_interactive_login()
        await self._close_search_browser()
