from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from plugins.google_search_plugin.search_engines.reverse_image.google_lens import GoogleLensReverseEngine  # noqa: E402

_PNG = b"\x89PNG\r\n\x1a\n" + bytes(32)


class LensLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_new_managed_default_can_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            default = root / "default"
            lens = GoogleLensReverseEngine(default_profile_dir=str(default))
            lens._prepare_profile(default)
            await lens.reset_login_state()
            self.assertFalse(default.exists())

            for directory in (default, root / "external"):
                with self.subTest(directory=directory):
                    directory.mkdir()
                    other = directory / "keep.txt"
                    other.write_bytes(b"keep")
                    lens.profile_dir = str(directory)
                    lens._prepare_profile(directory)
                    await lens.reset_login_state()
                    self.assertEqual(other.read_bytes(), b"keep")
                    self.assertIn("保留", lens.get_login_snapshot()["message"])

    async def test_external_profile_cannot_be_deleted_even_with_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            default = Path(tmp) / "default"
            external = Path(tmp) / "external"
            lens = GoogleLensReverseEngine(default_profile_dir=str(default))
            lens._prepare_profile(default)
            default.rename(external)
            lens.profile_dir = str(external)
            await lens.reset_login_state()
            self.assertTrue(external.is_dir())

    async def test_cleanup_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            default = Path(tmp) / "default"
            lens = GoogleLensReverseEngine(default_profile_dir=str(default))
            lens._prepare_profile(default)
            with patch("shutil.rmtree", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    await lens.reset_login_state()
            self.assertTrue(default.is_dir())
            self.assertEqual(lens.get_login_snapshot()["state"], "error")

    async def test_upload_disposes_response_without_following_redirect(self) -> None:
        for status in (303, 400):
            with self.subTest(status=status):
                response = SimpleNamespace(
                    status=status, headers={"location": "https://www.google.com/search"}, dispose=AsyncMock()
                )
                post = AsyncMock(return_value=response)
                lens = GoogleLensReverseEngine()
                url, error = await lens._upload_for_vsrid_url(SimpleNamespace(request=SimpleNamespace(post=post)), _PNG)
                self.assertEqual(bool(url), status == 303)
                self.assertEqual(bool(error), status == 400)
                self.assertEqual(post.await_args.kwargs["max_redirects"], 0)
                response.dispose.assert_awaited_once()

    async def test_queued_search_rechecks_captcha(self) -> None:
        lens = GoogleLensReverseEngine()
        with patch.object(lens, "_ensure_context", new_callable=AsyncMock) as create:
            async with lens._lock:
                pending = asyncio.create_task(lens.search(image_bytes=_PNG))
                await asyncio.sleep(0)
                lens._captcha_cooldown_until = time.time() + 60
            self.assertTrue((await pending).captcha_triggered)
            create.assert_not_awaited()

    async def test_retired_engine_waits_for_current_search_and_rejects_queued_work(self) -> None:
        lens = GoogleLensReverseEngine()
        ctx = MagicMock(close=AsyncMock())
        lens._context = ctx
        async with lens._lock:
            stop = asyncio.create_task(lens.aclose())
            await asyncio.sleep(0)
            refused = await lens.search(image_bytes=_PNG)
            self.assertTrue(refused.error)
            ctx.close.assert_not_awaited()
        await stop
        ctx.close.assert_awaited_once()

    async def test_cancelled_driver_start_releases_its_own_handle(self) -> None:
        lens = GoogleLensReverseEngine()
        driver = SimpleNamespace(stop=AsyncMock())
        started, ready = asyncio.Event(), asyncio.Event()

        async def start():
            started.set()
            await ready.wait()
            return driver

        with patch("playwright.async_api.async_playwright", return_value=SimpleNamespace(start=start)):
            task = asyncio.create_task(lens.search(image_bytes=_PNG))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            ready.set()
            await lens.aclose()
        driver.stop.assert_awaited_once()

    async def test_browser_files_are_not_proof_of_google_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Local State").write_bytes(b"{}")
            lens = GoogleLensReverseEngine(default_profile_dir=tmp)
            context = MagicMock()
            context.pages = [SimpleNamespace(goto=AsyncMock())]
            context.cookies = AsyncMock(return_value=[])
            context.close = AsyncMock()
            pw = SimpleNamespace(
                chromium=SimpleNamespace(launch_persistent_context=AsyncMock(return_value=context)),
                stop=AsyncMock(),
            )
            lens._login_done_event = asyncio.Event()
            lens._login_done_event.set()
            with patch.object(lens, "_start_driver", new_callable=AsyncMock, return_value=pw):
                await lens._run_interactive_login(tmp)
            self.assertTrue(lens.get_login_snapshot()["has_saved_profile"])
            self.assertEqual(lens.get_login_snapshot()["state"], "idle")
