"""Helyadat (GPS) eltávolítása — megosztáskor és exportkor (OMLETT 4, adatvédelem).

    videó (MP4/MOV/M4V/3GP, MKV/WebM)   ffmpeg -c copy: a hely-címkék (location, location-eng, ©xyz,
                                         com.apple.quicktime.location.ISO6709 …) üresre — a forgatás (display matrix),
                                         a létrehozás ideje és a többi metaadat marad; az adat-sávok (iPhone mebx) kimaradnak
    fotó (JPEG, HEIC, PNG, TIFF, WebP…)  ExifTool: EXIF GPS-blokk + XMP/IPTC hely-mezők (veszteségmentes);
                                         ExifTool nélkül JPEG/PNG Pillow-val (a képpontok nem változnak)

Mérve (ffmpeg 4.4, a prod-kép): a SIMA ffmpeg-átkódolás alapból ÁTVISZI a `location` címkét a kimenetbe (a globális
metaadatot az első bemenetről másolja) — tehát „a pipeline kimenetei tiszták” csak a saját renderre (EffectCraft,
overlay) igaz, az ffmpeg-alapú eszközökre (resize-video, merge-files, ffmpeg-advanced…) nem. Ezért az export (letöltés,
felhő, Mentés a Fotókba) mindig szűr; kikapcsolni: STRIP_LOCATION=0.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

log = logging.getLogger("aice.privacy")

VIDEO = {"mp4", "mov", "m4v", "3gp", "3g2", "qt", "mkv", "webm"}
IMAGE = {"jpg", "jpeg", "heic", "heif", "png", "tif", "tiff", "webp", "avif", "dng"}
_MUX = {"mp4": "mp4", "m4v": "mp4", "mov": "mov", "qt": "mov", "3gp": "3gp", "3g2": "3g2", "mkv": "matroska", "webm": "webm"}
_LOC = re.compile(r"location|gps|iso6709|xyz", re.I)
# ExifTool: EXIF GPS-blokk, minden GPS*-címke (XMP-exif, QuickTime Keys/UserData), XMP/IPTC helyszín-mezők
_EXIF_ARGS = ["-gps:all=", "-gps*=", "-location*=", "-city=", "-country*=", "-sub-location=", "-province-state=", "-state="]
_EXIF_HIT = re.compile(r"^(GPS|Location|City|Country|Sub-location|Province-State|State)", re.I)


class LocationNotRemoved(RuntimeError):
    """Helyadat van a fájlban, de nem sikerült kivenni — exportkor ilyenkor a fájl nem megy ki (422)."""


def on_export() -> bool:
    return os.getenv("STRIP_LOCATION", "1").strip().lower() not in ("0", "false", "no", "off")


def kind(path: Path) -> Optional[str]:
    ext = path.suffix.lower().lstrip(".")
    return "video" if ext in VIDEO else "image" if ext in IMAGE else None


async def _run(*args: str, timeout: float = 600) -> bytes:
    from app.convert.engines import run
    out, _ = await run(*args, timeout=timeout)
    return out


async def location_tags(path: Path) -> list[str]:
    """Milyen helyadat van a fájlban (címke-nevek) — üres lista: tiszta."""
    k = kind(path)
    if k == "video":
        raw = await _run("ffprobe", "-v", "error", "-print_format", "json", "-show_entries", "format_tags:stream_tags",
                         str(path), timeout=60)
        info = json.loads(raw or b"{}")
        keys = [f"format:{t}" for t in ((info.get("format") or {}).get("tags") or {})]
        for i, s in enumerate(info.get("streams") or []):
            keys += [f"stream{i}:{t}" for t in (s.get("tags") or {})]
        return [x for x in keys if _LOC.search(x.split(":", 1)[1])]
    if k == "image":
        if shutil.which("exiftool"):
            raw = await _run("exiftool", "-j", "-a", "-G1", "-s", "-gps:all", "-gps*", "-location*", "-city", "-country*",
                             "-sub-location", "-province-state", str(path), timeout=60)
            rows = json.loads(raw or b"[{}]") or [{}]
            return sorted(k for k in rows[0] if k != "SourceFile" and not k.startswith("Composite:")
                          and _EXIF_HIT.match(k.split(":", 1)[-1]))
        return ["EXIF:GPS"] if await asyncio.to_thread(_pillow_has_gps, path) else []
    return []


async def strip(src: Path, dst: Optional[Path] = None, tags: Optional[list[str]] = None) -> bool:
    """A helyadat ki; dst nélkül helyben (atomi csere, a módosítás ideje marad — a verziósorrend nem változik).
    → volt-e mit kivenni. Hiba → kivétel (a hívó dönti el, megáll-e)."""
    tags = await location_tags(src) if tags is None else tags
    if not tags:
        if dst is not None and dst != src:
            shutil.copyfile(src, dst)
        return False
    out = dst or src.with_name(f".loc-{src.name}")
    try:
        if kind(src) == "video":
            ext = src.suffix.lower().lstrip(".")
            blanks: list[str] = []
            for t in tags:
                where, key = t.split(":", 1)
                blanks += ["-metadata" if where == "format" else f"-metadata:s:{where[6:]}", f"{key}="]
            args = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v?", "-map", "0:a?", "-map", "0:s?",
                    "-c", "copy", "-map_metadata", "0", *blanks]
            if _MUX[ext] in ("mp4", "mov", "3gp", "3g2"):
                args += ["-movflags", "+faststart"]
            await _run(*args, "-f", _MUX[ext], str(out), timeout=1800)
        elif shutil.which("exiftool"):
            if out != src:
                shutil.copyfile(src, out)
            await _run("exiftool", "-overwrite_original", "-q", "-m", *_EXIF_ARGS, str(out), timeout=120)
        else:
            await asyncio.to_thread(_pillow_strip, src, out)
        left = await location_tags(out)
        if left:
            raise RuntimeError(f"location still present after stripping: {', '.join(left)}")
        if dst is None:
            st = src.stat()
            os.replace(out, src)
            os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
    except BaseException:
        if dst is None:
            out.unlink(missing_ok=True)
        raise
    log.info("helyadat eltávolítva: %s (%s)", src.name, ", ".join(tags)[:200])
    return True


async def export_path(path: Path) -> Path:
    """Exportkor (letöltés, felhő, Mentés a Fotókba): a fájl helyadat nélkül. A döntés és a tiszta másolat a
    munkaterület temp/privacy mappájában (a fájl módosítási idejéhez és méretéhez kötve) — egyszer fut, utána olvasás."""
    if not on_export() or kind(path) is None:
        return path
    from app import tenancy
    st = path.stat()
    d = tenancy.user_root() / "temp" / "privacy"
    d.mkdir(parents=True, exist_ok=True)
    key = f"{path.stem}-{st.st_mtime_ns}-{st.st_size}"
    clean, ok = d / f"{key}{path.suffix}", d / f"{key}.ok"
    if ok.exists():
        return path
    if clean.exists():
        return clean
    try:
        tags = await location_tags(path)
    except Exception as e:                   # olvashatatlan metaadat: nincs mit kivenni belőle
        log.info("helyadat nem vizsgálható (%s): %s", path.name, e)
        tags = []
    if not tags:
        ok.touch()
        return path
    tmp = d / f".{key}-{os.getpid()}-{id(path)}{path.suffix}"
    try:
        await strip(path, tmp, tags)
    except Exception as e:                   # biztonság előbb: ha van helyadat, de nem jön ki, a fájl nem megy ki
        tmp.unlink(missing_ok=True)
        log.warning("helyadat-szűrés nem sikerült (%s): %s", path.name, e)
        raise LocationNotRemoved("This file carries location (GPS) data that couldn't be removed, so it isn't sent out. "
                                 "Strip its metadata first (file tool remove-metadata), then try again.") from e
    os.replace(tmp, clean)
    return clean


async def strip_files(owner: str, fids: list[str]) -> int:
    """Megosztáskor: a tulajdonos munkaterületén a megosztott család felvételeiből/fotóiból helyben ki a helyadat.
    → hány fájlból. Egy fájl hibája nem állítja meg a megosztást (naplózzuk)."""
    if not on_export():
        return 0
    from app import tenancy
    root, n = tenancy.user_root(owner), 0
    for fid in fids:
        for sub in ("inputs", "outputs"):
            p = root / sub / fid
            if p.is_file() and kind(p):
                try:
                    n += await strip(p)
                except Exception as e:
                    log.warning("megosztás: helyadat nem vehető ki (%s): %s", fid, e)
                break
    return n


# ---------- ExifTool nélkül: Pillow (JPEG: az EXIF-szegmens cseréje a képpontok érintése nélkül; PNG: újramentés) ----------

def _pillow_has_gps(path: Path) -> bool:
    from PIL import Image
    try:
        with Image.open(path) as im:
            return bool(im.getexif().get_ifd(0x8825))
    except Exception:
        return False


def _pillow_strip(src: Path, dst: Path) -> None:
    from PIL import Image
    data = src.read_bytes()
    if data[:2] == b"\xff\xd8":
        dst.write_bytes(_jpeg_without_gps(data))
        return
    with Image.open(src) as im:
        ex = im.getexif()
        ex.pop(0x8825, None)
        im.load()
        fmt = im.format or "PNG"
        im.save(dst, format=fmt, exif=ex.tobytes() if len(ex) else b"",
                **({"icc_profile": im.info["icc_profile"]} if im.info.get("icc_profile") else {}))


def _jpeg_without_gps(data: bytes) -> bytes:
    """A JPEG szegmensei közül az Exif APP1 GPS-blokk nélkül újraírva, a hely-adatos XMP kihagyva; a többi bájt marad."""
    from PIL import Image
    out, i = bytearray(data[:2]), 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xDA:                       # SOS: innen a tömörített kép, változatlanul
            break
        n = int.from_bytes(data[i + 2:i + 4], "big")
        seg = data[i:i + 2 + n]
        body = seg[4:]
        if marker == 0xE1 and body.startswith(b"Exif\x00\x00"):
            ex = Image.Exif()
            ex.load(body)
            ex.pop(0x8825, None)
            payload = ex.tobytes()
            seg = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
        elif marker == 0xE1 and body.startswith(b"http://ns.adobe.com/xap/1.0/") and re.search(rb"GPS|[Ll]ocation", body):
            seg = b""
        out += seg
        i += 2 + n
    out += data[i:]
    return bytes(out)
