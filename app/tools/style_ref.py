"""Stílusminta → téma: „ilyesmi stílusú pptx-et / PDF-et csinálj”.

pptx: a téma XML-jéből pontosan (színséma dk1/lt1/dk2/lt2/accent1-6, címsor- és szövegbetű);
pdf / docx / kép: az első oldal képe → vision-modell (OCR_MODEL) leírja a témát JSON-ban (+ a PDF betűnevei tippként).
A kimenet a pptx_deck.DEFAULT_THEME kulcsai: primary, accent, bg, panel, text, muted, font_heading, font_body
(+ "mood": egy mondat a stílusról, a pakli-író promptjába).
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from app.config import settings

log = logging.getLogger("aice.style_ref")

FONTS = ["Calibri", "Cambria", "Arial", "Helvetica", "Georgia", "Times New Roman", "Garamond", "Verdana", "Trebuchet MS",
         "Montserrat", "Lato", "Open Sans", "Roboto", "Inter", "Playfair Display", "Merriweather", "Source Sans 3",
         "Source Serif 4", "Poppins", "Raleway", "Oswald", "Caladea", "Carlito", "DejaVu Sans", "Noto Sans", "Noto Serif"]
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _pptx_theme(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as z:
        name = next((n for n in z.namelist() if re.match(r"ppt/theme/theme\d+\.xml$", n)), None)
        if not name:
            raise ValueError("no theme in this pptx")
        xml = z.read(name).decode("utf-8", "replace")

    def color(tag: str) -> str | None:
        m = re.search(rf"<a:{tag}>(.*?)</a:{tag}>", xml, re.S)
        if not m:
            return None
        v = re.search(r'srgbClr val="([0-9A-Fa-f]{6})"', m.group(1)) or re.search(r'lastClr="([0-9A-Fa-f]{6})"', m.group(1))
        return f"#{v.group(1).upper()}" if v else None

    def font(kind: str) -> str | None:
        m = re.search(rf"<a:{kind}>.*?<a:latin typeface=\"([^\"]+)\"", xml, re.S)
        return m.group(1) if m and not m.group(1).startswith("+") else None
    c = {k: color(k) for k in ("dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3")}
    primary = c["dk2"] or c["accent1"] or "#14213D"
    accent = c["accent1"] if c["accent1"] and c["accent1"] != primary else (c["accent2"] or "#FCA311")
    return {"primary": primary, "accent": accent, "bg": c["lt1"] or "#FFFFFF", "panel": c["lt2"] or "#F4F5F7",
            "text": c["dk1"] or "#1E2430", "muted": "#6B7280",
            "font_heading": font("majorFont") or "Calibri", "font_body": font("minorFont") or "Calibri",
            "mood": "taken from the example's theme"}


def _first_page_png(src: Path, dst: Path) -> list[str]:
    """Az első oldal képe; PDF-nél a betűnevek is (tipp a modellnek)."""
    ext = src.suffix.lower()
    fonts: list[str] = []
    if ext == ".pdf":
        subprocess.run(["pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-scale-to", "1400", "-png", str(src), str(dst.with_suffix(""))],
                       check=True, capture_output=True, timeout=60)
        out = subprocess.run(["pdffonts", "-l", "3", str(src)], capture_output=True, text=True).stdout
        fonts = sorted({re.sub(r"^[A-Z]{6}\+", "", l.split()[0]) for l in out.splitlines()[2:] if l.strip()})[:8]
    elif ext in (".docx", ".odt", ".doc", ".rtf"):
        from app.tools import wordcraft
        subprocess.run([wordcraft.cli_path() or "wordcraft-cli", "render", str(src), str(dst), "--page", "1"], check=True,
                       capture_output=True, timeout=60, env={**__import__("os").environ, "WORDCRAFT_NO_PREFS": "1"})
    elif ext == ".pptx":
        from app.tools import crafts
        subprocess.run([crafts.office_cli("deck") or "deckcraft-cli", "render", str(src), "--slide", "1", str(dst)],
                       check=True, capture_output=True, timeout=60)
    else:
        from PIL import Image
        with Image.open(src) as im:
            im.convert("RGB").save(dst)
    return fonts


async def theme_from(file_id: str) -> dict[str, Any]:
    import asyncio
    from app.llm import vision
    from app.storage import temp_path
    from app.storage.files import new_file_id
    from app.tools.crafts import existing
    src = existing(file_id)
    if src.suffix.lower() == ".pptx":
        try:
            t = await asyncio.to_thread(_pptx_theme, src)
            return t
        except Exception as e:
            log.warning("pptx-téma: %s — képből próbálom", e)
    png = temp_path(new_file_id("png"))
    try:
        fonts = await asyncio.to_thread(_first_page_png, src, png)
        prompt = ("This is an example of the visual style someone wants. Describe its design theme as ONE JSON object: "
                  '{"primary":"#hex (main dark/brand colour)","accent":"#hex (highlight colour)","bg":"#hex (page background)",'
                  '"panel":"#hex (light boxes)","text":"#hex","muted":"#hex (secondary text)",'
                  f'"font_heading":"closest of {FONTS}","font_body":"closest of {FONTS}",'
                  '"mood":"one sentence: layout, density, imagery, tone"}'
                  + (f" Fonts found in the file: {', '.join(fonts)}." if fonts else "") + " Only the JSON.")
        msgs = [{"role": "user", "content": [{"type": "text", "text": prompt},
                                             {"type": "image_url", "image_url": {"url": vision.image_to_data_url(png, long_edge=1280)}}]}]
        raw = await vision._call(msgs, max_tokens=600, model=settings.OCR_MODEL)
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    finally:
        png.unlink(missing_ok=True)
    theme = {k: data[k].upper() for k in ("primary", "accent", "bg", "panel", "text", "muted") if _HEX.match(str(data.get(k, "")))}
    for k in ("font_heading", "font_body"):
        if data.get(k) in FONTS:
            theme[k] = data[k]
    theme["mood"] = str(data.get("mood") or "")[:240]
    return theme
