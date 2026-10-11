"""Régi fotók helyreállítása (családi album): karc, por, folt, szakadás, fakulás, elmosódás → tiszta kép,
opcionálisan színezve — RÉTEGZETT PSD-ben, hogy az AI semmit ne vehessen el visszavonhatatlanul.

Lánc képenként:
  1. (albumoldal / lefotózott kép) a papírképek megkeresése és kiegyenesítése — egy fotón több kép is lehet
  2. FLUX.1-Kontext-pro: helyreállítás fekete-fehérben (vagy az eredeti tónusban), „ugyanazok az arcok, keret”
  3. ORB-jellemzők + homográfia: az AI-kimenet pontos ráillesztése az eredetire (a Kontext keretet tol/nagyít)
  4. (színezés) Kontext a MÁR ráillesztett helyreállításon: „csak a színek” → újra ráillesztve
  5. PSD: alul az eredeti, felette „Restored (AI)”, legfelül „Colour (AI)” Color keveréssel (a fényesség és a
     részletek a helyreállításból, csak a szín az AI-tól) + JPG. Ha az AI egy arcot elrontott, a szerkesztőben
     egy maszkkal az eredeti visszahozható.

Mérve 10-11 (szintetikus 1950-es családi fotó, ismert tiszta eredetivel; SSIM tónus-kiegyenlítve):
  sérült 0,58 (arcok 0,59) → Kontext + ráillesztés 0,73 (arcok 0,62); színezve 0,72. Ráillesztés nélkül
  a Kontext keret-eltolása miatt 0,61. Qwen-Image-Edit hívebb (arcok 0,64), de „AI生成” vízjelet tesz rá → nem.
  Hagyományos OpenCV (inpaint + zajszűrés + CLAHE): a karc/por eltűnik, a szakadás/folt/elmosódás marad (0,59).
"""
from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any, Optional

from app.config import settings

log = logging.getLogger("aice.restore")

MODEL = "black-forest-labs/FLUX.1-Kontext-pro"
_KEEP = ("Keep exactly the same people, faces, expressions, poses, hands, clothing, background and framing; "
         "do not add, remove or reinterpret anything.")
PROMPT_RESTORE = ("Restore this damaged old photograph: remove scratches, cracks, dust spots, stains and creases, repair torn "
                  "or missing corners and edges, fix the fading with natural contrast and gentle sharpness. " + _KEEP +
                  " Keep it a black-and-white photograph.")
PROMPT_RESTORE_TONE = PROMPT_RESTORE.replace(" Keep it a black-and-white photograph.", " Keep its original colours and tone.")
PROMPT_COLOR = ("Colorize this black-and-white photograph with natural, realistic colours true to its era. Change only the "
                "colours. " + _KEEP)


# ---------- papírképek egy fotón / albumoldalon ----------

def find_prints(src: Path, workdir: Path) -> list[Path]:
    """A képen lévő papírképek (legfeljebb 6, egyenként ≥6% terület), kiegyenesítve; ha nincs, az egész kép."""
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(src.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    scale = 1000 / max(h, w)
    small = cv2.resize(img, (int(w * scale), int(h * scale)))
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    edges = cv2.dilate(cv2.Canny(gray, 30, 110), np.ones((3, 3), np.uint8))
    area_total = small.shape[0] * small.shape[1]
    quads: list[Any] = []
    for c in sorted(cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0], key=cv2.contourArea, reverse=True)[:12]:
        a = cv2.contourArea(c)
        if a < 0.06 * area_total or a > 0.97 * area_total:
            continue
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(approx) == 4:
            quads.append(approx.reshape(4, 2).astype("float32") / scale)
        if len(quads) == 6:
            break
    out: list[Path] = []
    for i, q in enumerate(sorted(quads, key=lambda q: (q[:, 1].min() // (h / 4), q[:, 0].min()))):   # sorrend: soronként, balról
        s, d = q.sum(1), np.diff(q, axis=1).ravel()
        tl, br, tr, bl = q[np.argmin(s)], q[np.argmax(s)], q[np.argmin(d)], q[np.argmax(d)]
        W = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
        H = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
        M = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl]), np.array([[0, 0], [W, 0], [W, H], [0, H]], dtype="float32"))
        p = workdir / f"print-{i + 1}.png"
        cv2.imwrite(str(p), trim_border(cv2.warpPerspective(img, M, (W, H))))
        out.append(p)
    if not out:
        p = workdir / "print-1.png"
        cv2.imwrite(str(p), img)
        out.append(p)
    return out


def trim_border(img):
    """A régi papírkép fehér/világos szegélye nem a fotó része (a színező elszínezné) → levágjuk:
    kívülről befelé, amíg a sor/oszlop egyenletesen világos (kis szórás, magas átlag)."""
    import cv2
    import numpy as np
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    h, w = g.shape
    ref = float(np.median(g))

    def plain(line) -> bool:
        return line.std() < 14 and line.mean() > max(150, ref + 25)
    t, b, l, r = 0, h, 0, w
    while t < h * 0.15 and plain(g[t, l:r]): t += 1
    while b > h * 0.85 and plain(g[b - 1, l:r]): b -= 1
    while l < w * 0.15 and plain(g[t:b, l]): l += 1
    while r > w * 0.85 and plain(g[t:b, r - 1]): r -= 1
    pad = 3 if (t or l or (h - b) or (w - r)) else 0     # a szegély és a kép közti árnyékcsík se maradjon
    return img[min(h - 1, t + pad):max(t + 1, b - pad), min(w - 1, l + pad):max(l + 1, r - pad)]


# ---------- AI-lépés + ráillesztés ----------

async def _kontext(src: Path, prompt: str) -> bytes:
    import httpx
    from PIL import Image
    with Image.open(src) as im:
        w, h = im.size
    ratio = w / h
    aspect = min(("21:9", 21 / 9), ("16:9", 16 / 9), ("3:2", 1.5), ("4:3", 4 / 3), ("1:1", 1.0), ("3:4", 0.75), ("2:3", 2 / 3),
                 ("9:16", 9 / 16), ("9:21", 9 / 21), key=lambda a: abs(a[1] - ratio))[0]
    data = "data:image/png;base64," + base64.b64encode(_png_for_model(src)).decode()
    last: Exception = RuntimeError("no answer")
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=300) as c:
                r = await c.post(f"{settings.SILICONFLOW_BASE_URL}/images/generations",
                                 headers={"Authorization": f"Bearer {settings.SILICONFLOW_API_KEY}"},
                                 json={"model": MODEL, "prompt": prompt, "input_image": data, "aspect_ratio": aspect})
                if r.status_code >= 500:
                    raise RuntimeError(f"{r.status_code} {r.text[:200]}")
                r.raise_for_status()
                img = (r.json().get("images") or r.json().get("data"))[0]
                g = await c.get(img["url"], timeout=60)     # ugyanabban a kliensben (az aláírt link érzékeny)
                g.raise_for_status()
                return g.content
        except Exception as e:
            last = e
            await asyncio.sleep(3 * (attempt + 1))
    raise RuntimeError(f"The restoration service did not answer: {last}")


def _png_for_model(src: Path) -> bytes:
    """≤ 1600 px PNG (a modell úgyis ~1 MP-en dolgozik; a nagy szken csak lassít)."""
    import io
    from PIL import Image
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((1600, 1600))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()


def align(ref: Path, moving: bytes, dst: Path) -> int:
    """Az AI-kimenet ráillesztése az eredetire (ORB + RANSAC homográfia). Vissza: inlier-pontok száma
    (0 → nem sikerült, ilyenkor egyszerű átméretezés)."""
    import cv2
    import numpy as np
    r = cv2.imread(str(ref))
    m = cv2.imdecode(np.frombuffer(moving, np.uint8), cv2.IMREAD_COLOR)
    H_, W_ = r.shape[:2]
    try:
        g1 = cv2.equalizeHist(cv2.cvtColor(r, cv2.COLOR_BGR2GRAY))
        g2 = cv2.equalizeHist(cv2.cvtColor(m, cv2.COLOR_BGR2GRAY))
        orb = cv2.ORB_create(4000)
        k1, d1 = orb.detectAndCompute(g1, None)
        k2, d2 = orb.detectAndCompute(g2, None)
        matches = sorted(cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(d2, d1), key=lambda x: x.distance)[:800]
        if len(matches) < 20:
            raise ValueError("kevés egyezés")
        src = np.float32([k2[x.queryIdx].pt for x in matches])
        dst_pts = np.float32([k1[x.trainIdx].pt for x in matches])
        Hm, inl = cv2.findHomography(src, dst_pts, cv2.RANSAC, 4.0)
        n = int(inl.sum()) if inl is not None else 0
        if Hm is None or n < 25:
            raise ValueError(f"kevés inlier ({n})")
        out = cv2.warpPerspective(m, Hm, (W_, H_), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        cover = cv2.warpPerspective(np.full(m.shape[:2], 255, np.uint8), Hm, (W_, H_), flags=cv2.INTER_NEAREST)
        # ahol az AI-kép nem fed: vékony szélen (≤2%) a szélső pixel ismétlése (különben az eredeti csíkja
        # látszik, pl. a kitörölt fűszál), nagyobb hiánynál (a modell kivágott) az eredeti pixel marad
        band = max(2, int(0.02 * min(W_, H_)))
        inner = np.zeros((H_, W_), bool)
        inner[band:H_ - band, band:W_ - band] = True
        use_orig = (cover == 0) & inner
        out[use_orig] = r[use_orig]
    except Exception as e:
        log.warning("ráillesztés nem sikerült (%s) — átméretezés", e)
        out, n = cv2.resize(m, (W_, H_), interpolation=cv2.INTER_CUBIC), 0
    cv2.imwrite(str(dst), out)
    return n


def _is_mono(path: Path) -> bool:
    """Fekete-fehér / szépia: alacsony színtelítettség (a szépia egyetlen árnyalat)."""
    import cv2
    import numpy as np
    hsv = cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2HSV)
    return float(np.std(hsv[:, :, 0][hsv[:, :, 1] > 40])) < 12 if (hsv[:, :, 1] > 40).mean() > 0.05 else True


async def _charge(n: int, ref: str, reason: str = "photo restoration") -> bool:
    from app import db, tenancy
    uid = tenancy.current_user_id()
    charged = db.enabled() and uid != tenancy.ADMIN_ID and settings.IMAGE_CREDIT_COST > 0
    if charged:
        await db.credits_spend(uid, settings.IMAGE_CREDIT_COST * n, reason, ref=ref)
    return charged


async def _refund(n: int, ref: str) -> None:
    from app import db, tenancy
    await db.credits_add(tenancy.current_user_id(), settings.IMAGE_CREDIT_COST * n, "refund: restoration failed", ref=ref)


async def restore_print(src: Path, colorize: Optional[bool], stem: str, source_id: str) -> list[dict[str, Any]]:
    """Egy papírkép → PSD (eredeti + helyreállított + szín) és JPG. colorize=None: fekete-fehérnél igen."""
    from app.storage import output_path, public_url, write_meta
    from app.storage.files import new_file_id
    from app.tools import crafts
    mono = await asyncio.to_thread(_is_mono, src)
    color = mono if colorize is None else (colorize and mono)
    steps = 2 if color else 1
    charged = await _charge(steps, stem)
    work = src.parent
    try:
        restored = work / f"{src.stem}-restored.png"
        raw = await _kontext(src, PROMPT_RESTORE if mono else PROMPT_RESTORE_TONE)
        inliers = await asyncio.to_thread(align, src, raw, restored)
        layers = [("Restored (AI)", restored, None)]
        if color:
            colored = work / f"{src.stem}-colour.png"
            raw_c = await _kontext(restored, PROMPT_COLOR)
            await asyncio.to_thread(align, src, raw_c, colored)
            layers.append(("Colour (AI)", colored, "color"))
    except Exception:
        if charged:
            await _refund(steps, stem)
        raise
    # PSD: alul az eredeti (Background), felette a beágyazott AI-rétegek, pontosan fedésben
    from PIL import Image
    with Image.open(src) as im:
        W, H = im.size
    cmds: list[dict[str, Any]] = []
    for name, path, blend in layers:
        cmds.append({"command": "file.placeEmbedded", "params": {"path": str(path), "scale": 100, "fit": False, "center": [W / 2, H / 2]}})
        cmds.append({"command": "layer.renameLayer", "params": {"name": name}})
        if blend:
            cmds.append({"command": "layer.layerStyle.blendingOptions", "params": {"blend": blend}})
    base_id = new_file_id("png")
    output_path(base_id).write_bytes(src.read_bytes())
    write_meta(base_id, source_file_id=source_id, operation="restore (original print)", label=f"{stem} (original).png")
    psd = await crafts.photo_run(file_id=base_id, commands=cmds, save_format="psd")
    write_meta(psd["file_id"], source_file_id=source_id, operation="restore (layers)", label=f"{stem} (restored, layers).psd")
    jpg_id = new_file_id("jpg")
    await crafts.run_cli(crafts.photo_cli(), "photocraft-cli", "convert", str(output_path(psd["file_id"])), str(output_path(jpg_id)),
                         "--quality", "92")
    from app.tools import ai_label
    ai_label.label(output_path(jpg_id), ai_label.EDITED, "Restored" + (" and colourised" if color else "") + " with AI (FLUX.1 Kontext)")
    label = f"{stem} (restored{', colour' if color else ''}).jpg"
    write_meta(jpg_id, source_file_id=source_id, operation="restore", label=label)
    return [{"file_id": jpg_id, "url": public_url(jpg_id), "label": label, "colorized": color, "aligned_points": inliers,
             "size_kb": output_path(jpg_id).stat().st_size // 1024},
            {"file_id": psd["file_id"], "url": psd["url"], "label": f"{stem} (restored, layers).psd", "size_kb": psd["size_kb"]}]


async def run(file_ids: list[str], colorize: Optional[bool], split_prints: bool, progress=None) -> dict[str, Any]:
    import shutil
    from app.storage import display_label, temp_path
    from app.storage.files import new_file_id
    from app.tools import crafts
    work = temp_path(new_file_id("rst"))
    work.mkdir(parents=True)
    files: list[dict[str, Any]] = []
    try:
        prints: list[tuple[Path, str, str]] = []
        for i, fid in enumerate(file_ids):
            src = crafts.existing(fid)
            sub = work / f"f{i:03d}"
            sub.mkdir()
            if split_prints:
                found = await asyncio.to_thread(find_prints, src, sub)
            else:                      # munkapéldány a saját mappában (a forrás mellé semmit nem írunk)
                from PIL import Image, ImageOps
                one = sub / "print-1.png"
                with Image.open(src) as im:
                    ImageOps.exif_transpose(im).convert("RGB").save(one)
                found = [one]
            stem = Path(display_label(fid)).stem
            prints += [(p, stem if len(found) == 1 else f"{stem} – {k + 1}", fid) for k, p in enumerate(found)]
        for k, (p, stem, fid) in enumerate(prints):
            if progress:
                progress(k, len(prints), "restoring")
            files += await restore_print(p, colorize, stem, fid)
        if progress:
            progress(len(prints), len(prints), "restoring")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"files": files, "file_id": files[0]["file_id"], "photos": len(prints)}
