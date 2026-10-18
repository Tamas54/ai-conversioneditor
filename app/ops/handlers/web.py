"""Weboldal mentése PDF-be vagy képbe valódi böngészővel (Chromium, Playwright).

SSRF-védelem: a cím és minden betöltött erőforrás (képek, szkriptek…) gazdagépét ellenőrizzük — belső címre
(localhost, privát hálózat, felhő-metaadat) a böngésző nem mehet."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.convert.options import OptionError
from app.ops.handlers import out_name


def _host_ok(url: str) -> bool:
    from app import net
    return net.url_ok(url)            # a közös szabály (belső, metaadat-, nem globális cím tiltva)


async def _page(url: str, width: int, wait_ms: int):
    from app import net
    await net.check_url(url)
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    import os
    exe = os.getenv("CHROMIUM_EXECUTABLE") or None       # egyéni Chromium (pl. ha a Playwright-verzió eltér)
    browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"], executable_path=exe)
    ua = await browser.new_page()                       # a böngésző saját UA-ja + a mienk (kik vagyunk, hol érnek el)
    agent = f"{await ua.evaluate('navigator.userAgent')} {net.APP}/{net.VERSION}"
    await ua.close()
    ctx = await browser.new_context(viewport={"width": width, "height": 900}, device_scale_factor=1, java_script_enabled=True,
                                    user_agent=agent)
    page = await ctx.new_page()

    async def guard(route):
        ok = await asyncio.to_thread(_host_ok, route.request.url)
        await (route.continue_() if ok else route.abort())
    await page.route("**/*", guard)
    try:
        await page.goto(url, wait_until="networkidle", timeout=45000)
    except Exception:
        await page.goto(url, wait_until="load", timeout=45000)
    if wait_ms:
        await page.wait_for_timeout(wait_ms)
    return pw, browser, page


async def to_pdf(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    url = o.get("url")
    if not url:
        raise OptionError("url is required")
    pw, browser, page = await _page(url, 1280, int(o.get("wait_ms") or 500))
    try:
        title = (await page.title() or urlparse(url).hostname or "page")[:60]
        dst = out_name(out, Path(_safe(title) + ".pdf"), "", "pdf")
        await page.emulate_media(media="print")
        await page.pdf(path=str(dst), format=o.get("page_size", "A4"), print_background=True,
                       margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"})
    finally:
        await browser.close()
        await pw.stop()
    return [dst]


async def to_image(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    url = o.get("url")
    if not url:
        raise OptionError("url is required")
    fmt = o.get("image_format", "png")
    pw, browser, page = await _page(url, int(o.get("width") or 1280), int(o.get("wait_ms") or 500))
    try:
        title = (await page.title() or urlparse(url).hostname or "page")[:60]
        shot = out / "shot.png"
        await page.screenshot(path=str(shot), full_page=bool(o.get("full_page", True)))
    finally:
        await browser.close()
        await pw.stop()
    dst = out_name(out, Path(_safe(title) + "." + fmt), "", fmt)
    if fmt == "png":
        shot.replace(dst)
    else:
        from app.convert.service import convert_path
        outs, _ = await convert_path(shot, fmt, {}, out / "cv")
        outs[0].replace(dst)
    return [dst]


def _safe(t: str) -> str:
    import re
    return re.sub(r"[^\w\-]+", "_", t).strip("_") or "page"
