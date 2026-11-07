"""Dizájnsablonok a VectorCrafttal: plakát, szórólap, jelentés-borító, oklevél, közösségi kártya, füzet, névjegy, meghívó.

Egy sablon = paraméteres elrendezés, VectorCraft-parancsokká fordítva (vector_run), PDF/PNG/SVG kimenettel.
Képek: images.resolve (file_id / URL / FLUX / stock), a keret arányára vágva, beágyazva.

Betűk: címek "Noto Serif" (Bold is van; a szerveren a fonts-noto adja), szöveg "Source Sans 3"
(a VectorCraft beépített betűje Regular/Semibold/Bold/Italic). A Source Serif 4 csak Regular — címnek nem jó.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from app.tools import crafts, images

TEMPLATES = {"poster", "flyer", "report_cover", "certificate", "social", "booklet", "card", "invitation"}
DEFAULT_THEME = {
    "primary": "#14213D", "accent": "#FCA311", "bg": "#FFFFFF", "panel": "#F4F5F7",
    "text": "#1E2430", "muted": "#6B7280", "font_heading": "Noto Serif", "font_body": "Source Sans 3",
}
SIZES = {"A4": (595.3, 841.9), "A3": (841.9, 1190.6), "A5": (419.5, 595.3), "A6": (297.6, 419.5), "letter": (612, 792)}
CARD = (240.9, 155.9)          # névjegy: 85×55 mm, fekvő
GAP = 60  # pt az artboardok között
# Képernyőre (social) ennél jobban nem nagyítunk fotót, nyomtatásban ennél kisebb felbontás lágy (a minőségből nem engedünk).
# 11-07: egy 312×464-es portrét a social-sablon 3,9×-re fújt fel teljes háttérnek — elmosódott, a fej levágva.
UPSCALE_MAX = 1.5
PRINT_DPI_MIN = 150


class Doc:
    """VectorCraft-parancslista építő; az objektum-id-k 2-től egyesével nőnek (mérve), oldalak artboardok."""

    def __init__(self, w: float, h: float, name: str):
        self.w, self.h = w, h
        self.cmds: list[dict[str, Any]] = [{"command": "file.new", "params": {"width": w, "height": h, "units": "Points", "name": name}}]
        self.next_id = 2
        self.page = 0
        self.images: list[tuple[Path, float, float]] = []   # a lerakott fotók (útvonal, szélesség, magasság pt) — a check méri
        self.tmp: list[Path] = []                           # a sablon saját ideiglenes képei (a render törli)
        self.layout: dict[str, Any] = {}                    # a sablon döntései (pl. social: teljes háttér vagy osztott)

    def _add(self, cid: str, params: Optional[dict] = None, creates: bool = False) -> Optional[int]:
        self.cmds.append({"command": cid, "params": params} if params else {"command": cid})
        if creates:
            self.next_id += 1
            return self.next_id - 1
        return None

    @property
    def ox(self) -> float:
        return self.page * (self.w + GAP)

    def new_page(self) -> None:
        self.page += 1
        self._add("artboard.new", {"x": self.ox, "y": 0, "width": self.w, "height": self.h})

    def rect(self, x, y, w, h, fill: str, opacity: Optional[float] = None) -> int:
        i = self._add("shape.rectangle", {"x": self.ox + x, "y": y, "width": w, "height": h}, creates=True)
        self._add("paint.setFill", {"color": fill})
        self._add("paint.setStroke", {"none": True})
        if opacity is not None:
            self._add("transparency.set", {"opacity": opacity})
        return i

    def line(self, x1, y1, x2, y2, color: str, weight: float = 1) -> int:
        i = self._add("shape.line", {"x1": self.ox + x1, "y1": y1, "x2": self.ox + x2, "y2": y2}, creates=True)
        self._add("paint.setStroke", {"color": color})
        self._add("stroke.set", {"weight": weight})
        return i

    def text(self, x, y, text: str, *, size, font, color, style=None, w=None, h=None, align=None,
             leading=None, fit: Optional[str] = None, tracking=None) -> int:
        p: dict[str, Any] = {"x": self.ox + x, "y": y, "text": text or " ", "size": size, "font": font, "color": color}
        if style:
            p["style"] = style
        if w:
            # legalább egy sor férjen bele: a sornál alacsonyabb dobozban a VectorCraft semmit nem rajzol ki
            # (11-07: a social-kártya telefonszáma 26,4 pt-os betűvel 31,5 pt-os dobozban — eltűnt a képről)
            p["area"] = {"width": w, "height": max(h or size * 3, (leading or size * 1.2) * 1.15)}
            if fit:
                p["area"]["fit"] = fit
        i = self._add("text.create", p, creates=True)
        st = {k: v for k, v in (("justify", align), ("leading", leading), ("tracking", tracking)) if v is not None}
        if st:
            self._add("text.setStyle", st)
        return i

    def image(self, path, x, y, w, h) -> int:
        self.images.append((Path(path), w, h))
        return self._add("file.place", {"path": str(path), "rect": [self.ox + x, y, w, h], "link": False}, creates=True)

    def thread(self, ids: list[int]) -> None:
        if len(ids) > 1:
            self._add("select.set", {"ids": ids})
            self._add("text.thread.create")


# az oklevél kísérő mondata a tartalom nyelvén (UI-próba 10-18: magyar oklevélen angol „This certificate is presented to”)
_CERT_LEAD = {"en-US": "This certificate is presented to", "hu-HU": "Ezt az oklevelet kapja",
              "de-DE": "Diese Urkunde wird verliehen an", "fr-FR": "Ce certificat est décerné à",
              "es-ES": "Este certificado se otorga a", "it-IT": "Questo attestato è conferito a",
              "pt-PT": "Este certificado é atribuído a", "nl-NL": "Dit certificaat wordt uitgereikt aan",
              "pl-PL": "Niniejszy dyplom otrzymuje", "cs-CZ": "Tento certifikát získává",
              "sk-SK": "Tento certifikát získava", "ro-RO": "Acest certificat este acordat lui"}


def _certificate_lead(data: dict) -> str:
    from app.tools import textlang
    text = " ".join(str(data.get(k) or "") for k in ("title", "subtitle", "text", "recipient", "place", "date"))
    return _CERT_LEAD.get(textlang.detect(text, min_hits=2, min_words=3) or "en-US", _CERT_LEAD["en-US"])

def _theme(data: dict[str, Any], theme: Optional[dict], brand: Optional[Any]) -> dict[str, str]:
    """A sablon színei/betűi: alap → arculat (a katalógus neve, vagy a felhasználó arculata dictként) → kézi téma."""
    t = apply_brand(dict(DEFAULT_THEME), brand)
    t.update({k: v for k, v in (theme or {}).items() if k in DEFAULT_THEME and v})
    return t


def apply_brand(t: dict[str, str], brand: Optional[Any]) -> dict[str, str]:
    """Egy téma (primary, accent, bg, panel, text, muted, font_heading, font_body) az arculat szerint — KÖZÖS a dizájn,
    a pakli, az album és az infografika között. brand: a katalógus neve, vagy a felhasználó arculata dictként."""
    if isinstance(brand, str) and brand:
        from app.brands import get_brand
        b = get_brand(brand)
        if b is None:
            raise ValueError(f"Unknown brand: {brand}")
        t.update(primary=b.colors.primary, accent=b.colors.accent, bg=b.colors.body_bg, panel=b.colors.panel_bg,
                 text=b.colors.text_primary, muted=b.colors.text_muted)
    elif isinstance(brand, dict):
        from app.tools.design_auto import font_family, mix, readable
        c, f = brand.get("colors") or {}, brand.get("fonts") or {}
        bg = c.get("bg") or t["bg"]
        if c.get("bg") and sum(int(bg[i:i + 2], 16) for i in (1, 3, 5)) < 384:     # sötét arculat-háttér: a lap maradjon világos
            bg = "#FFFFFF"
        t.update(primary=c.get("primary") or t["primary"], accent=c.get("accent") or t["accent"], bg=bg,
                 panel=mix(c.get("primary") or t["primary"], "#FFFFFF", 0.92), text=readable(c.get("text") or t["text"], bg),
                 muted=mix(readable(c.get("text") or t["text"], bg), bg, 0.4))
        if f.get("heading"):
            t["font_heading"] = font_family(f["heading"]) or t["font_heading"]
        if f.get("body"):
            t["font_body"] = font_family(f["body"], body=True) or t["font_body"]
    return t


def _logo(d: Doc, brand: Optional[Any], template: str) -> None:
    """A felhasználó arculatának logója a lap sarkába (oklevélen/meghívón fent középre)."""
    lp = brand.get("_logo_path") if isinstance(brand, dict) else None
    if not lp:
        return
    try:
        from PIL import Image
        with Image.open(lp) as im:
            lw, lh = im.size
    except Exception:
        return
    W, H = d.w, d.h
    base = min(W, H)
    w = base * (0.22 if template == "card" else 0.16)
    h = w * lh / max(lw, 1)
    if h > base * 0.1:
        h = base * 0.1
        w = h * lw / max(lh, 1)
    if template == "certificate":                     # a keret bal felső sarkába (középen a címre lógna)
        x, y = 58, 52
    elif template == "invitation":
        x, y = (W - w) / 2, base * 0.07
    else:
        x, y = W - base * 0.06 - w, base * 0.05
    from app.tools.design_auto import place_logo
    t = _theme({}, None, brand)
    under = t["primary"] if template in ("poster", "flyer", "report_cover", "social") else ("#FFFDF7" if template == "certificate" else t["bg"])
    place_logo(d, lp, x, y, w, h, under)


# ---------- sablonok ----------

def _poster(d: Doc, data, t, img):
    W, H = d.w, d.h
    m = W * 0.07
    d.rect(0, 0, W, H, t["primary"])
    ih = H * 0.56 if img else H * 0.3                 # kép nélkül a szöveg feljebb kezd (ne maradjon üres fél lap)
    if not img:
        d.rect(0, 0, W * 0.32, H * 0.22, t["accent"], opacity=90)
    if img:
        d.image(img.path, 0, 0, W, ih)
        d.rect(0, ih - H * 0.12, W, H * 0.12, t["primary"], opacity=55)
    d.rect(m, ih + H * 0.035, W * 0.12, 5, t["accent"])
    d.text(m, ih + H * 0.055, data.get("title", ""), size=W * 0.085, font=t["font_heading"], style="Bold",
           color="#FFFFFF", w=W - 2 * m, h=H * 0.16, fit="shrinkText", leading=W * 0.09)
    if data.get("subtitle"):
        d.text(m, ih + H * 0.22, data["subtitle"], size=W * 0.035, font=t["font_body"], style="Semibold",
               color=t["accent"], w=W - 2 * m, h=H * 0.06, fit="shrinkText")
    if data.get("body"):
        d.text(m, ih + H * 0.29, data["body"], size=W * 0.024, font=t["font_body"], color="#E8EAF0",
               w=W - 2 * m, h=H * 0.09, fit="shrinkText", leading=W * 0.034)
    meta = " · ".join(x for x in (data.get("date"), data.get("place")) if x)
    if meta:
        d.text(m, H * 0.925, meta.upper(), size=W * 0.024, font=t["font_body"], style="Bold", color=t["accent"],
               w=W - 2 * m, h=H * 0.03, tracking=80)
    if data.get("footer"):
        d.text(m, H * 0.955, data["footer"], size=W * 0.017, font=t["font_body"], color="#9AA3B2", w=W - 2 * m, h=H * 0.025)


def _flyer(d: Doc, data, t, img):
    W, H = d.w, d.h
    m = W * 0.08
    d.rect(0, 0, W, H, t["bg"])
    d.rect(0, 0, W, H * 0.2, t["primary"])
    d.rect(0, H * 0.2, W, 6, t["accent"])
    if data.get("kicker"):
        d.text(m, H * 0.04, data["kicker"].upper(), size=11, font=t["font_body"], style="Bold", color=t["accent"],
               w=W - 2 * m, h=16, tracking=100)
    d.text(m, H * 0.065, data.get("title", ""), size=36, font=t["font_heading"], style="Bold", color="#FFFFFF",
           w=W - 2 * m, h=H * 0.09, fit="shrinkText")
    if data.get("subtitle"):
        d.text(m, H * 0.155, data["subtitle"], size=14, font=t["font_body"], color="#DDE3EC", w=W - 2 * m, h=H * 0.035,
               fit="shrinkText")
    y = H * 0.24
    if img:
        d.image(img.path, m, y, W - 2 * m, H * 0.27)
        y += H * 0.29
    hl = (data.get("highlights") or [])[:3]
    body_h = H * (0.88 - y / H) - (H * 0.12 if hl else 0)
    bid = d.text(m, y, data.get("body", ""), size=11.5, font=t["font_body"], color=t["text"], w=W - 2 * m, h=body_h,
                 leading=16, fit="shrinkText")
    d.cmds.append({"command": "text.areaOptions", "params": {"ids": [bid], "columns": 2, "gutter": 22}})
    if hl:
        cw = (W - 2 * m - 20 * (len(hl) - 1)) / len(hl)
        yy = y + body_h + H * 0.02
        for k, item in enumerate(hl):
            x = m + k * (cw + 20)
            d.rect(x, yy, cw, H * 0.085, t["panel"])
            d.rect(x, yy, 4, H * 0.085, t["accent"])
            d.text(x + 14, yy + 10, str(item), size=11.5, font=t["font_body"], style="Semibold", color=t["primary"],
                   w=cw - 24, h=H * 0.085 - 18, fit="shrinkText", leading=15)
    d.rect(0, H * 0.92, W, H * 0.08, t["primary"])
    if data.get("cta"):
        d.text(m, H * 0.937, data["cta"], size=15, font=t["font_body"], style="Bold", color=t["accent"], w=W * 0.55, h=24,
               fit="shrinkText")
    if data.get("contact"):
        d.text(W * 0.55, H * 0.94, data["contact"], size=10, font=t["font_body"], color="#DDE3EC", w=W * 0.37, h=22,
               align="right", fit="shrinkText")


def _report_cover(d: Doc, data, t, img):
    W, H = d.w, d.h
    m = W * 0.09
    if img:
        d.image(img.path, 0, 0, W, H)
        d.rect(0, 0, W, H, t["primary"], opacity=55)
    else:
        d.rect(0, 0, W, H, t["primary"])
    d.rect(m, H * 0.14, 6, H * 0.36, t["accent"])
    if data.get("organization"):
        d.text(m + 24, H * 0.14, data["organization"].upper(), size=12, font=t["font_body"], style="Bold",
               color=t["accent"], w=W - 2 * m, h=18, tracking=120)
    d.text(m + 24, H * 0.19, data.get("title", ""), size=44, font=t["font_heading"], style="Bold", color="#FFFFFF",
           w=W - 2 * m - 24, h=H * 0.22, fit="shrinkText", leading=50)
    if data.get("subtitle"):
        d.text(m + 24, H * 0.42, data["subtitle"], size=17, font=t["font_body"], color="#E6E9EF", w=W - 2 * m - 24,
               h=H * 0.08, fit="shrinkText", leading=23)
    meta = [x for x in (data.get("author"), data.get("date")) if x]
    if meta:
        d.line(m, H * 0.86, W - m, H * 0.86, "#FFFFFF", 0.6)
        d.text(m, H * 0.875, "   ·   ".join(meta), size=12, font=t["font_body"], color="#FFFFFF", w=W - 2 * m, h=20)


def _certificate(d: Doc, data, t, img):
    W, H = d.w, d.h
    d.rect(0, 0, W, H, "#FFFDF7")
    d.rect(18, 18, W - 36, H - 36, t["primary"])
    d.rect(24, 24, W - 48, H - 48, "#FFFDF7")
    d.rect(34, 34, W - 68, H - 68, t["accent"], opacity=12)
    d.rect(40, 40, W - 80, H - 80, "#FFFDF7")
    c = dict(w=W - 160, align="center")
    # hosszú cím („Certificate of Completion”) egy sorban, kisebben — a ritkított nagybetű különben levágódna
    title = (data.get("title") or "Certificate").upper()
    tsize = max(20, min(40, (W - 160) / (max(len(title), 1) * 0.88)))
    d.text(80, H * 0.13, title, size=round(tsize, 1), font=t["font_heading"], style="Bold",
           color=t["primary"], h=56, tracking=150 if len(title) <= 14 else 90, fit="shrinkText", **c)
    d.rect(W / 2 - 40, H * 0.255, 80, 3, t["accent"])
    d.text(80, H * 0.3, data.get("lead") or _certificate_lead(data), size=15, font=t["font_body"], color=t["muted"], h=24, **c)
    d.text(80, H * 0.36, data.get("recipient", ""), size=38, font=t["font_heading"], style="Bold Italic",
           color=t["text"], h=56, fit="shrinkText", **c)
    d.text(80, H * 0.5, data.get("text", ""), size=15, font=t["font_body"], color=t["text"], h=H * 0.14,
           fit="shrinkText", leading=21, **c)
    meta = ", ".join(x for x in (data.get("place"), data.get("date")) if x)
    if meta:
        d.text(80, H * 0.67, meta, size=13, font=t["font_body"], color=t["muted"], h=20, **c)
    sigs = (data.get("signatures") or [])[:3]
    if sigs:
        sw = (W - 200) / len(sigs)
        for k, sgn in enumerate(sigs):
            x = 100 + k * sw
            d.line(x + 20, H * 0.82, x + sw - 20, H * 0.82, t["text"], 0.8)
            d.text(x, H * 0.83, sgn.get("name", ""), size=13, font=t["font_body"], style="Semibold", color=t["text"],
                   w=sw, h=20, align="center")
            if sgn.get("role"):
                d.text(x, H * 0.86, sgn["role"], size=11, font=t["font_body"], color=t["muted"], w=sw, h=18, align="center")


def _img_size(path) -> tuple[int, int]:
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def _social(d: Doc, data, t, img):
    """Közösségi kártya. A fotó a vászon arányához közeli és elég nagy → teljes háttér (alsó felén a szöveg).
    Álló / négyzetes / kicsi fotó (tipikusan egy portré a link-képhez) → OSZTOTT elrendezés: a fotó egészben, keretben,
    legfeljebb UPSCALE_MAX-szorosra nagyítva, a szöveg mellette (fekvő vásznon) vagy alatta (négyzetes, álló vásznon)."""
    W, H = d.w, d.h
    m = W * 0.07
    if not img:
        d.rect(0, 0, W, H, t["primary"])
        _social_texts(d, data, t, m, W - 2 * m, H * 0.58, H * 0.97)
        return
    iw, ih = _img_size(img.path)
    if abs(math.log((iw / ih) / (W / H))) < 0.35 and max(W / iw, H / ih) <= UPSCALE_MAX:
        full = images._crop_to(img.path, W / H, 2400)
        d.tmp.append(full)
        d.layout = {"photo": "full"}
        d.image(full, 0, 0, W, H)
        d.rect(0, 0, W, H, "#000000", opacity=30)
        d.rect(0, H * 0.5, W, H * 0.5, t["primary"], opacity=72)
        _social_texts(d, data, t, m, W - 2 * m, H * 0.58, H * 0.97)
        return
    d.rect(0, 0, W, H, t["primary"])
    wide = W / H >= 1.2
    bw, bh = (W * 0.34, H * 0.76) if wide else (W - 2 * m, H * (0.46 if W / H > 0.8 else 0.5))
    s = min(bw / iw, bh / ih, UPSCALE_MAX)
    dw, dh = iw * s, ih * s
    off = max(8.0, min(W, H) * 0.022)                       # eltolt színes lap a fotó mögött: keret-hatás
    d.layout = {"photo": "split"}
    if wide:
        x, y = W - m - dw - off, (H - dh - off) / 2
        d.rect(x + off, y + off, dw, dh, t["accent"], opacity=55)
        d.image(img.path, x, y, dw, dh)
        _social_texts(d, data, t, m, x - m - W * 0.05, H * 0.1, H * 0.9, center=True)
        return
    # négyzetes / álló vászon: fotó fent, szöveg alatta — a kettő együtt függőlegesen középen
    gap = H * 0.045
    text_h = _social_texts(d, data, t, m, W - 2 * m, 0, H * 0.45, measure=True)
    x = (W - dw - off) / 2
    y = max(H * 0.07, (H - (dh + off + gap + text_h)) / 2)
    d.rect(x + off, y + off, dw, dh, t["accent"], opacity=55)
    d.image(img.path, x, y, dw, dh)
    _social_texts(d, data, t, m, W - 2 * m, y + dh + off + gap, H * 0.95)


def _social_texts(d: Doc, data, t, x, w, top, bottom, center: bool = False, measure: bool = False) -> float:
    """A social-kártya szövegei a [top, bottom] sávban, egymás alatt, MÉRT magassággal (a betűfájlból tördelve):
    színes vonal, cím, alcím, elérhetőség. center: a tömb függőlegesen középre (a fotó mellett).
    measure: csak a tömb magasságát adja vissza, nem rajzol."""
    from app.tools.design_auto import fit_size
    W, H = d.w, d.h
    base = W * (1.2 if H / W > 1.4 else 1.0)                # álló (story) vásznon nagyobb betű: telefonon teljes szélességben nézik
    span = bottom - top
    gap = base * 0.022
    rule_h = max(4, H * 0.008)
    title, sub, handle = data.get("title", ""), data.get("subtitle"), data.get("handle")
    ts, th = fit_size(title, w, span * (0.5 if sub else 0.7), 0.55, 1.1, base * 0.06, 14, t["font_heading"], "Bold")
    ss, sh = fit_size(sub, w, span * 0.3, 0.5, 1.3, base * 0.027, 10, t["font_body"], "Regular") if sub else (0, 0)
    hs = base * 0.022
    total = rule_h + gap * 1.2 + th + (gap * 0.8 + sh if sub else 0) + (gap * 1.4 + hs * 1.3 if handle else 0)
    if measure:
        return total
    y = top + max(0.0, (span - total) / 2) if center else top
    d.rect(x, y, min(w * 0.18, W * 0.1), rule_h, t["accent"])
    y += rule_h + gap * 1.2
    d.text(x, y, title, size=ts, font=t["font_heading"], style="Bold", color="#FFFFFF",
           w=w, h=th * 1.08, fit="shrinkText", leading=ts * 1.1)
    y += th
    if sub:
        y += gap * 0.8
        d.text(x, y, sub, size=ss, font=t["font_body"], color="#E8EAF0", w=w, h=sh * 1.08, fit="shrinkText", leading=ss * 1.3)
        y += sh
    if handle:
        y += gap * 1.4
        d.text(x, min(y, bottom - hs * 1.45), handle, size=hs, font=t["font_body"], style="Bold", color=t["accent"],
               w=w, h=hs * 1.45, fit="shrinkText")
    return total


def _card(d: Doc, data, t, img):
    """Névjegy (85×55 mm): név, beosztás, szervezet, elérhetőségek; bal oldalt színcsík."""
    W, H = d.w, d.h
    m = W * 0.08
    d.rect(0, 0, W, H, t["bg"])
    d.rect(0, 0, W * 0.035, H, t["primary"])
    d.rect(W * 0.035, 0, W * 0.012, H, t["accent"])
    x0 = W * 0.047 + m
    d.text(x0, H * 0.16, data.get("name") or data.get("recipient") or "", size=15, font=t["font_heading"], style="Bold",
           color=t["primary"], w=W - x0 - m, h=22, fit="shrinkText")
    if data.get("role"):
        d.text(x0, H * 0.33, data["role"], size=8.5, font=t["font_body"], color=t["muted"], w=W - x0 - m, h=13, fit="shrinkText")
    if data.get("organization"):
        d.text(x0, H * 0.43, data["organization"].upper(), size=7, font=t["font_body"], style="Bold", color=t["accent"],
               w=W - x0 - m, h=11, tracking=80, fit="shrinkText")
    lines = [x for x in (data.get("phone"), data.get("email"), data.get("web"), data.get("address")) if x]
    if lines:
        d.text(x0, H * 0.6, "\n".join(lines), size=7.5, font=t["font_body"], color=t["text"], w=W - x0 - m, h=H * 0.34,
               leading=10.5, fit="shrinkText")


def _invitation(d: Doc, data, t, img):
    """Meghívó: középre zárt, keretes; „Dear {name}” sor, szöveg, időpont/hely, visszajelzés."""
    W, H = d.w, d.h
    m = W * 0.09
    d.rect(0, 0, W, H, t["bg"])
    d.rect(m * 0.4, m * 0.4, W - m * 0.8, H - m * 0.8, t["accent"], opacity=10)
    d.rect(m * 0.4 + 3, m * 0.4 + 3, W - m * 0.8 - 6, H - m * 0.8 - 6, t["bg"])
    c = dict(w=W - 2 * m, align="center")
    y = H * 0.16
    if img:
        ih = H * 0.24
        d.image(img.path, m, y, W - 2 * m, ih)
        y += ih + H * 0.04
    d.text(m, y, data.get("title") or "You're invited", size=W * 0.075, font=t["font_heading"], style="Bold",
           color=t["primary"], h=H * 0.12, fit="shrinkText", leading=W * 0.085, **c)
    y += H * 0.13
    d.rect(W / 2 - 26, y, 52, 2.5, t["accent"])
    y += H * 0.035
    if data.get("recipient"):
        d.text(m, y, data["recipient"], size=W * 0.04, font=t["font_heading"], style="Italic", color=t["text"], h=H * 0.06,
               fit="shrinkText", **c)
        y += H * 0.065
    if data.get("text"):
        d.text(m, y, data["text"], size=W * 0.028, font=t["font_body"], color=t["text"], h=H * 0.18, fit="shrinkText",
               leading=W * 0.042, **c)
        y += H * 0.19
    meta = "\n".join(x for x in (data.get("date"), data.get("place")) if x)
    if meta:
        d.text(m, y, meta, size=W * 0.032, font=t["font_body"], style="Bold", color=t["primary"], h=H * 0.1, fit="shrinkText",
               leading=W * 0.046, **c)
    if data.get("rsvp"):
        d.text(m, H * 0.86, data["rsvp"], size=W * 0.024, font=t["font_body"], color=t["muted"], h=H * 0.05, fit="shrinkText", **c)


def _booklet(d: Doc, data, t, imgs: dict):
    W, H = d.w, d.h
    m = W * 0.1
    _report_cover(d, {"title": data.get("title"), "subtitle": data.get("subtitle"), "author": data.get("author"),
                      "date": data.get("date"), "organization": data.get("organization")}, t, imgs.get("cover"))
    for k, sec in enumerate(data.get("sections") or []):
        d.new_page()
        d.rect(0, 0, W, H, t["bg"])
        d.text(m, H * 0.07, f"{k + 1:02d}", size=13, font=t["font_body"], style="Bold", color=t["accent"], w=60, h=20)
        d.text(m, H * 0.095, sec.get("heading", ""), size=26, font=t["font_heading"], style="Bold", color=t["primary"],
               w=W - 2 * m, h=H * 0.08, fit="shrinkText")
        d.rect(m, H * 0.18, 50, 3, t["accent"])
        y = H * 0.21
        simg = imgs.get(f"s{k}")
        if simg:
            d.image(simg.path, m, y, W - 2 * m, H * 0.25)
            y += H * 0.27
        text = sec.get("text", "")
        # Bőven becsült folytatólagos oldalak (≥2600 kar/oldal); a fölösleget a render() méréssel törli
        cap_first = 2600 * (H * 0.9 - y) / (H * 0.69)
        extra = max(0, math.ceil((len(text) - cap_first) / 2600)) if len(text) > cap_first else 0
        ids = [d.text(m, y, text, size=11, font=t["font_body"], color=t["text"], w=W - 2 * m, h=H * 0.9 - y, leading=15.5)]
        d.cmds.append({"command": "text.areaOptions", "params": {"ids": [ids[0]], "columns": 2, "gutter": 20}})
        for _ in range(min(extra, 8)):
            d.new_page()
            d.rect(0, 0, W, H, t["bg"])
            nid = d.text(m, H * 0.08, " ", size=11, font=t["font_body"], color=t["text"], w=W - 2 * m, h=H * 0.82, leading=15.5)
            d.cmds.append({"command": "text.areaOptions", "params": {"ids": [nid], "columns": 2, "gutter": 20}})
            ids.append(nid)
        d.thread(ids)


async def _pdftotext(pdf: Path, layout: bool) -> str:
    import asyncio
    proc = await asyncio.create_subprocess_exec("pdftotext", *(["-layout"] if layout else []), str(pdf), "-",
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return out.decode("utf-8", "replace")


async def _probe_text(d: Doc, layout: bool = True) -> str:
    """Próbakör: a parancslistát PDF-be rendereli, és visszaadja a szövegrétegét (oldalanként \\f-fel elválasztva)."""
    import json
    from app.storage import temp_path
    from app.storage.files import new_file_id
    tmp = temp_path(new_file_id("pdf"))
    args = ["run"]
    for c in d.cmds:
        args += ["--cmd", c["command"]] + (["--params", json.dumps(c["params"], ensure_ascii=False)] if c.get("params") else [])
    args += ["--export", str(tmp)]
    try:
        await crafts.run_cli(crafts.vector_cli(), "vectorcraft-cli", *args)
        return await _pdftotext(tmp, layout)
    finally:
        tmp.unlink(missing_ok=True)


async def _trim_empty_pages(d: Doc) -> None:
    """Próbakör: PDF-be renderel, pdftotext-tel megnézi, mely oldalak üresek (a túlbecsült
    folytatólagos oldalak), és artboard.delete-tel kiveszi őket (hátulról, hogy az indexek ne csússzanak)."""
    pages = (await _probe_text(d)).split("\f")
    empty = [i for i, txt in enumerate(pages[: d.page + 1]) if i > 0 and not txt.strip()]
    for i in sorted(empty, reverse=True):
        d.cmds.append({"command": "artboard.delete", "params": {"index": i}})
    d.page -= len(empty)


# ---------- ellenőrzés: minden szöveg látszik-e, és a fotó éles-e ----------

def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold()


def missing_texts(placed: list[str], pdf_text: str) -> list[str]:
    """A lerakott szövegek közül azok, amelyek NINCSENEK a PDF szövegrétegében (a doboz túl kicsi, lecsúszott a lapról).
    Egyezés: szóköz nélkül részsztringként (sortörés, ritkított betűk), vagy a szavak ≥90%-a megvan (hasábok sorrendje)."""
    flat = re.sub(r"\s+", "", _norm(pdf_text))
    words = Counter(_norm(pdf_text).split())
    out = []
    for s in placed:
        n = _norm(s or "").strip()
        if not n or re.sub(r"\s+", "", n) in flat:
            continue
        ws = n.split()
        if sum(1 for x in ws if words[x] > 0) / len(ws) >= 0.9:
            continue
        out.append(s)
    return out


def _short(s: str, n: int = 60) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


async def _check(d: Doc, template: str, res: dict[str, Any], n_photos: int) -> dict[str, Any]:
    from app.storage import output_path
    from app.tools import checks
    placed = [c["params"]["text"] for c in d.cmds
              if c["command"] == "text.create" and str(c["params"].get("text") or "").strip()]
    pdf = next((output_path(f["file_id"]) for f in res.get("files") or [] if str(f.get("file_id", "")).endswith(".pdf")), None)
    text = await (_pdftotext(pdf, False) if pdf is not None and pdf.exists() else _probe_text(d, False))
    findings, verified = [], []
    gone = missing_texts(placed, text)
    for s in gone:
        findings.append(f"Text not shown — its box is too small or it falls off the page: “{_short(s)}”.")
    if not gone and placed:
        verified.append(f"All {len(placed)} texts are on the design (checked in its PDF text layer).")
    for p, w, h in d.images[:n_photos]:
        try:
            iw, _ = _img_size(p)
        except Exception:
            continue
        if template == "social":
            up = w / max(iw, 1)
            if up > UPSCALE_MAX + 0.01:
                findings.append(f"A photo is enlarged {up:.1f}× — it will look soft. Use one at least {int(w / UPSCALE_MAX)} px wide.")
            else:
                how = " (too small or the wrong shape to fill the background, so it sits beside the text)" \
                    if d.layout.get("photo") == "split" else ""
                verified.append(f"The photo is shown at {up:.2f}× its pixels — sharp{how}.")
        else:
            dpi = 72 * iw / max(w, 1)
            if dpi < PRINT_DPI_MIN:
                findings.append(f"A photo prints at about {dpi:.0f} dpi — it will look soft. "
                                f"Use one at least {int(w / 72 * 300)} px wide (300 dpi).")
            else:
                verified.append(f"The photo prints at about {dpi:.0f} dpi.")
    status = "failed" if gone else ("needs_check" if findings else "ok")
    return checks.make(status, findings + ([checks.LOOK] if status != "ok" else []), verified)


# ---------- belépési pont ----------

def _frame_aspect(template: str, w: float, h: float) -> float:
    return {"poster": w / (h * 0.56), "flyer": (w * 0.84) / (h * 0.27), "report_cover": w / h,
            "social": w / h, "certificate": 1.0, "invitation": (w * 0.82) / (h * 0.24)}.get(template, w / h)


FNS = {"poster": _poster, "flyer": _flyer, "report_cover": _report_cover, "certificate": _certificate, "social": _social,
       "card": _card, "invitation": _invitation}


def size_for(template: str, size: Optional[str]) -> tuple[float, float]:
    """A sablon lapmérete (pt): közösségi méretek, fekvő oklevél, névjegy, nyomtatott lapméretek."""
    if template == "social":
        return {"landscape": (1200, 630), "square": (1080, 1080), "story": (1080, 1920)}.get(size or "landscape", (1200, 630))
    if template == "certificate":
        return SIZES.get(size or "A4", SIZES["A4"])[::-1]          # fekvő
    if template == "card":
        return CARD
    if template == "invitation":
        return SIZES.get(size or "A5", SIZES["A5"])
    return SIZES.get(size or "A4", SIZES["A4"])


async def render(template: str, data: dict[str, Any], *, size: Optional[str] = None, theme: Optional[dict] = None,
                 brand: Optional[Any] = None, export: Optional[list[str]] = None) -> dict[str, Any]:
    if template not in TEMPLATES:
        raise ValueError(f"Unknown template: {template} (choose from {', '.join(sorted(TEMPLATES))})")
    t = _theme(data, theme, brand)
    w, h = size_for(template, size)
    d = Doc(w, h, data.get("title") or data.get("name") or template)

    reqs: list[tuple[str, Any, float]] = []
    if template == "booklet":
        if data.get("cover_image"):
            reqs.append(("cover", data["cover_image"], w / h))
        for k, sec in enumerate(data.get("sections") or []):
            if sec.get("image"):
                reqs.append((f"s{k}", sec["image"], (w * 0.8) / (h * 0.25)))
    elif data.get("image"):
        # social: vágatlanul — a sablon a kép méretéből és arányából dönti el, hogy háttér lesz-e vagy keretes fotó
        reqs.append(("main", data["image"], None if template == "social" else _frame_aspect(template, w, h)))
    resolved = await images.resolve_many([(r[1], r[2]) for r in reqs])
    imgs = {r[0]: x for r, x in zip(reqs, resolved)}
    try:
        fn = FNS.get(template)
        if fn:
            fn(d, data, t, imgs.get("main"))
            n_photos = len(d.images)                      # a logó nem fotó: azt a check nem méri
            _logo(d, brand, template)
        else:
            _booklet(d, data, t, imgs)
            n_photos = len(d.images)
            await _trim_empty_pages(d)
        from app.storage.naming import design_name
        res = await crafts.vector_run(commands=d.cmds, export=export or ["pdf", "png"], name=design_name(template, data))
        try:
            res["check"] = await _check(d, template, res, n_photos)
        except Exception as e:                            # a mérés hiánya nem ronthatja el a kész dizájnt
            import logging
            logging.getLogger("aice.design").info("design check: %s", e)
    finally:
        for x in resolved:
            x.path.unlink(missing_ok=True)
        for p in d.tmp:
            p.unlink(missing_ok=True)
    res["template"] = template
    res["pages"] = d.page + 1
    res["credits"] = [x.attribution for x in resolved if x.attribution]
    if d.layout:
        res["layout"] = d.layout
    return res
