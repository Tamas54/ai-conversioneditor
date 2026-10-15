"""Nyílt forrású konverziós motorok: elérhetőség (futásidőben, a gépen) és biztonságos futtatás.

Minden motor külső program vagy Python-csomag; a Dockerfile mindet telepíti. Egy fejlesztői gépen hiányozhat
némelyik: a gráf (graph.py) ilyenkor csak az elérhető motorokkal tervez, a katalógus (weboldal, OpenAPI) pedig
a teljes, élesben elérhető készletet mutatja.
"""
from __future__ import annotations

import asyncio
import functools
import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

# motor → (leírás, ellenőrzés). Az ellenőrzés egy futtatható neve vagy "py:<modul>".
ENGINES: dict[str, tuple[str, tuple[str, ...]]] = {
    "libreoffice": ("LibreOffice (Writer, Calc, Impress, Draw)", ("soffice",)),
    "pandoc": ("Pandoc", ("pandoc",)),
    "calibre": ("Calibre ebook-convert", ("ebook-convert",)),
    "pillow": ("Pillow (Python Imaging Library)", ("py:PIL",)),
    "imagemagick": ("ImageMagick", ("convert",)),
    "vips": ("libvips", ("vips",)),
    "libraw": ("LibRaw (dcraw_emu)", ("dcraw_emu",)),
    "heif": ("libheif / pillow-heif", ("py:pillow_heif",)),
    "jxl": ("libjxl (pillow-jxl-plugin)", ("py:pillow_jxl",)),
    "inkscape": ("Inkscape", ("inkscape",)),
    "rsvg": ("librsvg (rsvg-convert)", ("rsvg-convert",)),
    "potrace": ("Potrace (bitmap tracing)", ("potrace",)),
    "ffmpeg": ("FFmpeg", ("ffmpeg", "ffprobe")),
    "7zip": ("7-Zip (p7zip)", ("7z",)),
    "unar": ("The Unarchiver (unar)", ("unar",)),
    "bsdtar": ("libarchive (bsdtar)", ("bsdtar",)),
    "zstd": ("Zstandard", ("zstd",)),
    "lzip": ("Lzip", ("lzip",)),
    "lzop": ("Lzop", ("lzop",)),
    "python": ("Python standard library", ()),
    "fonttools": ("fontTools", ("py:fontTools",)),
    "fontforge": ("FontForge", ("fontforge",)),
    "ezdxf": ("ezdxf + Matplotlib", ("py:ezdxf", "py:matplotlib")),
    "libredwg": ("LibreDWG", ("dwg2dxf",)),
    "djvulibre": ("DjVuLibre", ("ddjvu", "djvutxt")),
    "mupdf": ("MuPDF (mutool)", ("mutool",)),
    "pymupdf": ("PyMuPDF", ("py:pymupdf",)),
    "poppler": ("Poppler (pdftoppm, pdftotext)", ("pdftoppm", "pdftotext")),
    "ghostscript": ("Ghostscript", ("gs",)),
    "qpdf": ("QPDF", ("qpdf",)),
    "ocrmypdf": ("OCRmyPDF + Tesseract", ("py:ocrmypdf", "tesseract")),
    "tesseract": ("Tesseract OCR", ("tesseract",)),
    "weasyprint": ("WeasyPrint", ("py:weasyprint",)),
    "pdf2docx": ("pdf2docx", ("py:pdf2docx",)),
    "exiftool": ("ExifTool", ("exiftool",)),
    "chromium": ("Chromium (Playwright)", ("py:playwright",)),
}

# Python-alapú motorok, amelyek a rendszer (apt) Pythonját használják: a pip-csomagok (/usr/local) ne
# árnyékolják a sajátjaikat (pl. Calibre html5-parser ↔ lxml libxml2-verzió).
_APT_PYTHON = {"ebook-convert", "inkscape", "fontforge"}


@functools.lru_cache(maxsize=None)
def available(engine: str) -> bool:
    if os.getenv("AICE_DISABLE_ENGINES") and engine in os.environ["AICE_DISABLE_ENGINES"].split(","):
        return False
    checks = ENGINES.get(engine, ("", ()))[1]
    for c in checks:
        if c.startswith("py:"):
            if importlib.util.find_spec(c[3:]) is None:
                return False
        elif shutil.which(c) is None:
            return False
    return engine in ENGINES


@functools.lru_cache(maxsize=None)
def ffmpeg_encoder(name: str) -> bool:
    """Van-e az ffmpeg-buildben ez a kódoló (pl. libopencore_amrnb) — nem minden disztribúció fordítja bele."""
    import subprocess
    if not available("ffmpeg"):
        return False
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines() if len(line.split()) > 1)


def status() -> dict[str, bool]:
    return {e: available(e) for e in ENGINES}


class EngineError(RuntimeError):
    """A motor hibája — a felhasználónak szóló, rövid üzenettel."""


def _env_for(exe: str) -> dict[str, str]:
    env = {**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}
    if Path(exe).name in _APT_PYTHON and Path("/usr/lib/python3/dist-packages").is_dir():
        env["PYTHONPATH"] = "/usr/lib/python3/dist-packages"
        env.pop("VIRTUAL_ENV", None)
    return env


async def run(*args: str, timeout: float = 600, cwd: Optional[Path] = None, stdin: Optional[bytes] = None,
              ok_codes: tuple[int, ...] = (0,)) -> tuple[bytes, bytes]:
    """Külső program futtatása shell nélkül (argumentum-listával), időkorláttal. Hiba → EngineError."""
    exe = args[0]
    if exe in ("python", "python3"):
        args = (sys.executable, *args[1:])
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(cwd) if cwd else None, env=_env_for(exe),
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise EngineError(f"{Path(exe).name} took longer than {int(timeout)} s and was stopped")
    if proc.returncode not in ok_codes:
        msg = (err or out).decode("utf-8", "replace").strip().splitlines()
        tail = " ".join(msg[-3:])[:400] if msg else f"exit code {proc.returncode}"
        raise EngineError(f"{Path(exe).name} failed: {tail}")
    return out, err
