"""Konverziós és eszköz-opciók: séma (a weboldal, az OpenAPI és az MCP-leírás ebből dolgozik) és szigorú validálás.

Minden érték típus- és tartomány-ellenőrzésen megy át, mielőtt egy motor parancssorába kerülne: szöveges
értéket csak felsorolásból vagy szűk mintára illeszkedően fogadunk el (nincs shell, nincs fájlútvonal, nincs
szabad ffmpeg-szűrő). Ismeretlen opció → hiba (elírás ne vesszen el csendben).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.catalog.formats import FORMATS


class OptionError(ValueError):
    pass


@dataclass(frozen=True)
class Opt:
    key: str
    type: str                       # int, float, bool, enum, str, list, dict, time, file
    default: Any = None
    choices: tuple = ()
    min: Optional[float] = None
    max: Optional[float] = None
    pattern: str = ""
    categories: tuple = ()          # mely célkategóriáknál értelmes (konverziós opciók)

    def schema(self) -> dict[str, Any]:
        t = {"int": "integer", "float": "number", "bool": "boolean", "enum": "string", "str": "string",
             "list": "array", "dict": "object", "time": "string", "file": "string"}[self.type]
        s: dict[str, Any] = {"type": t}
        if self.choices:
            s["enum"] = list(self.choices)
        if self.min is not None:
            s["minimum"] = self.min
        if self.max is not None:
            s["maximum"] = self.max
        if self.pattern:
            s["pattern"] = self.pattern
        if self.default is not None:
            s["default"] = self.default
        return s


_TIME = r"^(\d+(\.\d+)?|\d{1,2}:\d{2}(:\d{2})?(\.\d+)?)$"
_BITRATE = r"^\d{1,6}(\.\d{1,3})?[kKmM]?$"
_RES = r"^(\d{2,5}x\d{2,5}|2160p|1440p|1080p|720p|480p|360p|240p|4k)$"
_ASPECT = r"^\d{1,2}:\d{1,2}$"
_PAGES = r"^\s*(\d+|\d+\s*-\s*\d*|-\s*\d+|last)(\s*,\s*(\d+|\d+\s*-\s*\d*|-\s*\d+|last))*\s*$"
_COLOR = r"^(#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?([0-9a-fA-F]{2})?|[a-zA-Z]{3,20}|transparent)$"
_LANGS = r"^[a-z]{3}(_[a-z]{3,4})?(\+[a-z]{3}(_[a-z]{3,4})?){0,4}$"
_VOLUME = r"^(\d{1,2}(\.\d{1,3})?|[+-]?\d{1,2}(\.\d)?dB)$"

_DOC = ("document", "spreadsheet", "presentation", "ebook")
_IMGV = ("image", "vector", "cad")
_AV = ("video", "audio")

OPTIONS: dict[str, Opt] = {o.key: o for o in [
    # közös / dokumentum
    Opt("engine", "enum", "auto", ("auto", "libreoffice", "pandoc", "calibre", "wordcraft", "pillow", "imagemagick",
                                   "vips", "inkscape", "rsvg", "ffmpeg", "ghostscript", "mupdf", "poppler")),
    Opt("page_range", "str", None, pattern=_PAGES, categories=_DOC + ("image",)),
    Opt("ocr", "enum", "auto", ("auto", "on", "off"), categories=("document",)),
    Opt("ocr_language", "str", "eng", pattern=_LANGS, categories=_DOC + ("image",)),
    Opt("title", "str", None, pattern=r"^[^\x00-\x1f]{1,200}$", categories=("ebook", "document")),
    Opt("author", "str", None, pattern=r"^[^\x00-\x1f]{1,200}$", categories=("ebook", "document")),
    Opt("cover", "file", None, categories=("ebook",)),
    Opt("output_profile", "enum", "default", ("default", "kindle", "kindle_pw3", "kobo", "tablet", "generic_eink"), categories=("ebook",)),
    Opt("sheet", "str", None, pattern=r"^[^\x00-\x1f/\\]{1,80}$", categories=("spreadsheet", "document")),
    Opt("delimiter", "enum", "auto", ("auto", ",", ";", "tab", "|"), categories=("spreadsheet", "document")),
    Opt("seconds_per_slide", "float", 3.0, min=0.5, max=60, categories=("video",)),
    # kép
    Opt("width", "int", None, min=1, max=20000, categories=_IMGV + _AV),
    Opt("height", "int", None, min=1, max=20000, categories=_IMGV + _AV),
    Opt("fit", "enum", "inside", ("inside", "cover", "contain", "fill"), categories=_IMGV),
    Opt("quality", "int", None, min=1, max=100, categories=("image",)),
    Opt("strip_metadata", "bool", False, categories=("image", "video", "audio")),
    Opt("dpi", "int", None, min=36, max=1200, categories=_IMGV + ("document",)),
    Opt("background", "str", None, pattern=_COLOR, categories=_IMGV),
    Opt("grayscale", "bool", False, categories=("image",)),
    Opt("lossless", "bool", False, categories=("image",)),
    Opt("frame", "int", 0, min=0, max=10000, categories=("image",)),
    Opt("threshold", "int", 128, min=1, max=254, categories=("vector",)),
    Opt("text_to_path", "bool", False, categories=("vector",)),
    # videó / hang
    Opt("video_codec", "enum", "auto", ("auto", "h264", "h265", "vp9", "av1", "mpeg4", "mpeg2", "prores", "copy"), categories=("video",)),
    Opt("audio_codec", "enum", "auto", ("auto", "aac", "mp3", "opus", "vorbis", "flac", "pcm", "ac3", "copy", "none"), categories=_AV),
    Opt("resolution", "str", None, pattern=_RES, categories=("video",)),
    Opt("aspect", "str", None, pattern=_ASPECT, categories=("video", "image")),
    Opt("aspect_mode", "enum", "pad", ("pad", "crop", "stretch"), categories=("video",)),
    Opt("fps", "float", None, min=1, max=120, categories=("video", "image")),
    Opt("crf", "int", None, min=0, max=51, categories=("video",)),
    Opt("preset", "enum", "medium", ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"), categories=("video",)),
    Opt("video_bitrate", "str", None, pattern=_BITRATE, categories=("video",)),
    Opt("audio_bitrate", "str", None, pattern=_BITRATE, categories=_AV),
    Opt("sample_rate", "int", None, min=8000, max=192000, categories=_AV),
    Opt("channels", "int", None, min=1, max=8, categories=_AV),
    Opt("bit_depth", "enum", None, (16, 24, 32), categories=("audio",)),
    Opt("start", "time", None, pattern=_TIME, categories=_AV),
    Opt("end", "time", None, pattern=_TIME, categories=_AV),
    Opt("rotate", "enum", None, (0, 90, 180, 270), categories=("video", "image")),
    Opt("flip", "enum", None, ("horizontal", "vertical"), categories=("video", "image")),
    Opt("subtitles", "file", None, categories=("video",)),
    Opt("subtitles_mode", "enum", "burn", ("burn", "soft"), categories=("video",)),
    Opt("volume", "str", None, pattern=_VOLUME, categories=_AV),
    Opt("remove_audio", "bool", False, categories=("video",)),
    Opt("at", "time", None, pattern=_TIME, categories=("image",)),
    Opt("loop", "bool", True, categories=("image", "video")),
    # archívum, betű, CAD
    Opt("password", "str", None, pattern=r"^[^\x00-\x1f]{1,128}$", categories=("archive", "document")),
    Opt("level", "int", None, min=0, max=9, categories=("archive",)),
    Opt("font_index", "int", 0, min=0, max=64, categories=("font",)),
    Opt("subset_text", "str", None, pattern=r"^[^\x00-\x08]{1,5000}$", categories=("font",)),
    # haladó, engedélyezőlistás paraméterek (app/convert/advanced.py ellenőrzi)
    Opt("advanced", "list", None, categories=("video", "audio", "image")),
    # kimenet
    Opt("output", "enum", "auto", ("auto", "zip", "files"), categories=()),
    Opt("filename", "str", None, pattern=r"^[^\x00-\x1f/\\]{1,180}$", categories=()),
]}

# eszköz-opciók (app/catalog/tools.py) — a konverziósakon felül
TOOL_OPTIONS: dict[str, Opt] = {o.key: o for o in [
    Opt("compression", "enum", "recommended", ("low", "recommended", "extreme")),
    Opt("max_width", "int", None, min=16, max=20000),
    Opt("colors", "int", 256, min=2, max=256),
    Opt("target_size_mb", "float", None, min=0.1, max=10000),
    Opt("page_ranges", "list", None),
    Opt("mode", "enum", "ranges", ("ranges", "every", "each")),
    Opt("every", "int", 1, min=1, max=10000),
    Opt("angle", "enum", 90, (90, 180, 270, -90)),
    Opt("expand", "bool", True),
    Opt("direction", "enum", "horizontal", ("horizontal", "vertical")),
    Opt("order", "str", None, pattern=_PAGES),
    Opt("margins_mm", "list", None),
    Opt("page_size", "enum", "A4", ("A3", "A4", "A5", "B5", "Letter", "Legal", "Tabloid")),
    Opt("fit_page", "enum", "fit", ("fit", "fill", "stretch")),
    Opt("pdfa_level", "enum", "2b", ("1b", "2b", "3b")),
    Opt("deskew", "bool", True),
    Opt("owner_password", "str", None, pattern=r"^[^\x00-\x1f]{1,128}$"),
    Opt("allow_print", "bool", True), Opt("allow_copy", "bool", True), Opt("allow_edit", "bool", False),
    Opt("allow_annotate", "bool", True),
    Opt("what", "enum", "all", ("all", "forms", "annotations")),
    Opt("min_size", "int", 32, min=1, max=10000),
    Opt("annotations", "list", None), Opt("edits", "list", None),
    Opt("words", "list", None), Opt("patterns", "list", None),
    Opt("position", "enum", "center", ("center", "top-left", "top", "top-right", "left", "right", "bottom-left", "bottom", "bottom-right", "tile")),
    Opt("start_number", "int", 1, min=0, max=100000),
    Opt("text_format", "str", "{n}", pattern=r"^[^\x00-\x1f]{1,40}$"),
    Opt("percent", "float", None, min=1, max=1000),
    Opt("box", "list", None),
    Opt("scale", "enum", 2, (2, 3, 4)),
    Opt("columns", "int", None, min=1, max=20), Opt("gap", "int", 8, min=0, max=200),
    Opt("text", "str", None, pattern=r"^[^\x00-\x08]{1,300}$"),
    Opt("image_file", "file", None),
    Opt("opacity", "float", 0.3, min=0.02, max=1),
    Opt("rotation", "float", 0, min=-360, max=360),
    Opt("font_size", "int", None, min=4, max=400),
    Opt("audio_format", "enum", "mp3", ("mp3", "m4a", "wav", "flac", "ogg", "opus", "aac")),
    Opt("loops", "int", 1, min=1, max=100),
    Opt("burn", "bool", True), Opt("language", "str", None, pattern=r"^[A-Za-z\- ]{2,40}$"),
    Opt("fade_in", "float", None, min=0, max=60), Opt("fade_out", "float", None, min=0, max=60),
    Opt("normalize", "bool", False),
    Opt("url", "str", None, pattern=r"^https?://\S{3,2000}$"),
    Opt("full_page", "bool", True), Opt("wait_ms", "int", 500, min=0, max=15000),
    Opt("image_format", "enum", "png", ("png", "jpg", "webp", "pdf")),
    Opt("output_format", "str", None, pattern=r"^[a-z0-9.]{1,10}$"),
    Opt("page", "int", 1, min=1, max=100000),
    Opt("archive_format", "enum", "zip", ("zip", "7z", "tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst")),
    Opt("fields", "dict", None),
    Opt("args", "list", None),
    Opt("keep_look", "bool", True),
    Opt("values", "dict", None), Opt("flatten", "bool", False), Opt("data_file", "file", None),
    Opt("name", "str", None, pattern=r"^[^\x00-\x1f]{1,80}$"),
    Opt("kind", "enum", "signature", ("signature", "initials")),
    Opt("color", "str", None, pattern=_COLOR),
    Opt("signature_id", "str", None, pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    Opt("placement", "enum", "auto", ("auto", "field", "xy", "anchor")),
    Opt("x", "float", None, min=0, max=100000), Opt("y", "float", None, min=0, max=100000),
    Opt("field", "str", None, pattern=r"^[^\x00-\x1f]{1,200}$"),
    Opt("profile", "dict", None), Opt("source_files", "list", None), Opt("answers", "dict", None),
    Opt("schema", "dict", None),
    Opt("deep", "enum", "auto", ("auto", "on", "off")),
    Opt("image_base64", "str", None, pattern=r"^(data:image/(png|jpeg|webp);base64,)?[A-Za-z0-9+/=\s]{16,3000000}$"),
    Opt("value", "float", None), Opt("from_unit", "str", None, pattern=r"^[A-Za-z0-9_°/\-.² ³µ]{1,30}$"),
    Opt("to_unit", "str", None, pattern=r"^[A-Za-z0-9_°/\-.² ³µ]{1,30}$"),
    Opt("time", "str", None, pattern=r"^[0-9T:\- .+Z]{1,40}$"),
    Opt("from_zone", "str", None, pattern=r"^[A-Za-z_+\-/0-9]{1,40}$"),
    Opt("to_zones", "list", None),
]}


def _coerce(o: Opt, v: Any) -> Any:
    if v is None:
        return None
    t = o.type
    try:
        if t == "int":
            if isinstance(v, bool):
                raise ValueError
            v = int(float(v)) if not isinstance(v, int) else v
        elif t == "float":
            if isinstance(v, bool):
                raise ValueError
            v = float(v)
        elif t == "bool":
            if isinstance(v, str):
                v = v.strip().lower() in ("1", "true", "yes", "on", "igen")
            else:
                v = bool(v)
        elif t == "enum":
            if v not in o.choices:
                alt = [c for c in o.choices if str(c) == str(v)]
                if not alt:
                    raise OptionError(f"{o.key}: one of {', '.join(map(str, o.choices))}")
                v = alt[0]
        elif t in ("str", "time", "file"):
            v = str(v)
            if o.pattern and not re.match(o.pattern, v):
                raise OptionError(f"{o.key}: invalid value")
            if t == "file" and not re.fullmatch(r"[A-Za-z0-9_\-]{1,80}\.[A-Za-z0-9.]{1,12}", v):
                raise OptionError(f"{o.key}: give a file_id")
        elif t == "list":
            if not isinstance(v, (list, tuple)):
                raise OptionError(f"{o.key}: a list is expected")
            v = list(v)[:1000]
        elif t == "dict":
            if not isinstance(v, dict):
                raise OptionError(f"{o.key}: an object is expected")
    except OptionError:
        raise
    except (TypeError, ValueError):
        raise OptionError(f"{o.key}: expected {t}")
    if t in ("int", "float"):
        if o.min is not None and v < o.min or o.max is not None and v > o.max:
            raise OptionError(f"{o.key}: between {o.min:g} and {o.max:g}")
    return v


def validate(opts: Optional[dict[str, Any]], tool: bool = False) -> dict[str, Any]:
    """Ismeretlen kulcs → OptionError; a None értékek kimaradnak."""
    if not opts:
        return {}
    if not isinstance(opts, dict):
        raise OptionError("options must be an object")
    out: dict[str, Any] = {}
    for k, v in opts.items():
        o = (TOOL_OPTIONS.get(k) if tool else None) or OPTIONS.get(k) or (TOOL_OPTIONS.get(k) if not tool else None)
        if o is None:
            raise OptionError(f"unknown option: {k}")
        cv = _coerce(o, v)
        if cv is not None:
            out[k] = cv
    if "advanced" in out:
        from app.convert import advanced
        try:
            advanced.check_any(out["advanced"])
        except advanced.AdvancedError as e:
            raise OptionError(str(e))
    return out


def for_target(dst: str) -> list[Opt]:
    """A célformátumhoz illő konverziós opciók (a weboldal és a dokumentáció számára)."""
    f = FORMATS.get(dst)
    cat = f.category if f else ""
    return [o for o in OPTIONS.values() if not o.categories or cat in o.categories]


def json_schema(keys: Optional[list[str]] = None, tool: bool = False) -> dict[str, Any]:
    src = {**OPTIONS, **TOOL_OPTIONS} if tool else OPTIONS
    if keys is not None:
        src = {k: (TOOL_OPTIONS.get(k) or OPTIONS[k]) for k in keys if k in TOOL_OPTIONS or k in OPTIONS}
    return {"type": "object", "additionalProperties": False, "properties": {k: o.schema() for k, o in src.items()}}


# ---------- oldalszám-tartomány ----------

def parse_pages(spec: Optional[str], n: int) -> list[int]:
    """"1-3,5,8-" → 0-alapú oldalindexek (n oldalon belül, sorrendtartó, ismétlés nélkül)."""
    if not spec:
        return list(range(n))
    out: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if part == "last":
            a = b = n
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            a = int(a_s) if a_s else 1
            b = int(b_s) if b_s else n
        else:
            a = b = int(part)
        for p in range(max(1, a), min(n, b) + 1):
            if p - 1 not in out:
                out.append(p - 1)
    if not out:
        raise OptionError(f"page_range selects no pages (the document has {n})")
    return out


_PAGED = {"pdf", "djvu", "xps", "ps", "pdfa"}


def for_pair(src: str, dst: str) -> list[Opt]:
    """A párhoz ténylegesen értelmes opciók (a weboldal widgetje és táblázata ezt mutatja)."""
    fs, fd = FORMATS.get(src), FORMATS.get(dst)
    if not fs or not fd:
        return []
    sc, dc = fs.category, fd.category
    keys: list[str] = ["engine"]
    paged = src in _PAGED or sc in ("document", "presentation", "ebook", "spreadsheet")
    if paged and (dst in _PAGED or dc == "image"):
        keys.append("page_range")
    if src in ("pdf", "djvu") and dc in ("document", "ebook") and dst not in _PAGED:
        keys += ["ocr", "ocr_language"]
    if dc == "ebook" or (dc == "document" and dst in ("epub", "docx", "odt", "pdf") and sc == "ebook"):
        keys += ["title", "author", "cover", "output_profile"]
    if sc == "spreadsheet" or src in ("csv", "tsv", "xlsx"):
        keys += ["delimiter", "sheet"] if dst in ("csv", "tsv", "json", "xml", "yaml", "md", "html", "xlsx", "ods") else []
    if sc == "presentation" and dc == "video":
        keys += ["seconds_per_slide", "width"]
    if dc == "image":
        keys += ["width", "height", "fit", "quality", "strip_metadata", "background", "grayscale", "rotate", "flip"]
        if dst in ("webp", "jxl"):
            keys.append("lossless")
        if sc in ("document", "presentation", "vector", "cad", "ebook"):
            keys.append("dpi")
        if sc == "video" or src in ("gif", "webp", "apng"):
            keys += ["start", "end", "fps", "loop"] if dst in ("gif", "webp", "apng") else ["at"]
        if src in ("gif", "tiff", "ico", "icns", "psd") and dst not in ("gif", "webp", "apng"):
            keys.append("frame")
        if sc == "image":
            keys.append("advanced")
    if dc == "vector":
        keys += ["text_to_path"] + (["threshold"] if sc == "image" else [])
        keys += ["width", "height", "dpi", "background"] if dst in ("png",) else []
    if dc == "video":
        keys += ["video_codec", "audio_codec", "resolution", "aspect", "aspect_mode", "fps", "crf", "preset", "video_bitrate",
                 "audio_bitrate", "start", "end", "rotate", "flip", "subtitles", "subtitles_mode", "volume", "remove_audio",
                 "strip_metadata", "advanced"]
        if sc == "audio":
            keys = ["engine", "cover", "resolution", "audio_bitrate", "start", "end", "volume"]
    if dc == "audio":
        keys += ["audio_codec", "audio_bitrate", "sample_rate", "channels", "start", "end", "volume", "strip_metadata", "advanced"]
        if dst in ("wav", "aiff", "flac"):
            keys.append("bit_depth")
    if dc == "archive":
        keys += ["password", "level"]
    if dc == "font":
        keys += ["subset_text"] + (["font_index"] if src == "ttc" else [])
    if dc == "cad" or sc == "cad":
        keys += ["dpi", "background"] if dc != "cad" else []
    if dc == "document" and dst == "pdf" and sc in ("document", "spreadsheet", "presentation"):
        keys.append("page_range") if "page_range" not in keys else None
    seen, out = set(), []
    for k in keys:
        if k in OPTIONS and k not in seen:
            seen.add(k)
            out.append(OPTIONS[k])
    return out


def engine_choices(src: str, dst: str) -> list[str]:
    """Mely motorok jöhetnek szóba ennél a párnál (az `engine` opció választéka)."""
    from app.convert import graph
    base = graph.plan(src, dst, static=True)
    if not base:
        return []
    found = {e.engine for e in base}
    for eng in OPTIONS["engine"].choices:
        if eng == "auto" or eng in found:
            continue
        p = graph.plan(src, dst, static=True, prefer=eng)
        if p and any(e.engine == eng for e in p):
            found.add(eng)
    return ["auto"] + sorted(e for e in found if e in OPTIONS["engine"].choices)
