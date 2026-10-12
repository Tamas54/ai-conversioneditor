"""Pakli a minta DIÁINAK klónozásával — amikor a dizájn a diákon ül, nem a mesterdián.

Canva/Gamma-export, a saját paklijaink és sok „szép” minta így készül: üres mesterdia, a diákon háttér, alakzatok,
képek, kártyák. A mesterdia-elrendezések ilyenkor fehér paklit adnának, a téma-út csak színt és betűt vesz át.
Itt a minta minden diáját elemezzük — cím, felülcím, alcím; ismétlődő tételek (kártya, idővonal-sor, statisztika:
egymás alatti szövegek + számláló + díszítés); tartalomkép, háttérkép, diagram/táblázat-terület; lábléc —, a
pakli-leírás minden diájához a legjobban illő mintadiát választjuk (borító, szakasz, felsorolás, kép+szöveg,
statisztika, diagram, idézet, záró), klónozzuk (háttér, alakzatok, képek a kapcsolataikkal), és a szöveghelyekbe az új
tartalom kerül a minta betűjével, színével, bekezdés-stílusával, a helyhez igazított betűmérettel. A fölös tételek a
díszítésükkel együtt törlődnek; régi szöveg, régi tartalomkép, „Made with …” jelvény nem marad. Ami több diájon is
ugyanaz (logó, textúra, intézménynév a láblécben), az a minta arculata — marad.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import math
import re
import time
from collections import Counter
from typing import Any, Optional

from lxml import etree
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn

from app.storage import output_path, public_url
from app.storage.files import new_file_id
from app.tools import images

log = logging.getLogger("aice.pptx_clone")

_PT = 12700                                                          # EMU / pont
_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_LEAF = ("sp", "pic", "cxnSp", "graphicFrame")
_NUM = re.compile(r"\(?0?\d{1,2}[.)]?|[IVX]{1,4}[.)]?")              # számláló: 1, 02, 3., (4), IV
_PAGE = re.compile(r"(\d{1,3})(\s*(?:/|of|\|)\s*)(\d{1,3})")          # oldalszám: „3 / 12”, „3 of 12”
_URLISH = re.compile(r"https?://|file://|www\.|/home/|\.(com|org|net|io|hu|de)\b", re.I)
_SPLIT = re.compile(r"^(.{2,60}?)\s*(?::|\s[—–-])\s+(\S.{2,})$")       # „Fejléc: leírás” / „Fejléc — leírás”
_BULLET = re.compile(r"^\s*([•·▪■●◦–—→✓✔➤►-])\s+")


class _Leaf:
    """Egy alakzat a mintadián: abszolút téglalap (csoporton belül is), fajta, szöveg, betűméret.
    link = mindig törlendő (kattintható jelvény, „Made with …”, videó/hang, üres helyőrző)."""
    __slots__ = ("el", "i", "x", "y", "w", "h", "kind", "text", "size", "num", "stat", "link", "grp", "card", "img")

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h


# ---------- geometria ----------

def _xfrm(el):
    tag = etree.QName(el).localname
    x = el.find(qn("p:xfrm")) if tag == "graphicFrame" else None
    if x is None:
        pr = el.find(qn("p:grpSpPr" if tag == "grpSp" else "p:spPr"))
        x = pr.find(qn("a:xfrm")) if pr is not None else None
    return x


def _rect(el) -> Optional[tuple[int, int, int, int]]:
    x = _xfrm(el)
    if x is None:
        return None
    off, ext = x.find(qn("a:off")), x.find(qn("a:ext"))
    if off is None or ext is None:
        return None
    return int(off.get("x", 0)), int(off.get("y", 0)), int(ext.get("cx", 0)), int(ext.get("cy", 0))


def _grp_tf(el, tf):
    """Csoport gyerek-koordinátái → szülő-koordináták (off/ext + chOff/chExt)."""
    r, x = _rect(el), _xfrm(el)
    co = x.find(qn("a:chOff")) if x is not None else None
    ce = x.find(qn("a:chExt")) if x is not None else None
    if r is None or co is None or ce is None:
        return tf
    ox, oy = int(co.get("x", 0)), int(co.get("y", 0))
    sx, sy = r[2] / max(1, int(ce.get("cx", 1))), r[3] / max(1, int(ce.get("cy", 1)))
    return lambda c: tf((r[0] + (c[0] - ox) * sx, r[1] + (c[1] - oy) * sy, c[2] * sx, c[3] * sy))


def _inside(a, b, tol: float = 0.8) -> bool:
    """a területének legalább tol része b-ben van."""
    ix = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    iy = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    if a.area <= 0:                                  # vonal: a két végpontja legyen bent
        return b.x <= a.x <= b.x + b.w and b.x <= a.x + a.w <= b.x + b.w and b.y <= a.y <= b.y + b.h and b.y <= a.y + a.h <= b.y + b.h
    return ix > 0 and iy > 0 and ix * iy >= tol * a.area


def _xover(a, b) -> float:
    ov = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    return max(0.0, ov) / max(1, min(a.w, b.w))


def _bbox(ls) -> tuple[int, int, int, int]:
    x0, y0 = min(l[0] for l in ls), min(l[1] for l in ls)
    x1, y1 = max(l[0] + l[2] for l in ls), max(l[1] + l[3] for l in ls)
    return x0, y0, x1 - x0, y1 - y0


def _dist(l, r, lead: bool = False) -> float:
    """Az alakzat középpontja és egy téglalap távolsága (0, ha benne van). lead: a számláló a tétele ELŐTT áll
    (balra vagy fölötte) — a tétel utáni (jobbra/alatta lévő) távolság háromszorosan számít."""
    cx, cy = l.x + l.w / 2, l.y + l.h / 2
    k = 3.0 if lead else 1.0
    dx = max(r[0] - cx, 0, (cx - (r[0] + r[2])) * k)
    dy = max(r[1] - cy, 0, (cy - (r[1] + r[3])) * k)
    return math.hypot(dx, dy)


class _R:                                            # téglalap _Leaf-szerű felülettel (_inside, _xover)
    __slots__ = ("x", "y", "w", "h")

    def __init__(self, r):
        self.x, self.y, self.w, self.h = r

    @property
    def area(self):
        return max(0, self.w) * max(0, self.h)


# ---------- olvasás ----------

def _text(el) -> str:
    body = el.find(qn("p:txBody"))
    if body is None:
        return ""
    return "\n".join("".join(t.text or "" for t in p.iter(qn("a:t"))) for p in body.findall(qn("a:p"))).strip()


def _ph_size(sh) -> float:
    """Helyőrző öröklött betűmérete: elrendezés → mester helyőrző → mester szövegstílus."""
    from pptx.enum.shapes import PP_PLACEHOLDER as T
    try:
        base = sh._base_placeholder
        while base is not None:
            for d in base._element.iter(qn("a:defRPr")):
                if d.get("sz"):
                    return int(d.get("sz")) / 100
            base = getattr(base, "_base_placeholder", None)
    except Exception:
        pass
    title = sh.placeholder_format.type in (T.TITLE, T.CENTER_TITLE)
    try:
        st = sh.part.slide_layout.slide_master._element.find(qn("p:txStyles"))
        d = st.find(qn("p:titleStyle" if title else "p:bodyStyle")).find(qn("a:lvl1pPr")).find(qn("a:defRPr"))
        if d.get("sz"):
            return int(d.get("sz")) / 100
    except Exception:
        pass
    return 40.0 if title else 20.0


def _font_scale(el) -> float:
    na = el.find(".//" + qn("a:normAutofit"))
    return int(na.get("fontScale")) / 100000 if na is not None and na.get("fontScale") else 1.0


def _size(el, sh) -> float:
    """A szöveg „fő” betűmérete (pt): a futások méretének szöveghosszal súlyozott átlaga."""
    tot = n = 0.0
    for r in el.iter(qn("a:r")):
        t = "".join(x.text or "" for x in r.iter(qn("a:t")))
        rp = r.find(qn("a:rPr"))
        if t.strip() and rp is not None and rp.get("sz"):
            tot += int(rp.get("sz")) / 100 * len(t)
            n += len(t)
    size = tot / n if n else None
    if size is None:
        for d in el.iter(qn("a:defRPr"), qn("a:endParaRPr")):
            if d.get("sz"):
                size = int(d.get("sz")) / 100
                break
    if size is None:
        size = _ph_size(sh) if sh is not None and sh.is_placeholder else 18.0
    return size * _font_scale(el)


def _is_stat(t: str) -> bool:
    """Statisztika-szám: „216 000”, „~10%”, „$102”, „5.5%→10%”, „~200 GWh”."""
    t = t.strip()
    if not re.search(r"\d", t) or len(t) > 14 or len(t.split()) > 3:
        return False
    return len(t) <= 8 or sum(c.isdigit() for c in t) / max(1, len(t.replace(" ", ""))) >= 0.3


def _linked(el) -> bool:
    c = el.find(".//" + qn("p:cNvPr"))
    return c is not None and c.find(qn("a:hlinkClick")) is not None


def _img_key(part, el) -> Optional[str]:
    b = el.find(".//" + qn("a:blip"))
    rid = b.get(qn("r:embed")) if b is not None else None
    try:
        return str(part.related_part(rid).partname) if rid else None
    except Exception:
        return None


def _filled(el) -> bool:
    """Látható kitöltés vagy körvonal (a stílus-hivatkozás is az)."""
    from app.tools.pptx_template import _plain
    pr = el.find(qn("p:spPr"))
    if pr is None:
        return False
    if any(pr.find(qn(t)) is not None for t in ("a:solidFill", "a:gradFill", "a:blipFill", "a:pattFill")) and not _plain(pr):
        return True
    if pr.find(qn("a:noFill")) is None and pr.find(qn("a:solidFill")) is None:
        st = el.find(qn("p:style"))
        fr = st.find(qn("a:fillRef")) if st is not None else None
        if fr is not None and fr.get("idx", "0") != "0":
            return True
    ln = pr.find(qn("a:ln"))
    return ln is not None and ln.find(qn("a:noFill")) is None and (ln.find(qn("a:solidFill")) is not None)


def _leaves(slide) -> list[_Leaf]:
    tree = slide.shapes._spTree
    order = {el: i for i, el in enumerate(tree.iter())}
    top = {sh._element: sh for sh in slide.shapes}
    out: list[_Leaf] = []

    def walk(parent, tf, in_grp):
        for el in parent:
            if not isinstance(el.tag, str):
                continue
            tag = etree.QName(el).localname
            if tag == "grpSp":
                walk(el, _grp_tf(el, tf), True)
                continue
            if tag not in _LEAF:
                continue
            r = _rect(el)
            if r is None and el in top and top[el].width is not None:          # helyőrző: az elrendezésből örököl
                sh = top[el]
                r = (sh.left, sh.top, sh.width, sh.height)
            if r is None:
                continue
            l = _Leaf()
            l.el, l.i, l.grp = el, order[el], in_grp
            l.x, l.y, l.w, l.h = (int(v) for v in tf(r))
            l.text = _text(el) if tag == "sp" else ""
            pr = el.find(qn("p:spPr"))
            if tag == "graphicFrame":
                l.kind = "frame"
            elif tag == "pic" or (pr is not None and pr.find(qn("a:blipFill")) is not None):
                l.kind = "pic"
            elif l.text:
                l.kind = "text"
            else:
                l.kind = "shape"
            l.link = _linked(el) or el.find(".//" + qn("a:videoFile")) is not None or el.find(".//" + qn("a:audioFile")) is not None
            nv = next((c for c in el if isinstance(c.tag, str) and c.tag.endswith("Pr") and etree.QName(c).localname.startswith("nv")), None)
            if nv is not None and nv.find(".//" + qn("p:ph")) is not None and not l.text and tag == "sp" \
                    and (pr is None or pr.find(qn("a:blipFill")) is None):
                l.link = True                          # üres helyőrző: a szerkesztőben „Click to add…” maradna
            l.size = _size(el, top.get(el)) if l.kind == "text" else 0.0
            l.num = l.kind == "text" and bool(_NUM.fullmatch(l.text))
            l.stat = l.kind == "text" and not l.num and _is_stat(l.text)
            l.card = l.img = None
            out.append(l)
    walk(tree, lambda r: r, False)
    return out


# ---------- egy mintadia elemzése ----------

def _stacked(a, b, SH: int) -> bool:
    """Egy tételhez tartozik-e (egymás alatt, átfedő oszlopban, kis réssel): fejléc → leírás."""
    up, lo = (a, b) if a.y <= b.y else (b, a)
    if lo.y < up.y + 0.4 * up.h or _xover(up, lo) < 0.5:
        return False
    return lo.y - (up.y + up.h) <= max(0.025 * SH, 1.6 * up.size * _PT)


def _reading(items: list[dict]) -> list[dict]:
    """Olvasási sorrend: sorok fentről lefelé, a soron belül balról jobbra."""
    rows: list[list[dict]] = []
    for it in sorted(items, key=lambda it: it["region"][1]):
        if rows and it["region"][1] < rows[-1][0]["region"][1] + 0.5 * rows[-1][0]["region"][3]:
            rows[-1].append(it)
        else:
            rows.append([it])
    return [it for r in rows for it in sorted(r, key=lambda it: it["region"][0])]


def _similar(a: dict, b: dict) -> bool:
    sa, sb = a["slots"], b["slots"]
    wa, wb = a["region"][2], b["region"][2]
    return (len(sa) == len(sb) and sa[0].stat == sb[0].stat
            and abs(sa[0].size - sb[0].size) <= 0.15 * max(sa[0].size, sb[0].size)
            and abs(wa - wb) <= 0.35 * max(wa, wb))


def _analyze(slide, leaves: list[_Leaf], idx: int, SW: int, SH: int, deco_imgs: set) -> Optional[dict[str, Any]]:
    from app.tools.pptx_template import _plain
    A = SW * SH
    texts = [l for l in leaves if l.kind == "text" and not l.link]
    sizes = sorted(l.size for l in texts if not l.num)
    med = sizes[len(sizes) // 2] if sizes else 18.0
    for l in texts:                                # nagy szám (60 pt „12”) statisztika, nem számláló
        if l.num and l.size >= 1.6 * med:
            l.num, l.stat = False, True

    def zone(l):                                   # lábléc-/fejléc-sáv
        return l.y >= 0.86 * SH or l.y + l.h <= 0.08 * SH

    cand = [l for l in texts if not l.num and not l.stat and len(l.text) >= 2 and not zone(l)]
    if not cand:
        return None
    big = max(c.size for c in cand)
    title = min((c for c in cand if c.size >= 0.65 * big), key=lambda c: (c.y, c.x))

    # kártyák: kitöltött/keretes alakzat, ami szöveget tart (a teljes szélességű sáv nem kártya)
    cards = [s for s in leaves if s.kind == "shape" and not s.link and 0.004 * A <= s.area <= 0.45 * A
             and 0.05 * SW <= s.w <= 0.96 * SW and s.h >= 0.04 * SH]
    for t in texts:
        cs = [c for c in cards if _inside(t, c)]
        t.card = min(cs, key=lambda c: c.area) if cs else None

    used = {title}

    def near(l, above):
        if l in used or l.num or l.card is not title.card or _xover(l, title) < 0.5:
            return False
        if above:
            gap = title.y - (l.y + l.h)
            far = 0.3 * SH if l.size <= 0.45 * title.size else 0.15 * SH          # kis felülcím messzebb is ülhet
            return -0.3 * title.h <= gap <= far and l.size <= 0.7 * title.size and len(l.text) <= 80
        gap = l.y - (title.y + title.h)
        return (l.y >= title.y + 0.5 * title.h and gap <= max(0.06 * SH, 1.2 * title.size * _PT)
                and l.size < title.size and l.w >= 0.5 * title.w
                and l.text.count("\n") <= 1 and len(l.text) <= 400)               # felsorolás nem alcím

    kick = [l for l in texts if near(l, True)]
    kicker = max(kick, key=lambda l: l.y) if kick else None
    if kicker:
        used.add(kicker)
    subs = sorted((l for l in texts if near(l, False)), key=lambda l: l.y)
    sub = subs[0] if subs else None
    if sub:
        used.add(sub)
    feet = [l for l in texts if l not in used and zone(l) and l.size <= max(14.0, 0.5 * title.size) and l.card is None]
    used.update(feet)

    # tételek: egy kártyában lévő, illetve egymás alatt szorosan álló szövegek
    rest = [l for l in texts if l not in used]
    nums = [l for l in rest if l.num]
    body = [l for l in rest if not l.num]
    uf = list(range(len(body)))

    def root(i):
        while uf[i] != i:
            uf[i] = uf[uf[i]]
            i = uf[i]
        return i
    for i in range(len(body)):
        for j in range(i + 1, len(body)):
            a, b = body[i], body[j]
            ok = (a.card is b.card) if (a.card is not None or b.card is not None) else _stacked(a, b, SH)
            if ok:
                uf[root(i)] = root(j)
    groups: dict[int, list[_Leaf]] = {}
    for i, l in enumerate(body):
        groups.setdefault(root(i), []).append(l)
    items = []
    for g in groups.values():
        slots = sorted(g, key=lambda l: (l.y, l.x))
        card = slots[0].card
        reg = card.rect if card is not None else _bbox([l.rect for l in slots])
        items.append({"slots": slots, "card": card, "region": reg, "nums": [], "deco": [card] if card is not None else []})
    orphans = []
    base = [it["region"] for it in items]               # a számlálók a tételek SAJÁT helyéhez mérve (nem a bővülőhöz)
    for n in nums:
        own = [k for k, it in enumerate(items) if n.card is not None and it["card"] is n.card]
        k = own[0] if own else min(range(len(items)), key=lambda k: _dist(n, base[k], True), default=None)
        if k is not None and (own or _dist(n, base[k], True) <= 0.1 * SW):
            items[k]["nums"].append(n)
        else:
            orphans.append(n)
    for it in items:
        if it["nums"]:
            it["region"] = _bbox([it["region"]] + [n.rect for n in it["nums"]])
    # díszítés: ami a tétel kártyájában van, vagy kicsi és a tétel közvetlen környezetében
    item_cards = {id(it["card"]) for it in items if it["card"] is not None}
    for l in leaves:
        if l.kind not in ("shape", "pic") or l.link or id(l) in item_cards or (l.kind == "pic" and (l.area >= 0.03 * A or l.img in deco_imgs)):
            continue
        hit = [it for it in items if it["card"] is not None and _inside(l, it["card"])]
        if not hit and l.area < 0.06 * A:
            hit = [it for it in items if _inside(l, _R((it["region"][0] - 0.05 * SW, it["region"][1] - 0.03 * SH,
                                                         it["region"][2] + 0.1 * SW, it["region"][3] + 0.06 * SH)), 0.98)]
        if hit:
            min(hit, key=lambda it: _dist(l, it["region"]))["deco"].append(l)

    items = _reading(items)
    sets: list[list[dict]] = []
    for it in items:
        for s in sets:
            if _similar(s[0], it):
                s.append(it)
                break
        else:
            sets.append([it])
    main = max(sets, key=lambda s: (len(s), sum(i["region"][2] * i["region"][3] for i in s)), default=[])
    if len(main) == 1 and len(items) > 1:          # nincs ismétlődő minta: különböző panelek
        main = items[:]
    extras = [it for it in items if it not in main]

    pics = [l for l in leaves if l.kind == "pic" and not l.link and l.img not in deco_imgs and l.area >= 0.03 * A]
    bg = [p for p in pics if p.area >= 0.6 * A]
    content = [p for p in pics if p.area < 0.6 * A]
    pic = max(content, key=lambda p: p.area, default=None)
    side = None
    if pic is not None:
        side = ("top" if pic.y + pic.h / 2 < SH / 2 else "bottom") if pic.w >= 0.75 * SW else ("left" if pic.x + pic.w / 2 < SW / 2 else "right")
    bgx = slide._element.find(qn("p:cSld")).find(qn("p:bg"))
    designed = ((bgx is not None and not _plain(bgx)) or any(l.kind == "pic" and not l.link for l in leaves)
                or any(l.kind in ("shape", "text") and _filled(l.el) for l in leaves))
    return {"slide": slide, "idx": idx, "leaves": leaves, "title": title, "kicker": kicker, "sub": sub, "footers": feet,
            "items": main, "extras": extras, "orphans": orphans, "n": len(main), "single": len(main) == 1,
            "stat": bool(main) and all(it["slots"][0].stat for it in main), "pic": pic, "pics": content,
            "bg": bg[0] if bg else None, "frames": [l for l in leaves if l.kind == "frame"], "side": side,
            "designed": designed, "last": False, "A": A,
            "quoteish": title.text[:1] in "“\"„«'" or len(title.text) >= 60}


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip().lower()


def _deck(prs) -> tuple[list[dict[str, Any]], set]:
    SW, SH = prs.slide_width, prs.slide_height
    per = []
    for idx, slide in enumerate(prs.slides):
        if slide._element.get("show") == "0":                  # rejtett dia
            continue
        lv = _leaves(slide)
        for l in lv:
            if l.kind == "pic":
                l.img = _img_key(slide.part, l.el)
        per.append((idx, slide, lv))
    # ugyanaz a (kisebb) kép több dián = arculat (logó, jelvény, díszítés) — nem tartalomkép, nem cseréljük
    A = SW * SH
    seen = Counter(k for _, _, lv in per for k in {l.img for l in lv if l.kind == "pic" and l.img and l.area < 0.1 * A})
    deco = {k for k, c in seen.items() if c >= 2}
    an = [a for a in (_analyze(s, lv, idx, SW, SH, deco) for idx, s, lv in per) if a]
    if an:
        an[-1]["last"] = True
    feet = Counter(t for a in an for t in {_norm(f.text) for f in a["footers"]})
    brand = {t for t, c in feet.items() if c >= 2 and not _URLISH.search(t) and not _PAGE.search(t) and not _NUM.fullmatch(t)}
    return an, brand


def clonable(prs) -> bool:
    """Klónozható-e: a diák fele legalább dizájnos (háttér, kép, kitöltött alakzat), és van tartalomra alkalmas dia."""
    try:
        an, _ = _deck(prs)
    except Exception as e:
        log.warning("mintadia-elemzés: %s", e)
        return False
    if not an:
        return False
    return 2 * sum(1 for a in an if a["designed"]) >= len(an) and any(a["n"] >= 1 or a["frames"] for a in an)


# ---------- mintadia választása ----------

def _lines(s: dict[str, Any]) -> list[str]:
    return [str(x) for x in (s.get("bullets") or ([s["text"]] if s.get("text") else []))]


def _hole(a: dict[str, Any]) -> float:
    """Ennyi (a dia területéhez mérve) tűnik el a mintadiáról tartalomként (külön panelek, diagramok) — üres folt."""
    return (sum(it["region"][2] * it["region"][3] for it in a["extras"]) + sum(f.area for f in a["frames"])) / a["A"]


def _cap(a: dict[str, Any]) -> float:
    """A tételek szöveghelyeinek összterülete — mennyi szöveg fér el kényelmesen (0…1)."""
    return min(1.0, sum(sl.area for it in a["items"] for sl in it["slots"]) / (0.25 * a["A"]))


def _score(a: dict[str, Any], s: dict[str, Any]) -> float:
    lay, n = s.get("layout"), a["n"]
    img = bool(s.get("image"))
    if lay == "cover":
        sc = 100 if a["idx"] == 0 else 40 if n == 0 else 10
        sc += 15 if a["sub"] else 0
        sc += (20 if (a["pic"] or a["bg"]) else -5) if img else (-25 if a["pic"] else 0)
        return sc - 60 * _hole(a)
    if lay in ("section", "closing", "quote"):
        sc = 60 if n == 0 else 5
        if lay == "closing" and a["last"] and n == 0:
            sc += 50
        if lay == "section" and a["idx"] != 0 and n == 0 and not a["last"]:
            sc += 20
        if lay == "quote" and n == 0 and a["quoteish"]:
            sc += 30
        if a["idx"] == 0:
            sc += 10
        return sc - (20 if a["pic"] else 0) - (30 if a["frames"] else 0) - 60 * _hole(a)
    cover = -30 if a["idx"] == 0 else 0
    if lay in ("bullets", "image_left", "image_right"):
        k = len(_lines(s)) or 1
        if n == 0:
            sc = 0 if a["frames"] else -50
        elif a["stat"]:
            sc = -20                                   # statisztika-kártya nem felsorolásnak való
        elif a["single"]:
            sc = 50 - (5 if k > 6 else 0)
        else:
            sc = 70 - 12 * (n - k) if n >= k else 40 - 15 * (k - n)
        if lay != "bullets" or img:
            sc += 30 if a["pic"] else (-10 if a["single"] else -40)
            if a["pic"] and lay != "bullets" and a["side"] == lay.split("_")[1]:
                sc += 5
        elif a["pic"]:
            sc -= 50                                   # a tartalomkép kiesne: üres folt
        return sc + cover + 12 * _cap(a) - 60 * _hole(a)
    if lay == "stats":
        k = len(s.get("stats") or []) or 1
        if n and a["stat"]:
            sc = 90 - 10 * (n - k) if n >= k else 50 - 15 * (k - n)
        elif n >= k and not a["single"]:
            sc = 50 - 8 * (n - k)
        else:
            sc = 25 if (a["frames"] or a["single"]) else 0
        return sc - (30 if a["pic"] else 0) + cover - 60 * _hole(a)
    if lay in ("chart", "table", "gallery"):
        sc = 80 if a["frames"] else 40 if n else 0
        return sc - (25 if a["pic"] else 0) + cover
    if lay == "full_image":
        sc = 80 if a["bg"] else 60 if (a["pic"] and n == 0) else 40 if a["pic"] else 0
        return sc + (0 if img else -40)
    return 0


def _choose(an: list[dict[str, Any]], s: dict[str, Any], used: dict[int, int]) -> dict[str, Any]:
    """A legjobb mintadia; a közel egyformán jók közül a legkevésbé használt (változatosság)."""
    if not any(a["pic"] for a in an):                       # a mintában nincs képhely: a kép nem szempont
        s = {k: v for k, v in s.items() if k != "image"}
        if s.get("layout") in ("image_left", "image_right"):
            s["layout"] = "bullets"
    scored = [(_score(a, s), a) for a in an]
    best = max(sc for sc, _ in scored)
    pool = [(sc, a) for sc, a in scored if sc >= best - 5]
    sc, a = min(pool, key=lambda p: (used.get(p[1]["idx"], 0), -p[0], p[1]["idx"]))
    used[a["idx"]] = used.get(a["idx"], 0) + 1
    return a


def _body_slot(it: dict) -> _Leaf:
    sl = it["slots"]
    return max(sl[1:], key=lambda l: l.area) if len(sl) >= 2 else sl[0]


def _below_title(a: dict[str, Any], SW: int, SH: int, keep_pic: Optional[_Leaf]) -> tuple[int, int, int, int]:
    """A cím (és alcím) alatti szabad terület, a megmaradó kép mellett."""
    t = a["sub"] or a["title"]
    y = max(t.y + t.h, a["title"].y + a["title"].h) + int(0.03 * SH)
    x0, x1 = int(0.06 * SW), int(0.94 * SW)
    if keep_pic is not None and a["side"] in ("left", "right"):
        if a["side"] == "left":
            x0 = max(x0, keep_pic.x + keep_pic.w + int(0.03 * SW))
        else:
            x1 = min(x1, keep_pic.x - int(0.03 * SW))
    if keep_pic is not None and a["side"] == "top":
        y = max(y, keep_pic.y + keep_pic.h + int(0.03 * SH))
    y1 = int(0.88 * SH)
    return x0, y, max(int(0.2 * SW), x1 - x0), max(int(0.2 * SH), y1 - y)


def _content_area(a: dict[str, Any], SW: int, SH: int) -> tuple[int, int, int, int]:
    """A tartalom (tételek, diagramok) helye a mintadián — ide kerül a diagram/táblázat/statisztika."""
    rs = [it["region"] for it in a["items"] + a["extras"]] + [f.rect for f in a["frames"]]
    t = a["sub"] or a["title"]
    top = max(t.y + t.h, a["title"].y + a["title"].h) + int(0.02 * SH)
    if rs:
        x, y, w, h = _bbox(rs)
        y2 = y + h
        y = max(y, top)
        if w * (y2 - y) >= 0.12 * SW * SH:
            return x, y, w, y2 - y
    return _below_title(a, SW, SH, None)


def _image_target(a: dict[str, Any], s: dict[str, Any], SW: int, SH: int):
    """Hová kerül a dia fő képe: ("swap", kép-alakzat) | ("split", téglalap) | ("area", téglalap) | None."""
    lay = s.get("layout")
    if not s.get("image") or lay in ("stats", "chart", "table", "gallery"):
        return None
    if lay == "full_image":
        if a["bg"] is not None:
            return "swap", a["bg"]
        if a["pic"] is not None:
            return "swap", a["pic"]
        return "area", _content_area(a, SW, SH)
    if a["pic"] is not None:
        return "swap", a["pic"]
    if a["bg"] is not None and lay in ("cover", "section", "closing", "quote"):
        return "swap", a["bg"]                         # teljes képes háttér (fátyollal): az új kép kerül oda
    if lay in ("bullets", "image_left", "image_right") and a["single"]:
        x, y, w, h = _body_slot(a["items"][0]).rect      # nagy szövegdoboz: jobb 42%-a a kép (ha elég nagy)
        if w >= 0.45 * SW and h >= 0.25 * SH:
            return "split", (x + int(w * 0.58), y, int(w * 0.42), h)
    return None


# ---------- klónozás ----------

def _clone(prs, src):
    """A mintadia másolata: háttér, alakzatok, színleképezés; a képek/hivatkozások kapcsolatai átírva."""
    new = prs.slides.add_slide(src.slide_layout)
    root = new._element
    root.replace(root.find(qn("p:cSld")), copy.deepcopy(src._element.find(qn("p:cSld"))))
    for k in ("shapes", "placeholders", "background"):        # a python-pptx a régi cSld-t gyorsítótárazta
        new.__dict__.pop(k, None)
    cm = src._element.find(qn("p:clrMapOvr"))
    if cm is not None:
        old = root.find(qn("p:clrMapOvr"))
        if old is not None:
            root.replace(old, copy.deepcopy(cm))
        else:
            root.find(qn("p:cSld")).addnext(copy.deepcopy(cm))
    rmap = {}
    for rid, rel in src.part.rels.items():
        if rel.reltype in (RT.SLIDE_LAYOUT, RT.NOTES_SLIDE):
            continue
        rmap[rid] = new.part.relate_to(rel.target_ref if rel.is_external else rel.target_part, rel.reltype, rel.is_external)
    for e in root.find(qn("p:cSld")).iter():
        if not isinstance(e.tag, str):
            continue
        for k, v in list(e.attrib.items()):
            if k.startswith(_R_NS) and v in rmap:
                e.set(k, rmap[v])
    return new


def _prune_rels(slide) -> None:
    """A törölt alakzatok (diagram, régi kép) kapcsolatai ne maradjanak — ne mentsük a régi részeket."""
    xml = etree.tostring(slide._element).decode()
    for rid, rel in list(slide.part.rels.items()):
        if rel.reltype not in (RT.SLIDE_LAYOUT, RT.NOTES_SLIDE) and f'"{rid}"' not in xml:
            slide.part.drop_rel(rid)


def _remove(el) -> None:
    p = el.getparent()
    if p is not None:
        p.remove(el)


def _drop_empty_groups(tree) -> None:
    changed = True
    while changed:
        changed = False
        for g in list(tree.iter(qn("p:grpSp"))):
            if not any(isinstance(c.tag, str) and etree.QName(c).localname in _LEAF + ("grpSp",) for c in g):
                _remove(g)
                changed = True


# ---------- szöveg ----------

def _rpr(p):
    runs = [r for r in p.findall(qn("a:r")) if "".join(t.text or "" for t in r.iter(qn("a:t"))).strip()]
    if runs:
        r = max(runs, key=lambda r: len("".join(t.text or "" for t in r.iter(qn("a:t")))))
        if r.find(qn("a:rPr")) is not None:
            return r.find(qn("a:rPr"))
    return p.find(qn("a:endParaRPr"))


def _psize(p) -> Optional[str]:
    rp = _rpr(p)
    return rp.get("sz") if rp is not None else None


def _write(e, lines: list[str]) -> None:
    """A szöveghely új tartalma a minta bekezdés- és betűstílusával (több bekezdésnél a minta 2. bekezdéséé)."""
    body = e.find(qn("p:txBody"))
    paras = body.findall(qn("a:p"))
    full = [p for p in paras if "".join(t.text or "" for t in p.iter(qn("a:t"))).strip()] or paras
    first = full[0]
    second = full[1] if len(full) > 1 else first
    if len(lines) > 1 and len(full) > 1 and _psize(first) != _psize(second):
        first = second                       # bevezető sor + lista a mintában: az új sorok mind listaelemek
    for p in paras:
        body.remove(p)
    for k, line in enumerate(lines or [""]):
        tp = first if k == 0 else second
        m = _BULLET.match("".join(t.text or "" for t in tp.iter(qn("a:t"))))
        p = etree.SubElement(body, qn("a:p"))
        ppr = tp.find(qn("a:pPr"))
        if ppr is not None:
            p.append(copy.deepcopy(ppr))
        r = etree.SubElement(p, qn("a:r"))
        rp = _rpr(tp)
        if rp is not None:
            rp = copy.deepcopy(rp)
            rp.tag = qn("a:rPr")
            for h in rp.findall(qn("a:hlinkClick")) + rp.findall(qn("a:hlinkMouseOver")):
                rp.remove(h)
            r.append(rp)
        t = etree.SubElement(r, qn("a:t"))
        line = str(line).strip()
        t.text = (m.group(1) + " " + line) if (m and line and not _BULLET.match(line)) else line
        end = tp.find(qn("a:endParaRPr"))
        if end is not None:
            p.append(copy.deepcopy(end))


def _em(t: str, caps: bool, bold: bool) -> float:
    """Szövegszélesség em-ben, betűosztályonként (keskeny i/l/t, széles m/w, nagybetű, számjegy)."""
    tot = 0.0
    for c in t:
        if caps and c.isalpha():
            c = c.upper()
        tot += (0.3 if c in "iljtfrI.,:;'!| " else 0.85 if c in "mwMW" else 0.68 if c.isupper()
                else 0.56 if c.isdigit() else 0.53)
    return tot * (1.07 if bold else 1.0)


def _need_pt(body, base: float, scale: float, w_pt: float) -> float:
    """Becsült szövegmagasság (pt) adott méretszorzónál: szóhatáros tördelés, sorköz, bekezdésköz.
    Ha egy szó sem fér egy sorba, nagy büntetés — inkább kisebb betű, mint kettétört szó."""
    bp = body.find(qn("a:bodyPr"))
    ins = lambda k, d: int(bp.get(k, d)) / _PT if bp is not None else d / _PT
    h = ins("tIns", 45720) + ins("bIns", 45720)
    w = max(10.0, w_pt - ins("lIns", 91440) - ins("rIns", 91440))
    for p in body.findall(qn("a:p")):
        rp = _rpr(p)
        s = (int(rp.get("sz")) / 100 if rp is not None and rp.get("sz") else base) * scale
        text = "".join(t.text or "" for t in p.iter(qn("a:t")))
        caps = (rp is not None and rp.get("cap") == "all") or (text.isupper() and len(text) > 3)
        bold = rp is not None and rp.get("b") == "1"
        cap = w / s * 0.97                                   # egy sor ennyi em
        n, cur = 1, 0.0
        for word in text.split():
            L = _em(word, caps, bold)
            if L > cap:
                h += 1000.0
            if cur and cur + 0.3 + L > cap:
                n, cur = n + 1, L
            else:
                cur += (0.3 if cur else 0.0) + L
            while cur > cap:
                n, cur = n + 1, cur - cap
        ppr = p.find(qn("a:pPr"))
        lh = s * 1.2
        if ppr is not None:
            ls = ppr.find(qn("a:lnSpc"))
            if ls is not None and ls.find(qn("a:spcPct")) is not None:
                lh = s * 1.2 * int(ls.find(qn("a:spcPct")).get("val", 100000)) / 100000
            elif ls is not None and ls.find(qn("a:spcPts")) is not None:
                lh = int(ls.find(qn("a:spcPts")).get("val", 1200)) / 100 * scale
            for k in ("a:spcBef", "a:spcAft"):
                sp = ppr.find(qn(k))
                if sp is not None and sp.find(qn("a:spcPts")) is not None:
                    h += int(sp.find(qn("a:spcPts")).get("val", 0)) / 100 * scale
                elif sp is not None and sp.find(qn("a:spcPct")) is not None:
                    h += s * int(sp.find(qn("a:spcPct")).get("val", 0)) / 100000
        h += n * lh
    return h


def _apply_scale(body, base: float, scale: float, fs: float) -> None:
    """Betűméret (és pontos sorköz/bekezdésköz) szorzása; a normAutofit fontScale beolvad a méretbe."""
    k = scale * fs
    for rp in body.iter(qn("a:rPr"), qn("a:endParaRPr")):
        sz = int(rp.get("sz")) if rp.get("sz") else base * 100 / max(fs, 0.01)
        rp.set("sz", str(max(600, int(round(sz * k / 50.0)) * 50)))
    if scale != 1.0:
        for tag in ("a:lnSpc", "a:spcBef", "a:spcAft"):
            for sp in body.iter(qn(tag)):
                pts = sp.find(qn("a:spcPts"))
                if pts is not None:
                    pts.set("val", str(int(int(pts.get("val", 0)) * scale)))
    na = body.find(".//" + qn("a:normAutofit"))
    if na is not None:
        for att in ("fontScale", "lnSpcReduction"):
            na.attrib.pop(att, None)


def _fit(e, l: _Leaf, kept: list[_Leaf], SW: int, SH: int) -> None:
    """A beírt szöveg férjen a helyére: a lefelé szabad hely (a következő alakzatig / a kártya aljáig) és — ha a
    minta nem tördelt (wrap=none), vagy a szöveg kártyán belül van — az oldalt szabad hely; ha így sem fér, a betű
    kisebb (legfeljebb a felére), szót nem törünk ketté."""
    body = e.find(qn("p:txBody"))
    bp = body.find(qn("a:bodyPr"))
    holders = [o for o in kept if o.el is not l.el and o.kind in ("shape", "pic") and _inside(l, o, 0.9) and o.area > l.area]
    card = min(holders, key=lambda o: o.area, default=None)
    bottom, right, left = 0.96 * SH, 0.95 * SW, 0.05 * SW
    if card is not None:
        bottom, right, left = card.y + card.h - 0.012 * SH, card.x + card.w - 0.01 * SW, card.x + 0.01 * SW
    for o in kept:
        if o is l or o.el is l.el or o in holders:
            continue
        if o.w > 0 and o.y >= l.y + 0.5 * l.h and _xover(o, l) > 0.1:   # alatta (a függőleges vonal nem korlát)
            bottom = min(bottom, o.y)
        if o.h > 0 and o.y < l.y + l.h and o.y + o.h > l.y:             # mellette (a vízszintes vonal nem korlát)
            if o.x >= l.x + 0.5 * l.w:
                right = min(right, o.x - 0.01 * SW)
            elif o.x + o.w <= l.x + 0.5 * l.w:
                left = max(left, o.x + o.w + 0.01 * SW)
    room_h = max(l.h, bottom - l.y)
    x, w = l.x, l.w
    nowrap = bp is not None and bp.get("wrap") == "none"
    if (nowrap or card is not None) and not l.grp:            # nem tördelt doboz, vagy kártyán belül: a szabad hely felé szélesedhet
        algn = None
        ppr = body.find(".//" + qn("a:pPr"))
        if ppr is not None:
            algn = ppr.get("algn")
        if algn == "ctr":
            ext = max(0, min(l.x - left, right - (l.x + l.w)))
            x, w = l.x - ext, l.w + 2 * ext
        elif algn == "r":
            x = min(l.x, left)
            w = l.x + l.w - x
        else:
            w = max(l.w, right - l.x)
    if bp is not None and nowrap:
        bp.set("wrap", "square")
    base, fs = l.size / max(_font_scale(e), 0.01), _font_scale(e)
    w_pt = w / _PT
    scale = 1.0
    for sc in (1.0, 0.92, 0.85, 0.78, 0.72, 0.66, 0.6, 0.55, 0.5, 0.45):
        if l.size * sc < 9 and sc < 1:
            break
        scale = sc
        if _need_pt(body, base * fs, sc, w_pt) * _PT <= room_h:
            break
    _apply_scale(body, base, scale, fs)
    need = _need_pt(body, base * fs * scale, 1.0, w_pt) * _PT        # a méretek már beírva
    if l.grp:
        return
    xf = _xfrm(e)
    if xf is None:
        return
    off, ext = xf.find(qn("a:off")), xf.find(qn("a:ext"))
    if (x, w) != (l.x, l.w):
        off.set("x", str(int(x)))
        ext.set("cx", str(int(w)))
    if need > l.h:
        ext.set("cy", str(int(min(room_h, need))))


# ---------- képek, terület ----------

def _swap(slide, e, path) -> None:
    """Kép csere a helyén: a keret, maszk (kör, lekerekítés), árnyék, tónus marad; a régi kivágás és SVG-változat nem."""
    _, rid = slide.part.get_or_add_image_part(str(path))
    blip = e.find(".//" + qn("a:blip"))
    if blip is None:
        return
    blip.set(qn("r:embed"), rid)
    blip.attrib.pop(qn("r:link"), None)
    ext = blip.find(qn("a:extLst"))
    if ext is not None:
        blip.remove(ext)
    fill = blip.getparent()
    for t in ("a:srcRect", "a:tile"):
        x = fill.find(qn(t))
        if x is not None:
            fill.remove(x)
    if fill.find(qn("a:stretch")) is None:
        etree.SubElement(etree.SubElement(fill, qn("a:stretch")), qn("a:fillRect"))


def _color_of(l: Optional[_Leaf]) -> Optional[str]:
    if l is None:
        return None
    rp = None
    for p in l.el.find(qn("p:txBody")).findall(qn("a:p")):
        rp = _rpr(p)
        if rp is not None:
            break
    c = rp.find(".//" + qn("a:srgbClr")) if rp is not None else None
    return c.get("val") if c is not None else None


def _light(hexv: Optional[str]) -> bool:
    if not hexv or len(hexv) != 6:
        return False
    r, g, b = (int(hexv[i:i + 2], 16) for i in (0, 2, 4))
    return 0.299 * r + 0.587 * g + 0.114 * b > 160


def _textbox_like(slide, rect, lines: list[str], ref: Optional[_Leaf], size: Optional[float] = None) -> None:
    """Saját szövegdoboz a minta egy szövegének betűjével/színével (sötét háttéren is olvasható)."""
    from pptx.util import Pt
    tb = slide.shapes.add_textbox(*rect)
    tf = tb.text_frame
    tf.word_wrap = True
    rp0 = None
    if ref is not None:
        for p in ref.el.find(qn("p:txBody")).findall(qn("a:p")):
            rp0 = _rpr(p)
            if rp0 is not None:
                break
    n = len(lines)
    pt = size or (24 if n <= 3 else 20 if n <= 5 else 16)
    for i, line in enumerate(lines):
        par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        r = par.add_run()
        if rp0 is not None:
            rp = copy.deepcopy(rp0)
            rp.tag = qn("a:rPr")
            for h in rp.findall(qn("a:hlinkClick")):
                rp.remove(h)
            old = r._r.find(qn("a:rPr"))
            if old is not None:
                r._r.remove(old)
            r._r.insert(0, rp)
        r.text = (f"•  {line}" if n > 1 else str(line))
        r.font.size = Pt(pt)
        par.space_after = Pt(pt * 0.5)


def _sat(h: str) -> bool:
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    mx, mn = max(r, g, b), min(r, g, b)
    return mx >= 64 and (mx - mn) / max(1, mx) >= 0.35


def _palette(an: list[dict[str, Any]]) -> list[str]:
    """A minta saját hangsúlyszínei (kitöltött alakzatok területtel, szövegek hosszal súlyozva; szürkék nélkül) —
    ezeket kapja a diagram, a táblázat fejléce, a statisztika száma (a pakli témája itt gyakran a gyári Office-kék)."""
    w: Counter = Counter()
    for a in an:
        for l in a["leaves"]:
            pr = l.el.find(qn("p:spPr"))
            sf = pr.find(qn("a:solidFill")) if pr is not None else None
            c = sf.find(qn("a:srgbClr")) if sf is not None else None
            if c is not None and l.kind in ("shape", "text") and l.area < 0.6 * a["A"]:     # a háttér nem hangsúly
                w[c.get("val", "").upper()] += 100.0 * l.area / a["A"]
            if l.kind == "text":
                for rp in l.el.iter(qn("a:rPr")):
                    c = rp.find(qn("a:solidFill") + "/" + qn("a:srgbClr"))
                    if c is not None:
                        w[c.get("val", "").upper()] += 0.5
    return [c for c, _ in w.most_common() if len(c) == 6 and _sat(c)][:4]


def _paint(shapes, kind: str, pal: list[str], fg: Optional[str]) -> None:
    """Az elhelyezett diagram/táblázat/statisztika a minta színeit kapja; sötét mintán világos felirattal."""
    from pptx.dml.color import RGBColor
    rgb = RGBColor.from_string
    dark_bg = bool(fg) and _light(fg)
    for sh in shapes:
        try:
            if getattr(sh, "has_chart", False) and sh.has_chart:
                ch = sh.chart
                if dark_bg:
                    ch.font.color.rgb = rgb(fg)
                if not pal:
                    continue
                plot = ch.plots[0]
                pie = "PIE" in str(ch.chart_type) or "DOUGHNUT" in str(ch.chart_type)
                for k, ser in enumerate(plot.series):
                    if pie:
                        for j, pt in enumerate(ser.points):
                            pt.format.fill.solid()
                            pt.format.fill.fore_color.rgb = rgb(pal[j % len(pal)])
                    elif "LINE" in str(ch.chart_type):
                        ser.format.line.color.rgb = rgb(pal[k % len(pal)])
                        ser.marker.format.fill.solid()
                        ser.marker.format.fill.fore_color.rgb = rgb(pal[k % len(pal)])
                    else:
                        ser.format.fill.solid()
                        ser.format.fill.fore_color.rgb = rgb(pal[k % len(pal)])
            elif getattr(sh, "has_table", False) and sh.has_table and pal:
                head = pal[0]
                for i, row in enumerate(sh.table.rows):
                    for c in row.cells:
                        c.fill.solid()
                        c.fill.fore_color.rgb = rgb(head if i == 0 else ("FFFFFF" if i % 2 else "F4F4F4"))
                        for p in c.text_frame.paragraphs:
                            for r in p.runs:
                                r.font.color.rgb = rgb(("FFFFFF" if not _light(head) else "1A1A1A") if i == 0 else "222222")
                                r.font.bold = i == 0 or None
            elif sh.has_text_frame and kind == "stats":
                for j, p in enumerate(sh.text_frame.paragraphs):
                    for r in p.runs:
                        if j == 0 and pal:
                            r.font.color.rgb = rgb(pal[0])
                        elif j and dark_bg:
                            r.font.color.rgb = rgb(fg)
        except Exception as e:                       # a színezés hibája ne buktassa a diát
            log.warning("színezés: %s", e)


# ---------- egy dia ----------

def _renumber(t: str, k: int) -> str:
    if re.search(r"\d", t):
        return re.sub(r"\d+", lambda m: str(k).zfill(len(m.group())), t, count=1)
    romans = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"]
    return re.sub(r"[IVX]+", romans[(k - 1) % len(romans)], t, count=1)


def _drop_item(it: dict, drop: set) -> None:
    drop.update(it["slots"])
    drop.update(it["nums"])
    drop.update(it["deco"])


def _put_lines(items: list[dict], lines: list[str], write: dict, drop: set) -> None:
    """Felsorolás a tételekbe: soronként egy tétel (a fölös tételek törlődnek), több sornál tételenként több;
    „Fejléc: leírás” sorok a tétel két szöveghelyébe, különben a fejlécbe (rövid) vagy a leírásba (hosszú)."""
    if not items:
        return
    if len(items) == 1:
        it = items[0]
        body = _body_slot(it)
        write[body] = lines or [""]
        drop.update(x for x in it["slots"] if x is not body)
        drop.update(it["nums"])
        return
    k, n = len(lines), len(items)
    if k <= n:
        groups = [[ln] for ln in lines]
        for it in items[k:]:
            _drop_item(it, drop)
    else:
        sizes = [k // n + (1 if j < k % n else 0) for j in range(n)]
        groups, p = [], 0
        for c in sizes:
            groups.append(lines[p:p + c])
            p += c
    used = items[:len(groups)]
    two = all(len(it["slots"]) >= 2 for it in used)
    split = two and all(len(g) == 1 and _SPLIT.match(g[0]) for g in groups)
    head_only = two and not split and all(len(g) == 1 for g in groups) and max(len(g[0]) for g in groups) <= 48
    # kártyában a fejléc lefelé a kártya aljáig nőhet (a leírás-hely kiürül): a hangsúlyos fejlécbetű marad
    roomy = two and not split and all(it["card"] is not None and it["card"].y + it["card"].h - it["slots"][0].y
                                      >= 2.2 * it["slots"][0].h for it in used)
    for j, (it, g) in enumerate(zip(used, groups)):
        sl = it["slots"]
        body = _body_slot(it)
        head = sl[0] if len(sl) >= 2 else None
        if split:
            m = _SPLIT.match(g[0])
            write[head], write[body] = [m.group(1)], [m.group(2)]
        elif head is not None and (head_only or roomy):
            write[head] = g
        else:                                   # több sor egy tételben: mind a leírás-helyre (a fejléc nem „címe” a többinek)
            write[body] = g
        drop.update(x for x in sl if x not in write)
        for nl in it["nums"]:
            write[nl] = [_renumber(nl.text, j + 1)]


def _put_stats(items: list[dict], stats: list[dict], write: dict, drop: set) -> None:
    for it in items[len(stats):]:
        _drop_item(it, drop)
    for j, (it, st) in enumerate(zip(items, stats)):
        sl = it["slots"]
        val, lab = str(st.get("value", "")), str(st.get("label", ""))
        if len(sl) == 1:
            write[sl[0]] = [val, lab] if lab else [val]
        else:
            write[sl[0]] = [val]
            write[sl[1]] = [lab or " "]
            drop.update(sl[2:])
        for nl in it["nums"]:
            write[nl] = [_renumber(nl.text, j + 1)]


def _fill(prs, a: dict[str, Any], s: dict[str, Any], i: int, total: int, spec: dict[str, Any], brand: set,
          target, imgs: list, pal: list[str]) -> None:
    from app.tools.pptx_template import _chart, _picture, _stats, _table
    SW, SH = prs.slide_width, prs.slide_height
    new = _clone(prs, a["slide"])
    tree = new.shapes._spTree
    E = list(tree.iter())
    lay = s.get("layout")
    write: dict[_Leaf, list[str]] = {}
    drop: set = set()
    title, sub, kicker = a["title"], a["sub"], a["kicker"]

    def head(text, leaf):
        if leaf is None:
            return False
        if text:
            write[leaf] = [str(text)]
            return True
        drop.add(leaf)
        return False

    place = None                      # (fajta, téglalap) — diagram, táblázat, statisztika, galéria, szövegdoboz
    items_used: list[dict] = []
    if lay == "cover":
        head(s.get("title"), title)
        placed = head(s.get("subtitle"), sub)
        head(s.get("kicker"), kicker)
        if s.get("subtitle") and not placed and a["items"]:
            items_used = a["items"][:1]
            _put_lines(items_used, [s["subtitle"]], write, drop)
    elif lay in ("section", "closing", "quote"):
        if lay == "quote":
            head(f"“{s.get('quote', '')}”", title)
            line = f"— {s['author']}" if s.get("author") else ""
            num = None
        else:
            head(s.get("title"), title)
            num = str(s["number"]) if s.get("number") else None
            line = s.get("subtitle") or ""
            if num and kicker is None and not a["orphans"]:
                line = " · ".join(x for x in (line, num) if x)
        placed = head(line, sub)
        if line and not placed and a["items"]:
            items_used = a["items"][:1]
            _put_lines(items_used, [line], write, drop)
        if num and kicker is not None:
            write[kicker] = [num]
        elif kicker is not None:
            drop.add(kicker)
        if num and a["orphans"] and kicker is None:
            write[a["orphans"][0]] = [num]
    else:
        head(s.get("title") or (s.get("caption") if lay == "full_image" else None), title)
        if kicker is not None:
            drop.add(kicker)
        if lay == "full_image" and s.get("title") and s.get("caption") and sub is not None:
            write[sub] = [str(s["caption"])]
        elif sub is not None:
            drop.add(sub)
        if lay in ("bullets", "image_left", "image_right"):
            lines = _lines(s)
            if a["items"]:
                items_used = a["items"]
                _put_lines(items_used, lines, write, drop)
                if target and target[0] == "split":
                    body = _body_slot(a["items"][0])
                    xf = _xfrm(E[body.i])
                    if body in write and xf is not None and not body.grp:
                        narrow = copy.copy(body)                 # a szöveg a bal 55%-ba, a kép jobbra
                        narrow.w = int(body.w * 0.55)
                        xf.find(qn("a:ext")).set("cx", str(narrow.w))
                        write[narrow] = write.pop(body)
            elif lines:
                place = ("text", _content_area(a, SW, SH))
        elif lay == "stats" and len(a["items"]) >= max(2, len(s.get("stats") or [])):    # minden szám kap helyet
            items_used = a["items"]
            _put_stats(items_used, (s.get("stats") or [])[:len(items_used)], write, drop)
        elif lay in ("stats", "chart", "table", "gallery"):
            place = (lay, _content_area(a, SW, SH))
        elif lay == "full_image" and target and target[0] == "area":
            place = ("picture", target[1])
    for it in a["items"]:
        if it not in items_used:
            _drop_item(it, drop)
    for it in a["extras"]:
        _drop_item(it, drop)
    for o in a["orphans"]:
        if o not in write:
            drop.add(o)
    # lábléc: arculat marad, oldalszám frissül, a pakli láblécszövege az első szabad helyre, a többi törlődik
    foot_text = spec.get("footer")
    for f in a["footers"]:
        t = _norm(f.text)
        pm = _PAGE.search(f.text)
        if t in brand:
            continue
        if pm:
            write[f] = [f.text[:pm.start()] + f"{i + 1}{pm.group(2)}{total}" + f.text[pm.end():]]
        elif _NUM.fullmatch(f.text.strip()):
            write[f] = [_renumber(f.text.strip(), i + 1)]
        elif foot_text:
            write[f] = [str(foot_text)]
            foot_text = None
        else:
            drop.add(f)
    swap = target[1] if target and target[0] == "swap" and imgs and imgs[0] is not None else None
    for p in a["pics"] + ([a["bg"]] if a["bg"] is not None else []):
        if p is swap or p is a["bg"]:
            continue
        drop.add(p)                                     # régi tartalomkép ne maradjon félrevezetően
    if lay in ("chart", "table", "gallery", "stats") and place:
        drop.update(a["pics"])
    written = {l.el for l in write}
    if not place:                                    # a kiürülő diagram/táblázat alatti „kártya” se maradjon üresen
        for f in a["frames"]:
            for o in a["leaves"]:
                if o.kind == "shape" and o.area < 0.6 * a["A"] and _inside(f, o, 0.9) and not any(
                        t.el in written for t in a["leaves"] if t.kind == "text" and _inside(t, o)):
                    drop.add(o)
    for l in a["leaves"]:
        if l.link or l.kind == "frame":
            drop.add(l)
        elif l.kind == "text" and l.el not in written and l not in drop and not (l in a["footers"] and _norm(l.text) in brand):
            drop.add(l)                                 # régi szöveg sehol
    for l in drop:
        _remove(E[l.i])
    _drop_empty_groups(tree)

    kept = [l for l in a["leaves"] if l not in drop] + ([_as_leaf(place[1])] if place else [])
    for l, lines in write.items():
        _write(E[l.i], lines)
    for l in sorted(write, key=lambda l: (l.y, l.x)):
        try:
            _fit(E[l.i], l, kept, SW, SH)
        except Exception as e:                         # a méretezés hibája ne buktassa a paklit
            log.warning("szöveg-illesztés: %s", e)

    fg = _color_of(sub) or _color_of(title)
    if swap is not None and imgs and imgs[0] is not None:
        _swap(new, E[swap.i], imgs[0].path)
    elif target and target[0] in ("split", "area") and imgs and imgs[0] is not None:
        _picture(new, imgs[0], target[1])
    if place:
        kind, rect = place
        before = len(new.shapes)
        if kind == "text":
            _textbox_like(new, rect, _lines(s), sub or title, size=None)
        elif kind == "chart":
            _chart(new, s, rect)
        elif kind == "table":
            _table(new, s, rect)
        elif kind == "stats":
            _stats(new, s, rect)
        elif kind == "picture" and imgs and imgs[0] is not None:
            _picture(new, imgs[0], rect)
        elif kind == "gallery" and imgs:
            x, y, w, h = rect
            n = len(imgs)
            cols = 2 if n > 1 else 1
            rows = -(-n // cols)
            gw, gh = w // cols, h // rows
            for k, img in enumerate(imgs):
                _picture(new, img, (x + (k % cols) * gw + gw // 40, y + (k // cols) * gh + gh // 40, gw - gw // 20, gh - gh // 20))
        _paint(list(new.shapes)[before:], kind, pal, fg)
    _prune_rels(new)
    credits = [x.attribution for x in imgs if x is not None and x.attribution]
    notes = "\n".join(filter(None, [s.get("notes"), *credits]))
    if notes:
        new.notes_slide.notes_text_frame.text = notes


def _as_leaf(r) -> _Leaf:
    """Egy elhelyezendő terület (diagram, táblázat) akadályként a szöveg-illesztéshez."""
    l = _Leaf()
    l.el, l.i, l.kind, l.text, l.size, l.grp, l.link, l.card, l.img, l.num, l.stat = None, -1, "frame", "", 0.0, False, False, None, None, False, False
    l.x, l.y, l.w, l.h = (int(v) for v in r)
    return l


# ---------- belépési pont ----------

async def build(spec: dict[str, Any], prs, t0: float) -> dict[str, Any]:
    from app.tools.pptx_deck import _persist_spec
    SW, SH = prs.slide_width, prs.slide_height
    an, brand = _deck(prs)
    pal = _palette(an)
    slides = spec.get("slides") or []
    if not slides:
        raise ValueError("slides: at least one slide is needed")
    used: dict[int, int] = {}
    plan = []
    asp = lambda r: max(0.4, min(3.0, r[2] / max(1, r[3])))
    known = ("cover", "section", "closing", "quote", "bullets", "image_left", "image_right", "full_image", "stats",
             "chart", "table", "gallery")
    slides = [s if s.get("layout") in known else {**s, "layout": "bullets"} for s in slides]
    for s in slides:
        a = _choose(an, s, used)
        target = _image_target(a, s, SW, SH)
        need = []
        if s.get("layout") == "gallery":
            need = [(img, 1.5, j) for j, img in enumerate((s.get("images") or [])[:4])]
        elif target is not None:
            need = [(s["image"], asp(target[1].rect if target[0] == "swap" else target[1]), -1)]
        elif s.get("layout") in ("section", "closing", "quote") and (a["pic"] or a["bg"]) is not None:
            target = ("borrow", a["pic"] or a["bg"])   # képhelyes mintadia kép nélküli diának: a pakli borítóképe
        plan.append((a, target, need))
    reqs = [(img, r) for _, _, need in plan for img, r, _ in need]
    resolved = await images.resolve_many(reqs) if reqs else []
    lst = prs.slides._sldIdLst
    orig = list(lst)
    by_slot, k, extra = {}, 0, []
    donor = None
    k0 = 0
    for _, _, need in plan:                          # az első „fő” kép (rendszerint a borítóé) a kölcsönkép forrása
        for (_, _, j), x in zip(need, resolved[k0:k0 + len(need)]):
            if j == -1 and donor is None and x is not None:
                donor = x
        k0 += len(need)
    try:
        for i, (s, (a, target, need)) in enumerate(zip(slides, plan)):
            imgs = resolved[k:k + len(need)]
            for (_, _, j), x in zip(need, imgs):
                by_slot[(i, j)] = x
            k += len(need)
            if target and target[0] == "borrow":
                if donor is not None:
                    p = await asyncio.to_thread(images._crop_to, donor.path, asp(target[1].rect), 1920)
                    imgs = [images.ResolvedImage(p, donor.attribution, donor.source_file_id)]
                    extra.append(p)
                    target = ("swap", target[1])
                else:
                    target = None
            _fill(prs, a, s, i, len(slides), spec, brand, target, imgs, pal)
        for sld in orig:                                   # a minta saját diái ki (a képeik a klónokban élnek tovább)
            prs.part.drop_rel(sld.rId)
            lst.remove(sld)
        fid = new_file_id("pptx")
        prs.save(str(output_path(fid)))
        saved = _persist_spec(spec, by_slot)
    finally:
        for x in resolved:
            if x is not None:
                x.path.unlink(missing_ok=True)
        for p in extra:
            p.unlink(missing_ok=True)
    log.info("klónozott pakli: %d dia, minta-diák: %s", len(slides), [p[0]["idx"] + 1 for p in plan])
    return {"file_id": fid, "url": public_url(fid), "slides": len(slides), "saved_spec": saved,
            "size_kb": output_path(fid).stat().st_size // 1024, "ms_elapsed": int((time.time() - t0) * 1000)}
