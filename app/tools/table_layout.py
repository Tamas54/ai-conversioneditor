"""Táblázat fotóról → élethű, kitölthető XLSX (image_to_xlsx mode="layout") — egy lefotózott papírűrlapból sablon.

  1. kiegyenesítés   a vonalakból a tábla külső négyszöge → perspektíva- és dőléskorrekció (telefonfotó)
  2. rács            vízszintes és függőleges vonalak (morfológia) → sor- és oszlophatárok; ahol két cella között
                     nincs vonal, ott egyesített cella; a meglévő vonalak a keretek; a sávok adják az arányos
                     oszlopszélességet és sormagasságot
  3. szöveg          a szerver látó modellje olvassa, a rács szerint (soronként, oszloponként), sávokban, hogy a
                     felbontás elég legyen; a bizonytalan olvasatokat a modell megjelöli (mint a digitize-nál)
  4. formázás        a képből mérve: félkövér (tollvastagság a betűmagassághoz), igazítás (a tinta helye a cellában)
  5. típus           szám, dátum, idő, időtartam típusosan; az üres cella kitölthető mező a forms szabálya szerint
                     (űrlapjellegű tábla üres cellája igen, kitöltött nem) — zárolás nélkül, a listában név szerint
  6. képletek        összesítő sor (SUM / COUNT / COUNTA) és sor-különbség (távozás − érkezés), „inferred”
                     jelöléssel, a fotón látott értékkel ellenőrizve
  7. összevetés      a fotó és a renderelt XLSX egymás mellett; sablon-javaslat a forms(action="template_register")-hez
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("aice.table_layout")

WORK_W = 1700                                   # a kiegyenesített tábla szélessége (px)
_TOTAL = re.compile(r"\b(összesen|összes|mindösszesen|total|sum|totals)\b", re.I)
_DURATION = re.compile(r"(ledolgozott|munkaid|idő|óra|hours?|duration|time worked)", re.I)
_ARRIVE = re.compile(r"(érkez|kezd|munkakezd|start|arriv|from|be\b)", re.I)
_LEAVE = re.compile(r"(távoz|befejez|munkavég|vége|end|leave|depart|to\b|ki\b)", re.I)
_MARKER = re.compile(r"(távollét|szabads|betegs|jelöl|absen|leave day)", re.I)
_TIME = re.compile(r"(\d{1,3}):(\d{2})")


@dataclass
class Grid:
    xs: list[int]                                # oszlophatárok (px)
    ys: list[int]                                # sorhatárok (px)
    groups: list[tuple[int, int, int, int]]      # (r0, c0, r1, c1) — minden cella egy csoport, az egyesítettek több
    edges: dict[tuple[int, int], dict[str, bool]] = field(default_factory=dict)

    @property
    def rows(self) -> int:
        return len(self.ys) - 1

    @property
    def cols(self) -> int:
        return len(self.xs) - 1

    def merged(self) -> list[tuple[int, int, int, int]]:
        return [g for g in self.groups if g[0] != g[2] or g[1] != g[3]]

    def owner(self) -> dict[tuple[int, int], tuple[int, int, int, int]]:
        return {(r, c): g for g in self.groups for r in range(g[0], g[2] + 1) for c in range(g[1], g[3] + 1)}


# ---------------------------------------------------------------- 1. kiegyenesítés

def _lines(bw, w: int, h: int):
    import cv2
    horiz = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(25, w // 45), 1)))
    vert = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(25, h // 60))))
    return horiz, vert


def _binary(gray):
    import cv2
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)


def _order(pts):
    import numpy as np
    pts = np.array(pts, dtype="float32").reshape(4, 2)
    s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype="float32")


def rectify(img):
    """A tábla külső négyszöge (a vonalak legnagyobb összefüggő alakzata) → felülnézet, WORK_W széles.
    Ha nincs értelmes négyszög, a kép marad (csak méretezve)."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    H, W = gray.shape
    scale = 1600 / max(H, W)
    small = cv2.resize(gray, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else gray
    s = scale if scale < 1 else 1.0
    bw = _binary(small)
    horiz, vert = _lines(bw, small.shape[1], small.shape[0])
    mask = cv2.dilate(cv2.bitwise_or(horiz, vert), np.ones((5, 5), np.uint8), iterations=2)
    cnts = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
    quad = None
    if cnts:
        c = max(cnts, key=cv2.contourArea)
        if cv2.contourArea(c) > 0.15 * small.shape[0] * small.shape[1]:
            peri = cv2.arcLength(c, True)
            ap = cv2.approxPolyDP(c, 0.02 * peri, True)
            quad = ap.reshape(-1, 2) if len(ap) == 4 else cv2.boxPoints(cv2.minAreaRect(c))
    if quad is None:
        out = cv2.resize(img, (WORK_W, int(H * WORK_W / W)))
        return out, None
    q = _order(np.array(quad, dtype="float32") / s)
    wt = np.linalg.norm(q[1] - q[0]); wb = np.linalg.norm(q[2] - q[3])
    hl = np.linalg.norm(q[3] - q[0]); hr = np.linalg.norm(q[2] - q[1])
    pad = 14
    tw = WORK_W - 2 * pad
    th = int(tw * max(hl, hr) / max(wt, wb))
    dst = np.array([[pad, pad], [pad + tw, pad], [pad + tw, pad + th], [pad, pad + th]], dtype="float32")
    M = cv2.getPerspectiveTransform(q, dst)
    out = cv2.warpPerspective(img, M, (WORK_W, th + 2 * pad), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))
    return _to_table(out), q.tolist()


def _to_table(img):
    """A kiegyenesített képen a legbelső nagy vonal-alakzat a tábla (a lap széle körülötte egy nagyobb keret):
    arra vágunk, és újra WORK_W szélesre méretezünk."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    H, W = gray.shape
    horiz, vert = _lines(_binary(gray), W, H)
    mask = cv2.dilate(cv2.bitwise_or(horiz, vert), np.ones((3, 3), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    big = [tuple(stats[i][:4]) for i in range(1, n) if stats[i][2] * stats[i][3] > 0.15 * W * H]
    if not big:
        return img
    x, y, w, h = min(big, key=lambda b: b[2] * b[3])
    if w * h > 0.97 * W * H:
        return img
    pad = 14
    crop = img[max(0, y - pad):min(H, y + h + pad), max(0, x - pad):min(W, x + w + pad)]
    return cv2.resize(crop, (WORK_W, int(crop.shape[0] * WORK_W / crop.shape[1])), interpolation=cv2.INTER_CUBIC)


# ---------------------------------------------------------------- 2. rács

def _peaks(profile, thr: float, merge: int = 6) -> list[int]:
    idx = [i for i, v in enumerate(profile) if v >= thr]
    out: list[list[int]] = []
    for i in idx:
        if out and i - out[-1][-1] <= merge:
            out[-1].append(i)
        else:
            out.append([i])
    return [int(round(sum(g) / len(g))) for g in out]


def grid(img) -> Grid:
    import cv2
    import numpy as np
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    H, W = gray.shape
    bw = _binary(gray)
    horiz, vert = _lines(bw, W, H)
    ys = _peaks(horiz.sum(axis=1) / 255, 0.12 * W)
    xs = _peaks(vert.sum(axis=0) / 255, 0.06 * H)
    if len(ys) < 2 or len(xs) < 2:
        raise ValueError("no table grid found in the picture (it needs drawn cell lines)")
    hv = cv2.dilate(horiz, np.ones((7, 1), np.uint8))
    vv = cv2.dilate(vert, np.ones((1, 7), np.uint8))

    def _edge(on) -> bool:
        """Cellahatár: a vonal a sáv nagy részén megvan, ÉS mindkét végén (a szomszéd vonalakhoz ér) — egy betű szára
        (a cím „I”-je egy oszlophatáron) nem ér el a cella széléig."""
        n = len(on)
        k = max(1, n // 7)
        return float(on.mean()) > 0.7 and bool(on[:k].any()) and bool(on[-k:].any())

    def vline(j: int, i: int) -> bool:          # függőleges vonal az xs[j]-nél, az i. sorban
        y0, y1 = ys[i] + 2, ys[i + 1] - 2
        if y1 <= y0:
            return True
        return _edge(vv[y0:y1, max(0, xs[j] - 4):xs[j] + 5].max(axis=1) > 0)

    def hline(i: int, j: int) -> bool:          # vízszintes vonal az ys[i]-nél, a j. oszlopban
        x0, x1 = xs[j] + 2, xs[j + 1] - 2
        if x1 <= x0:
            return True
        return _edge(hv[max(0, ys[i] - 4):ys[i] + 5, x0:x1].max(axis=0) > 0)

    R, C = len(ys) - 1, len(xs) - 1
    parent = {(r, c): (r, c) for r in range(R) for c in range(C)}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    for r in range(R):
        for c in range(C):
            if c + 1 < C and not vline(c + 1, r):
                parent[root((r, c))] = root((r, c + 1))
            if r + 1 < R and not hline(r + 1, c):
                parent[root((r, c))] = root((r + 1, c))
    comp: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for k in parent:
        comp.setdefault(root(k), []).append(k)
    groups = sorted((min(r for r, _ in v), min(c for _, c in v), max(r for r, _ in v), max(c for _, c in v))
                    for v in comp.values())
    edges = {}
    for g in groups:
        r0, c0, r1, c1 = g
        edges[(r0, c0)] = {"top": all(hline(r0, c) for c in range(c0, c1 + 1)),
                           "bottom": all(hline(r1 + 1, c) for c in range(c0, c1 + 1)),
                           "left": all(vline(c0, r) for r in range(r0, r1 + 1)),
                           "right": all(vline(c1 + 1, r) for r in range(r0, r1 + 1))}
    return Grid(xs, ys, groups, edges)


# ---------------------------------------------------------------- 4. formázás a képből

def styles(img, g: Grid) -> dict[tuple[int, int], dict[str, Any]]:
    """Cellánként: van-e tinta, igazítás (left/center/right), félkövér (a tollvastagság a sor medián­jához képest)."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    out: dict[tuple[int, int], dict[str, Any]] = {}
    widths = []
    for r0, c0, r1, c1 in g.groups:
        x0, x1, y0, y1 = g.xs[c0] + 6, g.xs[c1 + 1] - 6, g.ys[r0] + 6, g.ys[r1 + 1] - 6
        if x1 <= x0 or y1 <= y0:
            continue
        crop = ink[y0:y1, x0:x1]
        n = int(crop.sum() // 255)
        if n < 25:
            out[(r0, c0)] = {"ink": False}
            continue
        ys_, xs_ = np.nonzero(crop)
        bx0, bx1, by0, by1 = xs_.min(), xs_.max(), ys_.min(), ys_.max()
        d = cv2.distanceTransform(crop, cv2.DIST_L2, 3)
        ridge = (d > 0) & (d >= cv2.dilate(d, np.ones((3, 3), np.uint8)))
        stroke = 2.0 * float(np.median(d[ridge])) if ridge.any() else 1.0      # a vonalgerinc mentén: tollvastagság
        height = max(1, by1 - by0 + 1)
        cw = x1 - x0
        lm, rm = bx0 / cw, 1 - (bx1 + 1) / cw
        align = ("left" if lm < 0.12 and rm > 0.2 else "right" if rm < 0.12 and lm > 0.2
                 else "center" if abs(lm - rm) < 0.08 and lm > 0.08 else None)      # kétes: általános
        out[(r0, c0)] = {"ink": True, "align": align, "stroke": stroke, "height": height}
        widths.append((stroke, height))
    if widths:
        ms, mh = float(np.median([w for w, _ in widths])), float(np.median([h for _, h in widths]))
        for v in out.values():                     # a nagyobb betű vastagabb is: a magasság csak gyengén számít
            if v.get("ink"):
                v["bold"] = v["stroke"] > 1.15 * ms * (v["height"] / mh) ** 0.25
    return out


# ---------------------------------------------------------------- 3. szöveg: a látó modell a rács szerint

def _letters(n: int) -> str:
    from openpyxl.utils import get_column_letter
    return get_column_letter(n)


def _band_image(img, g: Grid, r0: int, r1: int, path: Path) -> None:
    """A tábla r0..r1 sorainak sávja, bal margón a sorszámmal, fölötte az oszlopbetűkkel (1-től, A-tól)."""
    from PIL import Image, ImageDraw, ImageFont
    import cv2
    y0, y1 = max(0, g.ys[r0] - 4), min(img.shape[0], g.ys[r1 + 1] + 4)
    crop = cv2.cvtColor(img[y0:y1], cv2.COLOR_BGR2RGB) if img.ndim == 3 else img[y0:y1]
    band = Image.fromarray(crop)
    pad_l, pad_t = 64, 40
    out = Image.new("RGB", (band.width + pad_l, band.height + pad_t), "white")
    out.paste(band, (pad_l, pad_t))
    dr = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    for r in range(r0, r1 + 1):
        cy = (g.ys[r] + g.ys[r + 1]) / 2 - y0 + pad_t
        dr.text((6, cy - 12), str(r + 1), fill=(200, 0, 0), font=font)
    for c in range(g.cols):
        cx = (g.xs[c] + g.xs[c + 1]) / 2 + pad_l
        dr.text((cx - 8, 6), _letters(c + 1), fill=(200, 0, 0), font=font)
    out.save(path, "PNG")


_PROMPT = """This picture is a band of rows {a}–{b} of a table (a photographed paper form). The red numbers on the left
are the ROW numbers, the red letters on top are the COLUMN letters (A–{last}). They are not part of the table.
{merged}
Read the text of EVERY cell exactly as printed or written — keep accents (á é í ó ö ő ú ü ű), punctuation, colons,
capitalisation, numbers, dates and times (e.g. "8:00", "0:00") as they appear. A blank cell is "". An merged cell's
text goes into its top-left cell; the rest of the merged cells are "".
Reply with JSON only:
{{"rows": {{"{a}": ["A text", "B text", …], …, "{b}": […]}},
  "uncertain": ["B12", …]  (cells you could not read with certainty — "" when none)}}
Every row has exactly {n} entries."""


async def read_cells(img, g: Grid, work: Path, band_rows: int = 18) -> tuple[list[list[str]], list[str]]:
    """A látó modell olvassa a cellákat, sávonként (párhuzamosan). → (szöveg[sor][oszlop], bizonytalan cellák)."""
    from app.llm import vision as V
    texts = [[""] * g.cols for _ in range(g.rows)]
    unsure: list[str] = []
    bands = [(r, min(g.rows - 1, r + band_rows - 1)) for r in range(0, g.rows, band_rows)]
    merged = g.merged()

    async def one(a: int, b: int):
        p = work / f"band-{a}.png"
        _band_image(img, g, a, b, p)
        ms = [f"{_letters(c0 + 1)}{r0 + 1}:{_letters(c1 + 1)}{r1 + 1}" for r0, c0, r1, c1 in merged if a <= r0 <= b]
        prompt = _PROMPT.format(a=a + 1, b=b + 1, last=_letters(g.cols), n=g.cols,
                                merged=("Merged cells in this band: " + ", ".join(ms) + ".") if ms else "No merged cells.")
        msgs = [{"role": "user", "content": [{"type": "text", "text": prompt},
                                             {"type": "image_url", "image_url": {"url": V.image_to_data_url(p, long_edge=1280)}}]}]
        raw = V._strip_json_fences(await V._call(msgs, max_tokens=6000))
        try:
            return a, b, json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        except ValueError as e:
            raise ValueError(f"the vision model answered something else than the table ({e})") from None
    for a, b, data in await asyncio.gather(*(one(a, b) for a, b in bands)):
        rows = data.get("rows") or {}
        for r in range(a, b + 1):
            vals = rows.get(str(r + 1)) or []
            for c in range(min(g.cols, len(vals))):
                texts[r][c] = str(vals[c] if vals[c] is not None else "").strip()
        for u in data.get("uncertain") or []:              # csak a sáv saját sorai, egyszer
            m = re.fullmatch(r"([A-Z]{1,3})(\d+)", u) if isinstance(u, str) else None
            if m and a + 1 <= int(m.group(2)) <= b + 1 and u not in unsure:
                unsure.append(u)
    own = g.owner()
    for (r, c), grp in own.items():                     # egyesített cella: csak a bal felső cellában
        if (r, c) != (grp[0], grp[1]) and texts[r][c]:
            if not texts[grp[0]][grp[1]]:
                texts[grp[0]][grp[1]] = texts[r][c]
            texts[r][c] = ""
    return texts, unsure


# ---------------------------------------------------------------- 5–6. típus, mezők, képletek

def typed(text: str, duration: bool = False) -> tuple[Any, Optional[str]]:
    """Szöveg → (érték, számformátum). Idő → time („h:mm”), 24 óránál több / időtartam-oszlop → nap-tört („[h]:mm”),
    dátum → date, szám → int/float (magyar tizedesvessző, szóköz-ezres), a többi szöveg."""
    t = (text or "").strip()
    if not t:
        return None, None
    m = _TIME.fullmatch(t)
    if m and int(m.group(2)) < 60:
        h, mi = int(m.group(1)), int(m.group(2))
        if duration or h > 23:
            return (h * 60 + mi) / 1440, "[h]:mm"
        return dt.time(h, mi), "h:mm"
    m = re.fullmatch(r"(\d{4})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})\.?", t)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), "yyyy.mm.dd"
        except ValueError:
            pass
    if re.fullmatch(r"-?\d{1,3}( \d{3})+|-?\d+", t):
        return int(t.replace(" ", "")), None
    if re.fullmatch(r"-?\d+(?: \d{3})*,\d+|-?\d+\.\d+", t):
        return float(t.replace(" ", "").replace(",", ".")), None
    return t, None


def _hours(v: Any) -> Optional[float]:
    if isinstance(v, dt.time):
        return v.hour + v.minute / 60
    if isinstance(v, float) and 0 <= v < 1000:
        return v * 24
    return None


@dataclass
class Plan:
    header_row: Optional[int] = None
    body: tuple[int, int] = (0, -1)                 # az első és utolsó törzssor
    total_row: Optional[int] = None
    roles: dict[str, int] = field(default_factory=dict)        # arrive / leave / absence / duration / day → oszlop


def plan(texts: list[list[str]]) -> Plan:
    """A napi tábla: a fejléc (sok szöveg, alatta egymást követő napszámok vagy üres sorok), az összesítő sor, és az
    oszlopok szerepe a fejléc-feliratokból."""
    R = len(texts)
    p = Plan()
    for r in range(R - 3):
        heads = [t for t in texts[r] if t and not re.fullmatch(r"[\d\s.,:]+", t)]
        if len(heads) < max(2, len(texts[r]) // 2):
            continue
        nums = 0
        for k in range(r + 1, min(R, r + 6)):
            v = texts[k][0].rstrip(".")
            if v.isdigit() and int(v) == k - r:
                nums += 1
        if nums >= 3:
            p.header_row = r
            break
    if p.header_row is None:
        return p
    h = p.header_row
    end = R - 1
    for k in range(h + 1, R):
        if _TOTAL.search(texts[k][0] or "") or (texts[k][0] and not texts[k][0].rstrip(".").isdigit()):
            p.total_row = k if _TOTAL.search(texts[k][0] or "") else None
            end = k - 1
            break
    p.body = (h + 1, end)
    p.roles["day"] = 0
    for c, t in enumerate(texts[h]):
        if c == 0 or not t:
            continue
        if "duration" not in p.roles and _DURATION.search(t) and not _ARRIVE.search(t) and not _LEAVE.search(t):
            p.roles["duration"] = c
        elif "arrive" not in p.roles and _ARRIVE.search(t):
            p.roles["arrive"] = c
        elif "leave" not in p.roles and _LEAVE.search(t):
            p.roles["leave"] = c
        elif "absence" not in p.roles and _MARKER.search(t):
            p.roles["absence"] = c
    return p


def fields(texts: list[list[str]], g: Grid, pl: Plan) -> list[dict[str, Any]]:
    """Kitölthető mezők a forms szabálya szerint: a napi táblában a beviteli oszlopok üres cellái, fölötte a
    „Felirat: ⎵” párok üres cellái. Szöveges cella sosem mező."""
    from app.formkit.detect import table_fields
    own = g.owner()
    out, seen = [], set()

    def add(r: int, c: int, label: str):
        grp = own[(r, c)]
        key = (grp[0], grp[1])
        if key in seen:
            return
        seen.add(key)
        out.append({"cell": f"{_letters(grp[1] + 1)}{grp[0] + 1}", "label": label})
    if pl.header_row is not None:
        idx = [pl.header_row] + list(range(pl.body[0], pl.body[1] + 1))
        block = [[None if own[(ri, c)][:2] != (ri, c) else texts[ri][c] for c in range(g.cols)] for ri in idx]
        for r, c, lab in table_fields(block):
            add(idx[r], c, lab)
    top = pl.header_row if pl.header_row is not None else len(texts)
    for r in range(top):
        row = [None if own[(r, c)][:2] != (r, c) else v for c, v in enumerate(texts[r])]
        for rr, c, lab in table_fields([row, [None] * len(row)]):
            if rr == 0:
                add(r, c, lab)
    return out


def formulas(texts: list[list[str]], pl: Plan) -> list[dict[str, Any]]:
    """A látható összesítők és a napi időtartam képlete, a fotón látott értékkel ellenőrizve („inferred”)."""
    out: list[dict[str, Any]] = []
    if pl.header_row is None:
        return out
    b0, b1 = pl.body
    col = lambda c: _letters(c + 1)
    dur, a, lv = pl.roles.get("duration"), pl.roles.get("arrive"), pl.roles.get("leave")
    if dur is not None and a is not None and lv is not None:
        ok, seen = True, 0
        for r in range(b0, b1 + 1):
            ta, tl, td = (typed(texts[r][c])[0] for c in (a, lv, dur))
            want = 0.0 if not (isinstance(ta, dt.time) and isinstance(tl, dt.time)) else \
                (tl.hour * 60 + tl.minute - ta.hour * 60 - ta.minute) / 60
            got = _hours(typed(texts[r][dur], duration=True)[0])
            if got is not None:
                seen += 1
                ok = ok and abs(got - want) < 1 / 60
        for r in range(b0, b1 + 1):
            n = r + 1
            out.append({"cell": f"{col(dur)}{n}", "formula": f"=IF(AND(ISNUMBER({col(a)}{n}),ISNUMBER({col(lv)}{n})),"
                        f"{col(lv)}{n}-{col(a)}{n},0)", "format": "[h]:mm", "inferred": True,
                        "verified": bool(seen) and ok, "kind": "row_duration"})
    if pl.total_row is not None:
        t = pl.total_row
        for c, shown in enumerate(texts[t]):
            if c == 0 or not shown:
                continue
            body = [texts[r][c] for r in range(b0, b1 + 1)]
            rng = f"{col(c)}{b0 + 1}:{col(c)}{b1 + 1}"
            v, _ = typed(shown, duration=c == dur)
            if c == dur or (isinstance(v, float) and _TIME.fullmatch(shown.strip())):
                total = sum(_hours(typed(x, duration=True)[0]) or 0 for x in body)
                out.append({"cell": f"{col(c)}{t + 1}", "formula": f"=SUM({rng})", "format": "[h]:mm", "inferred": True,
                            "verified": abs(total - (_hours(v) or 0)) < 1 / 60, "photo_value": shown, "kind": "total_hours"})
            elif isinstance(v, int):
                vals = [typed(x)[0] for x in body]
                if c in (a, lv) or any(isinstance(x, (dt.time, int, float)) for x in vals):
                    n = sum(1 for x in vals if isinstance(x, (dt.time, int, float)))
                    out.append({"cell": f"{col(c)}{t + 1}", "formula": f"=COUNT({rng})", "inferred": True,
                                "verified": n == v, "photo_value": shown, "kind": "count"})
                else:
                    n = sum(1 for x in body if x)
                    out.append({"cell": f"{col(c)}{t + 1}", "formula": f"=COUNTA({rng})", "inferred": True,
                                "verified": n == v, "photo_value": shown, "kind": "count_marked"})
            elif isinstance(v, float):
                total = sum(x for x in (typed(x)[0] for x in body) if isinstance(x, (int, float)))
                out.append({"cell": f"{col(c)}{t + 1}", "formula": f"=SUM({rng})", "inferred": True,
                            "verified": abs(total - v) < 1e-6, "photo_value": shown, "kind": "sum"})
    return out


# ---------------------------------------------------------------- az XLSX

def build_xlsx(texts: list[list[str]], g: Grid, st: dict, flds: list[dict], fms: list[dict], unsure: list[str],
               pl: Plan, dst: Path, sheet: str = "Sheet1") -> None:
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Border, Font, Protection, Side
    wb = Workbook()
    ws = wb.active
    ws.title = (sheet or "Sheet1")[:31]
    thin = Side(style="thin")
    total_w = sum(g.xs[i + 1] - g.xs[i] for i in range(g.cols))
    chars = 80.0                                                  # A4 álló, nyomtatható szélesség ≈ 80 karakter
    for c in range(g.cols):
        ws.column_dimensions[_letters(c + 1)].width = round(chars * (g.xs[c + 1] - g.xs[c]) / total_w, 2)
    pt_per_px = chars * 5.6 / total_w                              # egy karakter ≈ 5,6 pt (Calibri 11)
    for r in range(g.rows):
        ws.row_dimensions[r + 1].height = round(max(9.0, (g.ys[r + 1] - g.ys[r]) * pt_per_px), 1)
    fcells = {f["cell"] for f in flds}
    fm = {f["cell"]: f for f in fms}
    dur = pl.roles.get("duration")
    for r0, c0, r1, c1 in g.groups:
        coord = f"{_letters(c0 + 1)}{r0 + 1}"
        cell = ws[coord]
        e = g.edges.get((r0, c0), {})
        border = Border(left=thin if e.get("left") else None, right=thin if e.get("right") else None,
                        top=thin if e.get("top") else None, bottom=thin if e.get("bottom") else None)
        if r1 > r0 or c1 > c0:
            ws.merge_cells(f"{coord}:{_letters(c1 + 1)}{r1 + 1}")
            for rr in range(r0, r1 + 1):                          # a keret az egyesített tartomány minden cellájára
                for cc in range(c0, c1 + 1):
                    ws.cell(rr + 1, cc + 1).border = Border(left=border.left if cc == c0 else None,
                                                            right=border.right if cc == c1 else None,
                                                            top=border.top if rr == r0 else None,
                                                            bottom=border.bottom if rr == r1 else None)
        else:
            cell.border = border
        s = st.get((r0, c0), {})
        if coord in fm:
            cell.value = fm[coord]["formula"]
            if fm[coord].get("format"):
                cell.number_format = fm[coord]["format"]
            cell.comment = Comment(f"Formula inferred from the photo{' and checked against it' if fm[coord]['verified'] else ' — check it'}"
                                   f" ({fm[coord].get('photo_value') or texts[r0][c0] or 'empty'}).", "Studio")
        else:
            v, fmt = typed(texts[r0][c0], duration=c0 == dur and pl.header_row is not None and pl.body[0] <= r0 <= pl.body[1])
            cell.value = v
            if fmt:
                cell.number_format = fmt
            elif v is None and pl.header_row is not None and pl.body[0] <= r0 <= pl.body[1] and \
                    c0 in (pl.roles.get("arrive"), pl.roles.get("leave")):
                cell.number_format = "h:mm"                       # a napi idő-oszlop üres cellája időt vár
        cell.alignment = Alignment(horizontal=s.get("align") if s.get("ink") else None, vertical="center",
                                   wrap_text=bool(r1 > r0 or (isinstance(cell.value, str) and len(cell.value) > 18)))
        if s.get("bold"):
            cell.font = Font(bold=True)
        if coord in fcells:
            cell.protection = Protection(locked=False)            # kitölthető mező (lapvédelemnél is írható)
        if coord in unsure and cell.comment is None:
            cell.comment = Comment("Uncertain reading — compare with the photo.", "Studio")
    ws.print_area = f"A1:{_letters(g.cols)}{g.rows}"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    wb.save(str(dst))


def template_profile(texts: list[list[str]], g: Grid, pl: Plan, fms: list[dict]) -> Optional[dict[str, Any]]:
    """Ha a kép egy jelenléti ív (napi tábla érkezés/távozás oszloppal): javasolt profil a sablon-regisztrációhoz."""
    if pl.header_row is None or "arrive" not in pl.roles or "leave" not in pl.roles:
        return None
    from app.formkit.sheet_template import _ROLE_WORDS, _fold
    own = g.owner()
    header = {}
    for r in range(pl.header_row):
        for c, t in enumerate(texts[r]):
            if not t or own[(r, c)][:2] != (r, c):
                continue
            role = next((k for k, words in _ROLE_WORDS.items() if any(w in _fold(t) for w in words)), None)
            nxt = own[(r, c)][3] + 1
            if role and role not in header and nxt < g.cols and not texts[r][nxt]:
                header[role] = f"{_letters(nxt + 1)}{r + 1}"
    cols = {k: _letters(v + 1) for k, v in pl.roles.items() if k in ("arrive", "leave", "absence")}
    kinds = {"szabadság": {"absence": "+"}} if "absence" in cols else {}
    totals = {}
    for f in fms:
        k = {"total_hours": "hours", "count": "worked_days", "count_marked": "absence_days"}.get(f["kind"])
        if k and k not in totals:
            totals[k] = f["cell"]
    day1 = pl.body[0] + 1
    return {"header": header, "month_format": "date", "days": {"first_row": day1, "step": 1}, "columns": cols,
            "rules": {"workday": {"arrive": "{start}", "leave": "{end}"}, "kinds": kinds}, "totals": totals,
            "defaults": {"start": "8:00", "end": "16:00"}}


# ---------------------------------------------------------------- a teljes út

async def image_to_layout_xlsx(src: Path, name: str, sheet_name: str = "Sheet1") -> dict[str, Any]:
    import cv2
    from app.storage import output_path, public_url, temp_path, write_meta
    from app.storage.files import new_file_id
    from app.tools.checks import make
    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        from PIL import Image
        import numpy as np
        with Image.open(src) as im:
            img = cv2.cvtColor(np.array(im.convert("RGB")), cv2.COLOR_RGB2BGR)
    flat, quad = await asyncio.to_thread(rectify, img)
    g = await asyncio.to_thread(grid, flat)
    st = await asyncio.to_thread(styles, flat, g)
    work = temp_path(new_file_id("tl"))
    work.mkdir(parents=True)
    try:
        texts, unsure = await read_cells(flat, g, work)
        pl = plan(texts)
        flds, fms = fields(texts, g, pl), formulas(texts, pl)
        fid = new_file_id("xlsx")
        await asyncio.to_thread(build_xlsx, texts, g, st, flds, fms, unsure, pl, output_path(fid), sheet_name)
        write_meta(fid, label=f"{name}.xlsx", operation="image_to_xlsx layout")
        cmp_id = new_file_id("png")
        rendered = await _render_png(output_path(fid), work)
        flat_png = work / "flat.png"
        cv2.imwrite(str(flat_png), flat)
        from app.tools.rebuild import _side_by_side, _similarity
        if rendered:
            await asyncio.to_thread(_side_by_side, flat_png, rendered, output_path(cmp_id))
            write_meta(cmp_id, label=f"{name} (photo and spreadsheet).png", operation="image_to_xlsx comparison",
                       extra={"ephemeral": True})
            sim = await asyncio.to_thread(_similarity, flat_png, rendered)
        else:
            cmp_id, sim = None, None
    finally:
        shutil.rmtree(work, ignore_errors=True)
    tp = template_profile(texts, g, pl, fms)
    filled = sum(1 for row in texts for t in row if t)
    findings, verified = [], [f"Grid: {g.rows} rows × {g.cols} columns, {len(g.merged())} merged cells"
                                 + (", perspective corrected." if quad else ".")]
    verified.append(f"{filled} cells read; {len(flds)} empty cells marked as fillable fields (unlocked).")
    bad = [f["cell"] for f in fms if not f["verified"] and f["kind"] != "row_duration"]
    if fms:
        (findings if bad else verified).append(
            f"{len(fms)} formulas inferred from the photo" + (f"; these don't match the photo's numbers: {', '.join(bad)}"
                                                              if bad else ", all checked against the photo's numbers."))
    if unsure:
        findings.append(f"{len(unsure)} uncertain readings (marked with a comment): {', '.join(unsure[:12])}.")
    if sim is not None:
        verified.append(f"Similarity of the rendered sheet to the photo: {sim:.2f} — compare them side by side.")
    return {"file_id": fid, "url": public_url(fid), "mode": "layout",
            "grid": {"rows": g.rows, "cols": g.cols, "merged": [f"{_letters(c0 + 1)}{r0 + 1}:{_letters(c1 + 1)}{r1 + 1}"
                                                                for r0, c0, r1, c1 in g.merged()]},
            "fields": flds, "formulas": fms, "uncertain": unsure,
            **({"comparison": cmp_id, "comparison_url": public_url(cmp_id)} if cmp_id else {}),
            **({"template_profile": tp, "template_hint": "Register it as a recurring template: forms(action="
                "\"template_register\", file_id=<this file_id>, profile=template_profile) — check the profile first."}
               if tp else {}),
            "check": make("needs_check" if findings else "ok", findings, verified)}


async def _render_png(xlsx: Path, work: Path) -> Optional[Path]:
    from app.convert.engines import run
    try:
        out = work / "render"
        out.mkdir()
        await run("soffice", f"-env:UserInstallation=file://{work}/lo", "--headless", "--convert-to", "pdf",
                  "--outdir", str(out), str(xlsx), timeout=120)
        pdf = next(out.glob("*.pdf"))
        await run("pdftoppm", "-r", "110", "-png", "-singlefile", str(pdf), str(out / "page"), timeout=60)
        return out / "page.png"
    except Exception as e:
        log.info("layout render skipped: %s", e)
        return None
