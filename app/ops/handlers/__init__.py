"""Az eszközök végrehajtói (app/catalog/tools.py `handler` mezője ide mutat). Közös segédek."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

FONT_CANDIDATES = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                   "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf")
FONT_BOLD_CANDIDATES = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")


def font_path(bold: bool = False) -> Optional[str]:
    """Teljes Unicode-betű (ő, ű is) a szövegek rajzolásához."""
    for c in (FONT_BOLD_CANDIDATES if bold else ()) + FONT_CANDIDATES:
        if Path(c).exists():
            return c
    return None


def out_name(out: Path, src: Path, suffix: str = "", ext: Optional[str] = None) -> Path:
    from app.catalog.formats import from_filename
    from app.convert.steps import _stem
    e = ext or from_filename(src.name) or src.suffix.lstrip(".")
    stem = re.sub(r"[^\w\-]+", "_", _stem(src))[:60] or "file"
    p = out / f"{stem}{suffix}.{e}"
    i = 2
    while p.exists():
        p = out / f"{stem}{suffix}-{i}.{e}"
        i += 1
    return p


def resolve(opts: dict[str, Any], key: str) -> Optional[Path]:
    fid = opts.get(key)
    if not fid:
        return None
    return opts["_resolve"](fid)


def rgb(color: Optional[str], default=(0, 0, 0)) -> tuple[float, float, float]:
    """Szín → PyMuPDF 0–1 RGB."""
    if not color:
        return default
    from PIL import ImageColor
    c = ImageColor.getrgb(color)
    return (c[0] / 255, c[1] / 255, c[2] / 255)


async def to_pdf(src: Path, work: Path) -> Path:
    """Bármely támogatott fájl → PDF (a konverziós gráffal); a PDF változatlanul."""
    if src.suffix.lower() == ".pdf":
        return src
    from app.convert.service import convert_path
    outs, _ = await convert_path(src, "pdf", {}, work / f"pdf-{src.stem}")
    return outs[0]
