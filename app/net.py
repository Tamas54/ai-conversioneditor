"""Biztonságos kimenő hálózat — a KÖZÖS kliens minden URL-letöltéshez (upload_from_url, transfer és jobs import/url,
insert_image és a képek, a videó cikk-URL-je, convert source_url, website-capture), és a webhook-kézbesítés.

Csak http(s), és a cél nem lehet belső cím: minden, ami nem globális (loopback, privát, link-local — a felhők
metaadat-szolgáltatása, 169.254.169.254 —, a szolgáltatói NAT 100.64/10 — az Alibaba metaadata 100.100.100.200 —,
multicast, fenntartott, dokumentációs), és a metaadat-hosztnevek. Minden átirányítást újra ellenőrzünk, és a
kapcsolat TÉNYLEGES címét is (DNS-újrakötés ellen). Leíró User-Agent (app/verzió + kapcsolat — a Wikimedia és mások
enélkül 403-mal tiltanak), észszerű időkorlát, méretkorlát, átmeneti hibánál (429/503, kapcsolat) újrapróbálás.
Fejlesztéshez/teszthez ALLOW_PRIVATE_URLS=1 engedi a belső címeket.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urljoin, urlparse

import httpx

APP = "AIConversionEditor-Studio"
VERSION = "1.0"
_BLOCKED_HOSTS = {"metadata", "metadata.google.internal", "metadata.goog", "metadata.azure.internal", "instance-data",
                  "instance-data.ec2.internal", "localhost"}
_RETRY_STATUS = (429, 503)


class UnsafeURL(ValueError):
    pass


def user_agent() -> str:
    """„AIConversionEditor-Studio/1.0 (+https://…; kapcsolat) httpx/0.28” — a Wikimedia UA-szabálya szerint."""
    from app.config import settings
    contact = os.getenv("OUTBOUND_CONTACT") or "https://github.com/Tamas54/ai-conversioneditor"
    base = (settings.PUBLIC_BASE_URL or "").rstrip("/")
    return f"{APP}/{VERSION} (+{base + '; ' if base.startswith('https://') else ''}{contact}) httpx/{httpx.__version__}"


def _private_ok() -> bool:
    return os.getenv("ALLOW_PRIVATE_URLS", "").lower() in ("1", "true", "yes")


def blocked_ip(ip: Any) -> bool:
    """Minden, ami nem nyilvános cím (a térképezett IPv6 a benne lévő IPv4 szerint)."""
    ip = ipaddress.ip_address(str(ip).split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


def _check_parts(url: str) -> tuple[str, int]:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UnsafeURL("only http(s) URLs are allowed")
    if u.username or u.password:
        raise UnsafeURL("credentials in the URL are not allowed")
    host = u.hostname.rstrip(".").lower()
    if not _private_ok() and (host in _BLOCKED_HOSTS or host.endswith((".internal", ".localhost", ".local"))):
        raise UnsafeURL("internal addresses are not allowed")
    return host, u.port or (443 if u.scheme == "https" else 80)


async def check_url(url: str) -> None:
    host, port = _check_parts(url)
    if _private_ok():
        return
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise UnsafeURL(f"can't resolve {host}")
    if any(blocked_ip(info[4][0]) for info in infos):
        raise UnsafeURL("internal addresses are not allowed")


def url_ok(url: str) -> bool:
    """Szinkron változat (a website-capture böngészőjének minden kérésére)."""
    u = urlparse(url)
    if u.scheme in ("data", "blob", "about"):
        return True
    try:
        host, port = _check_parts(url)
        if _private_ok():
            return True
        return not any(blocked_ip(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
    except (UnsafeURL, socket.gaierror, ValueError):
        return False


def _peer_ok(r: httpx.Response) -> None:
    """A kapcsolat tényleges címe (a DNS-ellenőrzés és a kapcsolódás között a név máshová is mutathat)."""
    if _private_ok():
        return
    stream = r.extensions.get("network_stream")
    addr = stream.get_extra_info("server_addr") if stream is not None else None
    if addr and blocked_ip(addr[0]):
        raise UnsafeURL("internal addresses are not allowed")


def _filename(resp: httpx.Response, url: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    name = unquote(m.group(1)) if m else unquote(Path(urlparse(url).path).name)
    name = re.sub(r"[\\/\x00-\x1f]", "_", name).strip(". ") or "download"
    return name[:180]


def _too_big(max_bytes: int) -> ValueError:
    return ValueError(f"the file is larger than {max(1, max_bytes // (1024 * 1024))} MB")


@dataclass
class Fetched:
    content: bytes
    name: str
    content_type: str
    url: str                          # az átirányítások utáni végső cím
    status: int


async def _get(url: str, sink, reset, max_bytes: int, headers: Optional[dict[str, str]], timeout: float,
               params: Optional[dict[str, Any]], retries: int) -> tuple[httpx.Response, str]:
    """sink(darab) kapja a törzset; reset() minden nekifutás előtt (egy félbeszakadt próba ne maradjon benne)."""
    hdrs = {"User-Agent": user_agent(), **{k: v for k, v in (headers or {}).items()
                                           if k.lower() not in ("host", "content-length")}}
    tm = httpx.Timeout(timeout, connect=min(15.0, timeout))
    for attempt in range(1, retries + 1):
        cur, first = url, True
        try:
            async with httpx.AsyncClient(timeout=tm, follow_redirects=False) as c:
                for _ in range(6):
                    await check_url(cur)
                    async with c.stream("GET", cur, headers=hdrs, params=params if first else None) as r:
                        first = False
                        _peer_ok(r)
                        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                            cur = urljoin(str(r.url), r.headers["location"])
                            continue
                        if r.status_code in _RETRY_STATUS and attempt < retries:
                            ra = r.headers.get("Retry-After", "")
                            await asyncio.sleep(min(8.0, float(ra)) if ra.replace(".", "", 1).isdigit() else 2.0 * attempt)
                            break
                        if r.status_code >= 400:
                            raise httpx.HTTPStatusError(f"HTTP {r.status_code} from {urlparse(cur).hostname}",
                                                        request=r.request, response=r)
                        if int(r.headers.get("content-length") or 0) > max_bytes:
                            raise _too_big(max_bytes)
                        n = 0
                        reset()
                        async for chunk in r.aiter_bytes(1 << 20):
                            n += len(chunk)
                            if n > max_bytes:
                                raise _too_big(max_bytes)
                            sink(chunk)
                        return r, cur
                else:
                    raise ValueError("too many redirects")
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError):
            if attempt == retries:
                raise
            await asyncio.sleep(1.5 * attempt)
    raise ValueError(f"the server kept answering “too many requests” ({urlparse(url).hostname})")


async def download(url: str, dst: Path, max_bytes: int, headers: Optional[dict[str, str]] = None,
                   timeout: float = 120, retries: int = 3) -> tuple[str, int, str]:
    """Letöltés dst-be. Vissza: (fájlnév, méret, content-type). Túl nagy → ValueError; HTTP-hiba → ValueError."""
    with open(dst, "wb") as f:
        def reset() -> None:
            f.seek(0)
            f.truncate()
        try:
            r, final = await _get(url, f.write, reset, max_bytes, headers, timeout, None, retries)
        except httpx.HTTPStatusError as e:
            raise ValueError(f"download failed: HTTP {e.response.status_code}") from None
        n = f.tell()
    return _filename(r, final), n, r.headers.get("content-type", "")


async def fetch(url: str, max_bytes: int = 30 * 1024 * 1024, headers: Optional[dict[str, str]] = None,
                timeout: float = 60, params: Optional[dict[str, Any]] = None, retries: int = 3) -> Fetched:
    """Letöltés memóriába (képek, cikkek, API-válaszok). HTTP-hiba → httpx.HTTPStatusError (a hívó dönt)."""
    buf = bytearray()
    r, final = await _get(url, buf.extend, buf.clear, max_bytes, headers, timeout, params, retries)
    return Fetched(bytes(buf), _filename(r, final), r.headers.get("content-type", ""), final, r.status_code)


async def post_json(url: str, body: bytes, headers: dict[str, str], timeout: float = 15) -> int:
    await check_url(url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
        r = await c.post(url, content=body, headers={"Content-Type": "application/json", "User-Agent": user_agent(), **headers})
        return r.status_code
