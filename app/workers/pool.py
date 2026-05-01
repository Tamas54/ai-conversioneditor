"""
Worker pool — pre-warmed LibreOffice + Chromium instances.

Strategy:
    - LibreOffice: pre-spawn N background `soffice --headless` processes,
      each on its own UserInstallation dir (so they don't fight). Worker
      acquired via asyncio.Semaphore. Beyond `warm`, lazy-spawn up to MAX.
    - Chromium: Playwright async, persistent browser context per worker.
      Pages are created/closed per job; the browser stays alive.

Why per-instance UserInstallation dirs:
    LibreOffice locks its profile dir. Sharing one between concurrent
    soffice processes → lock conflicts → garbage. Each worker gets its own.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("aice.workers")


@dataclass
class LOWorker:
    """A LibreOffice subprocess wrapper with its own profile dir."""

    profile_dir: Path
    busy: bool = False

    async def convert(
        self, input_path: Path, output_dir: Path, target_filter: str
    ) -> Path:
        """
        Run `soffice --headless --convert-to <filter> <input>`.
        Returns the path of the converted file.
        """
        cmd = [
            "soffice",
            f"-env:UserInstallation=file://{self.profile_dir}",
            "--headless",
            "--norestore",
            "--nologo",
            "--nofirststartwizard",
            "--convert-to",
            target_filter,
            "--outdir",
            str(output_dir),
            str(input_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"LibreOffice convert failed (rc={proc.returncode}): "
                f"{stderr.decode(errors='ignore')[:500]}"
            )
        # soffice writes to <outdir>/<input_stem>.<target_ext>
        target_ext = target_filter.split(":")[0]
        out = output_dir / f"{input_path.stem}.{target_ext}"
        if not out.exists():
            raise RuntimeError(f"LibreOffice output not found: {out}")
        return out


class WorkerPool:
    def __init__(self, libreoffice_warm: int, chromium_warm: int):
        self.lo_warm = libreoffice_warm
        self.chromium_warm = chromium_warm

        self._lo_workers: list[LOWorker] = []
        self._lo_sem = asyncio.Semaphore(libreoffice_warm)
        self._lo_lock = asyncio.Lock()

        # Playwright lazily imported in start() so module loads fast in tests
        self._playwright = None
        self._browser = None
        self._chromium_sem = asyncio.Semaphore(max(1, chromium_warm))

    async def start(self):
        # Pre-warm LibreOffice profile dirs (the actual `soffice` is invoked
        # per job — pre-warming the *profile* avoids the slow first-run dir
        # creation, which is what actually costs 5-8s on cold start).
        for _ in range(self.lo_warm):
            self._lo_workers.append(self._spawn_lo_worker())
        log.info("Pre-warmed %d LibreOffice profiles", len(self._lo_workers))

        # Pre-launch Chromium
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        log.info("Chromium launched (warm=%d)", self.chromium_warm)

    async def stop(self):
        for w in self._lo_workers:
            shutil.rmtree(w.profile_dir, ignore_errors=True)
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    def _spawn_lo_worker(self) -> LOWorker:
        profile = Path(tempfile.mkdtemp(prefix="lo_profile_"))
        # Bootstrap the profile by running a no-op once. This is the
        # actual pre-warming — first soffice run creates the profile dir.
        subprocess.run(
            [
                "soffice",
                f"-env:UserInstallation=file://{profile}",
                "--headless",
                "--norestore",
                "--terminate_after_init",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        return LOWorker(profile_dir=profile)

    async def acquire_lo(self) -> LOWorker:
        await self._lo_sem.acquire()
        async with self._lo_lock:
            for w in self._lo_workers:
                if not w.busy:
                    w.busy = True
                    return w
            # Need to spawn lazily (above warm count)
            w = self._spawn_lo_worker()
            w.busy = True
            self._lo_workers.append(w)
            return w

    async def release_lo(self, w: LOWorker):
        w.busy = False
        self._lo_sem.release()

    async def acquire_chromium_page(self):
        """Returns (context, page). Caller must close both when done."""
        await self._chromium_sem.acquire()
        ctx = await self._browser.new_context()
        page = await ctx.new_page()
        return ctx, page

    async def release_chromium_page(self, ctx, page):
        try:
            await page.close()
            await ctx.close()
        finally:
            self._chromium_sem.release()
