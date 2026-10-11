"""Dizájnsablonok a VectorCrafttal: plakát, szórólap, jelentés-borító, oklevél, közösségi kártya, füzet.

Egy sablon = paraméteres elrendezés, VectorCraft-parancsokká fordítva (vector_run), PDF/PNG/SVG kimenettel.
Képek: images.resolve (file_id / URL / FLUX / stock), a keret arányára vágva, beágyazva.

Betűk: címek "Noto Serif" (Bold is van; a szerveren a fonts-noto adja), szöveg "Source Sans 3"
(a VectorCraft beépített betűje Regular/Semibold/Bold/Italic). A Source Serif 4 csak Regular — címnek nem jó.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from app.tools import crafts, images

TEMPLATES = {"poster", "flyer", "report_cover", "certificate", "social", "booklet"}
DEFAULT_THEME = {
    "primary": "#14213D", "accent": "#FCA311", "bg": "#FFFFFF", "panel": "#F4F5F7",
    "text": "#1E2430", "muted": "#6B7280", "font_heading": "Noto Serif", "font_body": "Source Sans 3",
}
SIZES = {"A4": (595.3, 841.9), "A3": (841.9, 1190.6), "A5": (419.5, 595.3), "letter": (612, 792)}
GAP = 60  # pt az artboardok között


class Doc:
    """VectorCraft-parancslista építő; az objektum-id-k 2-től egyesével nőnek (mérve), oldalak artboardok."""

    def __init__(self, w: float, h: float, name: str):
        self.w, self.h = w, h
        self.cmds: list[dict[str, Any]] = [{"command": "file.new", "params": {"width": w, "height": h, "units": "Points", "name": name}}]
        self.next_id = 2
        self.page = 0

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
            p["area"] = {"width": w, "height": h or size * 3}
            if fit:
                p["area"]["fit"] = fit
        i = self._add("text.create", p, creates=True)
        st = {k: v for k, v in (("justify", align), ("leading", leading), ("tracking", tracking)) if v is not None}
        if st:
            self._add("text.setStyle", st)
        return i

    def image(self, path, x, y, w, h) -> int:
        return self._add("file.place", {"path": str(path), "rect": [self.ox + x, y, w, h], "link": False}, creates=True)

    def thread(self, ids: list[int]) -> None:
        if len(ids) > 1:
            self._add("select.set", {"ids": ids})
            self._add("text.thread.create")


def _theme(data: dict[str, Any], theme: Optional[dict], brand: Optional[str]) -> dict[str, str]:
    t = dict(DEFAULT_THEME)
    if brand:
        from app.brands import get_brand
        b = get_brand(brand)
        if b is None:
            raise ValueError(f"ismeretlen brand: {brand}")
        t.update(primary=b.colors.primary, accent=b.colors.accent, bg=b.colors.body_bg, panel=b.colors.panel_bg,
                 text=b.colors.text_primary, muted=b.colors.text_muted)
    t.update({k: v for k, v in (theme or {}).items() if k in DEFAULT_THEME and v})
    return t


# ---------- sablonok ----------

def _poster(d: Doc, data, t, img):
    W, H = d.w, d.h
    m = W * 0.07
    d.rect(0, 0, W, H, t["primary"])
    ih = H * 0.56
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
    d.text(80, H * 0.13, (data.get("title") or "Oklevél").upper(), size=40, font=t["font_heading"], style="Bold",
           color=t["primary"], h=56, tracking=150, **c)
    d.rect(W / 2 - 40, H * 0.255, 80, 3, t["accent"])
    d.text(80, H * 0.3, data.get("lead", "Ezt az oklevelet"), size=15, font=t["font_body"], color=t["muted"], h=24, **c)
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


def _social(d: Doc, data, t, img):
    W, H = d.w, d.h
    m = W * 0.07
    if img:
        d.image(img.path, 0, 0, W, H)
        d.rect(0, 0, W, H, "#000000", opacity=30)
        d.rect(0, H * 0.5, W, H * 0.5, t["primary"], opacity=72)
    else:
        d.rect(0, 0, W, H, t["primary"])
    d.rect(m, H * 0.58, W * 0.1, max(4, H * 0.008), t["accent"])
    d.text(m, H * 0.61, data.get("title", ""), size=W * 0.06, font=t["font_heading"], style="Bold", color="#FFFFFF",
           w=W - 2 * m, h=H * 0.22, fit="shrinkText", leading=W * 0.066)
    if data.get("subtitle"):
        d.text(m, H * 0.84, data["subtitle"], size=W * 0.027, font=t["font_body"], color="#E8EAF0", w=W - 2 * m,
               h=H * 0.07, fit="shrinkText")
    if data.get("handle"):
        d.text(m, H * 0.93, data["handle"], size=W * 0.022, font=t["font_body"], style="Bold", color=t["accent"],
               w=W - 2 * m, h=H * 0.05)


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


async def _trim_empty_pages(d: Doc) -> None:
    """Próbakör: PDF-be renderel, pdftotext-tel megnézi, mely oldalak üresek (a túlbecsült
    folytatólagos oldalak), és artboard.delete-tel kiveszi őket (hátulról, hogy az indexek ne csússzanak)."""
    import asyncio
    from app.storage import temp_path
    from app.storage.files import new_file_id
    tmp = temp_path(new_file_id("pdf"))
    args = ["run"]
    for c in d.cmds:
        args += ["--cmd", c["command"]] + (["--params", __import__("json").dumps(c["params"], ensure_ascii=False)] if c.get("params") else [])
    args += ["--export", str(tmp)]
    try:
        await crafts.run_cli(crafts.vector_cli(), "vectorcraft-cli", *args)
        proc = await asyncio.create_subprocess_exec("pdftotext", "-layout", str(tmp), "-",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
    finally:
        tmp.unlink(missing_ok=True)
    pages = out.decode("utf-8", "replace").split("\f")
    empty = [i for i, txt in enumerate(pages[: d.page + 1]) if i > 0 and not txt.strip()]
    for i in sorted(empty, reverse=True):
        d.cmds.append({"command": "artboard.delete", "params": {"index": i}})
    d.page -= len(empty)


# ---------- belépési pont ----------

def _frame_aspect(template: str, w: float, h: float) -> float:
    return {"poster": w / (h * 0.56), "flyer": (w * 0.84) / (h * 0.27), "report_cover": w / h,
            "social": w / h, "certificate": 1.0}.get(template, w / h)


async def render(template: str, data: dict[str, Any], *, size: Optional[str] = None, theme: Optional[dict] = None,
                 brand: Optional[str] = None, export: Optional[list[str]] = None) -> dict[str, Any]:
    if template not in TEMPLATES:
        raise ValueError(f"ismeretlen sablon: {template} (lehet: {', '.join(sorted(TEMPLATES))})")
    t = _theme(data, theme, brand)
    if template == "social":
        w, h = {"landscape": (1200, 630), "square": (1080, 1080), "story": (1080, 1920)}.get(size or "landscape", (1200, 630))
    elif template == "certificate":
        w, h = SIZES.get(size or "A4", SIZES["A4"])[::-1]          # fekvő
    else:
        w, h = SIZES.get(size or "A4", SIZES["A4"])
    d = Doc(w, h, data.get("title") or template)

    reqs: list[tuple[str, Any, float]] = []
    if template == "booklet":
        if data.get("cover_image"):
            reqs.append(("cover", data["cover_image"], w / h))
        for k, sec in enumerate(data.get("sections") or []):
            if sec.get("image"):
                reqs.append((f"s{k}", sec["image"], (w * 0.8) / (h * 0.25)))
    elif data.get("image"):
        reqs.append(("main", data["image"], _frame_aspect(template, w, h)))
    resolved = await images.resolve_many([(r[1], r[2]) for r in reqs])
    imgs = {r[0]: x for r, x in zip(reqs, resolved)}
    try:
        fn = {"poster": _poster, "flyer": _flyer, "report_cover": _report_cover, "certificate": _certificate,
              "social": _social}.get(template)
        if fn:
            fn(d, data, t, imgs.get("main"))
        else:
            _booklet(d, data, t, imgs)
            await _trim_empty_pages(d)
        res = await crafts.vector_run(commands=d.cmds, export=export or ["pdf", "png"])
    finally:
        for x in resolved:
            x.path.unlink(missing_ok=True)
    res["template"] = template
    res["pages"] = d.page + 1
    res["credits"] = [x.attribution for x in resolved if x.attribution]
    return res
