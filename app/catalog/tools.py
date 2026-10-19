"""Eszköz-katalógus: minden nem-konverziós művelet (tömörítés, vágás, vízjel, PDF-műveletek, űrlap, aláírás…).

Egy eszköz egyszerre jelenik meg a webes felületen (saját oldal + widget), a REST API-ban (`tool/<kulcs>` művelet),
az MCP-ben (csoportosított eszköz: `group` szerint) és az app ügynökének eszközlistájában. A végrehajtás egyetlen
helyen van: `handler` = "modul:függvény", aláírása:

    async def handler(inputs: list[Path], options: dict, out_dir: Path) -> list[Path] | dict

(a dict alak: {"files": [Path, …], "data": {...}} — pl. a metaadat-olvasó adatot is ad vissza).

`accepts`: formátumkulcsok és/vagy kategóriák ("@image"); `min_files`/`max_files`: hány bemenet kell.
`app_path`: ha a meglévő /app felületen már van rá teljes szerkesztő (pl. PDF-szerkesztő), oda is mutatunk.
"""
from __future__ import annotations

import functools
import importlib
from dataclasses import dataclass, field
from typing import Optional

from app.catalog.formats import FORMATS, normalize

GROUPS = ("compress", "pdf", "image", "video", "audio", "watermark", "web", "files", "metadata", "forms", "signature",
          "ai", "utility")


@dataclass(frozen=True)
class Tool:
    key: str
    group: str
    accepts: tuple
    handler: str
    options: tuple = ()
    min_files: int = 1
    max_files: int = 1
    output: str = ""                 # a kimenet formátuma ("" = mint a bemenet, "pdf", "zip", "json", …)
    engines: tuple = ()
    quota: str = "convert"           # billing: melyik havi keretből fogy ("convert", "form_pages", "scan_pages"…)
    ai: bool = False                 # LLM-et használ (vendégnek fiók kell)
    app_path: str = ""
    tier: int = 1
    related: tuple = ()

    @property
    def operation(self) -> str:
        return f"tool/{self.key}"


def _t(key, group, accepts, handler, options=(), min_files=1, max_files=1, output="", engines=(), quota="convert",
       ai=False, app_path="", tier=1, related=()) -> Tool:
    acc = tuple(accepts.split()) if isinstance(accepts, str) else tuple(accepts)
    return Tool(key, group, acc, handler, tuple(options), min_files, max_files, output, tuple(engines), quota, ai,
                app_path, tier, tuple(related))


_H = "app.ops.handlers"
_IMG = "@image"
_VID = "@video"
_AUD = "@audio"

TOOLS_LIST: list[Tool] = [
    # ---------- tömörítés ----------
    _t("compress-pdf", "compress", "pdf", f"{_H}.pdf:compress", ("compression",), engines=("ghostscript", "qpdf"), related=("compress-image", "split-pdf", "pdf-to-pdfa")),
    _t("compress-image", "compress", _IMG, f"{_H}.image:compress", ("quality", "max_width", "strip_metadata"), max_files=100, engines=("pillow",), related=("resize-image", "compress-jpeg", "compress-png")),
    _t("compress-jpeg", "compress", "jpg jfif", f"{_H}.image:compress_jpeg", ("quality", "strip_metadata"), max_files=100, engines=("pillow",), related=("compress-png", "compress-image")),
    _t("compress-png", "compress", "png", f"{_H}.image:compress_png", ("colors", "strip_metadata"), max_files=100, engines=("pillow",), related=("compress-jpeg", "compress-image")),
    _t("compress-gif", "compress", "gif", f"{_H}.image:compress_gif", ("colors", "max_width", "fps"), max_files=20, engines=("ffmpeg", "pillow"), tier=2, related=("video-to-gif", "compress-image")),
    _t("compress-video", "compress", _VID, f"{_H}.media:compress_video", ("crf", "resolution", "target_size_mb", "video_codec"), engines=("ffmpeg",), related=("trim-video", "resize-video", "video-to-gif")),
    _t("compress-mp3", "compress", "mp3 @audio", f"{_H}.media:compress_mp3", ("audio_bitrate", "channels"), max_files=50, engines=("ffmpeg",), tier=2, related=("trim-audio", "compress-wav")),
    _t("compress-wav", "compress", "wav aiff flac", f"{_H}.media:compress_wav", ("sample_rate", "channels", "bit_depth"), max_files=50, engines=("ffmpeg",), tier=2, related=("compress-mp3",)),
    # ---------- PDF ----------
    _t("merge-pdf", "pdf", "pdf @document @image", f"{_H}.pdf:merge", ("page_ranges",), min_files=2, max_files=100, output="pdf", engines=("pymupdf",), app_path="/app/make/pdf-tools", related=("split-pdf", "organize-pdf", "merge-files")),
    _t("split-pdf", "pdf", "pdf", f"{_H}.pdf:split", ("mode", "page_range", "every"), output="zip", engines=("pymupdf",), related=("extract-pages", "merge-pdf", "delete-pdf-pages")),
    _t("extract-pages", "pdf", "pdf", f"{_H}.pdf:extract_pages", ("page_range",), output="pdf", engines=("pymupdf",), related=("split-pdf", "delete-pdf-pages")),
    _t("delete-pdf-pages", "pdf", "pdf", f"{_H}.pdf:delete_pages", ("page_range",), output="pdf", engines=("pymupdf",), related=("extract-pages", "organize-pdf")),
    _t("rotate-pdf", "pdf", "pdf", f"{_H}.pdf:rotate", ("angle", "page_range"), output="pdf", engines=("pymupdf",), related=("organize-pdf", "crop-pdf")),
    _t("organize-pdf", "pdf", "pdf", f"{_H}.pdf:organize", ("order",), output="pdf", engines=("pymupdf",), related=("rotate-pdf", "delete-pdf-pages", "merge-pdf")),
    _t("crop-pdf", "pdf", "pdf", f"{_H}.pdf:crop", ("margins_mm", "page_range"), output="pdf", engines=("pymupdf",), tier=2, related=("resize-pdf", "rotate-pdf")),
    _t("resize-pdf", "pdf", "pdf", f"{_H}.pdf:resize", ("page_size", "fit_page"), output="pdf", engines=("pymupdf",), tier=2, related=("crop-pdf", "compress-pdf")),
    _t("pdf-to-pdfa", "pdf", "pdf", f"{_H}.pdf:to_pdfa", ("pdfa_level",), output="pdf", engines=("ghostscript",), related=("ocr-pdf", "compress-pdf", "flatten-pdf")),
    _t("pdf-to-pdfx", "pdf", "pdf", f"{_H}.pdf:to_pdfx", (), output="pdf", engines=("ghostscript",), tier=2, related=("pdf-to-pdfa",)),
    _t("ocr-pdf", "pdf", "pdf @image djvu", f"{_H}.pdf:ocr", ("ocr_language", "deskew"), output="pdf", engines=("ocrmypdf",), quota="scan_pages", related=("pdf-to-pdfa", "extract-pdf-images")),
    _t("protect-pdf", "pdf", "pdf", f"{_H}.pdf:protect", ("password", "owner_password", "allow_print", "allow_copy", "allow_edit", "allow_annotate"), output="pdf", engines=("pikepdf",), related=("unlock-pdf", "watermark-pdf", "redact-pdf")),
    _t("unlock-pdf", "pdf", "pdf", f"{_H}.pdf:unlock", ("password",), output="pdf", engines=("pikepdf",), related=("protect-pdf",)),
    _t("flatten-pdf", "pdf", "pdf", f"{_H}.pdf:flatten", ("what",), output="pdf", engines=("pymupdf",), related=("fill-form", "pdf-to-pdfa")),
    _t("extract-pdf-images", "pdf", "pdf", f"{_H}.pdf:extract_images", ("min_size",), output="zip", engines=("pymupdf",), related=("pdf-to-image", "split-pdf")),
    _t("annotate-pdf", "pdf", "pdf", f"{_H}.pdf:annotate", ("annotations",), output="pdf", engines=("pymupdf",), tier=2, related=("edit-pdf", "sign-document")),
    _t("edit-pdf", "pdf", "pdf", f"{_H}.pdf:edit", ("edits",), output="pdf", engines=("pymupdf",), app_path="/app/make/pdf-tools", related=("annotate-pdf", "watermark-pdf", "sign-document")),
    _t("redact-pdf", "pdf", "pdf", f"{_H}.pdf:redact", ("words", "patterns"), output="pdf", engines=("pymupdf",), related=("protect-pdf", "remove-metadata")),
    _t("page-numbers", "pdf", "pdf", f"{_H}.pdf:page_numbers", ("position", "start_number", "text_format"), output="pdf", engines=("pymupdf",), related=("watermark-pdf", "merge-pdf")),
    _t("compare-pdf", "pdf", "pdf", f"{_H}.pdf:compare", (), min_files=2, max_files=2, output="pdf", engines=("pymupdf",), tier=2, related=("merge-pdf",)),
    # ---------- kép ----------
    _t("resize-image", "image", _IMG, f"{_H}.image:resize", ("width", "height", "fit", "percent"), max_files=100, engines=("pillow",), related=("crop-image", "compress-image", "upscale-image")),
    _t("crop-image", "image", _IMG, f"{_H}.image:crop", ("box", "aspect"), max_files=100, engines=("pillow",), related=("resize-image", "rotate-image")),
    _t("rotate-image", "image", _IMG, f"{_H}.image:rotate", ("angle", "expand"), max_files=100, engines=("pillow",), related=("flip-image", "crop-image")),
    _t("flip-image", "image", _IMG, f"{_H}.image:flip", ("direction",), max_files=100, engines=("pillow",), related=("rotate-image",)),
    _t("upscale-image", "image", _IMG, f"{_H}.image:upscale", ("scale",), max_files=20, engines=("pillow",), related=("resize-image", "compress-image")),
    _t("collage-maker", "image", _IMG, f"{_H}.image:collage", ("columns", "gap", "background", "width"), min_files=2, max_files=100, output="jpg", engines=("pillow",), related=("merge-files", "resize-image")),
    _t("color-picker", "image", _IMG, f"{_H}.image:palette", ("colors",), output="json", engines=("pillow",), related=("resize-image",)),
    # ---------- videó, hang ----------
    _t("trim-video", "video", _VID, f"{_H}.media:trim", ("start", "end"), engines=("ffmpeg",), related=("crop-video", "compress-video", "video-to-gif")),
    _t("crop-video", "video", _VID, f"{_H}.media:crop_video", ("aspect", "box"), engines=("ffmpeg",), related=("trim-video", "resize-video", "rotate-video")),
    _t("resize-video", "video", _VID, f"{_H}.media:resize_video", ("resolution", "aspect", "aspect_mode"), engines=("ffmpeg",), tier=2, related=("crop-video", "compress-video")),
    _t("rotate-video", "video", _VID, f"{_H}.media:rotate_video", ("angle", "flip"), engines=("ffmpeg",), tier=2, related=("crop-video", "trim-video")),
    _t("mute-video", "video", _VID, f"{_H}.media:mute", (), engines=("ffmpeg",), tier=2, related=("extract-audio", "adjust-volume")),
    _t("video-to-gif", "video", _VID, f"{_H}.media:to_gif", ("start", "end", "fps", "max_width"), output="gif", engines=("ffmpeg",), related=("gif-to-video", "trim-video", "compress-gif")),
    _t("gif-to-video", "video", "gif webp apng", f"{_H}.media:gif_to_video", ("loops",), output="mp4", engines=("ffmpeg",), related=("video-to-gif",)),
    _t("extract-audio", "video", _VID, f"{_H}.media:extract_audio", ("audio_format", "audio_bitrate"), output="mp3", engines=("ffmpeg",), related=("mute-video", "trim-audio")),
    _t("add-subtitles", "video", f"{_VID} srt vtt ass", f"{_H}.media:subtitles", ("burn", "language"), min_files=2, max_files=2, engines=("ffmpeg",), tier=2, related=("trim-video",)),
    _t("extend-video", "video", _VID, f"{_H}.media:extend_video", ("duration", "at"), engines=("ffmpeg",), tier=2, related=("trim-video", "image-to-video", "merge-files")),
    _t("image-to-video", "video", _IMG, f"{_H}.media:image_to_video", ("duration", "motion", "canvas", "fit", "fps"), output="mp4", engines=("ffmpeg", "pillow"), tier=2, related=("extend-video", "merge-files", "gif-to-video")),
    _t("trim-audio", "audio", _AUD, f"{_H}.media:trim", ("start", "end", "fade_in", "fade_out"), engines=("ffmpeg",), related=("adjust-volume", "compress-mp3", "merge-files")),
    _t("adjust-volume", "audio", f"{_AUD} {_VID}", f"{_H}.media:volume", ("volume", "normalize"), engines=("ffmpeg",), related=("trim-audio", "mute-video")),
    # ---------- vízjel ----------
    _t("watermark-pdf", "watermark", "pdf", f"{_H}.watermark:pdf", ("text", "image_file", "opacity", "position", "rotation", "font_size"), output="pdf", engines=("pymupdf",), app_path="/app/make/pdf-tools", related=("protect-pdf", "page-numbers")),
    _t("watermark-image", "watermark", _IMG, f"{_H}.watermark:image", ("text", "image_file", "opacity", "position", "font_size"), max_files=100, engines=("pillow",), related=("resize-image", "watermark-video")),
    _t("watermark-video", "watermark", _VID, f"{_H}.watermark:video", ("text", "image_file", "opacity", "position", "font_size"), engines=("ffmpeg",), related=("watermark-image", "trim-video")),
    # ---------- web ----------
    _t("website-to-pdf", "web", "url", f"{_H}.web:to_pdf", ("url", "page_size", "full_page", "wait_ms"), min_files=0, max_files=0, output="pdf", engines=("chromium",), related=("website-to-image", "html-to-pdf")),
    _t("website-to-image", "web", "url", f"{_H}.web:to_image", ("url", "width", "full_page", "image_format", "wait_ms"), min_files=0, max_files=0, output="png", engines=("chromium",), related=("website-to-pdf", "thumbnail")),
    # ---------- fájlok ----------
    _t("merge-files", "files", "@document @image @video @audio pdf", f"{_H}.files:merge", ("output_format", "canvas", "fit"), min_files=2, max_files=100, engines=("pymupdf", "ffmpeg"), related=("merge-pdf", "collage-maker", "create-archive")),
    _t("thumbnail", "files", "@document @image @video @presentation @ebook @vector @cad pdf", f"{_H}.files:thumbnail", ("width", "height", "image_format", "page", "at"), max_files=100, output="png", related=("website-to-image", "resize-image")),
    _t("create-archive", "files", "*", f"{_H}.files:create_archive", ("archive_format", "password", "level"), max_files=500, output="zip", engines=("python",), related=("extract-archive", "merge-files")),
    _t("extract-archive", "files", "@archive", f"{_H}.files:extract_archive", ("password",), output="zip", engines=("python",), related=("create-archive",)),
    # ---------- metaadat ----------
    _t("read-metadata", "metadata", "*", f"{_H}.metadata:read", (), output="json", engines=("exiftool",), related=("edit-metadata", "remove-metadata")),
    _t("edit-metadata", "metadata", "@image @video @audio pdf docx xlsx pptx", f"{_H}.metadata:edit", ("fields",), engines=("exiftool",), tier=2, related=("read-metadata", "remove-metadata")),
    _t("remove-metadata", "metadata", "@image @video @audio pdf docx xlsx pptx", f"{_H}.metadata:remove", (), max_files=100, engines=("exiftool",), related=("read-metadata", "redact-pdf")),
    # ---------- haladó ----------
    _t("ffmpeg-advanced", "video", f"{_VID} {_AUD}", f"{_H}.advanced:ffmpeg", ("output_format", "args"), engines=("ffmpeg",), tier=3, related=("compress-video", "trim-video")),
    _t("imagemagick-advanced", "image", _IMG, f"{_H}.advanced:imagemagick", ("output_format", "args"), max_files=50, engines=("imagemagick",), tier=3, related=("resize-image", "compress-image")),
    # ---------- űrlap, aláírás, ügynöki kitöltés ----------
    _t("form-recognize", "forms", "pdf docx xlsx doc odt rtf xls ods @image", f"{_H}.forms:recognize", ("language", "keep_look", "deep"), output="json", quota="form_pages", related=("fill-form", "ai-form-filler", "make-fillable")),
    _t("fill-form", "forms", "pdf docx xlsx doc odt rtf xls ods @image", f"{_H}.forms:fill", ("values", "flatten", "keep_look", "schema", "deep"), quota="form_pages", app_path="/app/make/pdf-tools", related=("form-recognize", "bulk-fill-form", "sign-document")),
    _t("bulk-fill-form", "forms", "pdf docx xlsx doc odt rtf xls ods", f"{_H}.forms:bulk_fill", ("data_file", "flatten", "schema"), min_files=1, max_files=1, quota="form_pages", related=("fill-form",)),
    _t("make-fillable", "forms", "pdf docx @image", f"{_H}.forms:make_fillable", ("language", "deep"), output="pdf", quota="form_pages", related=("form-recognize", "fill-form")),
    _t("create-signature", "signature", "@image pdf", f"{_H}.signature:create", ("name", "kind", "color", "image_base64"), min_files=0, output="png", related=("sign-document",)),
    _t("sign-document", "signature", "pdf docx", f"{_H}.signature:place", ("signature_id", "image_file", "placement", "page", "x", "y", "width", "field"), output="", related=("create-signature", "fill-form", "flatten-pdf")),
    _t("ai-form-filler", "forms", "pdf docx xlsx doc odt rtf xls ods @image", f"{_H}.forms:agent_fill", ("profile", "source_files", "answers", "values", "schema", "signature_id", "flatten", "keep_look", "deep"), quota="form_pages", ai=True, related=("form-recognize", "sign-document")),
    # ---------- segédek ----------
    _t("unit-converter", "utility", "none", f"{_H}.utility:units", ("value", "from_unit", "to_unit"), min_files=0, max_files=0, output="json", quota="", related=("time-zone-converter",)),
    _t("time-zone-converter", "utility", "none", f"{_H}.utility:timezones", ("time", "from_zone", "to_zones"), min_files=0, max_files=0, output="json", quota="", related=("unit-converter",)),
]

TOOLS: dict[str, Tool] = {t.key: t for t in TOOLS_LIST}


def get(key: str) -> Optional[Tool]:
    return TOOLS.get(key)


def accepts(tool: Tool, fmt: str) -> bool:
    if "*" in tool.accepts:
        return True
    fmt = normalize(fmt)
    f = FORMATS.get(fmt)
    return fmt in tool.accepts or (f is not None and f"@{f.category}" in tool.accepts)


def accepted_formats(tool: Tool) -> list[str]:
    if "*" in tool.accepts:
        return sorted(FORMATS)
    return sorted(k for k in FORMATS if accepts(tool, k))


@functools.lru_cache(maxsize=None)
def handler(tool_key: str):
    mod, fn = TOOLS[tool_key].handler.split(":")
    try:
        return getattr(importlib.import_module(mod), fn)
    except (ImportError, AttributeError):
        return None


def available(tool_key: str, static: bool = False) -> bool:
    """Él-e az eszköz: van végrehajtója, és (static=False esetén) a motorjai is elérhetők ezen a gépen."""
    t = TOOLS.get(tool_key)
    if t is None or handler(tool_key) is None:
        return False
    if static:
        return True
    from app.convert import engines
    return all(engines.available(e) for e in t.engines if e in engines.ENGINES)


def live(static: bool = True) -> list[Tool]:
    return [t for t in TOOLS_LIST if available(t.key, static)]


def by_group(static: bool = True) -> dict[str, list[Tool]]:
    out: dict[str, list[Tool]] = {}
    for t in live(static):
        out.setdefault(t.group, []).append(t)
    return out
