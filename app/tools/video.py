"""Videó a Studióban — storytold EffectCraft (After Effects-szerű, .ecproj) és FilmCraft (Premiere-szerű).

Kész upstream release-ek (Apache-2.0), mint a többi Craft-motor: a CLI a konténerben fut, a szerkesztő
a böngészőben (/editor/effectcraft/, /editor/filmcraft/). A videó SZERKESZTHETŐ idővonallal készül:
az eredmény egy .ecproj (jelenetenként beágyazott kompozíció, feliratsávok, hang) + a renderelt MP4.

    make()    cikk / URL / dokumentum / saját szöveg → forgatókönyv (DeepSeek, JSON) → képek (a felhasználóé,
              stock, MCP-n a kliens AI-képe) → video_builder.jsx → `effectcraft-cli script` → .ecproj
              → `effectcraft-cli render` → MP4
    render()  egy (akár kézzel szerkesztett) projekt újrarenderelése MP4-re
    blank()   üres videóprojekt (álló / fekvő / négyzetes) a szerkesztőbe
    info()    kompozíciók, hossz, rétegek, médiafájlok

A projekt saját médiafájljai (képek, zene, hang) rejtett fájlok a munkaterületen; a projekt metája tartja a
név → file_id párokat („assets”). Az .ecproj relatív névvel hivatkozik rájuk, így renderkor egy ideiglenes
mappába rakjuk össze (stage), a böngészős szerkesztőnek pedig a bridge adja át őket (addFile) a projekt előtt.

Mérve 10-12 (16 mag): 8 s 1080×1920 render 16 s; a CLI a termék Docker-képében (jammy) gond nélkül fut.
⛔ Felolvasó (TTS) NINCS — saját hangfájl mehet alá, feliratok mindig vannak. Remotion NEM (céges licenc).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.config import settings
from app.storage import display_label, output_path, public_url, read_meta, temp_path, write_meta
from app.storage.files import new_file_id
from app.tools import crafts

log = logging.getLogger("aice.video")

FORMATS = {"vertical": (1080, 1920), "landscape": (1920, 1080), "square": (1080, 1080)}
_ALIASES = {"9:16": "vertical", "story": "vertical", "stories": "vertical", "reel": "vertical", "reels": "vertical",
            "shorts": "vertical", "tiktok": "vertical", "portrait": "vertical", "16:9": "landscape", "youtube": "landscape",
            "wide": "landscape", "horizontal": "landscape", "1:1": "square", "post": "square", "instagram": "square"}
FPS = 30
TICKS_PER_S = 254_016_000_000          # az .ecproj időegysége (mint a Premiere „tick”-je)
MOVIE_EXTS = {"mp4", "mov", "webm", "m4v", "mkv"}
AUDIO_EXTS = {"mp3", "wav", "m4a", "aac", "ogg", "oga", "flac", "aiff", "aif", "opus"}
EFFECT_OPEN = {"ecproj"}
FILM_OPEN = {"fcproj"} | MOVIE_EXTS
# amit a két szerkesztő menthet / renderelhet (a mentés-végpont ezt engedi be)
VIDEO_SAVE = {"ecproj", "fcproj", "mp4", "mov", "webm", "gif", "wav", "aiff", "otio", "lottie"}
FONT = {"bold": "Inter-Bold", "semibold": "Inter-SemiBold", "regular": "Inter-Regular"}   # az EffectCraftba csomagolt Inter
DEFAULT_ACCENT = "#FF5A36"
BUILDER_JS = Path(__file__).with_name("video_builder.jsx")
MAX_FETCH = 6 * 1024 * 1024

_gate: Optional[asyncio.Semaphore] = None     # egyszerre egy render/építés: CPU-zabáló, a párhuzamosság nem gyorsít


def _render_gate() -> asyncio.Semaphore:
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(1)
    return _gate


def effect_cli() -> Optional[str]:
    return crafts.cli_path(settings.EFFECTCRAFT_CLI, "effectcraft-cli")


def film_cli() -> Optional[str]:
    return crafts.cli_path(settings.FILMCRAFT_CLI, "filmcraft-cli")


def available() -> bool:
    return effect_cli() is not None


def fmt_name(fmt: Optional[str]) -> str:
    f = (fmt or "vertical").strip().lower()
    f = _ALIASES.get(f, f)
    if f not in FORMATS:
        raise ValueError(f"format: vertical (9:16) | landscape (16:9) | square (1:1) — not {fmt!r}")
    return f


async def _run(*args: str, cwd: Path, timeout: Optional[int] = None) -> str:
    """effectcraft-cli a megadott mappában (a szkriptek csak a projekt mappájából olvashatnak fájlt)."""
    exe = effect_cli()
    if exe is None:
        raise RuntimeError("The video engine (EffectCraft) isn't installed on this server")
    env = {**os.environ, "EFFECTCRAFT_NO_PREFS": "1"}
    proc = await asyncio.create_subprocess_exec(exe, *args, cwd=str(cwd), env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout or settings.VIDEO_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"effectcraft-cli {args[0]}: timed out")
    if proc.returncode != 0:
        msg = err.decode("utf-8", "replace").strip() or out.decode("utf-8", "replace").strip()
        raise RuntimeError(f"effectcraft-cli {args[0]}: {msg[-600:] or f'exit code {proc.returncode}'}")
    return out.decode("utf-8", "replace")


# ---------- a projekt médiafájljai ----------

def assets_of(file_id: str) -> dict[str, str]:
    return dict((read_meta(file_id) or {}).get("assets") or {})


def add_asset(project_id: str, name: str, src: Path) -> str:
    """Egy médiafájl a projekthez: rejtett fájl a munkaterületen + név → file_id a projekt metájában."""
    name = _asset_name(name)
    fid = new_file_id(name.rsplit(".", 1)[-1].lower() if "." in name else "bin")
    shutil.copyfile(src, output_path(fid))
    write_meta(fid, operation="video asset", label=name, source_file_id=project_id, extra={"ephemeral": True})
    write_meta(project_id, extra={"assets": {**assets_of(project_id), name: fid}})
    return fid


def carry_assets(source_id: Optional[str], new_id: str) -> None:
    """Szerkesztőbeli mentés (.ecproj) → az új változat ugyanazokat a médiafájlokat látja."""
    if not source_id:
        return
    meta = read_meta(source_id) or {}
    extra = {k: meta[k] for k in ("assets", "video") if meta.get(k)}
    if extra:
        write_meta(new_id, extra=extra)


def _asset_name(name: str) -> str:
    name = Path(str(name).replace("\\", "/")).name.strip() or "media"
    return re.sub(r"[^\w.\- ]+", "_", name)[:120]


def stage(project_id: str, workdir: Path) -> Path:
    """Projekt + médiafájlok egy mappába; a médiahivatkozások a fájlnévre igazítva (a böngészőben importált
    fájl útvonala lehet /név is). → a projekt útvonala."""
    src = crafts.existing(project_id)
    assets = assets_of(project_id)
    for name, fid in assets.items():
        try:
            p = crafts.existing(fid)
        except FileNotFoundError:
            log.warning("hiányzó videó-médiafájl: %s (%s)", name, fid)
            continue
        dst = workdir / name
        if not dst.exists():
            try:
                os.symlink(p, dst)
            except OSError:
                shutil.copyfile(p, dst)
    data = json.loads(src.read_text("utf-8"))
    for item in (data.get("items") or {}).values():
        kind = item.get("kind") or {}
        if kind.get("type") == "Footage" and kind.get("path"):
            base = Path(str(kind["path"]).replace("\\", "/")).name
            if base in assets:
                kind["path"] = base
    out = workdir / "project.ecproj"
    out.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return out


def _comps(project: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for item in (project.get("items") or {}).values():
        k = item.get("kind") or {}
        if k.get("type") == "Comp":
            out.append({"name": item.get("name"), "width": k.get("width"), "height": k.get("height"),
                        "duration": round((k.get("duration") or 0) / TICKS_PER_S, 2), "layers": len(k.get("layers") or [])})
    return out


def _main_comp(project_path: Path) -> str:
    comps = _comps(json.loads(project_path.read_text("utf-8")))
    names = [c["name"] for c in comps]
    if "Video" in names:
        return "Video"
    return max(comps, key=lambda c: (c["layers"], c["duration"]))["name"] if comps else "-"


# ---------- forrás: cikk, URL, dokumentum ----------

async def fetch_article(url: str) -> dict[str, Any]:
    """Egy weboldal cikk-szövege (címsorok + bekezdések), címe, fő képe (og:image) és a webhely neve."""
    import httpx
    from bs4 import BeautifulSoup
    from app.tools.images import _UA as polite_ua
    browser = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
    host = (httpx.URL(url).host or "").lower()
    # 10-12 élesben: a Wikipedia 403-mal tiltja az adatközpontból jövő, böngészőnek álcázott kérést — a Wikimedia
    # a kapcsolatot megadó, őszinte User-Agentet kéri; máshol a böngésző-UA az első, 403/429 után az őszinte
    uas = [polite_ua, browser] if host.endswith(("wikipedia.org", "wikimedia.org", "wikinews.org", "wikivoyage.org")) else [browser, polite_ua]
    r = None
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
        for ua in uas:
            r = await c.get(url, headers={"User-Agent": ua, "Accept-Language": "en,hu;q=0.8,*;q=0.5"})
            if r.status_code not in (401, 403, 429):
                break
    if r is None or r.status_code >= 400:
        code = r.status_code if r is not None else "no answer"
        raise ValueError(f"That page wouldn't let us read it ({code}) — paste the article's text instead.")
    html = r.content[:MAX_FETCH].decode(r.encoding or "utf-8", "replace")
    soup = BeautifulSoup(html, "lxml")
    meta = lambda *keys: next((m.get("content") for k in keys for m in soup.find_all("meta", attrs={"property": k}) + soup.find_all("meta", attrs={"name": k}) if m.get("content")), None)
    title = meta("og:title", "twitter:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    for t in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "figure", "iframe", "svg"]):
        t.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    parts = []
    for el in root.find_all(["h1", "h2", "h3", "p", "li", "blockquote"]):
        txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if len(txt) >= (12 if el.name.startswith("h") else 40):
            parts.append(("# " if el.name.startswith("h") else "") + txt)
    text = "\n\n".join(dict.fromkeys(parts))
    if len(text) < 200:
        raise ValueError("Couldn't read an article on that page — paste its text instead.")
    return {"title": title or "", "text": text[:30_000], "image": meta("og:image", "twitter:image"),
            "site": meta("og:site_name") or re.sub(r"^www\.", "", httpx.URL(url).host or "")}


# ---------- forgatókönyv ----------

_PLAN = """You plan short social videos as JSON for a motion-graphics builder: full-screen pictures, an on-screen
headline per scene and timed captions. Reply with ONE json object:
{"title": str, "kicker": str, "image_query": str, "scenes": [{"headline": str, "caption": str, "image_query": str, "stat": {"value": str, "label": str} | null}],
 "end": {"title": str, "subtitle": str}}
Rules:
- exactly {n} scenes; write in the language of the source (unless told otherwise); no invented facts, names or numbers.
- title ≤ 60 characters, catchy; kicker ≤ 24 characters (the topic or the source's name); the top-level image_query is
  the title card's picture — a different one from every scene's.
- headline ≤ 55 characters — the scene's point, not a label. caption = what a narrator would say: 1–2 short sentences,
  ≤ 170 characters, and the captions together tell the story in order.
- image_query: 2–4 plain English words for a stock-photo search — concrete and visual ("wind turbines at sunset",
  "crowded city street"), never abstract.
- stat only when the source gives a striking number (at most 2 scenes); value ≤ 8 characters ("72%", "3.4 bn"), label ≤ 40.
- end.title ≤ 50 characters (the takeaway or a call to action); end.subtitle ≤ 80 (e.g. where to read more)."""

_KEEP = """
The user wrote this text as the video's SCRIPT: keep their wording for the captions (fix only typos), one scene per
paragraph or idea, in their order; write the headlines and image queries yourself."""


def _clean(s: Any, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 1].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


def _paragraphs(text: str) -> list[str]:
    paras = [re.sub(r"\s+", " ", p).strip(" #") for p in re.split(r"\n\s*\n|\n(?=#)", text) if p.strip()]
    return [p for p in paras if len(p) > 3]


def _fallback_plan(text: str, n: int, title: str) -> dict[str, Any]:
    """Ha a modell nem elérhető: bekezdésenként egy jelenet (zsákutca helyett egyszerűbb, de kész videó)."""
    paras = _paragraphs(text) or [text]
    sentences = [s for p in paras for s in re.split(r"(?<=[.!?…])\s+", p) if len(s) > 3]
    chunk = max(1, math.ceil(len(sentences) / n))
    scenes = []
    for i in range(0, len(sentences), chunk):
        cap = " ".join(sentences[i:i + chunk])
        scenes.append({"headline": _clean(cap.split(".")[0], 55), "caption": _clean(cap, 170), "image_query": "", "stat": None})
    return {"title": title or _clean(paras[0], 60), "kicker": "", "scenes": scenes[:max(n, 1)], "end": {"title": title or "", "subtitle": ""}}


async def plan(text: str, *, n: int, keep_wording: bool, title_hint: str = "", language: Optional[str] = None) -> dict[str, Any]:
    from app.llm.siliconflow import _call_model, _strip_json_fences
    system = _PLAN.replace("{n}", str(n)) + (_KEEP if keep_wording else "")
    user = (f"Language: {language}\n" if language else "") + (f"Working title: {title_hint}\n" if title_hint else "") + "SOURCE:\n" + text[:24_000]
    last: Exception = RuntimeError("no model")
    for model in (settings.LLM_MODEL_PRIMARY, settings.LLM_MODEL_FALLBACK):
        try:
            raw = await _call_model(model, [{"role": "system", "content": system}, {"role": "user", "content": user}], thinking=False)
            data = json.loads(_strip_json_fences(raw))
            if isinstance(data, dict) and data.get("scenes"):
                return data
            raise ValueError("no scenes in the plan")
        except Exception as e:   # következő modell
            last = e
            log.warning("videó-forgatókönyv %s modellel nem sikerült: %s", model, e)
    log.warning("videó-forgatókönyv: egyszerű bontás (%s)", last)
    return _fallback_plan(text, n, title_hint)


# ---------- elrendezés és időzítés ----------

def _layout(W: int, H: int) -> dict[str, Any]:
    m = min(W, H)
    vertical, landscape = H > W * 1.2, W > H * 1.2
    L: dict[str, Any] = {"bar": [round(m * 0.11), max(6, round(m * 0.009))], "kickerSize": round(m * 0.032),
                         "capSize": round(m * (0.042 if not landscape else 0.038)), "creditSize": round(m * 0.022),
                         "statSize": round(m * 0.24), "statLabelSize": round(m * 0.052), "titleAlign": "left"}
    if vertical:
        L.update(titleW=W * 0.84, titleX=W / 2, kickerTop=H * 0.575, barY=H * 0.618, titleTop=H * 0.635,
                 headW=W * 0.84, headX=W / 2, headBarY=H * 0.655, headTop=H * 0.67, capW=W * 0.84, capY=H * 0.865)
    elif landscape:
        L.update(titleW=W * 0.62, titleX=W * 0.07 + W * 0.31, kickerTop=H * 0.50, barY=H * 0.565, titleTop=H * 0.59,
                 headW=W * 0.56, headX=W * 0.07 + W * 0.28, headBarY=H * 0.60, headTop=H * 0.62, capW=W * 0.74, capY=H * 0.895)
    else:
        L.update(titleW=W * 0.84, titleX=W / 2, kickerTop=H * 0.53, barY=H * 0.58, titleTop=H * 0.60,
                 headW=W * 0.84, headX=W / 2, headBarY=H * 0.585, headTop=H * 0.60, capW=W * 0.84, capY=H * 0.885)
    L.update(statY=H * 0.42, endY=H * 0.42, endBarY=H * 0.52, endSubY=H * 0.585, creditY=H * 0.935, capPad=round(m * 0.022))
    # a nagy szám alatti címke a szám MÉRETÉHEZ igazodik (az a rövidebb oldalból jön) — a magasság fix aránya (0,53 H)
    # fekvő 16:9-ben a számba csúsztatta a címkét (10-12, gazdasági videó); állóban ez ~ugyanott marad
    L["statLabelY"] = L["statY"] + L["statSize"] * 0.5 + L["statLabelSize"] * 1.2
    return L


def _size_for(text: str, base: float, ref: int, floor: float) -> int:
    return round(max(floor, base * min(1.0, math.sqrt(ref / max(len(text), 1)))))


def _chunks(text: str, max_chars: int) -> list[str]:
    """Felirat-darabok: mondathatáron törünk (a rövid mondatok egy darabba kerülnek); a túl hosszú mondat
    tagmondathatáron (, ; : —), végső esetben szóhatáron. (10-12: a puszta karakterszámos bontás mondat
    közepén tört: „…keep millions from going” / „solar. Community…”.)"""
    def words(s: str) -> list[str]:
        out, cur = [], ""
        for w in s.split():
            if cur and len(cur) + 1 + len(w) > max_chars:
                out.append(cur); cur = w
            else:
                cur = f"{cur} {w}".strip()
        return out + ([cur] if cur else [])
    pieces: list[str] = []
    for sent in re.split(r"(?<=[.!?…])\s+", text.strip()):
        if len(sent) <= max_chars:
            pieces.append(sent)
            continue
        cur = ""
        for clause in re.split(r"(?<=[,;:—–])\s+", sent):
            if len(clause) > max_chars:
                if cur:
                    pieces.append(cur); cur = ""
                pieces += words(clause)
            elif cur and len(cur) + 1 + len(clause) > max_chars:
                pieces.append(cur); cur = clause
            else:
                cur = f"{cur} {clause}".strip()
        if cur:
            pieces.append(cur)
    out: list[str] = []
    for p in pieces:              # rövid mondatok összevonva, ha együtt is elférnek
        if out and len(out[-1]) + 1 + len(p) <= max_chars and (len(p) < max_chars * 0.45 or len(out[-1]) < max_chars * 0.45):
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    return [o for o in out if o]


def _shade_png(path: Path, W: int, H: int, top: float, full: float, opacity: float) -> None:
    """Alul sötétedő átmenet előre megrajzolva (fekete, alfa top·H-tól full·H-ig 0 → opacity, lágy görbével).
    A Linear Wipe-os változat képkockánként ~20 ms volt; a kész PNG csak összeillesztés."""
    from PIL import Image
    import numpy as np
    y = np.linspace(0, 1, H, dtype=np.float32)
    t = np.clip((y - top) / max(full - top, 1e-3), 0, 1)
    a = (t * t * (3 - 2 * t) * opacity * 255).astype(np.uint8)
    alpha = np.repeat(a[:, None], W, axis=1)
    img = Image.fromarray(np.dstack([np.zeros((H, W, 3), np.uint8), alpha]), "RGBA")
    img.save(path, "PNG", optimize=True)


def _blurred(src: Path, dst: Path, radius: float) -> None:
    from PIL import Image, ImageFilter
    with Image.open(src) as im:
        im.convert("RGB").filter(ImageFilter.GaussianBlur(radius)).save(dst, "JPEG", quality=85)


def _rgb(hex_color: Optional[str]) -> list[float]:
    h = (hex_color or DEFAULT_ACCENT).lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        h = DEFAULT_ACCENT.lstrip("#")
    return [round(int(h[i:i + 2], 16) / 255, 4) for i in (0, 2, 4)]


def _focus(path: Path) -> list[float]:
    """A kép „érdekes pontja" (0–1 koordináták) a Ken Burns-mozgás céljának: spectral residual szaliencia
    (Hou–Zhang; csak numpy + OpenCV-alap — az OpenCV 5-ből kikerült az arcfelismerő), enyhe középre húzással."""
    import cv2
    import numpy as np
    from PIL import Image
    try:
        im = Image.open(path).convert("L")
        im.thumbnail((128, 128))
        g = np.asarray(im, dtype=np.float32)
        F = np.fft.fft2(g)
        la = np.log(np.abs(F) + 1e-6)
        sal = np.abs(np.fft.ifft2(np.exp((la - cv2.blur(la, (3, 3))) + 1j * np.angle(F)))) ** 2
        sal = cv2.GaussianBlur(sal.astype(np.float32), (0, 0), 3)
        h, w = sal.shape
        yy, xx = np.mgrid[0:h, 0:w]
        sal *= np.exp(-(((xx - w / 2) / w) ** 2 + ((yy - h / 2) / h) ** 2))      # enyhe középre húzás
        top = (sal >= np.quantile(sal, 0.95)).astype(np.uint8)
        n, lab, stats, cent = cv2.connectedComponentsWithStats(top)
        if n > 1:      # a legerősebb összefüggő folt közepe (a szétszórt csúcsok átlaga középre húzna)
            k = 1 + int(np.argmax([sal[lab == j].sum() for j in range(1, n)]))
            cx, cy = cent[k]
        else:
            cx, cy = w / 2, h / 2
        return [float(round(min(0.8, max(0.2, cx / w)), 3)), float(round(min(0.8, max(0.2, cy / h)), 3))]
    except Exception:
        return [0.5, 0.45]


def _motions(n: int, ken_burns: str) -> list[str]:
    """Jelenetenkénti képmozgás. ken_burns: some (alap: kb. minden második jelenet határozott Ken Burns —
    közelítés + úsztatás a kép érdekes pontja felé, minden második ilyen fordítva, kifelé —, a többi finom
    mozgás) | all | off (csak a finom mozgás)."""
    gentle = ["in", "left", "out", "right"]
    out = []
    for i in range(n):
        kb = ken_burns == "all" or (ken_burns == "some" and i % 2 == 0)
        out.append(("kb-out" if (i // 2 if ken_burns == "some" else i) % 2 else "kb-in") if kb else gentle[i % 4])
    return out


def compose(story: dict[str, Any], *, fmt: str, total: Optional[float], images: list[Optional[str]],
            accent: Optional[str], credits: str, voice: Optional[str], voice_s: Optional[float], music: Optional[str],
            focus: Optional[dict[str, list[float]]] = None, ken_burns: str = "some") -> dict[str, Any]:
    """A forgatókönyvből a builder adatcsomagja (D): méretek, jelenet-hosszak, feliratok időzítve."""
    W, H = FORMATS[fmt]
    L = _layout(W, H)
    m = min(W, H)
    scenes = story["scenes"]
    n = len(scenes)
    X = 0.5
    if voice_s:
        total = max(8.0, voice_s + 1.0)
    Tt, Te = (2.6, 3.0) if (total or 60) < 22 else (3.6, 4.2)
    weights = [max(len(s.get("caption") or s.get("headline") or ""), 45) for s in scenes]
    if total is None:                        # saját szöveg, hossz nélkül: olvasási tempó (~14 karakter/s)
        durs = [max(3.5, w / 14 + 1.0) for w in weights]
        total = Tt + sum(durs) + Te - X * (n + 1)
    else:
        room = max(total - Tt - Te + X * (n + 1), 3.0 * n)
        durs = [max(3.0, room * w / sum(weights)) for w in weights]
        k = room / sum(durs)
        durs = [d * k for d in durs]
        total = Tt + sum(durs) + Te - X * (n + 1)
    total = round(total, 2)
    cpl = L["capW"] / (0.53 * L["capSize"])
    max_chunk = int(cpl * 2 * 0.9)
    captions, t = [], Tt - X
    d_scenes = []
    motions = _motions(len(scenes), ken_burns)
    for i, (s, d) in enumerate(zip(scenes, durs)):
        cap = s.get("caption") or ""
        parts = _chunks(cap, max_chunk) if cap else []
        a, b = t + X + 0.15, t + d - X - 0.05
        tot = sum(len(p) for p in parts) or 1
        cur = a
        for p in parts:
            span = (b - a) * len(p) / tot
            captions.append({"t0": round(cur, 3), "t1": round(cur + span, 3), "text": p, "lines": 1 if len(p) <= cpl * 0.95 else 2})
            cur += span
        head = s.get("headline") or ""
        stat = s.get("stat") or None
        if stat:      # a nagy szám EGY sorban férjen el (10-12: „7,800 MW” két sorba tört és a címkére csúszott)
            stat = {**stat, "size": round(min(L["statSize"], W * 0.88 / (0.62 * max(len(stat["value"]), 1))))}
        d_scenes.append({"headline": head, "stat": stat, "dur": round(d, 3), "image": images[i + 1] if i + 1 < len(images) else None,
                         "motion": motions[i], "focus": (focus or {}).get(images[i + 1] if i + 1 < len(images) else "") or [0.5, 0.45],
                         "size": _size_for(head, m * (0.066 if fmt != "landscape" else 0.058), 34, m * 0.046)})
        t += d - X
    title = story.get("title") or ""
    end = story.get("end") or {}
    end_title = end.get("title") or title
    # zárókép: a csík a cím ALATT, az alcím a csík alatt — a cím becsült sorszámából (10-12: fix helyen az alcímre csúszott)
    end_size = _size_for(end_title, m * 0.072, 30, m * 0.05)
    end_lines = max(1, math.ceil(len(end_title) / max(1.0, (W * 0.84) / (0.55 * end_size))))
    end_top = H * (0.30 if H >= W else 0.26)
    L["endY"] = end_top + m * 0.22
    L["endBarY"] = end_top + end_lines * end_size * 1.1 + H * 0.03
    L["endSubY"] = L["endBarY"] + H * 0.035 + L["capSize"] * 1.7
    D = {
        "W": W, "H": H, "fps": FPS, "total": total, "xfade": X, "accent": _rgb(accent), "dark": [0.055, 0.06, 0.08],
        "font": FONT,
        "L": {**L, "titleH": m * 0.44, "headH": m * 0.36,
              "kickerY": L["kickerTop"] + L["kickerSize"] * 0.8, "titleY": L["titleTop"] + m * 0.22, "headY": L["headTop"] + m * 0.18},
        "title": {"text": title, "kicker": story.get("kicker") or "", "image": images[0] if images else None, "dur": Tt,
                  "motion": "in", "size": _size_for(title, m * (0.088 if fmt != "landscape" else 0.075), 26, m * 0.056)},
        "scenes": d_scenes,
        "end": {"title": end_title, "subtitle": end.get("subtitle") or "", "credits": credits, "dur": Te,
                "image": images[0] if images else None, "size": end_size},
        "captions": captions,
        "audio": {"voice": voice, "music": music, "musicDb": -20 if voice else -10},
    }
    return D


# ---------- építés ----------

async def _probe_duration(workdir: Path, name: str) -> Optional[float]:
    code = f'app.project.importFile(new ImportOptions(new File({json.dumps(name)}))).duration'
    try:
        out = await _run("script", "--eval", code, "--empty", cwd=workdir, timeout=120)
        return float(re.findall(r"[\d.]+", out.strip().splitlines()[-1])[0])
    except Exception as e:
        log.warning("hanghossz nem olvasható (%s): %s", name, e)
        return None


async def _materialize_images(specs: list[Any], fmt: str, workdir: Path) -> tuple[list[Optional[str]], list[str]]:
    """Képleírások (file_id / {url} / {stock} / {generate}) → fájlnevek a munkamappában + forrásmegjelölések."""
    from app.tools import images as imgs
    W, H = FORMATS[fmt]
    names: list[Optional[str]] = []
    credits: list[str] = []

    async def one(i: int, spec: Any) -> tuple[Optional[str], Optional[str]]:
        if not spec:
            return None, None
        try:
            r = await imgs.resolve(spec, aspect=W / H, max_px=max(W, H))
        except Exception as e:
            log.warning("videókép %s nem jött le (%s): %s", i, spec, e)
            return None, None
        name = f"picture-{i + 1:02d}.jpg"
        shutil.move(str(r.path), workdir / name)
        return name, r.attribution
    sem = asyncio.Semaphore(4)

    async def guarded(i, s):
        async with sem:
            return await one(i, s)
    res = await asyncio.gather(*(guarded(i, s) for i, s in enumerate(specs)))
    for name, credit in res:
        names.append(name)
        if credit and credit not in credits:
            credits.append(credit)
    return names, credits


_PICK_N = 6


def _contact_sheet(thumbs: list[bytes]) -> str:
    """A jelöltek bélyegképei számozott rácsban (3×2) — egy képen, egy látómodell-hívásnak."""
    import base64
    import io
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    cw, ch, cols = 360, 240, 3
    rows = math.ceil(len(thumbs) / cols)
    sheet = Image.new("RGB", (cols * cw, rows * ch), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 34)
    except Exception:
        font = ImageFont.load_default()
    for k, data in enumerate(thumbs):
        x, y = (k % cols) * cw, (k // cols) * ch
        try:
            im = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGB"), (cw - 6, ch - 6))
            sheet.paste(im, (x + 3, y + 3))
        except Exception:
            pass
        draw.rectangle([x + 3, y + 3, x + 50, y + 46], fill="black")
        draw.text((x + 14, y + 6), str(k + 1), fill="white", font=font)
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


async def _rank(query: str, headline: str, cands: list[dict[str, Any]]) -> list[int]:
    """A látómodell sorrendje a jelöltekre (0-tól), a legjobban illő elöl; hiba esetén a kereső sorrendje.
    Nem tartalmi szűrés (semmit nem dob ki): a kereső a FÁJLNÉVBEN keres, és absztrakt témánál mellé nyúl —
    10-12, gazdasági videó: „oil barrels" → vasúti fordítókorong, „bond" → múzeumi kötvénypapír."""
    from app.llm import vision as v
    from app.tools import images as imgs
    thumbs = await asyncio.gather(*(imgs._get(c["thumb_url"]) for c in cands), return_exceptions=True)
    ok = [(i, t.content) for i, t in enumerate(thumbs) if not isinstance(t, Exception) and t.status_code == 200]
    if len(ok) < 2:
        return list(range(len(cands)))
    prompt = (f'A scene of a short news video: "{headline}". Its full-screen background picture should show: "{query}". '
              "Rank the numbered pictures by how well they fit: a clear, real photo of that subject first; old documents, "
              "maps, diagrams, logos or unrelated places last. Return JSON only: {\"ranking\": [numbers, best first]}")
    msgs = [{"role": "user", "content": [{"type": "text", "text": prompt},
                                         {"type": "image_url", "image_url": {"url": _contact_sheet([b for _, b in ok])}}]}]
    for model, tmo in ((settings.LLM_MODEL_VISION, 40), (settings.LLM_MODEL_VISION_FALLBACK, 60)):
        try:
            raw = await asyncio.wait_for(v._call(msgs, max_tokens=200, model=model), tmo)
            m = re.search(r"\{.*\}", v._strip_json_fences(raw), re.S)
            order = [ok[int(n) - 1][0] for n in (json.loads(m.group(0)).get("ranking") or []) if 1 <= int(n) <= len(ok)]
            if order:
                return list(dict.fromkeys(order + [i for i, _ in ok]))
        except Exception as e:
            log.info("képrangsor (%s, %s): %s", model, query, e)
    return [i for i, _ in ok]


async def _choose_stock(jobs: list[tuple[int, str, str]]) -> dict[int, dict[str, Any]]:
    """Jelenetenként: stock-jelöltek → a látómodell rangsorolja → a legjobb, ami még nem szerepelt másik jelenetben.
    jobs = [(spec-index, keresőszó, a jelenet címe)] → {spec-index: {"url", "credit"}}; ami nem sikerült, kimarad."""
    from app.tools import images as imgs
    sem = asyncio.Semaphore(4)

    async def one(q: str, head: str) -> tuple[list[dict[str, Any]], list[int]]:
        async with sem:
            try:
                # „gold bars and oil barrels": a Commons a fájlnévben keres, az összetett kifejezésre rossz jelölteket ad →
                # a részek külön keresve, a jelöltek felváltva összefésülve (a látómodell választ közülük)
                parts = [x.strip() for x in re.split(r"\s+(?:and|&|with)\s+|,", q) if len(x.strip()) > 2]
                if len(parts) > 1:
                    pools = await asyncio.gather(*(imgs.search(x, n=_PICK_N) for x in parts[:3]))
                    seen, cands = set(), []
                    for row in zip(*[pl + [None] * (_PICK_N - len(pl)) for pl in pools]):
                        for c in row:
                            if c and c["url"] not in seen:
                                seen.add(c["url"]); cands.append(c)
                    cands = cands[:_PICK_N]
                else:
                    cands = (await imgs.search(q, n=_PICK_N + 2))[:_PICK_N]
                return cands, (await _rank(q, head, cands) if len(cands) > 1 else list(range(len(cands))))
            except Exception as e:
                log.info("képjelöltek (%s): %s", q, e)
                return [], []
    got = await asyncio.gather(*(one(q, h) for _, q, h in jobs))
    out, used = {}, set()
    for (i, _, _), (cands, order) in zip(jobs, got):
        for k in order:
            if cands[k]["url"] not in used:
                used.add(cands[k]["url"])
                out[i] = {"url": cands[k]["url"], "credit": cands[k]["credit"]}
                break
    return out


def _title_stem(title: str) -> str:
    t = re.sub(r"[\\/:*?\"<>|\s]+", " ", title or "").strip(" .")
    return (t[:60].rsplit(" ", 1)[0] if len(t) > 60 else t) or "Video"


async def make(body: dict[str, Any], progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    """Automata videó. body: {mode: article|text|new, text?, url?, source_file_id?, format?, duration?,
    images?: [file_id | {url} | {stock} | {generate}], pictures?: stock|mine|none, music_file_id?, voice_file_id?,
    accent?, language?, title?, render?: bool (alap true)}."""
    step = progress or (lambda *a, **k: None)
    mode = (body.get("mode") or "article").lower()
    fmt = fmt_name(body.get("format"))
    if mode == "new":
        return await blank(fmt, body.get("duration"))
    if not available():
        raise RuntimeError("The video engine isn't installed on this server")
    text = (body.get("text") or "").strip()
    url = (body.get("url") or "").strip()
    if not url and re.fullmatch(r"https?://\S+", text):
        url, text = text, ""
    title_hint, og_image, site, doc_pics = (body.get("title") or "").strip(), None, "", []
    step(0, 5, "reading the source")
    if url:
        art = await fetch_article(url)
        text, title_hint, og_image, site = art["text"], title_hint or art["title"], art["image"], art["site"]
    elif body.get("source_file_id"):
        from app.tools import doc_source
        try:
            src = await doc_source.read(body["source_file_id"])
        except ValueError as e:
            raise ValueError(str(e).replace("Slides", "A video").replace("make slides from", "make a video from"))
        text, title_hint = src["text"], title_hint or src["title"]
        doc_pics = [p["file_id"] for p in src.get("pictures") or []]
    if len(text) < 20:
        raise ValueError("Give the video something to say — an article link, a document, or a few sentences of your own.")
    keep = mode == "text"
    dur = body.get("duration")
    total = None if (keep and not dur) else float(min(max(float(dur or 60), 10), 180))
    n = len(_paragraphs(text)) if keep else max(2, min(12, round(((total or 60) - 7) / 6.5)))
    n = max(1, min(n, 14))

    step(1, 5, "writing the script")
    story = await plan(text, n=n, keep_wording=keep, title_hint=title_hint, language=body.get("language"))
    story["title"] = _clean(story.get("title") or title_hint or "Video", 70)
    story["kicker"] = _clean(story.get("kicker") or site, 28)
    story["scenes"] = [{"headline": _clean(s.get("headline"), 60), "caption": _clean(s.get("caption"), 200),
                        "image_query": _clean(s.get("image_query"), 60),
                        "stat": ({"value": _clean(s["stat"].get("value"), 10), "label": _clean(s["stat"].get("label"), 48)}
                                 if isinstance(s.get("stat"), dict) and s["stat"].get("value") else None)}
                       for s in story.get("scenes") or [] if isinstance(s, dict)][:14]
    if not story["scenes"]:
        raise ValueError("Couldn't plan any scenes from that text.")
    story["end"] = {"title": _clean((story.get("end") or {}).get("title"), 60), "subtitle": _clean((story.get("end") or {}).get("subtitle"), 90)}

    step(2, 5, "finding pictures")
    pictures = (body.get("pictures") or "stock").lower()
    mine = [s for s in (body.get("images") or []) if s] + doc_pics
    specs: list[Any] = []
    for i in range(len(story["scenes"]) + 1):       # 0 = címkép, 1… = jelenetek
        if pictures == "none":
            specs.append(None)
        elif mine and (i < len(mine) or pictures == "mine"):
            specs.append(mine[i % len(mine)])
        elif i == 0 and og_image:
            specs.append({"url": og_image, "credit": site or None})
        elif pictures != "mine":
            # a címkártya saját képet kap (ne az 1. jelenetét); ha a terv nem adott, az utolsó jelenet keresőszava
            q = (_clean(story.get("image_query"), 60) or story["scenes"][-1]["image_query"]) if i == 0 \
                else story["scenes"][i - 1]["image_query"]
            specs.append({"stock": q or story["title"]})
        else:
            specs.append(None)

    # a stock-keresések jelöltjeiből a látómodell választ (és egy kép nem kerül két jelenetbe)
    stock_jobs = [(i, sp["stock"], story["title"] if i == 0 else story["scenes"][i - 1]["headline"])
                  for i, sp in enumerate(specs) if isinstance(sp, dict) and sp.get("stock")]
    if stock_jobs:
        for i, chosen in (await _choose_stock(stock_jobs)).items():
            specs[i] = chosen

    work = Path(tempfile.mkdtemp(prefix="video-", dir=str(temp_path("").parent)))
    try:
        names, credit_list = await _materialize_images(specs, fmt, work)
        voice = music = None
        voice_s = None
        for key, base in (("voice_file_id", "voice"), ("music_file_id", "music")):
            fid = body.get(key)
            if fid:
                p = crafts.existing(fid)
                ext = p.suffix.lstrip(".").lower()
                if ext not in AUDIO_EXTS:
                    raise ValueError(f"The {base} file should be audio (mp3, wav, m4a…), not .{ext}")
                shutil.copyfile(p, work / f"{base}.{ext}")
                if base == "voice":
                    voice = f"voice.{ext}"
                else:
                    music = f"music.{ext}"
        if voice:
            voice_s = await _probe_duration(work, voice)
        credits = ("Pictures: " + " · ".join(credit_list)) if credit_list else ""     # teljes (linkekkel): a projekt metájába
        # a képernyőn rövid: név + licenc, linkek nélkül („Photo: X (Wikimedia Commons), CC BY 4.0 — https://…”)
        short = [re.sub(r"\s+—\s+https?://\S+", "", re.sub(r"^Photo:\s*", "", c)).strip() for c in credit_list]
        on_screen = ("Pictures: " + " · ".join(dict.fromkeys(s for s in short if s)))[:300] if short else ""
        kb = str(body.get("ken_burns") or "some").lower()
        kb = kb if kb in ("some", "all", "off") else "some"
        focus = {nm: await asyncio.to_thread(_focus, work / nm) for nm in names if nm} if kb != "off" else {}
        D = compose(story, fmt=fmt, total=total, images=names, accent=body.get("accent"), credits=on_screen, focus=focus, ken_burns=kb,
                    voice=voice, voice_s=voice_s, music=music)
        # előre megrajzolt rétegek (képkockánként effekt helyett): sötét átmenet a cím/jelenet alá, elmosott zárókép
        W, H = FORMATS[fmt]
        _shade_png(work / "shade-title.png", W, H, 0.28, 0.80, 0.92)
        _shade_png(work / "shade-scene.png", W, H, 0.34, 0.84, 0.90)
        extra = ["shade-title.png", "shade-scene.png"]
        if names and names[0]:
            _blurred(work / names[0], work / "end-blur.jpg", max(W, H) * 0.018)
            D["end"]["image"] = "end-blur.jpg"
            extra.append("end-blur.jpg")
        D["shade"] = {"title": "shade-title.png", "scene": "shade-scene.png"}
        step(3, 5, "building the timeline")
        (work / "build.jsx").write_text("var D = " + json.dumps(D, ensure_ascii=False) + ";\n" + BUILDER_JS.read_text("utf-8"), "utf-8")
        async with _render_gate():
            out = await _run("script", "build.jsx", "--save-as", "project.ecproj", cwd=work)
        log.info("videó felépítve: %s", out.strip().splitlines()[-2:] if out.strip() else "")
        stem = _title_stem(story["title"])
        pid = new_file_id("ecproj")
        shutil.copyfile(work / "project.ecproj", output_path(pid))
        from app.routes.make import _new_group
        group = _new_group()
        info_meta = {"format": fmt, "duration": D["total"], "scenes": len(D["scenes"]), "title": story["title"]}
        write_meta(pid, label=f"{stem}.ecproj", operation="video", source_file_id=body.get("source_file_id"),
                   extra={"video": info_meta, "group": group, "credit": credits or None})
        for name in [x for x in names if x] + [x for x in (voice, music) if x] + extra:
            add_asset(pid, name, work / name)
        result: dict[str, Any] = {"file_id": pid, "project": _file_entry(pid), "title": story["title"], "format": fmt,
                                  "duration": D["total"], "scenes": len(D["scenes"]), "captions": len(D["captions"]),
                                  "credits": credits or None, "story": story}
        if body.get("render", True):
            step(4, 5, "rendering the video")
            vid = await _render_staged(pid, work / "project.ecproj", stem, group=group, want_s=D["total"])
            result["check"] = vid.pop("check", None)          # a közös check-szerződés: a feladat (a videó) ellenőrzése
            result["video"] = vid
            result["files"] = [result["project"], vid]
        else:
            result["files"] = [result["project"]]
        step(5, 5, "done")
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _file_entry(fid: str) -> dict[str, Any]:
    return {"file_id": fid, "url": public_url(fid), "label": display_label(fid), "size_kb": output_path(fid).stat().st_size // 1024}


async def _render_staged(project_id: str, project_path: Path, stem: str, *, group: Optional[str] = None,
                         fmt: str = "mp4", draft: bool = False, want_s: Optional[float] = None) -> dict[str, Any]:
    ext = "webm" if fmt == "webm" else "gif" if fmt == "gif" else "mp4"
    out = project_path.parent / f"render.{ext}"
    args = ["render", "--project", project_path.name, "--comp", _main_comp(project_path), "--out", out.name,
            "--format", {"mp4": "h264", "webm": "webm", "gif": "gif"}[ext], "--json"]
    if ext == "mp4":
        args += ["--bitrate", "2500" if draft else "8000", "--audio", "auto"]
    if draft:
        args += ["--resolution", "half", "--quality", "draft"]
    t0 = time.time()
    async with _render_gate():
        await _run(*args, cwd=project_path.parent)
    if not out.exists():
        raise RuntimeError("The render produced no file")
    vid = new_file_id(ext)
    shutil.move(str(out), output_path(vid))
    meta = read_meta(project_id) or {}
    # a projekt és a renderje egy kártya (group); a gyors vázlat külön „változat”, hogy a Download sose azt adja
    write_meta(vid, label=f"{stem} (draft).{ext}" if draft else f"{stem}.{ext}", operation="video render", source_file_id=project_id,
               extra={"group": group or meta.get("group"), "variant": "draft" if draft else None, "credit": meta.get("credit")})
    if not draft:
        write_meta(project_id, extra={"render_file_id": vid})
    log.info("videó renderelve: %s → %s (%.1f s)", project_id, vid, time.time() - t0)
    from app.tools import checks
    fmt_wh = (read_meta(project_id) or {}).get("video", {}).get("format")
    chk = await asyncio.to_thread(checks.check_video, output_path(vid), want_s, FORMATS.get(fmt_wh) if ext == "mp4" else None, draft)
    return {**_file_entry(vid), "render_seconds": round(time.time() - t0, 1), "check": chk}


async def render(file_id: str, fmt: str = "mp4", draft: bool = False) -> dict[str, Any]:
    """Egy videóprojekt (akár kézzel szerkesztett) újrarenderelése."""
    if not file_id.lower().endswith(".ecproj"):
        raise ValueError("render needs a video project (.ecproj)")
    work = Path(tempfile.mkdtemp(prefix="video-", dir=str(temp_path("").parent)))
    try:
        proj = stage(file_id, work)
        stem = re.sub(r"( \(edited [^)]*\))+$", "", Path(display_label(file_id)).stem)   # a render neve a projekté, „(edited …)” nélkül
        return await _render_staged(file_id, proj, stem, fmt=fmt, draft=draft)
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def blank(fmt: Optional[str] = None, duration: Any = None) -> dict[str, Any]:
    """Üres videóprojekt: egy „Video” kompozíció a választott méretben, a szerkesztőbe."""
    fmt = fmt_name(fmt)
    W, H = FORMATS[fmt]
    dur = float(min(max(float(duration or 30), 1), 600))
    work = Path(tempfile.mkdtemp(prefix="video-", dir=str(temp_path("").parent)))
    try:
        params = {"name": "Video", "width": W, "height": H, "frameRate": FPS, "duration": dur, "background": "#0e0f14"}
        await _run("exec", "comp.new", "--params", json.dumps(params), "--empty", "--save-as", "project.ecproj", cwd=work, timeout=120)
        fid = new_file_id("ecproj")
        shutil.copyfile(work / "project.ecproj", output_path(fid))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    label = "Untitled video.ecproj"
    write_meta(fid, operation="new video", label=label, extra={"video": {"format": fmt, "duration": dur},
                                                                "blank": {"kind": "video", "size": fmt}})
    return {"file_id": fid, "url": public_url(fid), "label": label, "size_kb": output_path(fid).stat().st_size // 1024}


def info(file_id: str) -> dict[str, Any]:
    p = crafts.existing(file_id)
    ext = p.suffix.lstrip(".").lower()
    meta = read_meta(file_id) or {}
    if ext != "ecproj":
        return {"file_id": file_id, "kind": "movie" if ext in MOVIE_EXTS else ext, "size_kb": p.stat().st_size // 1024,
                "made_from": meta.get("source_file_id")}
    data = json.loads(p.read_text("utf-8"))
    footage = [Path(str((i.get("kind") or {}).get("path"))).name for i in (data.get("items") or {}).values()
               if (i.get("kind") or {}).get("type") == "Footage"]
    assets = assets_of(file_id)
    rid = meta.get("render_file_id")
    return {"file_id": file_id, "comps": _comps(data), "footage": footage, "missing": [f for f in footage if f not in assets],
            "video": meta.get("video"), "last_render": _file_entry(rid) if rid and output_path(rid).exists() else None,
            "credits": meta.get("credit")}


async def thumb(src: Path, raw: Path) -> None:
    """Bélyegkép: projektnél egy képkocka a címjelenetből, filmnél az 1. másodpercből."""
    ext = src.suffix.lstrip(".").lower()
    work = Path(tempfile.mkdtemp(prefix="vthumb-", dir=str(temp_path("").parent)))
    try:
        if ext == "ecproj":
            proj = stage(src.name, work)
            comp, t = _main_comp(proj), "1.6"
        else:
            name = f"movie.{ext}"
            os.symlink(src, work / name)
            code = (f'var f = app.project.importFile(new ImportOptions(new File({json.dumps(name)})));'
                    f'var c = app.project.items.addComp("Thumb", f.width, f.height, 1, Math.max(f.duration, 0.2), 30); c.layers.add(f);')
            (work / "t.jsx").write_text(code, "utf-8")
            await _run("script", "t.jsx", "--save-as", "project.ecproj", cwd=work, timeout=120)
            proj, comp, t = work / "project.ecproj", "Thumb", "1"
        await _run("render-frame", "--project", proj.name, "--comp", comp, "--time", t, "--max-side", "640",
                   "--out", str(raw), cwd=work, timeout=120)
    finally:
        shutil.rmtree(work, ignore_errors=True)
