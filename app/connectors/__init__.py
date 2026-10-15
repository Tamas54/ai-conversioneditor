"""Felhős import/export: S3 (és S3-kompatibilis), Azure Blob, Google Cloud Storage, SFTP, Google Drive, Dropbox, OneDrive.

A hitelesítő adatokat a hívó adja (a task paramétereiben), mi nem tároljuk őket: csak a task idejére élnek,
és a task nyilvános nézetéből kimaradnak. SDK nélkül, httpx-szel (S3 SigV4 és GCS JWT saját aláírással),
az SFTP Paramikóval.

    fetch(provider, spec, max_bytes, tmpdir) → (útvonal, fájlnév)
    push(provider, spec, file_ids)           → [{file_id, filename, size, location}]
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

import httpx

PROVIDERS = ("s3", "azure", "gcs", "sftp", "google-drive", "dropbox", "onedrive")
_REQUIRED = {
    "s3": (("bucket", "key"), ("bucket",)),
    "azure": (("container", "blob"), ("container",)),
    "gcs": (("bucket", "object"), ("bucket",)),
    "sftp": (("host", "username", "path"), ("host", "username", "path")),
    "google-drive": (("file_id", "access_token"), ("access_token",)),
    "dropbox": (("path", "access_token"), ("path", "access_token")),
    "onedrive": (("access_token",), ("access_token",)),
}


def validate_spec(provider: str, p: dict[str, Any], export: bool = False) -> None:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider}")
    need = _REQUIRED[provider][1 if export else 0]
    missing = [k for k in need if not p.get(k)]
    if missing:
        raise ValueError(f"missing: {', '.join(missing)}")
    if provider == "s3" and not (p.get("access_key_id") and p.get("secret_access_key")) and not p.get("public"):
        raise ValueError("access_key_id and secret_access_key are required (or public: true for a public object)")
    if provider == "azure" and not (p.get("sas_token") or p.get("url") or p.get("connection_string")):
        raise ValueError("give sas_token (+ account), a SAS url, or connection_string")
    if provider == "gcs" and not (p.get("credentials") or p.get("access_token")):
        raise ValueError("credentials (service-account JSON) or access_token is required")
    if provider == "sftp" and not (p.get("password") or p.get("private_key")):
        raise ValueError("password or private_key is required")
    if provider == "onedrive" and not export and not (p.get("item_id") or p.get("path")):
        raise ValueError("item_id or path is required")
    for k in ("endpoint", "url"):
        if p.get(k) and not str(p[k]).startswith("https://") and not os.getenv("ALLOW_PRIVATE_URLS"):
            raise ValueError(f"{k} must be https")


# ======================================================================
# S3 — AWS Signature Version 4
# ======================================================================

def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _hm(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(method: str, url: str, region: str, access_key: str, secret: str, payload_hash: str,
                  session_token: Optional[str] = None, now: Optional[dt.datetime] = None, service: str = "s3",
                  extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """AWS SigV4 fejlécek (Authorization, x-amz-date, x-amz-content-sha256)."""
    u = urlparse(url)
    now = now or dt.datetime.now(dt.timezone.utc)
    amz = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    headers = {"host": u.netloc, "x-amz-content-sha256": payload_hash, "x-amz-date": amz, **{k.lower(): v for k, v in (extra or {}).items()}}
    if session_token:
        headers["x-amz-security-token"] = session_token
    signed = ";".join(sorted(headers))
    canon_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers))
    q = "&".join(sorted(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in
                        (x.split("=", 1) if "=" in x else (x, "") for x in u.query.split("&") if x)))
    canon = "\n".join([method, quote(u.path or "/", safe="/-_.~"), q, canon_headers, signed, payload_hash])
    scope = f"{date}/{region}/{service}/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, _sha(canon.encode())])
    k = _hm(_hm(_hm(_hm(("AWS4" + secret).encode(), date), region), service), "aws4_request")
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    out = {k2: v for k2, v in headers.items() if k2 != "host"}
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return out


def _s3_url(p: dict[str, Any], key: str) -> str:
    region = p.get("region") or "us-east-1"
    k = quote(key.lstrip("/"), safe="/-_.~")
    if p.get("endpoint"):
        return f"{p['endpoint'].rstrip('/')}/{p['bucket']}/{k}"          # útvonal-stílus (MinIO, R2, Wasabi…)
    return f"https://{p['bucket']}.s3.{region}.amazonaws.com/{k}"


# ======================================================================
# GCS — szolgáltatásfiók JWT → access token
# ======================================================================

async def _gcs_token(p: dict[str, Any], scope: str = "https://www.googleapis.com/auth/devstorage.read_write") -> str:
    if p.get("access_token"):
        return p["access_token"]
    creds = p["credentials"]
    if isinstance(creds, str):
        creds = json.loads(creds)
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    now = int(time.time())
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claim = b64(json.dumps({"iss": creds["client_email"], "scope": scope, "aud": "https://oauth2.googleapis.com/token",
                            "iat": now, "exp": now + 3600}).encode())
    key = serialization.load_pem_private_key(creds["private_key"].encode(), password=None)
    sig = b64(key.sign(f"{head}.{claim}".encode(), padding.PKCS1v15(), hashes.SHA256()))
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": f"{head}.{claim}.{sig}"})
        if r.status_code >= 300:
            raise ValueError(f"Google auth failed: {r.text[:200]}")
        return r.json()["access_token"]


# ======================================================================
# Azure Blob
# ======================================================================

def _azure_url(p: dict[str, Any], blob: str) -> str:
    if p.get("url"):
        base, _, sas = p["url"].partition("?")
        return f"{base.rstrip('/')}/{quote(blob)}?{sas}" if not base.rstrip("/").endswith(quote(blob)) else p["url"]
    account = p.get("account")
    sas = p.get("sas_token", "").lstrip("?")
    if p.get("connection_string") and not account:
        parts = dict(x.split("=", 1) for x in p["connection_string"].split(";") if "=" in x)
        account = parts.get("AccountName")
        sas = sas or parts.get("SharedAccessSignature", "")
    if not account:
        raise ValueError("account (storage account name) is required")
    return f"https://{account}.blob.core.windows.net/{p['container']}/{quote(blob)}" + (f"?{sas}" if sas else "")


def _azure_sharedkey(p: dict[str, Any], method: str, url: str, length: int, ctype: str = "") -> dict[str, str]:
    """Connection string AccountKey-jével SharedKey-aláírás (ha nincs SAS)."""
    parts = dict(x.split("=", 1) for x in p["connection_string"].split(";") if "=" in x)
    account, key = parts["AccountName"], base64.b64decode(parts["AccountKey"])
    date = dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    xms = {"x-ms-date": date, "x-ms-version": "2021-08-06"}
    if method == "PUT":
        xms["x-ms-blob-type"] = "BlockBlob"
    u = urlparse(url)
    canon_h = "".join(f"{k}:{v}\n" for k, v in sorted(xms.items()))
    canon_r = f"/{account}{u.path}"
    sts = "\n".join([method, "", "", str(length) if length else "", "", ctype, "", "", "", "", "", ""]) + "\n" + canon_h + canon_r
    sig = base64.b64encode(hmac.new(key, sts.encode(), hashlib.sha256).digest()).decode()
    return {**xms, "Authorization": f"SharedKey {account}:{sig}"}


# ======================================================================
# letöltés / feltöltés
# ======================================================================

async def _stream_to(c: httpx.AsyncClient, method: str, url: str, dst: Path, max_bytes: int, **kw) -> httpx.Response:
    async with c.stream(method, url, **kw) as r:
        if r.status_code >= 300:
            body = (await r.aread())[:300]
            raise ValueError(f"HTTP {r.status_code}: {body.decode('utf-8', 'replace')}")
        n = 0
        with open(dst, "wb") as f:
            async for chunk in r.aiter_bytes(1 << 20):
                n += len(chunk)
                if n > max_bytes:
                    raise ValueError(f"the file is larger than {max_bytes // (1024 * 1024)} MB")
                f.write(chunk)
        return r


async def fetch(provider: str, p: dict[str, Any], max_bytes: int, tmpdir: Path,
                transport: Optional[httpx.AsyncBaseTransport] = None) -> tuple[Path, str]:
    validate_spec(provider, p)
    dst = tmpdir / "cloud"
    if provider == "sftp":
        name = await asyncio.to_thread(_sftp_get, p, dst, max_bytes)
        return dst, name
    async with httpx.AsyncClient(timeout=300, transport=transport, follow_redirects=True) as c:
        if provider == "s3":
            url = _s3_url(p, p["key"])
            await _guard(p, url)
            h = {} if p.get("public") else sigv4_headers("GET", url, p.get("region") or "us-east-1", p["access_key_id"],
                                                         p["secret_access_key"], _sha(b""), p.get("session_token"))
            await _stream_to(c, "GET", url, dst, max_bytes, headers=h)
            return dst, Path(p["key"]).name
        if provider == "azure":
            url = _azure_url(p, p["blob"])
            await _guard(p, url)
            h = {"x-ms-version": "2021-08-06"}
            if p.get("connection_string") and "AccountKey=" in p["connection_string"] and not p.get("sas_token"):
                h = _azure_sharedkey(p, "GET", url, 0)
            await _stream_to(c, "GET", url, dst, max_bytes, headers=h)
            return dst, Path(p["blob"]).name
        if provider == "gcs":
            tok = await _gcs_token(p)
            url = f"https://storage.googleapis.com/storage/v1/b/{quote(p['bucket'], safe='')}/o/{quote(p['object'], safe='')}?alt=media"
            await _stream_to(c, "GET", url, dst, max_bytes, headers={"Authorization": f"Bearer {tok}"})
            return dst, Path(p["object"]).name
        if provider == "google-drive":
            h = {"Authorization": f"Bearer {p['access_token']}"}
            meta = await c.get(f"https://www.googleapis.com/drive/v3/files/{quote(p['file_id'])}",
                               params={"fields": "name,mimeType", "supportsAllDrives": "true"}, headers=h)
            if meta.status_code >= 300:
                raise ValueError(f"Google Drive: HTTP {meta.status_code}")
            m = meta.json()
            name, mime = m.get("name") or "drive-file", m.get("mimeType", "")
            if mime.startswith("application/vnd.google-apps."):          # Google Docs/Sheets/Slides → Office export
                kind = mime.rsplit(".", 1)[-1]
                exp = {"document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
                       "spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
                       "presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"),
                       "drawing": ("image/png", "png")}.get(kind, ("application/pdf", "pdf"))
                await _stream_to(c, "GET", f"https://www.googleapis.com/drive/v3/files/{quote(p['file_id'])}/export", dst, max_bytes,
                                 params={"mimeType": exp[0]}, headers=h)
                return dst, f"{name}.{exp[1]}"
            await _stream_to(c, "GET", f"https://www.googleapis.com/drive/v3/files/{quote(p['file_id'])}", dst, max_bytes,
                             params={"alt": "media", "supportsAllDrives": "true"}, headers=h)
            return dst, name
        if provider == "dropbox":
            await _stream_to(c, "POST", "https://content.dropboxapi.com/2/files/download", dst, max_bytes,
                             headers={"Authorization": f"Bearer {p['access_token']}", "Dropbox-API-Arg": json.dumps({"path": p["path"]})})
            return dst, Path(p["path"]).name
        if provider == "onedrive":
            base = "https://graph.microsoft.com/v1.0/me/drive"
            url = f"{base}/items/{quote(p['item_id'])}/content" if p.get("item_id") else f"{base}/root:/{quote(p['path'].lstrip('/'))}:/content"
            r = await _stream_to(c, "GET", url, dst, max_bytes, headers={"Authorization": f"Bearer {p['access_token']}"})
            name = Path(p.get("path") or "").name or "onedrive-file"
            return dst, name
    raise ValueError(f"unknown provider {provider}")


async def push(provider: str, p: dict[str, Any], file_ids: list[str],
               transport: Optional[httpx.AsyncBaseTransport] = None) -> list[dict[str, Any]]:
    from app.storage import display_label
    from app.tools import crafts
    validate_spec(provider, p, export=True)
    out = []
    async with httpx.AsyncClient(timeout=600, transport=transport) as c:
        for fid in file_ids:
            src = crafts.existing(fid)
            name = display_label(fid)
            data = src.read_bytes()
            loc = await _push_one(c, provider, p, name, data)
            out.append({"file_id": fid, "filename": name, "size": len(data), "location": loc})
    return out


def _target_name(p: dict[str, Any], name: str, key: str) -> str:
    """A cél neve: a megadott kulcs/útvonal; ha az / jelre végződik (mappa), a fájlnév hozzáfűzve."""
    v = p.get(key) or ""
    return v + name if (not v or v.endswith("/")) else v


async def _push_one(c: httpx.AsyncClient, provider: str, p: dict[str, Any], name: str, data: bytes) -> str:
    if provider == "s3":
        key = _target_name(p, name, "key")
        url = _s3_url(p, key)
        await _guard(p, url)
        h = sigv4_headers("PUT", url, p.get("region") or "us-east-1", p["access_key_id"], p["secret_access_key"], _sha(data),
                          p.get("session_token"), extra={"content-type": "application/octet-stream"})
        r = await c.put(url, content=data, headers=h)
        _ok(r, "S3")
        return f"s3://{p['bucket']}/{key}"
    if provider == "azure":
        blob = _target_name(p, name, "blob")
        url = _azure_url(p, blob)
        await _guard(p, url)
        h = {"x-ms-blob-type": "BlockBlob", "x-ms-version": "2021-08-06"}
        if p.get("connection_string") and "AccountKey=" in p["connection_string"] and not p.get("sas_token"):
            h = _azure_sharedkey(p, "PUT", url, len(data), "application/octet-stream")
        r = await c.put(url, content=data, headers={**h, "Content-Type": "application/octet-stream"})
        _ok(r, "Azure")
        return url.split("?", 1)[0]
    if provider == "gcs":
        obj = _target_name(p, name, "object")
        tok = await _gcs_token(p)
        r = await c.post(f"https://storage.googleapis.com/upload/storage/v1/b/{quote(p['bucket'], safe='')}/o",
                         params={"uploadType": "media", "name": obj}, content=data,
                         headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/octet-stream"})
        _ok(r, "GCS")
        return f"gs://{p['bucket']}/{obj}"
    if provider == "sftp":
        path = _target_name(p, name, "path")
        await asyncio.to_thread(_sftp_put, p, path, data)
        return f"sftp://{p['host']}{path if path.startswith('/') else '/' + path}"
    if provider == "google-drive":
        meta = {"name": p.get("filename") or name, **({"parents": [p["folder_id"]]} if p.get("folder_id") else {})}
        boundary = "aiceboundary"
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--".encode()
        r = await c.post("https://www.googleapis.com/upload/drive/v3/files", params={"uploadType": "multipart", "supportsAllDrives": "true"},
                         content=body, headers={"Authorization": f"Bearer {p['access_token']}",
                                                "Content-Type": f"multipart/related; boundary={boundary}"})
        _ok(r, "Google Drive")
        return f"gdrive://{r.json().get('id')}"
    if provider == "dropbox":
        path = _target_name(p, name, "path")
        r = await c.post("https://content.dropboxapi.com/2/files/upload", content=data,
                         headers={"Authorization": f"Bearer {p['access_token']}", "Content-Type": "application/octet-stream",
                                  "Dropbox-API-Arg": json.dumps({"path": path, "mode": "overwrite" if p.get("overwrite") else "add",
                                                                 "autorename": not p.get("overwrite")})})
        _ok(r, "Dropbox")
        return f"dropbox://{r.json().get('path_display', path)}"
    if provider == "onedrive":
        folder = (p.get("path") or "").strip("/")
        target = f"{folder}/{name}" if folder and not p.get("path", "").endswith(tuple("." + e for e in ("pdf", "docx", "jpg", "png", "zip", "mp4"))) else (folder or name)
        url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{quote(target)}:/content"
        r = await c.put(url, content=data, headers={"Authorization": f"Bearer {p['access_token']}", "Content-Type": "application/octet-stream"})
        _ok(r, "OneDrive")
        return f"onedrive://{target}"
    raise ValueError(f"unknown provider {provider}")


async def _guard(p: dict[str, Any], url: str) -> None:
    """A hívó adta végpont (S3-kompatibilis endpoint, Azure SAS-URL) nem mutathat belső címre."""
    if p.get("endpoint") or p.get("url"):
        from app import net
        await net.check_url(url)


def _ok(r: httpx.Response, what: str) -> None:
    if r.status_code >= 300:
        raise ValueError(f"{what}: HTTP {r.status_code} {r.text[:200]}")


# ======================================================================
# SFTP (Paramiko)
# ======================================================================

def _check_host(host: str) -> None:
    import ipaddress
    import socket
    if os.getenv("ALLOW_PRIVATE_URLS", "").lower() in ("1", "true", "yes"):
        return
    for info in socket.getaddrinfo(host, 22, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("internal addresses are not allowed")


def _sftp(p: dict[str, Any]):
    import io
    import paramiko
    pkey = None
    if p.get("private_key"):
        for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
            try:
                pkey = cls.from_private_key(io.StringIO(p["private_key"]), password=p.get("passphrase"))
                break
            except Exception:
                continue
    _check_host(p["host"])
    t = paramiko.Transport((p["host"], int(p.get("port") or 22)))
    t.banner_timeout = 30
    t.connect(username=p["username"], password=p.get("password"), pkey=pkey)
    if p.get("host_key_fingerprint"):
        fp = hashlib.sha256(t.get_remote_server_key().asbytes()).hexdigest()
        if fp != p["host_key_fingerprint"].lower().replace(":", ""):
            t.close()
            raise ValueError("SFTP host key fingerprint mismatch")
    return t, paramiko.SFTPClient.from_transport(t)


def _sftp_get(p: dict[str, Any], dst: Path, max_bytes: int) -> str:
    t, s = _sftp(p)
    try:
        if s.stat(p["path"]).st_size > max_bytes:
            raise ValueError(f"the file is larger than {max_bytes // (1024 * 1024)} MB")
        s.get(p["path"], str(dst))
        return Path(p["path"]).name
    finally:
        s.close()
        t.close()


def _sftp_put(p: dict[str, Any], path: str, data: bytes) -> None:
    import io
    t, s = _sftp(p)
    try:
        s.putfo(io.BytesIO(data), path)
    finally:
        s.close()
        t.close()
