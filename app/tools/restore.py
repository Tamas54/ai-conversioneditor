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
import logging
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger("aice.restore")

MODEL = "flux1-kontext"                 # FLUX.1 Kontext [pro] a kie.ai-n (app/tools/ai_image.py)
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
    return [p for p, _ in find_prints_q(src, workdir)]


def find_prints_q(src: Path, workdir: Path) -> list[tuple[Path, Optional[list[list[float]]]]]:
    """Mint a find_prints, de minden kivágott (szegély nélküli) kép mellé a helye az oldalon: a négy sarka
    (bal-felső, jobb-felső, jobb-alsó, bal-alsó) a forrás pixeleiben — a közös album-PSD ide teszi vissza a rétegeket.
    Ha nincs papírkép (az egész kép egy fotó): (útvonal, None)."""
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
    out: list[tuple[Path, Optional[list[list[float]]]]] = []
    for i, q in enumerate(sorted(quads, key=lambda q: (q[:, 1].min() // (h / 4), q[:, 0].min()))):   # sorrend: soronként, balról
        s, d = q.sum(1), np.diff(q, axis=1).ravel()
        tl, br, tr, bl = q[np.argmin(s)], q[np.argmax(s)], q[np.argmin(d)], q[np.argmax(d)]
        W = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
        H = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
        M = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl]), np.array([[0, 0], [W, 0], [W, H], [0, H]], dtype="float32"))
        flat = cv2.warpPerspective(img, M, (W, H))
        y0, y1, x0, x1 = _trim_box(flat)
        p = workdir / f"print-{i + 1}.png"
        cv2.imwrite(str(p), flat[y0:y1, x0:x1])
        # a levágott (szegély nélküli) téglalap sarkai vissza az oldal koordinátáiba
        rect = np.array([[[x0, y0], [x1, y0], [x1, y1], [x0, y1]]], dtype="float32")
        quad = cv2.perspectiveTransform(rect, np.linalg.inv(M))[0]
        out.append((p, [[float(x), float(y)] for x, y in quad]))
    if not out:
        p = workdir / "print-1.png"
        cv2.imwrite(str(p), img)
        out.append((p, None))
    return out


def _trim_box(img) -> tuple[int, int, int, int]:
    """A régi papírkép fehér/világos szegélye nem a fotó része (a színező elszínezné, a Kontext keretnek nézné) → levágjuk:
    kívülről befelé, amíg a sor/oszlop szegély-szerű (a pixelek ≥90%-a egy világos tónus körül).
    10-12 (ChatGPT-teszt, albumoldal): a kiegyenesített kivágás legszélén 1-3 px sötét albumháttér marad (a talált
    négyszög kicsit nagyobb a papírnál) — a régi „egész sor egyenletes” próba ezen elbukott, és a teljes keret bennmaradt.
    Ezért: a szél sötét fringe-sorait átlépjük, és a sor arányát nézzük (a sarkokban lévő háttér, porszem nem számít)."""
    import cv2
    import numpy as np
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    h, w = g.shape
    ref = float(np.median(g))

    def plain(line) -> bool:
        m = float(np.median(line))
        return m > max(150, ref + 25) and float((np.abs(line - m) < 18).mean()) > 0.9

    def inward(line_at, n: int) -> int:
        """Hány sort/oszlopot vágjunk le az egyik oldalról (0, ha nincs szegély)."""
        fringe, i = max(3, round(n * 0.02)), 0
        while i < fringe and not plain(line_at(i)):
            i += 1
        if i == fringe:
            return 0
        while i < n * 0.15 and plain(line_at(i)):
            i += 1
        return i
    t = inward(lambda i: g[i, :], h)
    b = h - inward(lambda i: g[h - 1 - i, :], h)
    l = inward(lambda i: g[t:b, i], w)
    r = w - inward(lambda i: g[t:b, w - 1 - i], w)
    pad = 3 if (t or l or (h - b) or (w - r)) else 0     # a szegély és a kép közti árnyékcsík se maradjon
    return min(h - 1, t + pad), max(t + 1, b - pad), min(w - 1, l + pad), max(l + 1, r - pad)


def trim_border(img):
    """A szegély nélküli kép (lásd _trim_box)."""
    y0, y1, x0, x1 = _trim_box(img)
    return img[y0:y1, x0:x1]


# ---------- AI-lépés + ráillesztés ----------

async def _kontext(src: Path, prompt: str, bill=None) -> bytes:
    """FLUX.1 Kontext [pro] a kie.ai-n (app/tools/ai_image.py); bill: a fizetős művelet számlája (ai_image.paid)."""
    from app.tools import ai_image
    try:
        return await ai_image.kontext(src, prompt, bill)
    except Exception as e:
        raise RuntimeError(f"The restoration service did not answer: {e}") from e


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
            raise ValueError("too few matches")
        src = np.float32([k2[x.queryIdx].pt for x in matches])
        dst_pts = np.float32([k1[x.trainIdx].pt for x in matches])
        Hm, inl = cv2.findHomography(src, dst_pts, cv2.RANSAC, 4.0)
        n = int(inl.sum()) if inl is not None else 0
        if Hm is None or n < 25:
            raise ValueError(f"too few inliers ({n})")
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


async def restore_print(src: Path, colorize: Optional[bool], stem: str, source_id: str,
                        layers_out: Optional[list] = None) -> list[dict[str, Any]]:
    """Egy papírkép → PSD (eredeti + helyreállított + szín) és JPG. colorize=None: fekete-fehérnél igen.
    layers_out: ide kerülnek a rétegképek (név, útvonal, keverés) a közös album-PSD-hez."""
    from app.storage import output_path, public_url, write_meta
    from app.storage.files import new_file_id
    from app.tools import crafts
    mono = await asyncio.to_thread(_is_mono, src)
    color = mono if colorize is None else (colorize and mono)
    from app.tools import ai_image
    calls = 2 if color else 1           # Kontext-hívásonként a listaár (helyreállítás, + színezés); hibánál semmi
    work = src.parent
    async with ai_image.paid("ai_edit", calls * ai_image.edit_price(), f"restore:{stem}") as bill:
        restored = work / f"{src.stem}-restored.png"
        raw = await _kontext(src, PROMPT_RESTORE if mono else PROMPT_RESTORE_TONE, bill)
        inliers = await asyncio.to_thread(align, src, raw, restored)
        layers = [("Restored (AI)", restored, None)]
        if color:
            colored = work / f"{src.stem}-colour.png"
            raw_c = await _kontext(restored, PROMPT_COLOR, bill)
            await asyncio.to_thread(align, src, raw_c, colored)
            layers.append(("Colour (AI)", colored, "color"))
    if layers_out is not None:
        layers_out += [("Original", src, None)] + layers
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
    from app import billing
    with billing.prepaid():          # a helyreállítás kreditből fizet — a modellhívásai nem külön AI-lépések
        return await _run(file_ids, colorize, split_prints, progress)


async def _run(file_ids: list[str], colorize: Optional[bool], split_prints: bool, progress=None) -> dict[str, Any]:
    import shutil
    from app.storage import display_label, temp_path
    from app.storage.files import new_file_id
    from app.tools import crafts
    work = temp_path(new_file_id("rst"))
    work.mkdir(parents=True)
    files: list[dict[str, Any]] = []
    try:
        prints: list[tuple[Path, str, str]] = []
        pages: dict[str, list[tuple[Optional[list[list[float]]], list]]] = {}   # albumoldal → [(hely, rétegek)]
        for i, fid in enumerate(file_ids):
            src = crafts.existing(fid)
            sub = work / f"f{i:03d}"
            sub.mkdir()
            if split_prints:
                found = await asyncio.to_thread(find_prints_q, src, sub)
            else:                      # munkapéldány a saját mappában (a forrás mellé semmit nem írunk)
                from PIL import Image, ImageOps
                one = sub / "print-1.png"
                with Image.open(src) as im:
                    ImageOps.exif_transpose(im).convert("RGB").save(one)
                found = [(one, None)]
            stem = Path(display_label(fid)).stem
            if len(found) > 1:
                pages[fid] = [(q, []) for _, q in found]
            prints += [(p, stem if len(found) == 1 else f"{stem} – {k + 1}", fid) for k, (p, _) in enumerate(found)]
        seen: dict[str, int] = {}
        for k, (p, stem, fid) in enumerate(prints):
            if progress:
                progress(k, len(prints), "restoring")
            n = seen.get(fid, 0)
            seen[fid] = n + 1
            files += await restore_print(p, colorize, stem, fid, layers_out=pages[fid][n][1] if fid in pages else None)
        if progress:
            progress(len(prints), len(prints), "restoring")
        # 10-12 (A8): az albumoldal képei EGY PSD-ben is — az oldal a háttér, fotónként egy rétegcsoport
        # (eredeti / helyreállított / szín) pontosan a kép helyén; a külön PSD-k is megmaradnak.
        for fid, placed in pages.items():
            if all(q for q, _ in placed):
                files.append(await album_psd(crafts.existing(fid), placed, Path(display_label(fid)).stem, fid, work))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    album = next((f["file_id"] for f in files if f.get("album")), None)
    return {"files": files, "file_id": files[0]["file_id"], "photos": len(prints),
            **({"album_file_id": album} if album else {})}


async def album_psd(page: Path, placed: list, stem: str, source_id: str, work: Path) -> dict[str, Any]:
    """Közös album-PSD: alul az albumoldal, fölötte fotónként egy csoport („Photo 1”…) a rétegeivel.
    A rétegeket MI torzítjuk a kép helyére (teljes vásznas, átlátszó PNG-k) — a PhotoCraft okosobjektum-
    transzformációja más határokat adott (10-12 mérés); két CLI-menet: elhelyezés, aztán csoportosítás
    (a rétegazonosítók a mentéskor átszámozódnak, ezért a második menet az `info`-ból dolgozik)."""
    import json
    import cv2
    import numpy as np
    from app.storage import output_path, public_url, write_meta
    from app.storage.files import new_file_id
    from app.tools import crafts
    page_img = cv2.imdecode(np.frombuffer(page.read_bytes(), np.uint8), cv2.IMREAD_COLOR)   # mint a find_prints_q
    H, W = page_img.shape[:2]
    base = work / f"album-{source_id}.png"
    cv2.imwrite(str(base), page_img)

    def warp(path: Path, quad, dst: Path) -> None:
        im = cv2.imread(str(path), cv2.IMREAD_COLOR)
        h, w = im.shape[:2]
        M = cv2.getPerspectiveTransform(np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype="float32"),
                                        np.array(quad, dtype="float32"))
        rgb = cv2.warpPerspective(im, M, (W, H), flags=cv2.INTER_CUBIC)
        a = cv2.warpPerspective(np.full((h, w), 255, np.uint8), M, (W, H), flags=cv2.INTER_LINEAR)
        cv2.imwrite(str(dst), np.dstack([rgb, a]))

    cmds: list[str] = []
    names: list[list[str]] = []
    for k, (quad, layers) in enumerate(placed, 1):
        group = []
        for j, (name, path, blend) in enumerate(layers):
            dst = work / f"album-{source_id}-{k}-{j}.png"
            await asyncio.to_thread(warp, path, quad, dst)
            lname = f"Photo {k} · {name}"
            cmds += ["--cmd", "file.placeEmbedded", "--params",
                     json.dumps({"path": str(dst), "scale": 100, "fit": False, "center": [W / 2, H / 2]})]
            cmds += ["--cmd", "layer.renameLayer", "--params", json.dumps({"name": lname})]
            if blend:
                cmds += ["--cmd", "layer.layerStyle.blendingOptions", "--params", json.dumps({"blend": blend})]
            group.append(lname)
        names.append(group)
    flat = work / f"album-{source_id}-flat.psd"
    await crafts.run_cli(crafts.photo_cli(), "photocraft-cli", "run", str(base), *cmds, "--out", str(flat))
    info = json.loads(await crafts.run_cli(crafts.photo_cli(), "photocraft-cli", "info", str(flat), "--compact"))
    ids = {l["name"]: l["id"] for l in info.get("layers") or []}
    gcmds: list[str] = []
    for k, group in enumerate(names, 1):
        for j, lname in enumerate(group):
            if lname in ids:
                gcmds += ["--cmd", "layer.select", "--params",
                          json.dumps({"layer": ids[lname], "mode": "replace" if j == 0 else "add"})]
        gcmds += ["--cmd", "layer.groupLayers", "--params", json.dumps({"name": f"Photo {k}"})]
    fid = new_file_id("psd")
    await crafts.run_cli(crafts.photo_cli(), "photocraft-cli", "run", str(flat), *gcmds, "--out", str(output_path(fid)))
    label = f"{stem} (album, restored layers).psd"
    write_meta(fid, source_file_id=source_id, operation="restore (album layers)", label=label)
    return {"file_id": fid, "url": public_url(fid), "label": label, "album": True, "photos": len(placed),
            "size_kb": output_path(fid).stat().st_size // 1024}
