"""Háttéreltávolítás neurális hálókkal (onnxruntime) — a PhotoCraft saját „Select Subject”-je nehéz fotón
(ellenfény, átlógó fűszálak) bokrokat is kivágott.

Lánc:
  1. ALANY: BiRefNet-lite (MIT, BIREFNET_MODEL_PATH, CPU-n ~8-10 s) — tömör alany, haj, vékony tárgyak
     (mikrofonállvány, kábel); hiányában az ISNet (CUTOUT_MODEL_PATH, ~1,5 s, a rembg útjának hű másolata).
  2. EMBEREK: U2Net human-seg (HUMAN_SEG_MODEL_PATH, ~1 s) — a háttérben álló, elmosódott ember is maradjon.
     A 320²-es maszk széle elkenődik és túllóg a testen (világos körvonal) → keményebb küszöb + enyhe simítás.
  3. Takarítás: a bizonytalan alany-köd (α < 0,7) csak a biztos alany mellett marad, beljebb tömör a fedés,
     és a félig átlátszó szélekből kivonjuk a RÉGI háttér színét (különben világos „glória” marad az új háttéren).

Mérve 10-12 (ChatGPT-teszt, színpadi fotó, fehér lámpa és égbolt a háttérben):
  • ISNet + a régi „a>0,1 belseje tömör” tömörítés: a fej melletti félig átlátszó ködből (a lámpa a hajszálak
    között) tömör fehér folt lett; a gitáros körül az égbolt világos körvonala maradt.
  • BiRefNet-lite: a lámpa nincs benne, a ruha tömör (az ISNet lyukasra hagyta), az állvány és a kábel megvan;
    a háttérbeli gitárost ez sem veszi fel → az emberek maszkja továbbra is kell.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from app.config import settings

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


def _alpha(img):
    """A végső maszk (0–1, float32): alany ∪ emberek, a köd a biztos alany mellé szorítva, belül tömör."""
    import cv2
    import numpy as np
    a0 = np.asarray(_subject(img), dtype=np.float32) / 255
    m = min(a0.shape)
    hp = np.zeros_like(a0)
    hum = _people(img)
    if hum is not None:
        h = np.asarray(hum, dtype=np.float32) / 255
        if (h > 0.5).mean() > 0.01:                        # van ember: mindenki maradjon, egészben
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


def _cut(src: Path, dst: Path) -> None:
    import numpy as np
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    a = _alpha(img)
    rgb = _decontaminate(np.asarray(img), a)
    Image.fromarray(np.dstack([rgb, (a * 255 + 0.5).astype(np.uint8)]), mode="RGBA").save(dst, "PNG")


async def remove_background(src: Path, dst: Path) -> None:
    """src kép → dst PNG átlátszó háttérrel (az alany és MINDEN ember marad)."""
    async with _run_lock:
        await asyncio.to_thread(_cut, src, dst)


def available() -> bool:
    if not (Path(settings.BIREFNET_MODEL_PATH).is_file() or Path(settings.CUTOUT_MODEL_PATH).is_file()):
        return False
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False
