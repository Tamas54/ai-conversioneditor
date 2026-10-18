"""PDF-be ágyazott betű — részhalmaz, nem a teljes DejaVu Sans (~740 kB, ami egy 19 kB-os egyoldalas PDF-et 425 kB-ra
fújt fel). Az ő, ű és a többi ékezet mindkét úton megmarad; a forrásdokumentum saját betűihez nem nyúlunk.

  subset_for(text)   amit MI írunk a lapra (oldalszám, vízjel, felirat, kitöltött érték, lapítás, újraszedett űrlap):
                     pontosan ezeknek a jeleknek a betűje — néhány kB
  typing_font()      a kitölthető mezők betűje (AcroForm /DR): amit egy európai felhasználó begépelhet — latin
                     (ő, ű, ș, ț…), görög, cirill, írásjelek, €, ✓ — ~60 kB, a PDF-ben tömörítve ~35 kB
"""
from __future__ import annotations

import functools
import io
import logging

log = logging.getLogger("aice.pdffonts")

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
TYPING = (list(range(0x20, 0x7F)) + list(range(0xA0, 0x250))          # latin-1, latin A/B (ő ű ș ț …)
          + list(range(0x370, 0x400)) + list(range(0x400, 0x460))      # görög, cirill
          + list(range(0x2010, 0x2028)) + list(range(0x2030, 0x203B))  # gondolatjel, „idézőjelek”, …
          + [0x20AC, 0x20BD, 0x2116, 0x2122, 0x2022, 0x2713, 0x2717, 0x2610, 0x2611, 0x2612])


def _subset(path: str, unicodes: list[int]) -> bytes:
    from fontTools import subset
    from fontTools.ttLib import TTFont
    o = subset.Options()
    o.hinting, o.desubroutinize, o.notdef_outline = False, True, True
    o.name_IDs = ["*"]                      # a név marad (DejaVuSans — a forms._is_uni erről ismeri fel)
    o.layout_features = ["kern", "liga"]
    o.drop_tables += ["FFTM"]
    f = TTFont(path)
    s = subset.Subsetter(o)
    s.populate(unicodes=unicodes)
    s.subset(f)
    b = io.BytesIO()
    f.save(b)
    return b.getvalue()


@functools.lru_cache(maxsize=4)
def typing_font(path: str = DEJAVU) -> bytes:
    return _subset(path, TYPING)


@functools.lru_cache(maxsize=256)
def _subset_cached(path: str, chars: frozenset) -> bytes:
    return _subset(path, sorted(chars))


def subset_for(text: str, path: str = DEJAVU) -> bytes:
    """A szöveg jeleinek betűje (+ számjegyek, alap írásjelek, ✓) — a beírt szöveghez."""
    return _subset_cached(path, frozenset(ord(c) for c in text + " 0123456789.,:;-–/()✓" if not c.isspace() or c == " "))
