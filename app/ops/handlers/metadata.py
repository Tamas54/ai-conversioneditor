"""Metaadat: olvasás (bármely fájl), szerkesztés és törlés.

    kép        ExifTool (ha nincs: Pillow újramentés metaadat nélkül); az ICC-profil és a tájolás megmarad
    videó/hang FFmpeg stream-másolással (-c copy) — újrakódolás nélkül, minőségvesztés nincs
    PDF        pikepdf: dokumentum-infó és XMP
    DOCX/XLSX/PPTX  a docProps/core.xml (és app.xml, custom.xml) átírása a csomagban

Szerkeszthető mezők (egységes nevekkel): title, author, subject, keywords, description, comment, copyright, date.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.catalog.formats import FORMATS, from_filename
from app.convert.engines import available, run
from app.convert.options import OptionError
from app.ops.handlers import out_name

FIELDS = ("title", "author", "subject", "keywords", "description", "comment", "copyright", "date")
_HIDE_GROUPS = {"System", "ExifTool", "File:System"}
_HIDE_KEYS = {"SourceFile", "Directory", "FileName", "FilePermissions", "FileAccessDate", "FileInodeChangeDate",
              "FileModifyDate"}

# egységes mező → ExifTool-címke (kép), FFmpeg-kulcs (videó/hang), PDF-infó kulcs
_EXIF = {"title": "Title", "author": "Artist", "subject": "Subject", "keywords": "Keywords",
         "description": "ImageDescription", "comment": "UserComment", "copyright": "Copyright", "date": "DateTimeOriginal"}
_FF = {"title": "title", "author": "artist", "subject": "album", "keywords": "genre", "description": "description",
       "comment": "comment", "copyright": "copyright", "date": "date"}
_PDF = {"title": "/Title", "author": "/Author", "subject": "/Subject", "keywords": "/Keywords",
        "description": "/Subject", "comment": "/Subject", "copyright": "/Copyright", "date": "/CreationDate"}
_OOXML = {"title": "dc:title", "author": "dc:creator", "subject": "dc:subject", "keywords": "cp:keywords",
          "description": "dc:description", "comment": "dc:description", "copyright": "dc:rights", "date": "dcterms:created"}
_NS = {"cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
       "dc": "http://purl.org/dc/elements/1.1/", "dcterms": "http://purl.org/dc/terms/",
       "dcmitype": "http://purl.org/dc/dcmitype/", "xsi": "http://www.w3.org/2001/XMLSchema-instance"}


def _kind(p: Path) -> str:
    fmt = from_filename(p.name) or ""
    if fmt == "pdf":
        return "pdf"
    if fmt in ("docx", "xlsx", "pptx", "docm", "xlsm", "pptm", "dotx", "potx", "ppsx"):
        return "ooxml"
    cat = FORMATS[fmt].category if fmt in FORMATS else ""
    return {"image": "image", "video": "media", "audio": "media"}.get(cat, "other")


def _fields(o: dict[str, Any]) -> dict[str, str]:
    raw = o.get("fields") or {}
    bad = [k for k in raw if k not in FIELDS]
    if bad:
        raise OptionError(f"fields: unknown {', '.join(bad)}. Allowed: {', '.join(FIELDS)}")
    out = {}
    for k, v in raw.items():
        v = "" if v is None else str(v)
        if len(v) > 2000 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", v):
            raise OptionError(f"fields.{k}: plain text up to 2000 characters")
        out[k] = v
    if not out:
        raise OptionError("fields: give at least one of " + ", ".join(FIELDS))
    return out


# ---------- olvasás ----------

async def _exiftool_read(p: Path) -> dict[str, Any]:
    out, _ = await run("exiftool", "-json", "-G1", "-charset", "filename=utf8", "-api", "LargeFileSupport=1",
                       str(p), timeout=120)
    data = json.loads(out.decode("utf-8", "replace") or "[]")
    rec = data[0] if data else {}
    groups: dict[str, dict[str, Any]] = {}
    for k, v in rec.items():
        grp, _, tag = k.partition(":") if ":" in k else ("", "", k)
        if grp in _HIDE_GROUPS or tag in _HIDE_KEYS or k in _HIDE_KEYS:
            continue
        groups.setdefault(grp or "Other", {})[tag] = v
    return groups


def _py_read(p: Path) -> dict[str, Any]:
    kind = _kind(p)
    res: dict[str, Any] = {}
    if kind == "pdf":
        import pikepdf
        with pikepdf.open(str(p)) as pdf:
            res["PDF"] = {"Version": pdf.pdf_version, "Pages": len(pdf.pages), "Encrypted": pdf.is_encrypted,
                          **{str(k).lstrip("/"): str(v) for k, v in pdf.docinfo.items()}}
            try:
                with pdf.open_metadata() as meta:
                    res["XMP"] = {k: str(v) for k, v in meta.items()}
            except Exception:
                pass
    elif kind == "ooxml":
        res.update(_ooxml_read(p))
    elif kind == "image":
        from PIL import ExifTags, Image
        from app.convert.steps import _register_plugins
        _register_plugins()
        with Image.open(p) as im:
            res["Image"] = {"Format": im.format, "Width": im.width, "Height": im.height, "Mode": im.mode,
                            "Frames": getattr(im, "n_frames", 1)}
            ex = im.getexif()
            if ex:
                res["EXIF"] = {ExifTags.TAGS.get(k, str(k)): (v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v))
                               for k, v in ex.items()}
    return res


async def _ffprobe(p: Path) -> dict[str, Any]:
    out, _ = await run("ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p), timeout=60)
    d = json.loads(out.decode("utf-8", "replace") or "{}")
    fmt = d.get("format", {})
    fmt.pop("filename", None)
    return {"Container": {k: v for k, v in fmt.items() if k != "tags"}, "Tags": fmt.get("tags", {}),
            "Streams": [{k: s[k] for k in ("index", "codec_type", "codec_name", "width", "height", "sample_rate",
                                             "channels", "duration", "bit_rate", "avg_frame_rate") if k in s}
                        for s in d.get("streams", [])]}


def _ooxml_read(p: Path) -> dict[str, Any]:
    from xml.etree import ElementTree as ET
    out: dict[str, Any] = {}
    with zipfile.ZipFile(p) as z:
        for part, grp in (("docProps/core.xml", "Core"), ("docProps/app.xml", "App"), ("docProps/custom.xml", "Custom")):
            if part not in z.namelist():
                continue
            root = ET.fromstring(z.read(part))
            vals = {}
            for el in root.iter():
                if el is root:
                    continue
                tag = el.tag.split("}")[-1]
                txt = (el.text or "").strip()
                if grp == "Custom" and tag == "property":
                    vals[el.get("name", "?")] = "".join(x.text or "" for x in el)
                elif txt and len(el) == 0:
                    vals[tag] = txt
            if vals:
                out[grp] = vals
    return out


async def read(inputs: list[Path], o: dict[str, Any], out: Path) -> dict[str, Any]:
    src = inputs[0]
    kind = _kind(src)
    data: dict[str, Any] = {"file": {"name": src.name.split("__", 1)[-1], "format": from_filename(src.name),
                                     "size_bytes": src.stat().st_size}}
    if available("exiftool"):
        try:
            data["metadata"] = await _exiftool_read(src)
        except Exception:
            data["metadata"] = {}
    else:
        data["metadata"] = {}
    if kind in ("pdf", "ooxml") or (kind == "image" and not data["metadata"]):
        try:
            data["metadata"].update(await asyncio.to_thread(_py_read, src))
        except Exception:
            pass
    if kind == "media" and available("ffmpeg"):
        try:
            data["media"] = await _ffprobe(src)
        except Exception:
            pass
    dst = out_name(out, src, "-metadata", "json")
    dst.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), "utf-8")
    return {"files": [dst], "data": data}


# ---------- szerkesztés, törlés: formátumonként ----------

def _pdf_write(src: Path, dst: Path, fields: dict[str, str] | None) -> None:
    import pikepdf
    with pikepdf.open(str(src)) as pdf:
        if fields is None:
            for k in list(pdf.docinfo.keys()):
                del pdf.docinfo[k]
            if "/Metadata" in pdf.Root:
                del pdf.Root.Metadata
            for page in pdf.pages:
                if "/PieceInfo" in page.obj:
                    del page.obj.PieceInfo
            if "/PieceInfo" in pdf.Root:
                del pdf.Root.PieceInfo
        else:
            with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                for k, v in fields.items():
                    if k == "title":
                        meta["dc:title"] = v
                    elif k == "author":
                        meta["dc:creator"] = [v]
                    elif k in ("subject", "description", "comment"):
                        meta["dc:description"] = v
                    elif k == "keywords":
                        meta["pdf:Keywords"] = v
                    elif k == "copyright":
                        meta["dc:rights"] = v
            for k, v in fields.items():
                if k == "date":
                    try:
                        dt = datetime.fromisoformat(v)
                    except ValueError:
                        raise OptionError("fields.date: use YYYY-MM-DD or an ISO date-time")
                    pdf.docinfo["/CreationDate"] = dt.strftime("D:%Y%m%d%H%M%S")
                else:
                    pdf.docinfo[_PDF[k]] = v
        pdf.save(str(dst))


def _ooxml_write(src: Path, dst: Path, fields: dict[str, str] | None) -> None:
    with zipfile.ZipFile(src) as zin:
        names = zin.namelist()
        core = zin.read("docProps/core.xml").decode("utf-8") if "docProps/core.xml" in names else None
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "docProps/core.xml":
                    data = _core_xml(core, fields).encode("utf-8")
                elif item.filename == "docProps/app.xml" and fields is None:
                    data = re.sub(rb"<(Company|Manager|HyperlinkBase|Template)>[^<]*</\1>", b"", data)
                elif item.filename == "docProps/custom.xml" and fields is None:
                    data = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties xmlns="http://schemas.'
                            b'openxmlformats.org/officeDocument/2006/custom-properties" xmlns:vt="http://schemas.'
                            b'openxmlformats.org/officeDocument/2006/docPropsVTypes"/>')
                zout.writestr(item, data)


def _core_xml(core: str | None, fields: dict[str, str] | None) -> str:
    from xml.sax.saxutils import escape
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    vals: dict[str, str] = {}
    if fields is None:
        vals = {"dcterms:created": now, "dcterms:modified": now}
    else:
        if core:
            for tag in ("dc:title", "dc:creator", "dc:subject", "cp:keywords", "dc:description", "dc:rights",
                        "cp:lastModifiedBy", "dcterms:created", "dcterms:modified", "cp:revision", "cp:category"):
                m = re.search(rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>", core, re.S)
                if m:
                    vals[tag] = m.group(1)
        for k, v in fields.items():
            vals[_OOXML[k]] = escape(v)
        vals["dcterms:modified"] = now
    body = []
    for tag, v in vals.items():
        attr = ' xsi:type="dcterms:W3CDTF"' if tag.startswith("dcterms:") else ""
        body.append(f"<{tag}{attr}>{v}</{tag}>")
    ns = " ".join(f'xmlns:{k}="{u}"' for k, u in _NS.items())
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<cp:coreProperties {ns}>{"".join(body)}</cp:coreProperties>'


async def _media_write(src: Path, dst: Path, fields: dict[str, str] | None) -> None:
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(src), "-map", "0", "-c", "copy"]
    if fields is None:
        args += ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact"]
    else:
        args += ["-map_metadata", "0"]
        for k, v in fields.items():
            args += ["-metadata", f"{_FF[k]}={v}"]
    if dst.suffix.lower() in (".mp4", ".mov", ".m4a", ".m4v", ".3gp"):
        args += ["-movflags", "+faststart" + ("+use_metadata_tags" if fields else "")]
    await run(*args, str(dst), timeout=1800)


async def _image_write(src: Path, dst: Path, fields: dict[str, str] | None) -> None:
    shutil.copy(src, dst)
    if available("exiftool"):
        if fields is None:
            await run("exiftool", "-overwrite_original", "-q", "-m", "-all=", "--icc_profile:all",
                      "-tagsfromfile", "@", "-Orientation", str(dst), timeout=120)
        else:
            args = ["exiftool", "-overwrite_original", "-q", "-m", "-charset", "exif=utf8", "-codedcharacterset=utf8"]
            for k, v in fields.items():
                tag = _EXIF[k]
                if k == "date":
                    try:
                        v = datetime.fromisoformat(v).strftime("%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        raise OptionError("fields.date: use YYYY-MM-DD or an ISO date-time")
                args.append(f"-{tag}={v}")
                if k in ("title", "author", "description", "copyright"):
                    args.append(f"-XMP-dc:{ {'title': 'Title', 'author': 'Creator', 'description': 'Description', 'copyright': 'Rights'}[k]}={v}")
            await run(*args, str(dst), timeout=120)
        return
    if fields is not None:
        raise OptionError("editing image metadata needs ExifTool, which isn't installed on this server")
    from app.ops.handlers.image import _fmt, _open, _save

    def go():
        im, info = _open(src)
        info.pop("exif", None)
        _save(im, dst, _fmt(src), {"strip_metadata": True}, {k: v for k, v in info.items() if k == "icc_profile"})
    await asyncio.to_thread(go)


async def _write(src: Path, dst: Path, fields: dict[str, str] | None) -> None:
    kind = _kind(src)
    if kind == "pdf":
        await asyncio.to_thread(_pdf_write, src, dst, fields)
    elif kind == "ooxml":
        await asyncio.to_thread(_ooxml_write, src, dst, fields)
    elif kind == "media":
        await _media_write(src, dst, fields)
    elif kind == "image":
        await _image_write(src, dst, fields)
    else:
        raise OptionError(f"metadata of .{from_filename(src.name)} files can't be changed")


async def edit(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    fields = _fields(o)
    res = []
    for src in inputs:
        dst = out_name(out, src)
        await _write(src, dst, fields)
        res.append(dst)
    return res


async def remove(inputs: list[Path], o: dict[str, Any], out: Path) -> list[Path]:
    res = []
    for src in inputs:
        dst = out_name(out, src, "-clean")
        await _write(src, dst, None)
        res.append(dst)
    return res
