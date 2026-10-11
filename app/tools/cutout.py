"""Háttéreltávolítás neurális hálóval (ISNet, onnxruntime) — a PhotoCraft saját „Select Subject”-je
nehéz fotón (ellenfény, átlógó fűszálak) bokrokat is kivágott, az ISNet a bajuszszálakat is megtartja.

A rembg `isnet-general-use` útjának hű másolata (előfeldolgozás, min–max maszk, naiv kivágás), a rembg
függőségei (numba, scipy, scikit-image) nélkül; a modellt a Dockerfile tölti le (CUTOUT_MODEL_PATH).
CPU-n ~2–3 s egy 5 MP-es képre.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from app.config import settings

_session = None
_session_lock = threading.Lock()
# Egyszerre egy kivágás: a háló memóriaigénye nagy, a párhuzamosság itt nem gyorsít.
_run_lock = asyncio.Lock()
_SIZE = (1024, 1024)


def _get_session():
    global _session
    with _session_lock:
        if _session is None:
            import onnxruntime as ort
            _session = ort.InferenceSession(str(settings.CUTOUT_MODEL_PATH), providers=["CPUExecutionProvider"])
        return _session


def _mask(img):
    import numpy as np
    from PIL import Image
    im = np.array(img.convert("RGB").resize(_SIZE, Image.Resampling.LANCZOS), dtype=np.float64)
    im = im / max(im.max(), 1e-6)
    x = ((im - 0.5) / 1.0).transpose(2, 0, 1)[None].astype(np.float32)
    s = _get_session()
    pred = s.run(None, {s.get_inputs()[0].name: x})[0][:, 0, :, :]
    pred = np.squeeze((pred - pred.min()) / (pred.max() - pred.min()))
    return Image.fromarray((pred * 255).astype("uint8"), mode="L").resize(img.size, Image.Resampling.LANCZOS)


def _cut(src: Path, dst: Path) -> None:
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    Image.composite(img, Image.new("RGBA", img.size, 0), _mask(img)).save(dst, "PNG")


async def remove_background(src: Path, dst: Path) -> None:
    """src kép → dst PNG átlátszó háttérrel (az alany marad)."""
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
