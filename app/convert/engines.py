"""Nyílt forrású konverziós motorok: elérhetőség (futásidőben, a gépen) és biztonságos futtatás.

Minden motor külső program vagy Python-csomag; a Dockerfile mindet telepíti. Egy fejlesztői gépen hiányozhat
némelyik: a gráf (graph.py) ilyenkor csak az elérhető motorokkal tervez, a katalógus (weboldal, OpenAPI) pedig
a teljes, élesben elérhető készletet mutatja.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

# motor → (leírás, ellenőrzés). Az ellenőrzés egy futtatható neve vagy "py:<modul>".
ENGINES: dict[str, tuple[str, tuple[str, ...]]] = {
    "wordcraft": ("WordCraft (Rust Word engine)", ()),
    "deckcraft": ("DeckCraft (Rust PowerPoint engine)", ()),
    "gridcraft": ("GridCraft (Rust spreadsheet engine)", ()),
    "photocraft": ("PhotoCraft (Rust image engine)", ()),
    "vectorcraft": ("VectorCraft (Rust vector engine)", ()),
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


RUST_CRAFTS = ("deckcraft", "gridcraft", "photocraft", "vectorcraft")


@functools.lru_cache(maxsize=None)
def available(engine: str) -> bool:
    if os.getenv("AICE_DISABLE_ENGINES") and engine in os.environ["AICE_DISABLE_ENGINES"].split(","):
        return False
    if engine == "wordcraft":                       # Rust-motor: a meglévő WordCraft-CLI (settings.WORDCRAFT_CLI)
        from app.tools import wordcraft
        return wordcraft.available()
    if engine in RUST_CRAFTS:                       # a többi Rust-motor: a meglévő Craft-CLI-k (/opt/<x>craft/bin)
        from app.tools import crafts
        cli = {"deckcraft": lambda: crafts.office_cli("deck"), "gridcraft": lambda: crafts.office_cli("sheet"),
               "photocraft": crafts.photo_cli, "vectorcraft": crafts.vector_cli}[engine]()
        return bool(cli)
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
    name = Path(exe).name
    if name == "ebook-convert":            # Calibre fej nélkül (Qt): nincs kijelző
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
    real = os.path.realpath(shutil.which(exe) or exe)
    # a rendszer (apt) Pythonját használó motorok a rendszer csomagjait lássák; a saját Pythonnal érkező hivatalos
    # Calibre-build (/opt/calibre) nem kap PYTHONPATH-t
    if name in _APT_PYTHON and not real.startswith("/opt/") and Path("/usr/lib/python3/dist-packages").is_dir():
        env["PYTHONPATH"] = "/usr/lib/python3/dist-packages"
        env.pop("VIRTUAL_ENV", None)
    return env


_BACKGROUND: "contextvars.ContextVar[bool]" = contextvars.ContextVar("engines_background", default=False)


@contextlib.contextmanager
def background():
    """A blokkon belül (és az onnan indított taskokban) induló külső programok alacsony prioritással (nice 10): a
    végleges render a háttérben fut, a gyors előnézet és a weboldal ne várjon rá (10-23)."""
    tok = _BACKGROUND.set(True)
    try:
        yield
    finally:
        _BACKGROUND.reset(tok)


def niced(argv: list[str]) -> list[str]:
    nice = shutil.which("nice") if _BACKGROUND.get() else None
    return [nice, "-n", "10", *argv] if nice else list(argv)


# a nehéz munka kapuja (app/jobs.py heavy()) állítja: ennyi szálon dolgozik az ffmpeg ebben a munkában
FFMPEG_THREADS: "contextvars.ContextVar[Optional[int]]" = contextvars.ContextVar("aice_ffmpeg_threads", default=None)


def _threads(args: tuple[str, ...]) -> tuple[str, ...]:
    """ffmpeg a nehéz munka kapuján belül: -threads minden bemenet elé (dekóder) és a kimenet elé (kódoló), a szűrők
    szálai globálisan. Az utolsó érvényes, így a hívó saját -threads-e sem viheti el az összes magot."""
    n = FFMPEG_THREADS.get()
    if not n or len(args) < 2 or Path(args[0]).name != "ffmpeg":
        return args
    t = str(n)
    out = [args[0], "-filter_threads", t, "-filter_complex_threads", t]
    for a in args[1:-1]:
        out += ["-threads", t, a] if a == "-i" else [a]
    return (*out, "-threads", t, args[-1])


async def run(*args: str, timeout: float = 600, cwd: Optional[Path] = None, stdin: Optional[bytes] = None,
              ok_codes: tuple[int, ...] = (0,)) -> tuple[bytes, bytes]:
    """Külső program futtatása shell nélkül (argumentum-listával), időkorláttal. Hiba → EngineError."""
    args = _threads(args)
    exe = args[0]
    if exe in ("python", "python3"):
        args = (sys.executable, *args[1:])
    proc = await asyncio.create_subprocess_exec(
        *niced(list(args)), cwd=str(cwd) if cwd else None, env=_env_for(exe),
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
