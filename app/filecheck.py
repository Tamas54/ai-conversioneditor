"""Feltöltött fájl józan-ellenőrzése + felhasználónak szóló hibaszöveg.

`problem()` a menthetetlenül rossz fájlt fogja meg MÁR a feltöltéskor (minden úton: web, API, MCP), nem a
konvertálás közepén: üres fájl; szövegként továbbított bináris (minden 0x7F feletti bájt helyén U+FFFD → EF BF BD,
visszafordíthatatlan); képnek/dokumentumnak nevezett weboldal. Csak biztos eseteket jelez — kétesnél átenged.

`public_message()` a kivételből a felhasználónak szóló mondatot adja: ismert hibaosztályokra emberi szöveg,
és SOHA nem ad ki szerveroldali útvonalat (DATA_DIR, /tmp, …).
"""
from __future__ import annotations

import re
from pathlib import Path

# kiterjesztések, amelyek biztosan binárisak (a szövegként továbbítás tönkreteszi őket)
_BINARY = {
    "png", "jpg", "jpeg", "jfif", "gif", "webp", "bmp", "tif", "tiff", "heic", "heif", "avif", "ico", "psd", "jxl",
    "pdf", "docx", "xlsx", "pptx", "odt", "ods", "odp", "epub", "zip", "doc", "xls", "ppt", "key", "pages",
    "mp3", "wav", "m4a", "aac", "ogg", "flac", "mp4", "mov", "m4v", "webm", "mkv", "avi",
}
_MANGLED = b"\xef\xbf\xbd"          # U+FFFD UTF-8-ban: a „nem dekódolható bájt” helye
_PAGE_STARTS = (b"<!doctype html", b"<html", b"<head", b"<?xml version=\"1.0\"?>\n<!doctype html")


class DamagedFile(ValueError):
    """A feltöltött fájl biztosan használhatatlan — a szöveg a felhasználónak szól."""


def problem(head: bytes, filename: str) -> str | None:
    """`head` = a fájl első (legfeljebb ~256 KB) bájtja. None = rendben (vagy nem tudjuk biztosan)."""
    ext = Path(filename or "").suffix.lstrip(".").lower()
    name = Path(filename or "file").name
    if not head:
        return f"{name} is empty (0 bytes) — upload the file again."
    if ext not in _BINARY:
        return None
    if head.count(_MANGLED) >= 8:
        return (f"{name} is damaged: somewhere on its way it was saved as text, which replaced every special byte. "
                "It can't be repaired — download or export it again from where it came from.")
    if head[:64].lstrip().lower().startswith(_PAGE_STARTS):
        return (f"{name} is a web page, not a .{ext} file — the link probably opened a page around the file. "
                "Open the link in your browser and save the file itself.")
    return None


def check_path(path: Path, filename: str) -> None:
    with open(path, "rb") as f:
        msg = problem(f.read(256 * 1024), filename)
    if msg:
        raise DamagedFile(msg)


def check_bytes(data: bytes, filename: str) -> None:
    msg = problem(data[:256 * 1024], filename)
    if msg:
        raise DamagedFile(msg)


# abszolút útvonal (legalább két tag), idézőjellel vagy anélkül — URL-t (https://…/a/b), MIME-típust (image/png),
# törtet (1/2/3) nem bánt: a perjel előtt nem állhat betű, szám, kettőspont vagy perjel
_PATH = re.compile(r"""['"]?(?<![\w:/.])(?:/[\w.\-@+%~]+){2,}/?['"]?""")


def scrub(text: str) -> str:
    """Szerveroldali útvonalak ki a felhasználónak szóló szövegből."""
    return _PATH.sub("the file", text or "")


def public_message(e: BaseException) -> str:
    """A kivétel felhasználónak szóló mondata (ismert osztályok emberi szöveggel, útvonal nélkül)."""
    import zipfile
    try:
        from PIL import UnidentifiedImageError
    except ImportError:          # pragma: no cover
        UnidentifiedImageError = ()  # type: ignore[assignment]
    if UnidentifiedImageError and isinstance(e, UnidentifiedImageError):
        return "This picture can't be read — the file is damaged or isn't really an image. Try saving it again from the original."
    if isinstance(e, zipfile.BadZipFile):
        return "This file is damaged — it isn't a complete Word, Excel, PowerPoint or ZIP file. Try saving it again from the original."
    return scrub(str(e))[:300]
