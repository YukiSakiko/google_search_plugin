"""Measure actual reverse-image tool calls with a public image and an isolated browser profile."""

import argparse
import asyncio
import json
import logging
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from plugins.google_search_plugin.config import GoogleSearchPluginConfig  # noqa: E402
from plugins.google_search_plugin.plugin import GoogleSearchPlugin  # noqa: E402

PUBLIC_IMAGE = "https://www.python.org/static/community_logos/python-logo.png"


async def measure(args, profile):
    cfg = GoogleSearchPluginConfig()
    data = args.public_file.read_bytes() if args.public_file else None
    if args.config:
        # Reuse only transport settings, never the bot's cookies or browser directory.
        with args.config.open("rb") as source:
            transport = tomllib.load(source).get("search_backend", {})
        for field in ("timeout", "content_timeout", "proxy"):
            if field in transport:
                setattr(cfg.search_backend, field, transport[field])
    cfg.reverse_image.google_lens_profile_dir = profile
    cfg.reverse_image.google_lens_enabled = not args.no_lens
    plugin = GoogleSearchPlugin()
    plugin.set_plugin_config(cfg.model_dump())
    plugin._set_context(SimpleNamespace(logger=logging.getLogger("probe")))
    await plugin.on_load()
    pipe = plugin._reverse_image_pipeline
    stages = {}

    def observe(obj, name, label):
        original = getattr(obj, name)

        async def timed(*pos, **kw):
            start = time.perf_counter()
            try:
                return await original(*pos, **kw)
            finally:
                stages[label] = round(time.perf_counter() - start, 3)

        setattr(obj, name, timed)

    observe(pipe, "download_image_url", "download")
    observe(pipe.google_lens, "_ensure_context", "browser_ready")
    for engine in ("google_lens", "yandex", "bing"):
        observe(getattr(pipe, engine), "search", engine)
    ok = True
    try:
        for label in ["cold"] + ["warm"] * args.warm_runs + ["repeat"]:
            if label == "warm":
                pipe._cache.clear()
            stages.clear()
            reused = pipe.google_lens._context is not None
            cooling = pipe.google_lens.cooldown_remaining_seconds() > 0
            start = time.perf_counter()
            if data is None:
                result = await plugin.handle_reverse_image_search(image_url=args.url)
            else:
                # Start just after the host resolves the chat image; include real engine calls.
                try:
                    result = {"content": await pipe.run(image_bytes=data), "success": True}
                except ValueError as exc:
                    result = {"content": str(exc), "success": False}
            elapsed = time.perf_counter() - start
            body = result.get("content", "")
            matched = bool(result.get("success")) and "【以图搜图结果】" in body
            ran_search = any(name in stages for name in ("google_lens", "yandex", "bing"))
            print(
                json.dumps(
                    {
                        "run": label,
                        "seconds": round(elapsed, 3),
                        "stages": stages,
                        "matched": matched,
                        "engine": pipe.last_engine if matched else None,
                        "cache_hit": matched and not ran_search,
                        "mentions_python": "python" in body.lower(),
                        "browser_already_open": reused,
                        "lens_in_cooldown": cooling,
                    }
                ),
                flush=True,
            )
            ok = ok and matched
    finally:
        await plugin.on_unload()
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Optional plugin TOML for proxy and timeouts only")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--url", default=PUBLIC_IMAGE, help="Public image that may be sent to search providers")
    source.add_argument("--public-file", type=Path, help="Public image file to upload, skipping URL download")
    parser.add_argument("--warm-runs", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--no-lens", action="store_true", help="Measure the HTTP fallback without contacting Google")
    options = parser.parse_args()
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="maibot-lens-probe-") as tmp:
        raise SystemExit(0 if asyncio.run(measure(options, tmp)) else 1)
