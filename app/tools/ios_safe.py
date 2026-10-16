"""Minden kimenet iPhone-on is menjen (Fájlok/Quick Look, Safari, Keynote/Pages/Numbers, Fotók).

Egy kapu, kiterjesztés szerint:
  pptx-család   → keynote_safe (jegyzetmester, sldSz, nyomtató-csonk, tengely-id…)
  docx/xlsx     → ooxml_safe (sémasorrend, „#szín”, „16.0”, w:shd típus, w:nsid)
  epub          → Apple Books: „mimetype” elöl, tömörítetlenül
  minden OOXML  → [Content_Types].xml a csomag első bejegyzése (a Word így ír; a szigorú, folyamként olvasó
                  nézegetők ezt várják)
  fb2/xml/svg,  → az XML 1.0-ban tiltott vezérlőkarakterek (pl. a PDF-szöveg lapdobása, &#12;) ki — különben
  fodt/fods…      a szigorú olvasók (Apple Books, e-könyv-olvasók) meg sem nyitják
  mp4/m4v/mov,  → csak ami kell, és amennyire kell:
  m4a/m4b/m4r,      • moov a fájl végén (az iPhone/Safari csak az egész letöltése után játszaná) → +faststart
  3gp/3g2           • HEVC „hev1” címkével (az Apple csak „hvc1”-et játszik) → -tag:v hvc1
                    • H.264 nem 4:2:0 / nem 8 bites (High 4:4:4, High 10 — az iPhone nem dekódolja),
                      páratlan méret, HEVC 4:2:2/4:4:4, VP9/AV1/Theora/MPEG-2… MP4-ben → H.264 4:2:0 újrakódolás
                    • Opus/Vorbis/FLAC/MP2/WMA hang MP4/M4A-ban → AAC
                    Ami jó, azt csak átcsomagoljuk (-c copy), ami rendben van, ahhoz hozzá sem nyúlunk.
A felhasználó kifejezett kodekválasztását (video_codec / audio_codec, „copy” is) és saját ffmpeg-kapcsolóit
tiszteletben tartjuk: azt nem kódoljuk újra, csak a veszteségmentes javítások (faststart, hvc1) futnak. Hiba esetén a kimenet marad, ahogy volt.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import struct
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("aice.ios")

MEDIA_EXTS = {"mp4", "m4v", "mov", "m4a", "m4b", "m4r", "3gp", "3g2"}
_MUX = {"mp4": "mp4", "m4v": "mp4", "mov": "mov", "m4a": "ipod", "m4b": "ipod", "m4r": "ipod", "3gp": "3gp", "3g2": "3g2"}
_AUDIO_OK = {"aac", "mp3", "alac", "ac3", "eac3"}
_AUDIO_OK_MOV = _AUDIO_OK | {"pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be", "pcm_f32le"}
_H264_PIX = {"yuv420p", "yuvj420p"}
_HEVC_PIX = {"yuv420p", "yuvj420p", "yuv420p10le"}
_VIDEO_KEEP = {"mpeg4", "mjpeg", "prores"}          # az Apple lejátszói ismerik (a ProRes-t kifejezetten kérik)


def _ext(p: Path) -> str:
    from app.catalog.formats import from_filename
    return from_filename(p.name) or p.suffix.lstrip(".").lower()


def moov_first(path: Path) -> Optional[bool]:
    """A felső szintű dobozok sorrendje: True, ha a moov az mdat előtt van (faststart); None, ha nem ISO-BMFF."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            pos = 0
            seen_mdat = False
            while pos + 8 <= size:
                f.seek(pos)
                head = f.read(16)
                n, typ = struct.unpack(">I4s", head[:8])
                if n == 1 and len(head) >= 16:
                    n = struct.unpack(">Q", head[8:16])[0]
                elif n == 0:
                    n = size - pos
                if n < 8:
                    return None
                if typ == b"moov":
                    return not seen_mdat
                if typ == b"mdat":
                    seen_mdat = True
                pos += n
    except (OSError, struct.error):
        return None
    return None


def plan(info: dict[str, Any], fmt: str, moov_ok: Optional[bool], keep_video: bool = False,
         keep_audio: bool = False) -> tuple[list[str], list[str]]:
    """ffprobe-eredmény → (ffmpeg kodek-argumentumok, mit javít). Üres javításlista: nincs teendő."""
    streams = info.get("streams") or []
    vids = [s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]
    auds = [s for s in streams if s.get("codec_type") == "audio"]
    fixes: list[str] = []
    vargs: list[str] = ["-c:v", "copy"]
    aargs: list[str] = ["-c:a", "copy"]
    if vids:
        v = vids[0]
        codec, pix, tag = v.get("codec_name"), v.get("pix_fmt") or "", (v.get("codec_tag_string") or "").lower()
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        reencode = False
        if codec == "h264":
            reencode = pix not in _H264_PIX or w % 2 or h % 2
        elif codec == "hevc":
            if pix not in _HEVC_PIX:
                reencode = True
            elif tag != "hvc1":
                vargs = ["-c:v", "copy", "-tag:v", "hvc1"]
                fixes.append("HEVC tagged hvc1")
        elif codec not in _VIDEO_KEEP:
            reencode = True
        if reencode and not keep_video:
            vargs = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                     "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
            fixes.append(f"video {codec}/{pix} → H.264 4:2:0")
    if auds:
        a = auds[0]
        ok = _AUDIO_OK_MOV if fmt == "mov" else _AUDIO_OK
        if a.get("codec_name") not in ok and not keep_audio:
            aargs = ["-c:a", "aac", "-b:a", "192k"]
            fixes.append(f"audio {a.get('codec_name')} → AAC")
    if moov_ok is False:
        fixes.append("faststart")
    return (vargs + aargs if fixes else []), fixes


async def media(path: Path, opts: Optional[dict[str, Any]] = None) -> list[str]:
    """Egy MP4/MOV/M4A… kimenet iPhone-biztossá tétele helyben. → mit javított."""
    from app.convert.engines import run
    from app.convert.steps import VIDEO_TIMEOUT, probe
    o = opts or {}
    fmt = _ext(path)
    if fmt not in MEDIA_EXTS:
        return []
    try:
        info = await probe(path)
        manual = bool(o.get("advanced"))           # saját ffmpeg-kapcsolók: a kódolás a felhasználóé
        codec_args, fixes = plan(info, fmt, await asyncio.to_thread(moov_first, path),
                                 keep_video=manual or o.get("video_codec") not in (None, "auto"),
                                 keep_audio=manual or o.get("audio_codec") not in (None, "auto"))
        if not fixes:
            return []
        tmp = path.with_name(f".ios-{path.name}")
        args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
                "-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-map_metadata", "0", "-map_chapters", "0",
                *codec_args, "-c:s", "mov_text", "-movflags", "+faststart", "-f", _MUX[fmt], str(tmp)]
        try:
            await run(*args, timeout=VIDEO_TIMEOUT)
            if not tmp.exists() or tmp.stat().st_size == 0:
                raise RuntimeError("empty output")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return fixes
    except Exception as e:      # kényelmi javítás — a kész kimenetet nem buktatja
        log.warning("iPhone pass skipped for %s: %s", path.name, e)
        return []


def epub_ocf(path: Path) -> list[str]:
    """Apple Books: a „mimetype” az első bejegyzés, tömörítetlenül, „application/epub+zip” tartalommal."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            first = infos[0] if infos else None
            ok = (first is not None and first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
                  and not first.extra and zf.read("mimetype").strip() == b"application/epub+zip")
            if ok or "META-INF/container.xml" not in zf.namelist():
                return []
            tmp = path.with_name(f".ios-{path.name}")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo:
                zo.writestr(zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0)), b"application/epub+zip",
                            compress_type=zipfile.ZIP_STORED)
                for i in infos:
                    if i.filename == "mimetype" or i.filename.endswith("/"):
                        continue
                    ni = zipfile.ZipInfo(i.filename, date_time=i.date_time)
                    ni.compress_type = zipfile.ZIP_DEFLATED
                    zo.writestr(ni, zf.read(i.filename))
        os.replace(tmp, path)
        return ["EPUB mimetype first"]
    except Exception as e:
        log.warning("EPUB pass skipped for %s: %s", path.name, e)
        Path(path.with_name(f".ios-{path.name}")).unlink(missing_ok=True)
        return []


XML_EXTS = {"fb2", "xml", "svg", "fodt", "fods", "fodp", "fodg", "opml", "xhtml", "kml", "gpx"}
_BAD_REF = re.compile(rb"&#(?:[xX]0*(?:[0-8bBcCeEfF]|1[0-9a-fA-F])|0*(?:[0-8]|1[124-9]|2[0-9]|3[01]));")
_BAD_CHR = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def xml_clean(path: Path) -> list[str]:
    """Az XML 1.0-ban tiltott vezérlőkarakterek (nyersen vagy &#12;-ként) eltávolítva. UTF-16-hoz nem nyúl."""
    try:
        b = path.read_bytes()
        if b[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in b[:256]:
            return []
        nb = _BAD_CHR.sub(b"", _BAD_REF.sub(b"", b))
        if nb == b:
            return []
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".xml-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(nb)
        os.replace(tmp, path)
        return ["invalid XML characters removed"]
    except Exception as e:
        log.warning("XML pass skipped for %s: %s", path.name, e)
        return []


def ct_first(path: Path) -> list[str]:
    """OOXML: a [Content_Types].xml legyen az első bejegyzés (a LibreOffice a végére teszi)."""
    from app.tools.keynote_safe import _rewrite
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if not names or names[0] == "[Content_Types].xml" or "[Content_Types].xml" not in names:
                return []
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ct-")
            try:
                with os.fdopen(fd, "wb") as fh:
                    _rewrite(zf, fh, {}, set())
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        os.replace(tmp, path)
        return ["[Content_Types].xml first"]
    except Exception as e:
        log.warning("OOXML order pass skipped for %s: %s", path.name, e)
        return []


async def finalize(path: Path, opts: Optional[dict[str, Any]] = None) -> list[str]:
    """Bármely kimenetre hívható: a formátumának megfelelő iPhone-javítás, vagy semmi."""
    from app.tools import keynote_safe, ooxml_safe
    path = Path(path)
    fmt = _ext(path)
    if fmt in keynote_safe.EXTS:
        return await asyncio.to_thread(keynote_safe.fix, path) + await asyncio.to_thread(ct_first, path)
    if fmt in ooxml_safe.DOCX_EXTS | ooxml_safe.XLSX_EXTS:
        return await asyncio.to_thread(ooxml_safe.fix, path) + await asyncio.to_thread(ct_first, path)
    if fmt in XML_EXTS:
        return await asyncio.to_thread(xml_clean, path)
    if fmt == "epub":
        return await asyncio.to_thread(epub_ocf, path)
    if fmt in MEDIA_EXTS:
        return await media(path, opts)
    return []
