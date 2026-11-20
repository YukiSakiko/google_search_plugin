"""Google Lens 持久化浏览器登录辅助脚本。

桌面端（Windows / macOS / Linux GUI）首次启用 Google Lens 以图搜图前，可运行一次本脚本:

    python tools/google_lens_login.py

脚本会读取插件目录的 ``config.toml``（代理与持久化目录配置），以有头模式打开浏览器
访问 https://www.google.com/。手动登录你的 Google 账号并确认搜索页正常后，直接关闭
浏览器窗口或在终端按回车，登录态会自动保存在持久化目录中供插件长期复用。

无桌面环境的 Docker / Linux 服务器可直接跳过本脚本，在 ``config.toml`` 的
``[reverse_image].google_lens_cookies`` 中填写 google.com Cookie 即可。
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_PROFILE_DIR = _PLUGIN_DIR / ".google_lens_profile"


def _load_plugin_config(config_path: Path) -> tuple[str, str, str]:
    """从 config.toml 读取 (proxy, profile_dir, browser_channel)。"""
    if not config_path.exists():
        return "", str(_DEFAULT_PROFILE_DIR), "auto"
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    proxy = str((data.get("search_backend") or {}).get("proxy") or "").strip()
    ri = data.get("reverse_image") or {}
    profile_dir = str(ri.get("google_lens_profile_dir") or "").strip() or str(_DEFAULT_PROFILE_DIR)
    channel = str(ri.get("google_lens_browser_channel") or "auto").strip().lower()
    return proxy, str(Path(profile_dir).expanduser()), channel


def main() -> int:
    parser = argparse.ArgumentParser(description="打开持久化浏览器登录 Google 账号，供 Google Lens 以图搜图复用")
    parser.add_argument("--config", default=str(_PLUGIN_DIR / "config.toml"), help="插件 config.toml 路径")
    parser.add_argument("--proxy", default="", help="覆盖代理地址，例如 http://127.0.0.1:7890")
    parser.add_argument("--profile-dir", default="", help="覆盖持久化浏览器目录")
    parser.add_argument(
        "--channel",
        default="",
        choices=("", "auto", "chrome", "msedge", "chromium"),
        help="覆盖浏览器内核（auto/chrome/msedge/chromium）",
    )
    args = parser.parse_args()

    cfg_proxy, cfg_profile, cfg_channel = _load_plugin_config(Path(args.config))
    proxy = (args.proxy or cfg_proxy).strip()
    profile_dir = str(Path(args.profile_dir or cfg_profile).expanduser())
    channel = (args.channel or cfg_channel or "auto").strip().lower()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("错误：当前 Python 环境未安装 playwright，请使用 MaiBot 的虚拟环境运行本脚本。", file=sys.stderr)
        return 1

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    print(f"持久化目录: {profile_dir}")
    print(f"代理地址:   {proxy or '(未使用代理)'}")
    print(f"浏览器内核: {channel}")

    candidates = [None] if channel == "chromium" else ([channel, None] if channel in {"chrome", "msedge"} else ["chrome", "msedge", None])

    with sync_playwright() as p:
        context = None
        last_err = None
        for ch in candidates:
            kwargs: dict = {
                "headless": False,
                "locale": "zh-CN",
                "viewport": {"width": 1280, "height": 800},
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if proxy:
                kwargs["proxy"] = {"server": proxy}
            if ch:
                kwargs["channel"] = ch
            try:
                context = p.chromium.launch_persistent_context(user_data_dir=profile_dir, **kwargs)
                print(f"已启动浏览器: {ch or 'playwright-chromium'}")
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        if context is None:
            print(f"启动浏览器失败: {last_err}", file=sys.stderr)
            return 1

        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.google.com/?hl=zh-CN", wait_until="domcontentloaded")
        print("\n请在弹出的浏览器窗口中登录 Google 账号（可顺手试搜一次验证未触发验证码）。")
        print("完成登录后，在终端按 Enter 键保存并关闭浏览器...")
        try:
            input()
        except EOFError:
            pass
        finally:
            context.close()

    print("登录态已保存至持久化目录，现在可以正常启动 MaiBot 使用 Google Lens 以图搜图。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
