"""Háttéreltávolítás neurális hálóval (ISNet, onnxruntime) — a PhotoCraft saját „Select Subject”-je
nehéz fotón (ellenfény, átlógó fűszálak) bokrokat is kivágott, az ISNet a bajuszszálakat is megtartja.

A rembg `isnet-general-use` útjának hű másolata (előfeldolgozás, min–max maszk, naiv kivágás), a rembg
függőségei (numba, scipy, scikit-image) nélkül; a modellt a Dockerfile tölti le (CUTOUT_MODEL_PATH).
CPU-n ~2–3 s egy 5 MP-es képre.

10-12 (ChatGPT-teszt, színpadi fotó): az ISNet egy „fő alanyt” tart meg — a háttérben álló, elmosódott
gitárost kidobta, a mintás ruhán félig átlátszó foltokat hagyott. Ezért:
  • ha van ember a képen, a U2Net human-seg maszkja (HUMAN_SEG_MODEL_PATH, ~0,3 s) is belekerül —
    MINDEN ember marad, egészben; a finom szél (haj) az ISNet-ből jön (pixelenként a nagyobb érték);
  • belső tömörítés: a maszk széltől k px-nél beljebb eső része teljesen fedett (a félig átlátszó
    belső foltok eltűnnek), a valódi rések (pl. kar és test között, ott a maszk ~0) megmaradnak.
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


def _get_session(path: Path):
    key = str(path)
    with _session_lock:
        if key not in _sessions:
            import onnxruntime as ort
            _sessions[key] = ort.InferenceSession(key, providers=["CPUExecutionProvider"])
        return _sessions[key]


def _mask(img):
    """ISNet: a fő alany finom szélekkel (0–255, a kép méretében)."""
    import numpy as np
    from PIL import Image
    im = np.array(img.convert("RGB").resize(_SIZE, Image.Resampling.LANCZOS), dtype=np.float64)
    im = im / max(im.max(), 1e-6)
    x = ((im - 0.5) / 1.0).transpose(2, 0, 1)[None].astype(np.float32)
    s = _get_session(settings.CUTOUT_MODEL_PATH)
    pred = s.run(None, {s.get_inputs()[0].name: x})[0][:, 0, :, :]
    pred = np.squeeze((pred - pred.min()) / (pred.max() - pred.min()))
    return Image.fromarray((pred * 255).astype("uint8"), mode="L").resize(img.size, Image.Resampling.LANCZOS)


def _people(img):
    """U2Net human-seg (rembg előfeldolgozás: 320², ImageNet-normalizálás): minden ember, egészben — vagy None."""
    if not Path(settings.HUMAN_SEG_MODEL_PATH).is_file():
        return None
    import numpy as np
    from PIL import Image
    im = np.array(img.convert("RGB").resize((320, 320), Image.Resampling.LANCZOS), dtype=np.float32) / 255.0
    im = im / max(im.max(), 1e-6)
    x = ((im - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])).transpose(2, 0, 1)[None].astype(np.float32)
    s = _get_session(settings.HUMAN_SEG_MODEL_PATH)
    p = np.squeeze(s.run(None, {s.get_inputs()[0].name: x})[0][:, 0])
    p = (p - p.min()) / max(p.max() - p.min(), 1e-6)
    return Image.fromarray((p * 255).astype("uint8"), mode="L").resize(img.size, Image.Resampling.LANCZOS)


def _alpha(img):
    """A végső maszk: ISNet ∪ emberek, belül tömörítve."""
    import cv2
    import numpy as np
    from PIL import Image
    a = np.asarray(_mask(img), dtype=np.float32) / 255
    hum = _people(img)
    if hum is not None:
        h = np.asarray(hum, dtype=np.float32) / 255
        if (h > 0.5).mean() > 0.01:                        # van ember: mindenki maradjon, egészben
            a = np.maximum(a, np.clip((h - 0.3) / 0.4, 0, 1))
    k = max(4, round(min(a.shape) * 0.006))                # a széltől k px-nél beljebb teljes fedés (puha átmenettel)
    dist = cv2.distanceTransform((a > 0.1).astype(np.uint8), cv2.DIST_L2, 5)
    a = np.maximum(a, np.clip((dist - k / 2) / (k / 2), 0, 1))
    return Image.fromarray((a * 255).astype("uint8"), mode="L")


def _cut(src: Path, dst: Path) -> None:
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    Image.composite(img, Image.new("RGBA", img.size, 0), _alpha(img)).save(dst, "PNG")


async def remove_background(src: Path, dst: Path) -> None:
    """src kép → dst PNG átlátszó háttérrel (az alany és MINDEN ember marad)."""
    async with _run_lock:
        await asyncio.to_thread(_cut, src, dst)


def available() -> bool:
    if not Path(settings.CUTOUT_MODEL_PATH).is_file():
        return False
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False
