"""Biztonságos kimenő hálózat: URL-letöltés és webhook-kézbesítés SSRF-védelemmel.

Csak http(s), és a cél nem lehet belső cím (loopback, privát, link-local, multicast, fenntartott — a felhő
metaadat-szolgáltatása, 169.254.169.254 sem). Minden átirányítást újra ellenőrzünk. Fejlesztéshez/teszthez
ALLOW_PRIVATE_URLS=1 engedi a belső címeket.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

import httpx


class UnsafeURL(ValueError):
    pass


def _private_ok() -> bool:
    return os.getenv("ALLOW_PRIVATE_URLS", "").lower() in ("1", "true", "yes")


async def check_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UnsafeURL("only http(s) URLs are allowed")
    if u.username or u.password:
        raise UnsafeURL("credentials in the URL are not allowed")
    if _private_ok():
        return
    host = u.hostname
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80),
                                                              type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise UnsafeURL(f"can't resolve {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified or (isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped
                                         and not ip.ipv4_mapped.is_global)):
            raise UnsafeURL("internal addresses are not allowed")


def _filename(resp: httpx.Response, url: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    name = unquote(m.group(1)) if m else unquote(Path(urlparse(url).path).name)
    name = re.sub(r"[\\/\x00-\x1f]", "_", name).strip(". ") or "download"
    return name[:180]


async def download(url: str, dst: Path, max_bytes: int, headers: Optional[dict[str, str]] = None,
                   timeout: float = 120) -> tuple[str, int, str]:
    """Letöltés dst-be. Vissza: (fájlnév, méret, content-type). Túl nagy → ValueError."""
    hdrs = {"User-Agent": "Mozilla/5.0 (compatible; file-converter/1.0)", **{k: v for k, v in (headers or {}).items()
                                                                          if k.lower() not in ("host", "content-length")}}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
        for _ in range(6):
            await check_url(url)
            async with c.stream("GET", url, headers=hdrs) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise ValueError(f"download failed: HTTP {r.status_code}")
                if int(r.headers.get("content-length") or 0) > max_bytes:
                    raise ValueError(f"the file is larger than {max_bytes // (1024 * 1024)} MB")
                n = 0
                with open(dst, "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 20):
                        n += len(chunk)
                        if n > max_bytes:
                            raise ValueError(f"the file is larger than {max_bytes // (1024 * 1024)} MB")
                        f.write(chunk)
                return _filename(r, url), n, r.headers.get("content-type", "")
    raise ValueError("too many redirects")


async def post_json(url: str, body: bytes, headers: dict[str, str], timeout: float = 15) -> int:
    await check_url(url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
        r = await c.post(url, content=body, headers={"Content-Type": "application/json", **headers})
        return r.status_code
