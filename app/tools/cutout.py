"""Háttéreltávolítás neurális hálókkal (onnxruntime) — a PhotoCraft saját „Select Subject”-je nehéz fotón
(ellenfény, átlógó fűszálak) bokrokat is kivágott.

Lánc:
  1. ALANY: BiRefNet-lite (MIT, BIREFNET_MODEL_PATH, CPU-n ~8-10 s) — tömör alany, haj, vékony tárgyak
     (mikrofonállvány, kábel); hiányában az ISNet (CUTOUT_MODEL_PATH, ~1,5 s, a rembg útjának hű másolata).
  2. EMBEREK: U2Net human-seg (HUMAN_SEG_MODEL_PATH, ~1 s) — a háttérben álló, elmosódott ember is maradjon.
     A 320²-es maszk széle elkenődik és túllóg a testen (világos körvonal) → keményebb küszöb + enyhe simítás.
  3. Takarítás: a bizonytalan alany-köd (α < 0,7) csak a biztos alany mellett marad, beljebb tömör a fedés,
     és a félig átlátszó szélekből kivonjuk a RÉGI háttér színét (különben világos „glória” marad az új háttéren).
  4. keep / drop (szavak → látómodell-dobozok → BiRefNet a kivágott részen; pixeles doboz; sokszög), külső
     maszk, korábbi kivágás javítása — és a tartalmi ellenőrzés (check): emberek, megőrzendők, kézben tartott tárgyak.

Mérve 10-12 (ChatGPT-teszt, színpadi fotó, fehér lámpa és égbolt a háttérben):
  • ISNet + a régi „a>0,1 belseje tömör” tömörítés: a fej melletti félig átlátszó ködből (a lámpa a hajszálak
    között) tömör fehér folt lett; a gitáros körül az égbolt világos körvonala maradt.
  • BiRefNet-lite: a lámpa nincs benne, a ruha tömör (az ISNet lyukasra hagyta), az állvány és a kábel megvan;
    a háttérbeli gitárost ez sem veszi fel → az emberek maszkja továbbra is kell.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional

from app.config import settings

log = logging.getLogger("aice.cutout")
_sessions: dict[str, object] = {}
_session_lock = threading.Lock()
# Egyszerre egy kivágás: a háló memóriaigénye nagy, a párhuzamosság itt nem gyorsít.
_run_lock = asyncio.Lock()
_SIZE = (1024, 1024)
_IMAGENET = ((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))


def _get_session(path: Path):
    key = str(path)
    with _session_lock:
        if key not in _sessions:
            import onnxruntime as ort
            o = ort.SessionOptions()
            # memória-aréna KI: a BiRefNet egy futása ~6 GB-ot foglal — aréna mellett ez a futás után is lefoglalva
            # maradna (mért: 6,6 GB vs 6,0 GB csúcs, ~10% lassabb); így a kivágás után felszabadul
            o.enable_cpu_mem_arena = False
            o.enable_mem_pattern = False
            _sessions[key] = ort.InferenceSession(key, o, providers=["CPUExecutionProvider"])
        return _sessions[key]


def _prep(img, size, norm):
    import numpy as np
    from PIL import Image
    im = np.array(img.convert("RGB").resize(size, Image.Resampling.LANCZOS), dtype=np.float32) / 255.0
    im = im / max(im.max(), 1e-6)
    mean, std = norm
    return ((im - np.array(mean)) / np.array(std)).transpose(2, 0, 1)[None].astype(np.float32)


def _to_mask(p, img):
    """Háló-kimenet → 0–255 maszk a kép méretében (min–max normalizálva)."""
    import numpy as np
    from PIL import Image
    p = (p - p.min()) / max(float(p.max() - p.min()), 1e-6)
    return Image.fromarray((p * 255).astype("uint8"), mode="L").resize(img.size, Image.Resampling.LANCZOS)


def _mask(img):
    """ISNet: a fő alany finom szélekkel (0–255, a kép méretében)."""
    import numpy as np
    s = _get_session(settings.CUTOUT_MODEL_PATH)
    x = _prep(img, _SIZE, ((0.5, 0.5, 0.5), (1.0, 1.0, 1.0)))
    return _to_mask(np.squeeze(s.run(None, {s.get_inputs()[0].name: x})[0][:, 0, :, :]), img)


def _birefnet(img):
    """BiRefNet-lite: az alany (0–255) — vagy None, ha nincs meg a modell."""
    if not Path(settings.BIREFNET_MODEL_PATH).is_file():
        return None
    import numpy as np
    s = _get_session(settings.BIREFNET_MODEL_PATH)
    p = np.squeeze(s.run(None, {s.get_inputs()[0].name: _prep(img, _SIZE, _IMAGENET)})[0][0, 0])
    return _to_mask(1 / (1 + np.exp(-p)), img)


def _subject(img):
    m = _birefnet(img)
    return m if m is not None else _mask(img)


def _people(img):
    """U2Net human-seg (rembg előfeldolgozás: 320², ImageNet-normalizálás): minden ember, egészben — vagy None."""
    if not Path(settings.HUMAN_SEG_MODEL_PATH).is_file():
        return None
    import numpy as np
    s = _get_session(settings.HUMAN_SEG_MODEL_PATH)
    return _to_mask(np.squeeze(s.run(None, {s.get_inputs()[0].name: _prep(img, (320, 320), _IMAGENET)})[0][:, 0]), img)


def _people_arr(img):
    """Az emberek maszkja 0–1 float-ként, vagy None (nincs modell / nincs ember a képen)."""
    import numpy as np
    hum = _people(img)
    if hum is None:
        return None
    h = np.asarray(hum, dtype=np.float32) / 255
    return h if (h > 0.5).mean() > 0.01 else None


def _combine(a0, h):
    """Alany (a0) ∪ emberek (h) → végső maszk: a köd a biztos alany mellé szorítva, belül tömör."""
    import cv2
    import numpy as np
    m = min(a0.shape)
    hp = np.zeros_like(a0)
    if h is not None:                                      # van ember: mindenki maradjon, egészben
        # a 320²-es maszk széle elkenődik → keményebb küszöb (a valódi él ~0,5-nél), enyhe simítás
        # (guided filterrel a háttér mintázata — napfényes levelek, vászon — pöttyökként a szélre került)
        hp = cv2.GaussianBlur(np.clip((h - 0.45) / 0.3, 0, 1), (0, 0), max(1.0, m * 0.001))
    conf = (hp > 0.5) | (a0 > 0.7)
    d = cv2.distanceTransform((~conf).astype(np.uint8), cv2.DIST_L2, 5)
    band = max(6, round(m * 0.012))                        # a bizonytalan köd a biztos alanytól band px-en túl elhalványul
    a = np.maximum(np.where(a0 > 0.7, a0, a0 * np.clip(1 - (d - band) / band, 0, 1)), hp)
    k = max(4, round(m * 0.006))                           # a BIZTOS rész széltől k px-nél beljebb teljes fedés
    dist = cv2.distanceTransform((a > 0.5).astype(np.uint8), cv2.DIST_L2, 5)
    return np.maximum(a, np.clip((dist - k / 2) / (k / 2), 0, 1)).astype(np.float32)


def _alpha(img):
    """A végső maszk (0–1, float32): alany ∪ emberek."""
    import numpy as np
    return _combine(np.asarray(_subject(img), dtype=np.float32) / 255, _people_arr(img))


def _decontaminate(rgb, a):
    """Szélszín-tisztítás: a félig átlátszó pixelek színéből kivonjuk a régi háttér becsült színét,
    F = (I − (1−α)·B) / α. B és a közeli biztos előtér színe kicsinyített képen, simítva becsülve — így egy
    24 MP-es fotón is csak a szélpixelek kerülnek teljes felbontású számolásba."""
    import cv2
    import numpy as np
    H, W = a.shape
    f = max(1, int(np.ceil(max(H, W) / 1024)))
    small = cv2.resize(rgb, (W // f or 1, H // f or 1), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    sa = cv2.resize(a, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_AREA)
    s = max(9, round(min(sa.shape) * 0.03)) | 1

    def spread(w):
        num = cv2.GaussianBlur(small * w[..., None], (s, s), 0)
        den = cv2.GaussianBlur(w, (s, s), 0)
        return num / np.maximum(den, 1e-4)[..., None], den
    bg, bg_w = spread((sa < 0.05).astype(np.float32))
    fg, fg_w = spread((sa > 0.95).astype(np.float32))
    ys, xs = np.nonzero((a > 0.02) & (a < 0.98))
    if not len(ys):
        return rgb
    sy, sx = np.minimum(ys // f, small.shape[0] - 1), np.minimum(xs // f, small.shape[1] - 1)
    al = a[ys, xs][:, None]
    px = rgb[ys, xs].astype(np.float32) / 255
    est = (px - (1 - al) * bg[sy, sx]) / np.maximum(al, 1e-3)
    ok = (bg_w[sy, sx] > 0.02)[:, None] & (al > 0.15)
    near = (fg_w[sy, sx] > 0.02)[:, None]
    out = np.where(ok, est, np.where(near, fg[sy, sx], px))
    res = rgb.copy()
    res[ys, xs] = (np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8)
    return res


# ---------- megőrzendő / eldobandó objektumok, külső maszk, ellenőrzés ----------
#
# 10-12 (ChatGPT-teszt): a háttércsere „sikeres” volt, miközben a gitáros gitárnyaka és keze eltűnt — egyik
# modell sem veszi fel a háttérbeli ember kezében lévő tárgyat. Ezért:
#   • keep / drop: szavak („guitar”, „hands”) → a látómodell (DeepSeek-V4.1-Flash) dobozokat ad → a dobozon
#     (+ráhagyás) BiRefNet fut a KIVÁGOTT részen („box-prompted”), és az eredmény a dobozon belül hozzáadódik /
#     kivonódik; pixeles doboz ugyanígy; sokszög = pontosan az a terület.
#   • a közeli dobozok EGY futásba kerülnek (a BiRefNet ~8–12 s, a bemenete mindig 1024²);
#   • a látómodell-hívás a teljes-kép maszkokkal PÁRHUZAMOSAN fut (hálózat vs. CPU) → nem nyújtja az időt;
#   • check: emberek (megvan / csonka), keep-tárgyak, és a kézben tartott tárgyak (hangszer, mikrofon, táska…),
#     amelyek kimaradtak → needs_check + javaslat (keep=[…]). „Technikai siker ≠ a feladat teljesült.”

_FIND_TIMEOUT_S = 45
_HELD_MIN = 0.08          # a kézben tartott tárgy dobozának személyen kívüli részéből ennyi legyen fedett


def _targets(spec) -> tuple[list[str], list[dict]]:
    """keep/drop bemenet → (szavak, régiók). Elfogad: "guitar, hands" · ["guitar", {"box": [x0,y0,x1,y1]},
    {"polygon": [[x,y],…]}, [x0,y0,x1,y1], {"name": "guitar"}]. A régiók pixelben, a forráskép méretében."""
    if not spec:
        return [], []
    if isinstance(spec, str):
        spec = [w for w in spec.split(",")]
    if isinstance(spec, dict):
        spec = [spec]
    words: list[str] = []
    regions: list[dict] = []
    if isinstance(spec, list) and len(spec) == 4 and all(isinstance(v, (int, float)) for v in spec):
        spec = [list(spec)]
    for it in spec:
        if isinstance(it, str):
            if it.strip():
                words.append(it.strip())
        elif isinstance(it, (list, tuple)) and len(it) == 4 and all(isinstance(v, (int, float)) for v in it):
            regions.append({"box": [float(v) for v in it]})
        elif isinstance(it, dict):
            if it.get("polygon"):
                regions.append({"polygon": [[float(x), float(y)] for x, y in it["polygon"]], "name": it.get("name")})
            elif it.get("box"):
                regions.append({"box": [float(v) for v in it["box"]], "name": it.get("name")})
            elif it.get("name") or it.get("word"):
                words.append(str(it.get("name") or it.get("word")).strip())
        else:
            raise ValueError("keep/drop: words (\"guitar\"), boxes {\"box\": [x0,y0,x1,y1]} or polygons {\"polygon\": [[x,y],…]}")
    return words, regions


def _data_url(img, long_edge: int = 1280) -> str:
    """A (már elforgatott) kép JPEG data URL-je — a látómodell ugyanazt a tájolást lássa, mint mi."""
    import base64
    import io
    im = img.copy()
    im.thumbnail((long_edge, long_edge))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


async def _find_objects(img, words: list[str], held: bool) -> dict:
    """Látómodell: a kért szavak dobozai + (held) a személyek kezében lévő tárgyak. Pixel-dobozokat ad vissza:
    {"objects": [{"name", "box"}], "held": [{"name", "box"}], "error": str|None}."""
    import json
    import re
    from app.llm import vision as v
    W, H = img.size
    parts = []
    if words:
        parts.append("Find these objects: " + "; ".join(words) + ". One entry per separate instance, "
                     "name = exactly the word asked; leave out what is not visible.")
    if held:
        parts.append("Also list under \"held\" every object a person is holding, playing, carrying or touching that "
                     "is NOT clothing or jewellery (e.g. guitar, violin, microphone, bag, umbrella, ball, phone, cup), "
                     "and under \"people\" every person (one box each, the whole visible body).")
    prompt = (" ".join(parts) + " Return STRICT JSON only: {\"objects\": [{\"name\": str, \"box\": [x0,y0,x1,y1]}], "
              "\"held\": [{\"name\": str, \"box\": [x0,y0,x1,y1]}], \"people\": [{\"box\": [x0,y0,x1,y1]}]} "
              "with coordinates normalized to 0-1000 (x to the right, y down).")
    msgs = [{"role": "user", "content": [{"type": "text", "text": prompt},
                                         {"type": "image_url", "image_url": {"url": _data_url(img)}}]}]
    last = None
    for model, tmo in ((settings.LLM_MODEL_VISION, _FIND_TIMEOUT_S), (settings.LLM_MODEL_VISION_FALLBACK, 60)):
        try:
            raw = await asyncio.wait_for(v._call(msgs, max_tokens=1500, model=model), tmo)
            m = re.search(r"\{.*\}", v._strip_json_fences(raw), re.S)
            d = json.loads(m.group(0)) if m else {}
            break
        except Exception as e:  # a hívó jelzi a check-ben, hogy nem sikerült ellenőrizni
            last = f"{type(e).__name__}: {str(e)[:120]}"
    else:
        return {"objects": [], "held": [], "people": [], "error": last or "vision model unavailable"}

    def px(items):
        out = []
        for o in items or []:
            try:
                x0, y0, x1, y1 = (float(c) for c in o["box"])
            except Exception:
                continue
            x0, x1 = sorted((x0, x1))
            y0, y1 = sorted((y0, y1))
            b = [x0 * W / 1000, y0 * H / 1000, x1 * W / 1000, y1 * H / 1000]
            if b[2] - b[0] >= 4 and b[3] - b[1] >= 4:
                out.append({"name": str(o.get("name") or "object").strip(), "box": b})
        return out
    return {"objects": px(d.get("objects")), "held": px(d.get("held")),
            "people": px([dict(o, name="person") for o in d.get("people") or [] if isinstance(o, dict)]), "error": None}


def _grow(box, W, H, frac: float = 0.12):
    x0, y0, x1, y1 = box
    mx, my = (x1 - x0) * frac, (y1 - y0) * frac
    return [max(0, int(x0 - mx)), max(0, int(y0 - my)), min(W, int(round(x1 + mx))), min(H, int(round(y1 + my)))]


def _clusters(boxes: list[list[float]], W, H) -> list[list[int]]:
    """Átfedő (ráhagyással nőtt) dobozok → közös kivágások: egy BiRefNet-futás klaszterenként."""
    cl = [_grow(b, W, H) for b in boxes]
    merged = True
    while merged:
        merged = False
        for i in range(len(cl)):
            for j in range(i + 1, len(cl)):
                a, b = cl[i], cl[j]
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    cl[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del cl[j]
                    merged = True
                    break
            if merged:
                break
    return cl


def _segment_boxes(img, boxes: list[list[float]]):
    """A dobozokban lévő tárgyak maszkja (0–1, teljes képméret; dobozon kívül 0) — BiRefNet a kivágott részen."""
    import numpy as np
    W, H = img.size
    out = np.zeros((H, W), np.float32)
    for x0, y0, x1, y1 in _clusters(boxes, W, H):
        crop = img.crop((x0, y0, x1, y1))
        m = _birefnet(crop)
        m = m if m is not None else _mask(crop)
        out[y0:y1, x0:x1] = np.maximum(out[y0:y1, x0:x1], np.asarray(m, dtype=np.float32) / 255)
    return out


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _box_mask(shape, box, grow: float = 0.06):
    import numpy as np
    H, W = shape
    x0, y0, x1, y1 = _grow(box, W, H, grow)
    m = np.zeros(shape, bool)
    m[y0:y1, x0:x1] = True
    return m


def _poly_mask(shape, poly):
    import cv2
    import numpy as np
    m = np.zeros(shape, np.uint8)
    cv2.fillPoly(m, [np.round(np.array(poly, np.float32)).astype(np.int32)], 255, lineType=cv2.LINE_AA)
    return m.astype(np.float32) / 255


def _people_parts(h, boxes=()):
    """Személyenként [(maszk, középpont-x)] balról jobbra. A látómodell személy-dobozai számítanak (a kéz vagy a
    felemelt kar külön foltként ne legyen külön ember); ha nincsenek, a maszk nagy összefüggő részei."""
    import cv2
    import numpy as np
    if h is None:
        return []
    hm = h > 0.5
    if boxes:
        out = []
        for b in boxes:
            bm = _box_mask(h.shape, b["box"], 0.02)
            out.append((hm & bm, (b["box"][0] + b["box"][2]) / 2))
        return sorted(out, key=lambda p: p[1])
    n, lab, stats, cent = cv2.connectedComponentsWithStats(hm.astype(np.uint8), 8)
    if n <= 1:
        return []
    big = max(stats[i, cv2.CC_STAT_AREA] for i in range(1, n))
    parts = [(lab == i, float(cent[i][0])) for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= 0.15 * big]
    return sorted(parts, key=lambda p: p[1])


def _where(cx: float, W: int) -> str:
    return "left" if cx < W / 3 else "right" if cx > 2 * W / 3 else "middle"


def _check(a, h, W, keep_found, keep_missing, drop_found, held, find_error, people=()) -> dict:
    """{status: ok|needs_check|failed, findings, verified} + suggest — a kivágás tartalmi ellenőrzése."""
    import cv2
    import numpy as np
    findings, verified, suggest = [], [], []
    cov = float((a > 0.5).mean())
    if cov < 0.003:
        return {"status": "failed", "findings": ["Nothing was cut out — no subject was found in the picture."],
                "verified": [], "suggest": {}}
    if cov > 0.99:
        return {"status": "failed", "findings": ["The whole picture was kept — the background couldn't be told apart."],
                "verified": [], "suggest": {}}
    problem = False
    parts = _people_parts(h, people)
    if parts:
        whole = 0
        for i, (pm, cx) in enumerate(parts, 1):
            k = float((a[pm] > 0.5).mean()) if pm.any() else 0.0
            if k >= 0.85:
                whole += 1
            else:
                problem = True
                findings.append(f"Person {i} ({_where(cx, W)}) is only {round(k * 100)}% kept"
                                + (" — mostly lost." if k < 0.4 else "."))
        verified.append(f"{whole} of {len(parts)} people kept whole")
    person = (h > 0.5) if h is not None else np.zeros(a.shape, bool)
    person = cv2.dilate(person.astype(np.uint8), np.ones((15, 15), np.uint8)).astype(bool)
    for o in keep_found:
        b = _box_mask(a.shape, o["box"], 0)
        k = float((a[b] > 0.5).mean()) if b.any() else 0.0
        verified.append(f"kept: {o['name']}")
        if o.get("added", 1.0) < 0.05:
            problem = True
            findings.append(f"Found the {o['name']} but couldn't separate it from the background — check it, or pass "
                            f"its outline as a polygon.")
        elif k < 0.12:
            problem = True
            findings.append(f"The {o['name']} is only partly kept ({round(k * 100)}% of its box).")
    for w in keep_missing:
        problem = True
        findings.append(f"Couldn't find \"{w}\" in the picture — pass its box in pixels instead: "
                        f"keep=[{{\"box\": [x0, y0, x1, y1]}}].")
    for o in drop_found:
        verified.append(f"removed: {o['name']}" + (" — where it is in front of a person it stays (it can't be cut out "
                                                     "of the person)" if o.get("guarded") else ""))
    keep_names = {o["name"].lower() for o in keep_found}
    for o in held:
        if o["name"].lower() in keep_names:
            continue
        b = _box_mask(a.shape, o["box"], 0)
        outside = b & ~person                               # a tárgy a személy maszkján KÍVÜL eső része
        if outside.sum() < 0.15 * max(1, b.sum()):
            verified.append(f"kept: {o['name']} (held, in front of the person)")
            continue
        k = float((a[outside] > 0.5).mean())
        log.debug("held %s: outside %.3f of box, coverage %.3f", o["name"], outside.sum() / max(1, b.sum()), k)
        if k < _HELD_MIN:
            problem = True
            who = ""
            if parts:
                cx = (o["box"][0] + o["box"][2]) / 2
                idx = min(range(len(parts)), key=lambda i: abs(parts[i][1] - cx))
                who = f" held by person {idx + 1} ({_where(parts[idx][1], W)})"
            findings.append(f"The {o['name']}{who} was not kept — pass keep=[\"{o['name']}\"] to keep it.")
            suggest.append(o["name"])
        else:
            verified.append(f"kept: {o['name']} (held)")
    if find_error and (keep_missing or h is not None):
        findings.append(f"Objects held by people couldn't be checked ({find_error}).")
    out = {"status": "needs_check" if problem else "ok", "findings": findings, "verified": verified}
    out["suggest"] = {"keep": sorted(set(suggest))} if suggest else {}
    return out


def _load_mask(path: Path, size) -> "object":
    """Külső / korábbi maszk → 0–1 float a kép méretében. Átlátszó PNG-nél az alfa, különben a világosság (fehér = marad)."""
    import numpy as np
    from PIL import Image, ImageOps
    with Image.open(path) as m:
        m = ImageOps.exif_transpose(m)
        has_alpha = m.mode in ("RGBA", "LA") or (m.mode == "P" and "transparency" in m.info)
        ch = m.convert("RGBA").getchannel("A") if has_alpha else m.convert("L")
        if has_alpha and ch.getextrema() == (255, 255):     # alfa van, de minden fedett → a világosság a maszk
            ch = m.convert("L")
        ch = ch.resize(size, Image.Resampling.LANCZOS)
    return np.asarray(ch, dtype=np.float32) / 255


async def remove_background(src: Path, dst: Path, *, keep=None, drop=None, mask: Optional[Path] = None,
                            base_alpha: Optional[Path] = None, alpha_out: Optional[Path] = None) -> dict:
    """src kép → dst PNG átlátszó háttérrel (az alany és MINDEN ember marad), és a tartalmi ellenőrzés (check).

    keep / drop: szavak, pixeles dobozok, sokszögek (lásd _targets). mask: külső maszk (fehér = marad) — a hálók
    helyett ez az alap. base_alpha: egy korábbi kivágás maszkja (célzott javítás). alpha_out: ide menti a végső
    maszkot (a későbbi javításhoz)."""
    import numpy as np
    from PIL import Image, ImageOps
    keep_words, keep_regions = _targets(keep)
    drop_words, drop_regions = _targets(drop)
    async with _run_lock:
        img = await asyncio.to_thread(lambda: ImageOps.exif_transpose(Image.open(src)).convert("RGB"))
        W, H = img.size
        h = await asyncio.to_thread(_people_arr, img)
        words = keep_words + [w for w in drop_words if w not in keep_words]
        find = asyncio.create_task(_find_objects(img, words, held=h is not None)) if (words or h is not None) else None
        try:
            if mask is not None or base_alpha is not None:
                a = await asyncio.to_thread(_load_mask, mask or base_alpha, img.size)
            else:
                a0 = await asyncio.to_thread(lambda: np.asarray(_subject(img), dtype=np.float32) / 255)
                a = _combine(a0, h)
            found = await find if find else {"objects": [], "held": [], "people": [], "error": None}
        except BaseException:
            if find:
                find.cancel()
            raise
        by_name: dict[str, list[dict]] = {}
        for o in found["objects"]:
            by_name.setdefault(o["name"].lower(), []).append(o)
        keep_found = [dict(o, name=w) for w in keep_words for o in by_name.get(w.lower(), [])]
        keep_missing = [w for w in keep_words if not by_name.get(w.lower())]
        drop_found = [dict(o, name=w) for w in drop_words for o in by_name.get(w.lower(), [])]
        keep_boxes = [o["box"] for o in keep_found] + [r["box"] for r in keep_regions if "box" in r]
        drop_boxes = [o["box"] for o in drop_found] + [r["box"] for r in drop_regions if "box" in r]
        seg = await asyncio.to_thread(_segment_boxes, img, keep_boxes + drop_boxes) if keep_boxes or drop_boxes else None
        # keep: a doboz (kis ráhagyással) területén a kivágott rész tárgya hozzáadódik
        for o in keep_found + [dict(r, name=r.get("name") or "region") for r in keep_regions if "box" in r]:
            b = _box_mask(a.shape, o["box"])
            add = np.where(b, seg, 0)
            o["added"] = float((add > 0.5).sum()) / max(1.0, float(b.sum()))
            a = np.maximum(a, add)
        for r in keep_regions:
            if "polygon" in r:
                a = np.maximum(a, _poly_mask(a.shape, r["polygon"]))
        # drop: kivonás (az eldobás erősebb a megtartásnál). Emberből NEM vág ki (a dobozban a BiRefNet a mögötte álló
        # embert is tárgynak látja — mért: „microphone stand” → az énekesnő 65%-a eltűnt), kivéve, ha maga a cél egy
        # ember: a doboza nagyrészt egy személy dobozával esik egybe (IoU ≥ 0,5).
        guard = None
        if h is not None:
            import cv2
            guard = cv2.dilate((h > 0.5).astype(np.uint8), np.ones((9, 9), np.uint8)).astype(bool)
        guarded: set[int] = set()
        for i, b in enumerate(drop_boxes):
            bm = _box_mask(a.shape, b)
            cut_out = np.where(bm, seg, 0)
            if guard is not None and not any(_iou(b, pb["box"]) >= 0.5 for pb in found.get("people") or []):
                if (guard & bm & (seg > 0.5)).any():
                    guarded.add(i)
                cut_out = np.where(guard, 0, cut_out)
            a = a * (1 - cut_out)
        for r in drop_regions:
            if "polygon" in r:
                a = a * (1 - _poly_mask(a.shape, r["polygon"]))
        a = np.clip(a, 0, 1).astype(np.float32)
        drop_named = drop_found + [dict(r, name=r.get("name") or "region") for r in drop_regions if "box" in r]
        for i, o in enumerate(drop_named):
            o["guarded"] = i in guarded
        report = _check(a, h, W, [o for o in keep_found] + [dict(r, name=r.get("name") or "region", added=1.0)
                                                             for r in keep_regions if "box" in r],
                        keep_missing, drop_named, found["held"], found.get("error"), found.get("people") or ())

        def write():
            rgb = _decontaminate(np.asarray(img), a)
            a8 = (a * 255 + 0.5).astype(np.uint8)
            Image.fromarray(np.dstack([rgb, a8]), mode="RGBA").save(dst, "PNG")
            if alpha_out is not None:
                Image.fromarray(a8, mode="L").save(alpha_out, "PNG")
        await asyncio.to_thread(write)
    return report


def available() -> bool:
    if not (Path(settings.BIREFNET_MODEL_PATH).is_file() or Path(settings.CUTOUT_MODEL_PATH).is_file()):
        return False
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False
