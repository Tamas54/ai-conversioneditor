"""Konverziós gráf: közvetlen élek (forrás → cél, motor, költség), ebből legrövidebb út bármely párra.

Egy él = egy motor egy lépése. A terv legfeljebb 3 lépés (pl. DWG → DXF → SVG → PNG); a célok a forrás
kategóriája szerint szűrve (egy MP3-ból nem lesz DOCX). Két nézet:
    static=True   minden motor (az éles Docker-kép) — a weboldal, az OpenAPI, az MCP-leírás ebből dolgozik
    static=False  csak a gépen ténylegesen elérhető motorok — a futtatás ebből tervez
"""
from __future__ import annotations

import functools
import heapq
from dataclasses import dataclass
from typing import Iterable, Optional

from app.catalog.formats import FORMATS, by_category, normalize
from app.convert import engines


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    engine: str
    cost: float


def _keys(cat: str, exclude: Iterable[str] = ()) -> set[str]:
    ex = set(exclude)
    return {f.key for f in by_category(cat) if f.key not in ex}


# ---------- motoronkénti olvasó/író halmazok ----------

LO_WRITER_IN = {"doc", "docx", "docm", "dot", "dotx", "odt", "ott", "fodt", "rtf", "txt", "html", "pages", "wpd",
                "wps", "lwp", "abw", "sdw", "hwp"}
LO_WRITER_OUT = {"pdf", "docx", "doc", "odt", "ott", "fodt", "rtf", "txt", "html", "dotx", "dot", "docm", "epub"}
LO_CALC_IN = {"xls", "xlsx", "xlsm", "xlsb", "xlt", "xltx", "ods", "fods", "csv", "tsv", "numbers", "dif", "slk"}
LO_CALC_OUT = {"xlsx", "xls", "xltx", "xlt", "xlsm", "ods", "fods", "csv", "pdf", "html", "dif", "slk"}
LO_IMPRESS_IN = {"ppt", "pptx", "pptm", "pps", "ppsx", "ppsm", "pot", "potx", "potm", "odp", "fodp", "key"}
LO_IMPRESS_OUT = {"pptx", "ppt", "pptm", "pps", "ppsx", "pot", "potx", "odp", "fodp", "pdf", "html"}
LO_DRAW_IN = {"pub", "vsd", "vsdx", "cdr", "cgm", "odg", "fodg", "emf", "wmf"}
LO_DRAW_OUT = {"pdf", "odg", "fodg", "svg", "png", "emf", "wmf"}

PANDOC_IN = {"md", "rst", "tex", "html", "docx", "odt", "epub", "fb2", "org", "textile", "wiki", "ipynb"}
PANDOC_OUT = {"md", "rst", "tex", "html", "docx", "odt", "epub", "fb2", "org", "adoc", "textile", "wiki", "ipynb",
              "txt", "rtf"}

CALIBRE_IN = {"epub", "mobi", "azw", "azw3", "azw4", "fb2", "fbz", "lit", "lrf", "pdb", "pml", "prc", "rb", "snb",
              "tcr", "txtz", "htmlz", "cbz", "cbr", "cb7", "cbc", "chm", "docx", "odt", "rtf", "txt", "html", "md",
              "pdf", "oeb", "djvu"}
CALIBRE_OUT = {"epub", "mobi", "azw3", "azw", "prc", "fb2", "lit", "lrf", "pdb", "rb", "snb", "tcr", "txtz",
               "htmlz", "oeb", "pdf", "docx", "rtf", "txt"}

_CAMERA_RAW = {f.key for f in FORMATS.values() if "camera-raw" in f.tags}
PILLOW_IN = {"jpg", "png", "gif", "bmp", "tiff", "webp", "avif", "ico", "icns", "cur", "psd", "tga", "jp2", "jfif",
             "dds", "pcx", "ppm", "pgm", "pbm", "pnm", "sgi", "xbm", "fits", "apng"}
PILLOW_OUT = {"jpg", "png", "gif", "bmp", "tiff", "webp", "avif", "ico", "icns", "tga", "jp2", "jfif", "pcx", "ppm",
              "pgm", "pbm", "pnm", "sgi", "xbm", "dds", "apng", "pdf", "eps"}
IM_IN = {"jpg", "png", "gif", "bmp", "tiff", "webp", "ico", "cur", "psd", "xcf", "tga", "jp2", "jfif", "hdr",
         "dds", "pcx", "ppm", "pgm", "pbm", "pnm", "sgi", "xbm", "xpm", "wbmp", "fits", "heic", "heif", "avif"}
IM_OUT = {"jpg", "png", "gif", "bmp", "tiff", "webp", "ico", "psd", "tga", "jp2", "hdr", "pcx", "ppm", "pgm",
          "pbm", "pnm", "sgi", "xbm", "xpm", "wbmp", "fits", "cur", "pdf"}
HEIF_IN, HEIF_OUT = {"heic", "heif", "avif"}, {"heic", "heif"}
JXL_IN, JXL_OUT = {"jxl"}, {"jxl"}

# (az .ai PDF-kompatibilis: PDF-ként olvassuk/írjuk; a HPGL-t saját Python-kód kezeli — az Inkscape fej nélkül nem tudja)
INKSCAPE_IN = {"svg", "svgz", "eps", "pdf", "emf", "wmf", "cdr", "ps"}
INKSCAPE_OUT = {"svg", "svgz", "pdf", "ps", "eps", "png", "emf", "wmf", "odg"}

_VIDEO = _keys("video")
_AUDIO = _keys("audio")
FFMPEG_VIDEO_IN = _VIDEO | {"gif", "apng", "webp"}
FFMPEG_VIDEO_OUT = (_VIDEO - {"rm", "rmvb"}) | {"gif", "apng", "webp"}
FFMPEG_AUDIO_IN = _AUDIO
FFMPEG_AUDIO_OUT = _AUDIO - {"ape", "ra"}

ARCHIVE_IN = _keys("archive")
ARCHIVE_OUT = {"zip", "7z", "tar", "tar.gz", "tgz", "tar.bz2", "tbz2", "tbz", "tar.bz", "tar.xz", "txz", "tar.zst",
               "tar.7z", "tar.lzo", "tzo", "tar.z", "tz", "gz", "bz2", "bz", "xz", "zst", "lz", "lzma", "lzo", "z",
               "jar", "war", "cpio", "iso", "wim"}

FONTTOOLS_IN = {"ttf", "otf", "woff", "woff2", "ttc", "eot"}
FONTTOOLS_OUT = {"ttf", "otf", "woff", "woff2", "ttc", "eot"}
FONTFORGE_IN = {"ttf", "otf", "pfb", "pfa", "sfd", "dfont", "bdf", "woff"}
FONTFORGE_OUT = {"ttf", "otf", "pfb", "pfa", "sfd", "dfont", "bdf"}

DATA = {"json", "yaml", "xml", "csv", "tsv"}

# a kategóriák közti megengedett átjárás (forrás-kategória → cél-kategóriák)
CROSS: dict[str, set[str]] = {
    "document": {"document", "ebook", "image", "vector", "spreadsheet"},
    "spreadsheet": {"spreadsheet", "document", "image"},
    "presentation": {"presentation", "document", "image", "video"},
    "ebook": {"ebook", "document"},
    "image": {"image", "document", "vector", "video"},
    "vector": {"vector", "image", "document"},
    "video": {"video", "audio", "image"},
    "audio": {"audio", "video"},
    "archive": {"archive"},
    "cad": {"cad", "vector", "image", "document"},
    "font": {"font"},
}
# ami kategórián kívül is csak bizonyos célokra mehet (a fenti átjárás finomítása)
CROSS_ONLY: dict[tuple[str, str], set[str]] = {
    ("document", "image"): {"jpg", "png", "webp", "tiff", "gif", "bmp", "avif"},
    ("document", "vector"): {"svg", "eps"},
    ("document", "spreadsheet"): {"csv", "xlsx", "tsv"},        # csak adatformátumból (json, xml, yaml)
    ("spreadsheet", "document"): {"pdf", "html", "json", "xml", "yaml", "md", "txt"},
    ("spreadsheet", "image"): {"png", "jpg"},
    ("presentation", "document"): {"pdf", "html", "docx"},
    ("presentation", "image"): {"png", "jpg", "webp", "gif", "tiff"},
    ("presentation", "video"): {"mp4", "webm", "mov", "gif"},
    ("ebook", "document"): {"pdf", "docx", "rtf", "txt", "html", "odt", "md"},
    ("image", "document"): {"pdf"},
    ("image", "vector"): {"svg", "eps", "ai"},
    ("image", "video"): {"mp4", "webm"},
    ("vector", "document"): {"pdf", "ps"},
    ("video", "image"): {"gif", "apng", "webp", "jpg", "png"},
    ("audio", "video"): {"mp4", "webm", "mov", "mkv"},
    ("cad", "vector"): {"svg", "eps", "ai"},
    ("cad", "image"): {"png", "jpg", "webp", "tiff"},
    ("cad", "document"): {"pdf"},
}
# a dokumentum → táblázat csak adatformátumból
_DATA_TO_SHEET = {"json", "xml", "yaml"}
# kép → videó csak animált képből
_ANIMATED = {"gif", "apng", "webp"}


def _edges() -> list[Edge]:
    E: list[Edge] = []

    def add(srcs, dsts, engine, cost=1.0):
        for s in srcs:
            for d in dsts:
                if s != d:
                    E.append(Edge(s, d, engine, cost))

    # dokumentum
    add(LO_WRITER_IN, LO_WRITER_OUT, "libreoffice", 1.0)
    add({"zabw"}, {"abw"}, "python", 0.1)
    # Pandoc a jelölőnyelvekhez; irodai forrásból irodai célra a LibreOffice tartja jobban az elrendezést
    _office_pair = {"docx", "odt", "html"}, {"rtf", "odt", "docx", "html", "txt", "doc"}
    add(PANDOC_IN - _office_pair[0], PANDOC_OUT, "pandoc", 0.8)
    add(_office_pair[0], PANDOC_OUT - _office_pair[1], "pandoc", 0.8)
    add(_office_pair[0], _office_pair[1], "pandoc", 1.3)
    add({"html"}, {"pdf"}, "weasyprint", 0.6)
    add({"pdf"}, {"docx"}, "pdf2docx", 0.9)
    add({"pdf"}, {"txt"}, "poppler", 0.3)
    add({"pdf"}, {"png", "jpg", "tiff"}, "poppler", 0.4)
    add({"pdf"}, {"svg", "html"}, "mupdf", 0.9)
    add({"pdf"}, {"pdfa"}, "ghostscript", 0.8)
    add({"pdf"}, {"ps", "eps"}, "ghostscript", 0.6)
    add({"ps", "eps"}, {"pdf"}, "ghostscript", 0.5)
    add({"xps"}, {"pdf"}, "mupdf", 0.5)
    add({"djvu"}, {"pdf", "tiff"}, "djvulibre", 0.6)
    add({"djvu"}, {"txt"}, "djvulibre", 0.3)
    add({"eml"}, {"html", "txt"}, "python", 0.2)
    add({"hwpx"}, {"docx", "html", "txt"}, "python", 0.4)
    add({"xml"}, {"json", "yaml", "html", "txt"}, "python", 0.2)
    add({"json"}, {"yaml", "xml", "csv", "tsv", "xlsx", "html", "txt"}, "python", 0.2)
    add({"yaml"}, {"json", "xml", "csv", "xlsx", "txt"}, "python", 0.2)
    add({"csv", "tsv"}, {"json", "yaml", "xml", "md", "html", "csv", "tsv", "xlsx"}, "python", 0.2)
    add({"xlsx"}, {"json", "csv", "tsv", "md"}, "python", 0.3)
    add({"txt"}, {"md", "html"}, "python", 0.2)
    add({"pdfa"}, {"pdf"}, "python", 0.05)
    # táblázat, prezentáció, rajz
    add(LO_CALC_IN, LO_CALC_OUT, "libreoffice", 1.0)
    add({"csv"}, {"tsv"}, "python", 0.1)
    add(LO_IMPRESS_IN, LO_IMPRESS_OUT, "libreoffice", 1.0)
    add(LO_IMPRESS_IN, {"mp4", "webm", "mov", "gif"}, "slideshow", 2.0)
    add(LO_DRAW_IN, LO_DRAW_OUT, "libreoffice", 1.2)
    # e-könyv
    add(CALIBRE_IN, CALIBRE_OUT, "calibre", 1.5)
    add({"cbz", "cbr", "cb7"}, {"pdf"}, "python", 0.6)
    add({"pdf"}, {"cbz"}, "python", 0.8)
    # kép
    add(PILLOW_IN, PILLOW_OUT, "pillow", 0.5)
    add(IM_IN, IM_OUT, "imagemagick", 0.7)
    add(HEIF_IN, {"jpg", "png", "tiff", "webp"}, "heif", 0.5)
    add({"jpg", "png", "tiff", "webp", "bmp"}, HEIF_OUT, "heif", 0.5)
    add(JXL_IN, {"png", "jpg"}, "jxl", 0.5)
    add({"png", "jpg", "tiff", "webp", "bmp", "gif"}, JXL_OUT, "jxl", 0.5)
    add(_CAMERA_RAW, {"tiff"}, "libraw", 0.8)
    add({"png", "jpg", "bmp", "gif", "tiff", "webp", "pbm", "pgm", "ppm"}, {"svg", "eps"}, "potrace", 0.9)
    add({"gif", "apng", "webp"}, {"mp4", "webm"}, "ffmpeg", 1.0)
    add({"png", "jpg", "tiff", "bmp", "webp", "tga", "hdr"}, {"exr"}, "ffmpeg", 0.6)       # OpenEXR: az FFmpeg ír és olvas
    add({"exr"}, {"png", "tiff", "jpg", "hdr"}, "ffmpeg", 0.6)
    # vektor
    add(INKSCAPE_IN, INKSCAPE_OUT, "inkscape", 1.0)
    add({"svg"}, {"png", "pdf", "ps", "eps"}, "rsvg", 0.4)
    add({"svgz"}, {"svg"}, "python", 0.05)
    add({"pdf"}, {"ai"}, "python", 0.1)
    add({"ai"}, {"pdf"}, "python", 0.1)
    add({"plt"}, {"svg"}, "python", 0.6)
    add({"pdf"}, {"plt"}, "python", 1.5)
    add({"svg"}, {"svgz"}, "python", 0.05)
    add({"emz"}, {"emf"}, "python", 0.05)
    add({"wmz"}, {"wmf"}, "python", 0.05)
    add({"emf"}, {"emz"}, "python", 0.05)
    add({"wmf"}, {"wmz"}, "python", 0.05)
    # videó, hang
    add(FFMPEG_VIDEO_IN, FFMPEG_VIDEO_OUT, "ffmpeg", 1.0)
    add(_VIDEO, FFMPEG_AUDIO_OUT, "ffmpeg", 0.8)
    add(_VIDEO, {"jpg", "png"}, "ffmpeg", 0.6)
    add(FFMPEG_AUDIO_IN, FFMPEG_AUDIO_OUT, "ffmpeg", 0.8)
    add(_AUDIO, {"mp4", "webm", "mov", "mkv"}, "ffmpeg", 1.2)
    # archívum
    add(ARCHIVE_IN, ARCHIVE_OUT, "archive", 1.0)
    # CAD
    add({"dwg"}, {"dxf"}, "libredwg", 0.8)
    add({"dxf"}, {"dwg"}, "libredwg", 1.0)
    add({"dxf"}, {"svg", "pdf", "png", "jpg"}, "ezdxf", 1.0)
    # betű
    add(FONTTOOLS_IN, FONTTOOLS_OUT, "fonttools", 0.5)
    add(FONTFORGE_IN, FONTFORGE_OUT, "fontforge", 1.0)
    return [e for e in E if e.src in FORMATS and e.dst in FORMATS]


# az "archive" és "slideshow" összetett lépések: a hozzájuk kellő motorok
_COMPOSITE = {"archive": ("python",), "slideshow": ("libreoffice", "poppler", "ffmpeg")}


def engine_ok(engine: str) -> bool:
    if engine in _COMPOSITE:
        return all(engines.available(e) for e in _COMPOSITE[engine])
    return engines.available(engine)


# FFmpeg-célok, amelyekhez külső kódoló kell (nem minden ffmpeg-build tartalmazza): futásidőben ellenőrizve
FFMPEG_ENCODER = {"amr": "libopencore_amrnb", "gsm": "libgsm", "opus": "libopus", "weba": "libopus", "mp3": "libmp3lame",
                  "ogg": "libvorbis", "ogv": "libtheora", "webm": "libvpx-vp9", "hevc": "libx265", "mp4": "libx264"}


def edge_ok(e: Edge) -> bool:
    if not engine_ok(e.engine):
        return False
    if e.engine == "ffmpeg" and e.dst in FFMPEG_ENCODER:
        return engines.ffmpeg_encoder(FFMPEG_ENCODER[e.dst])
    return True


@functools.lru_cache(maxsize=2)
def _adjacency(static: bool) -> dict[str, list[Edge]]:
    adj: dict[str, list[Edge]] = {}
    for e in _edges():
        if static or edge_ok(e):
            adj.setdefault(e.src, []).append(e)
    return adj


def _allowed(src: str, dst: str) -> bool:
    s, d = FORMATS[src].category, FORMATS[dst].category
    if d not in CROSS.get(s, set()):
        return False
    if s == d:
        return True
    only = CROSS_ONLY.get((s, d))
    if only is not None and dst not in only:
        return False
    if (s, d) == ("document", "spreadsheet") and src not in _DATA_TO_SHEET:
        return False
    if (s, d) == ("image", "video") and src not in _ANIMATED:
        return False
    return True


MAX_STEPS = 3


def _search(src: str, static: bool, prefer: Optional[str] = None, dst: Optional[str] = None) -> dict[str, tuple[Edge, ...]]:
    """Dijkstra (csúcs, lépésszám) állapotokon: src-ből minden elérhető csúcs legolcsóbb útja (≤ MAX_STEPS)."""
    adj = _adjacency(static)
    best: dict[tuple[str, int], float] = {(src, 0): 0.0}
    done: dict[str, tuple[Edge, ...]] = {}
    heap: list[tuple[float, int, str, tuple[Edge, ...]]] = [(0.0, 0, src, ())]
    while heap:
        cost, n, node, path = heapq.heappop(heap)
        if node not in done:
            done[node] = path
            if node == dst:
                break
        if n >= MAX_STEPS:
            continue
        for e in adj.get(node, ()):
            if e.dst == src or any(p.src == e.dst for p in path):
                continue
            c = cost + (e.cost * 0.5 if prefer and e.engine == prefer else e.cost) + 0.05
            k = (e.dst, n + 1)
            if c < best.get(k, float("inf")):
                best[k] = c
                heapq.heappush(heap, (c, n + 1, e.dst, path + (e,)))
    done.pop(src, None)
    return done


@functools.lru_cache(maxsize=512)
def _reach(src: str, static: bool) -> dict[str, tuple[Edge, ...]]:
    return _search(src, static)


def plan(src: str, dst: str, static: bool = False, prefer: Optional[str] = None) -> Optional[list[Edge]]:
    """Legolcsóbb út src → dst (legfeljebb MAX_STEPS él). `prefer`: egy motor élei olcsóbbak."""
    src, dst = normalize(src), normalize(dst)
    if src not in FORMATS or dst not in FORMATS or src == dst or not _allowed(src, dst):
        return None
    path = (_search(src, static, prefer, dst) if prefer else _reach(src, static)).get(dst)
    return list(path) if path else None


@functools.lru_cache(maxsize=1024)
def _targets(src: str, static: bool) -> tuple[str, ...]:
    reach = _reach(src, static)
    out = [d for d in reach if _allowed(src, d)]
    cat = FORMATS[src].category
    # rendezés: azonos kategória elöl, azon belül népszerűség, majd betűrend
    return tuple(sorted(out, key=lambda d: (FORMATS[d].category != cat, FORMATS[d].tier, d)))


def targets(src: str, static: bool = False) -> list[str]:
    src = normalize(src)
    if src not in FORMATS:
        return []
    return list(_targets(src, static))


def sources(dst: str, static: bool = True) -> list[str]:
    dst = normalize(dst)
    return [s for s in FORMATS if dst in _targets(s, static)]


def all_pairs(static: bool = True) -> list[tuple[str, str]]:
    return [(s, d) for s in FORMATS for d in _targets(s, static)]


def engines_for(src: str, dst: str, static: bool = True) -> list[str]:
    p = plan(src, dst, static=static)
    return [e.engine for e in p] if p else []


def reset_cache() -> None:
    _adjacency.cache_clear()
    _targets.cache_clear()
    _reach.cache_clear()
    engines.available.cache_clear()
    engines.ffmpeg_encoder.cache_clear()
