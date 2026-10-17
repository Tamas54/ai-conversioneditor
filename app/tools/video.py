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
import contextvars
import copy
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


_JAIL: list[bool] = []


async def _jail_ok(exe: str) -> bool:
    """Egyszeri önteszt: a motor elindul-e a zárkában (1+1). Ha nem — a megbízható munka zárka nélkül fut tovább
    (naplózva), idegen szkript viszont nem."""
    from app.tools import jail
    if not _JAIL:
        ok = False
        if jail.available():
            work = Path(tempfile.mkdtemp(prefix="jailtest-", dir=str(temp_path("").parent)))
            try:
                argv, env = jail.command(exe, ["script", "--eval", "1+1", "--empty"], work=str(work))
                proc = await asyncio.create_subprocess_exec(*argv, cwd=str(work), env=env, stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.PIPE)
                out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
                ok = proc.returncode == 0 and out.decode("utf-8", "replace").strip().endswith("2")
                if not ok:
                    log.error("a videómotor zárkája nem indul (rc %s): %s", proc.returncode, err.decode("utf-8", "replace")[-300:])
            except Exception as e:
                log.error("a videómotor zárkája nem indul: %s", e)
            finally:
                shutil.rmtree(work, ignore_errors=True)
        _JAIL.append(ok)
        log.info("videómotor zárkában: %s (Landlock ABI %s)", ok, jail.abi())
    return _JAIL[0]


async def _run(*args: str, cwd: Path, timeout: Optional[int] = None, untrusted: bool = False) -> str:
    """effectcraft-cli a megadott mappában, ZÁRKÁBAN (jail.py): csak a munkamappát írja, rajta kívül csak a
    rendszerkönyvtárakat és a mappába linkelt médiát olvassa, hálózat és alfolyamat nélkül, a környezeti változók
    (kulcsok) nélkül. A motor saját kapuja is él (a szkriptek csak a projekt mappájából olvasnak, nem írnak, nem
    hálóznak) — a zárka a második fal. untrusted (az ügynök saját szkriptje): zárka nélkül NEM fut."""
    from app.tools import jail
    exe = effect_cli()
    if exe is None:
        raise RuntimeError("The video engine (EffectCraft) isn't installed on this server")
    argv, env = [exe, *args], {**os.environ, "EFFECTCRAFT_NO_PREFS": "1"}
    if await _jail_ok(exe):
        argv, env = jail.command(exe, list(args), work=str(cwd), env={"EFFECTCRAFT_NO_PREFS": "1"},
                                 mem_gb=8.0 if untrusted else None)
    elif untrusted:
        raise RuntimeError("Custom scripts need the sandbox, and it isn't available on this server — use a graphic (type \"graphic\") instead.")
    else:
        log.warning("zárka nélkül fut a videómotor (a kernel nem tud Landlockot)")
    proc = await asyncio.create_subprocess_exec(*argv, cwd=str(cwd), env=env,
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
    extra = {k: meta[k] for k in ("assets", "video", "spec") if meta.get(k)}
    if extra.get("spec"):
        extra["hand_edited"] = True            # az ügynöki szerkesztés a forgatókönyvből épít újra — ezt jelezni kell
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
{"title": str, "kicker": str, "image_query": str, "scenes": [{"headline": str, "caption": str, "image_query": str, "stat": {"value": str, "label": str} | null,
  "chart": {"type": "column|bar|line|area|donut|ring|icons|timeline|steps|counter|compare|progress|gauge|funnel|proscons|quote",
            "title": str, "unit": str, "total": number | null, "min": number | null, "max": number | null,
            "items": [{"label": str, "value": number | null, "text": str}]} | null}],
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
- chart: an animated graphic for ANY topic (science, health, history, sport, culture, business, how-to…), only when
  the source supports it — pick the type that fits:
    column / bar — 2–8 comparable numbers of the SAME kind (bar for long names or many items);
    line — a value over time, in time order;
    donut — parts of one whole (2–6 shares the source gives; leave out "the rest" — it is added automatically);
    ring — ONE percentage worth showing (unit "%", one item: label = what it is);
    icons — "x in N" or a percentage of people/things (one item: value = x; total = N, or unit "%" for x out of 100);
    timeline — 2–6 dated events in order (label = the date exactly as written in the source, text = what happened);
    steps — 3–6 steps of a process or how-to in order (label = short step, text = one short line);
    area — a value over time where the amount matters (filled under the line);
    counter — ONE striking number that counts up (one item: label = what it is);
    compare — 2–3 numbers of the same kind side by side (before/after, A vs B);
    progress — 2–6 separate percentages that do NOT add up to a whole (e.g. "64% agree", "41% use daily");
    gauge — ONE value on a scale (min/max given in the source, or 0–100%), e.g. a risk level or a score;
    funnel — 3–6 shrinking stages of a process (applicants → interviewed → hired);
    proscons — arguments for (label "+") and against (label "-"), each a short text; labels? = the two headings;
    quote — ONE striking sentence quoted WORD FOR WORD from the source (text = the quote, label = who said it).
  Copy every number and date EXACTLY from the source (value = a plain number, no unit); unit = "%" or a short unit
  ("$", "km", "bn") or ""; item labels ≤ 18 characters (ring/icons: ≤ 60); at most 3 chart scenes (4 for 80 s or longer) and never two of the
  same type in a row; a scene has a stat OR a chart, never both; headline = what the graphic shows.
- end.title ≤ 50 characters (the takeaway or a call to action); end.subtitle ≤ 80 (e.g. where to read more)."""

_KEEP = """
The user wrote this text as the video's SCRIPT: keep their wording for the captions (fix only typos), one scene per
paragraph or idea, in their order; write the headlines and image queries yourself."""


_NUM_IN_TEXT = re.compile(r"(?<![\d.,])-?\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?(?![\d])|-?\d+(?:[.,]\d+)?")


_NUM_WORDS = {w: i for i, ws in enumerate((
    (), ("one", "egy"), ("two", "kettő", "két"), ("three", "három"), ("four", "négy"), ("five", "öt"), ("six",), ("seven",),
    ("eight", "nyolc"), ("nine", "kilenc"), ("ten", "tíz"), ("eleven", "tizenegy"), ("twelve", "tizenkettő"),
    ("thirteen",), ("fourteen",), ("fifteen",), ("sixteen",), ("seventeen",), ("eighteen",), ("nineteen",),
    ("twenty", "húsz"))) for w in ws}
_NUM_WORD_RE = re.compile(r"\b(" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")\b", re.I)


def _text_numbers(text: str) -> set[float]:
    """A szöveg összes száma (1,8 · 1.8 · 4 194 · 41 670 · 3,35, és a kis számnevek: „one in four", „négyből egy")
    — a grafikon minden értékének itt kell lennie."""
    out = {float(_NUM_WORDS[w.lower()]) for w in _NUM_WORD_RE.findall(text or "")}
    for t in _NUM_IN_TEXT.findall(text or ""):
        t = re.sub(r"[ \u00a0\u202f]", "", t)
        for cand in (t.replace(",", "."), t.replace(",", "")):
            try:
                out.add(round(float(cand), 6))
            except ValueError:
                pass
    return out


_GFX_SHAPES = ("rect", "circle", "line", "arrow", "path", "polygon", "text", "image", "pin", "lottie", "map")
_GFX_ANIMS = ("appear", "pop", "grow", "draw", "slide-left", "slide-right", "slide-up", "slide-down", "pulse", "none")


def _graphic_from_plan(c: dict[str, Any], source: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Saját grafika elemekből (az ügynök rajzolja le): alakzatok, nyilak, útvonalak, szövegek, képek, tűk, Lottie —
    a képkocka arányában (0–1), mozgással. Ellenőrzés: ismert elemek, ésszerű koordináták, legfeljebb 80 elem, a
    szövegekben csak forrásbeli szám (különben AZ az elem kimarad — a többi marad, a check jelzi)."""
    nums = _text_numbers(source)
    num = lambda x, d=None: float(x) if isinstance(x, (int, float)) else d
    pt = lambda q: [max(-0.2, min(1.2, num(q[0], 0.5))), max(-0.2, min(1.2, num(q[1], 0.5)))] if isinstance(q, (list, tuple)) and len(q) >= 2 else None

    def geo(q: Any) -> Optional[dict[str, Any]]:      # földrajzi hely: {"place": "Dóm tér, Szeged"} | {"lat", "lon"}
        if isinstance(q, dict) and isinstance(q.get("place"), str) and q["place"].strip():
            return {"place": q["place"].strip()[:160]}
        if isinstance(q, dict) and num(q.get("lat")) is not None and num(q.get("lon")) is not None:
            return {"lat": max(-85.0, min(85.0, float(q["lat"]))), "lon": max(-180.0, min(180.0, float(q["lon"])))}
        return None
    out, dropped, warn, expanded = [], [], [], 0
    alias = {"marker": "pin", "point": "pin", "place": "pin", "label": "text", "title": "text", "route": "path",
             "polyline": "path", "dot": "circle", "ellipse": "circle", "square": "rect", "rectangle": "rect", "box": "rect",
             "picture": "image", "photo": "image", "animation": "lottie", "basemap": "map"}
    flat: list[Any] = []
    for e in (c.get("elements") or [])[:80]:
        if isinstance(e, dict) and isinstance(e.get("shape"), str):
            e = {**e, "shape": alias.get(e["shape"].lower(), e["shape"].lower())}
        flat.append(e)
        if isinstance(e, dict) and e.get("shape") == "map":       # a térképbe írt tűk és útvonalak külön elemként
            for key in ("pins", "markers", "places", "points_of_interest"):
                for q in e.get(key) or []:
                    q = {"place": q} if isinstance(q, str) else q
                    if isinstance(q, dict):
                        flat.append({"shape": "pin", "anim": "pop", **q, "text": q.get("text") or q.get("label") or q.get("name") or ""})
                        expanded += 1
            for key in ("route", "routes", "paths", "lines"):
                rs = e.get(key) or []
                rs = [rs] if rs and not isinstance(rs[0], list) and not (isinstance(rs[0], dict) and "points" in rs[0]) else rs
                for r_ in rs:
                    pts_ = r_.get("points") if isinstance(r_, dict) else r_
                    if isinstance(pts_, list) and len(pts_) >= 2:
                        pts_ = [{"place": q} if isinstance(q, str) else q for q in pts_]
                        flat.append({"shape": "path", "stroke": "accent", "width": 8, "anim": "draw",
                                     **({k: v for k, v in r_.items() if k != "points"} if isinstance(r_, dict) else {}), "points": pts_})
                        expanded += 1
    for n_, e in enumerate(flat[:120], 1):
        if not isinstance(e, dict) or e.get("shape") not in _GFX_SHAPES:
            what = e.get("shape") if isinstance(e, dict) else type(e).__name__
            warn.append(f"element {n_}: unknown shape “{what}” — left out (use {', '.join(_GFX_SHAPES)})")
            continue
        for k_ in ("location", "position"):                          # rokon kulcsok a helyre
            if e.get(k_) is not None and not e.get("place") and "lat" not in e:
                q = e[k_]
                if isinstance(q, str):
                    e = {**e, "place": q}
                elif isinstance(q, dict) and (q.get("place") or "lat" in q):
                    e = {**e, **{k: q[k] for k in ("place", "lat", "lon") if k in q}}
                elif isinstance(q, (list, tuple)) and len(q) == 2 and "x" not in e:
                    e = {**e, "x": q[0], "y": q[1]}
        el: dict[str, Any] = {"shape": e["shape"]}
        for k in ("x", "y", "w", "h", "r"):
            if num(e.get(k)) is not None:
                el[k] = max(-0.2, min(1.2, float(e[k])))
        for k in ("size", "width", "radius", "at", "dur", "opacity"):
            if num(e.get(k)) is not None:
                el[k] = float(e[k])
        for k in ("from", "to", "move_to"):
            if pt(e.get(k)):
                el[k] = pt(e[k])
            elif geo(e.get(k)) or geo(e.get("geo_" + k)):
                el["geo_" + k] = geo(e.get(k)) or geo(e.get("geo_" + k))
        g_ = geo(e.get("geo")) or (geo(e) if e["shape"] != "map" else None)   # a tű / szöveg helye a jelenet térképén
        if g_:
            el["geo"] = g_
        if isinstance(e.get("points"), list) or isinstance(e.get("geo_points"), list):
            raw = e.get("points") if isinstance(e.get("points"), list) else e["geo_points"]
            gp = [geo(q) for q in raw[:200]]
            if any(gp):
                el["geo_points"] = [q for q in gp if q]
            else:
                el["points"] = [q for q in (pt(q) for q in raw[:200]) if q]
        if e["shape"] == "map":                          # valódi térkép: hely vagy bbox [s, w, n, e]; a tűk is rajta legyenek
            mp = e.get("map") if isinstance(e.get("map"), dict) else e
            el["map"] = {k: v for k, v in {
                "place": mp.get("place").strip()[:160] if isinstance(mp.get("place"), str) and mp["place"].strip() else None,
                "bbox": [float(v) for v in mp["bbox"]] if isinstance(mp.get("bbox"), list) and len(mp["bbox"]) == 4
                and all(isinstance(v, (int, float)) for v in mp["bbox"]) else None,
                "detail": mp.get("detail") if mp.get("detail") in ("streets", "outline") else None}.items() if v is not None}
        for k in ("fill", "stroke", "color", "align", "weight"):
            if isinstance(e.get(k), str):
                el[k] = e[k][:24]
        el["anim"] = e.get("anim") if e.get("anim") in _GFX_ANIMS else "appear"
        if e["shape"] in ("text", "pin"):
            txt = _clean(e.get("text") or e.get("label") or "", 160, "Graphic text")
            if [x for x in _text_numbers(txt) if x not in nums]:
                dropped.append(txt)
                continue
            el["text"] = txt
        if e["shape"] in ("image", "lottie"):
            src = e.get("image") or e.get("lottie") or e.get("file_id") or e.get("url") or e.get("src")   # src: mentett alak
            if not src:
                warn.append(f"element {n_} ({e['shape']}): no {e['shape']} given — left out")
                continue
            el["fit"] = "cover" if e.get("fit") == "cover" else "contain"
            el["loop"] = e.get("loop") is not False
            el["src"] = src if isinstance(src, (str, dict)) else None
            if e.get("asset"):
                el["asset"] = str(e["asset"])
            if isinstance(e.get("credit"), str) and e["credit"].strip():
                el["credit"] = e["credit"].strip()[:200]
        if e["shape"] in ("path", "polygon") and len(el.get("points") or el.get("geo_points") or []) < 2:
            warn.append(f"element {n_} ({e['shape']}): fewer than 2 usable points — left out")
            continue
        if e["shape"] in ("line", "arrow") and not ((el.get("from") or el.get("geo_from")) and (el.get("to") or el.get("geo_to"))):
            warn.append(f"element {n_} ({e['shape']}): needs from and to ([x, y], {{\"place\"}} or {{\"lat\", \"lon\"}}) — left out")
            continue
        if e["shape"] == "map" and not (el.get("map") or {}).get("place") and not (el.get("map") or {}).get("bbox"):
            pass                                          # hely nélkül a tűk köré rajzol (geo.build) — ha nincs tű, ott jelez
        out.append(el)
    title = _clean(c.get("title"), 60, "Chart title")
    warns = [f"⚠ Graphic “{title or 'custom'}”: {w}." for w in warn[:8]]
    if dropped:
        warns.append(f"⚠ Graphic “{title or 'custom'}”: left out {len(dropped)} text(s) with a number not in the source, "
                     f"e.g. “{dropped[0][:60]}”.")
    if not out:
        return None, [f"⚠ A suggested graphic “{title}” had nothing to draw."] + warns if c.get("elements") else None
    g = {"type": "graphic", "title": title, "unit": "", "items": [], "elements": out}
    if num(c.get("backdrop")) is not None:
        g["backdrop"] = max(0.0, min(1.0, float(c["backdrop"])))
    note = f"Graphic “{title or 'custom'}”: {len(out)} elements" + (f" ({expanded} pins/routes taken from inside the map)" if expanded else "") + "."
    return g, [note] + warns


def _script_from_plan(c: dict[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Az ügynök saját EffectCraft-szkriptje egy jelenetre (After Effects-szerű JS: comp.layers.addShape…). ZÁRKÁBAN fut
    (jail.py) — a motor saját kapuja mellett. A számait a build után a jelenet szövegrétegeiből ellenőrizzük."""
    code = c.get("code") if isinstance(c.get("code"), str) else ""
    title = _clean(c.get("title"), 60, "Chart title")
    if not code.strip():
        return None, f"A suggested script scene “{title}” had no code."
    if len(code) > 20000:
        return None, f"A suggested script scene “{title}” is too long ({len(code)} characters; 20 000 at most)."
    g = {"type": "script", "title": title, "unit": "", "items": [], "code": code}
    if isinstance(c.get("backdrop"), (int, float)):
        g["backdrop"] = max(0.0, min(1.0, float(c["backdrop"])))
    return g, f"Script scene “{title or 'custom'}”: {code.count(chr(10)) + 1} lines, run in the sandbox."


_CHART_TYPES = ("column", "bar", "line", "area", "donut", "ring", "icons", "timeline", "steps",
                "counter", "compare", "progress", "gauge", "funnel", "proscons", "quote", "graphic", "script")


def _chart_from_plan(c: Any, source: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """A modell javasolta ábra → ellenőrzött ábra | None + megjegyzés. Szabály minden típusra: szám és dátum CSAK a
    forrásból (számos ábra: minden érték; ikonrács: az „N-ből" is; idővonal: a dátum szó szerint; lépések: a szövegük
    számai) — különben az ábra kimarad. A modell nem talál ki számot."""
    if not isinstance(c, dict):
        return None, None
    typ = str(c.get("type") or "column").lower()
    if typ not in _CHART_TYPES:
        return None, None
    if typ == "graphic":
        return _graphic_from_plan(c, source)
    if typ == "script":
        return _script_from_plan(c)
    title = _clean(c.get("title"), 60, "Chart title")
    nums = _text_numbers(source)
    src = re.sub(r"\s+", " ", source or "").lower()
    bad = lambda t: [x for x in _text_numbers(t) if x not in nums]
    left_out = f"A suggested chart “{title}” was left out: not all its numbers are in the source."
    raw = [it for it in c.get("items") or [] if isinstance(it, dict)]
    if typ == "quote":                 # idézet: CSAK szó szerint a forrásból (kitalált idézet nincs)
        it = raw[0] if raw else {}
        q = _clean(it.get("text") or it.get("label"), 220, "Quote")
        norm = lambda x: re.sub(r"[^\w ]+", "", re.sub(r"\s+", " ", x or "")).lower().strip()
        if not q or norm(q.rstrip("…")) not in norm(source):
            return None, f"A suggested quote “{q[:60]}” was left out: it is not word for word in the source."
        who = _clean(it.get("label") if it.get("text") else "", 40, "Quote author")
        return {"type": typ, "title": title, "unit": "", "items": [{"text": q, "label": who}]}, \
            f"Quote “{q[:50]}…”: word for word in the source."
    if typ == "proscons":
        items = []
        for it in raw[:8]:
            txt = _clean(it.get("text"), 70, "Point")
            if bad(txt):
                return None, left_out
            side = "+" if str(it.get("label") or "").strip().lower() in ("+", "pro", "for", "mellette", "előny", "yes") else "-"
            if txt:
                items.append({"label": side, "text": txt})
        if len(items) < 2:
            return None, None
        heads = [h for h in (c.get("labels") or []) if isinstance(h, str)][:2]
        return ({"type": typ, "title": title, "unit": "", "items": items, **({"labels": [_clean(h, 18) for h in heads]} if heads else {})},
                f"For/against “{title}”: {len(items)} points, no number that isn't in the source.")
    if typ in ("timeline", "steps"):
        items = []
        for it in raw[:6]:
            lab, txt = _clean(it.get("label"), 26 if typ == "timeline" else 30, "Chart item"), _clean(it.get("text"), 70, "Chart text")
            if typ == "timeline" and re.sub(r"\s+", " ", lab).lower() not in src:
                return None, f"A suggested timeline “{title}” was left out: the date “{lab}” is not in the source."
            if bad(txt) or (typ == "steps" and bad(lab)):
                return None, left_out
            if lab:
                items.append({"label": lab, "text": txt})
        if len(items) < (2 if typ == "timeline" else 3):
            return None, None
        return ({"type": typ, "title": title, "unit": "", "items": items},
                f"{'Timeline' if typ == 'timeline' else 'Steps'} “{title}”: {len(items)} items"
                + (", every date is in the source text." if typ == "timeline" else ", no number that isn't in the source."))
    items, missing = [], []
    for it in raw:
        try:
            v = float(str(it.get("value")).replace(",", ".").replace(" ", ""))
        except ValueError:
            continue
        (items if round(v, 6) in nums else missing).append({"label": _clean(it.get("label"), 22 if typ not in ("ring", "icons") else 60, "Chart label"), "value": v})
    unit = _clean(c.get("unit"), 14, "Chart unit")   # „millió" is beférjen (10-12: „1,1 milli…”)
    out: dict[str, Any] = {"type": typ, "title": title, "unit": unit}
    if typ in ("ring", "icons", "counter", "gauge"):
        if missing or not items:
            return None, left_out
        out["items"] = items[:1]
        if typ == "ring" and not (0 <= items[0]["value"] <= 100):
            return None, None
        if typ == "counter":
            return out, f"Counter “{title}”: the number is in the source text."
        if typ == "gauge":            # a skála vége is a forrásból (a 0–100 % alapértelmezés kivétel)
            for key in ("min", "max"):
                if c.get(key) not in (None, ""):
                    try:
                        val = float(str(c[key]).replace(",", "."))
                    except ValueError:
                        return None, None
                    if round(val, 6) not in nums and not (val in (0, 100) and (unit == "%" or key == "min")):
                        return None, left_out
                    out[key] = val
            lo_, hi_ = out.get("min", 0), out.get("max", 100)
            if not lo_ <= items[0]["value"] <= hi_:
                return None, None
            return out, f"Gauge “{title}”: the value (and its scale) are in the source text."
        if typ == "icons":
            tot = c.get("total")
            if tot not in (None, "", 0):
                try:
                    tot = float(str(tot).replace(",", "."))
                except ValueError:
                    return None, None
                if round(tot, 6) not in nums and not (unit == "%" and tot == 100):
                    return None, left_out
                out["total"] = tot
            elif unit != "%":
                return None, None
        return out, f"{'Ring' if typ == 'ring' else 'Icons'} “{title}”: the number is in the source text."
    checked, rest = len(items), ""
    # fánk: a „többiek" sora (100% − a többi) KISZÁMOLT érték, nem kitalált — a modell címkéjével marad
    # (10-12: „48% telefon, 22% olvas, 18% tévé, a többiek…" — a 12% nincs a szövegben, a fánk emiatt kiesett)
    if typ == "donut" and unit == "%" and len(missing) == 1 and abs(missing[0]["value"] - (100 - sum(i["value"] for i in items))) < 0.01:
        items.append(missing.pop())
        rest = f" “{items[-1]['label']}” is the rest of 100%."
    if missing or len(items) < 2:
        return None, left_out
    if typ == "compare":
        items = items[:3]
    if typ == "progress" and not all(0 <= i["value"] <= 100 for i in items):
        return None, None
    if typ == "funnel" and len(items) < 3:
        return None, None
    if typ == "area" and len(items) < 3:
        typ = out["type"] = "column"
    if typ == "donut":
        tot = sum(i["value"] for i in items)
        if any(i["value"] < 0 for i in items) or len(items) > 6:
            return None, None
        if unit == "%" and 60 <= tot < 99.5 and not rest:   # a hiányzó rész „Egyéb" — kiszámolt, nem kitalált
            from app.tools.infographic import _HU
            items.append({"label": "Egyéb" if _HU.search(source or "") else "Other", "value": round(100 - tot, 6)})
            rest = f" “{items[-1]['label']}” is the rest of 100%."
    if typ == "column" and (len(items) > 7 or max(len(i["label"]) for i in items) > 14):
        typ = out["type"] = "bar"
    out["items"] = items[:12]
    return out, f"Chart “{title}”: all {checked} values are in the source text.{rest}"


def charts_from_table(file_id: str, limit: int = 2) -> list[dict[str, Any]]:
    """Táblázatból (CSV/XLSX) grafikon-jelenetek: az értékek a cellákból (az infografika táblázat-kezelésével —
    a „mutató × ország/év" tábla megfordítva), oszloponként egy grafikon."""
    from app.tools import infographic as ig
    tb = ig.load_table(crafts.existing(file_id))
    if ig.indicator_table(tb):
        tb = ig.transpose(tb)
    cols = ig.numeric_columns(tb)
    lab = ig.label_column(tb, cols)
    cols = [c for c in cols if c.j != lab]          # az évszám-oszlop címke, nem adat (10-12: „Year” grafikon lett)
    labels = tb.col(lab) if lab is not None else [str(i + 1) for i in range(len(tb.rows))]
    timey = ig.time_like(labels)
    out = []
    for c in cols[:limit]:
        items = [{"label": _clean(labels[i], 22), "value": v} for i, v in enumerate(c.values) if v is not None][:12]
        if len(items) < 2:
            continue
        tot = sum(x["value"] for x in items)
        share = c.percent and 95 <= tot <= 105 and 2 <= len(items) <= 6 and all(x["value"] >= 0 for x in items)
        typ = "donut" if share and not timey else "line" if timey and len(items) >= 3 else (
            "bar" if len(items) > 7 or max(len(x["label"]) for x in items) > 14 else "column")
        unit = "%" if c.percent else ""
        out.append({"type": typ, "title": _clean(re.sub(r"\s*\(%\)|\s*%", "", c.name), 60), "unit": unit, "items": items,
                    "cells": f"{tb.refs[0][c.j]}:{tb.refs[-1][c.j]}"})
    return out


# egy gyártás / szerkesztés alatt: mely szövegek rövidültek (a check jelzi — 10-12: „millió” → „milli…” észrevétlenül)
_TRUNC: "contextvars.ContextVar[Optional[list[str]]]" = contextvars.ContextVar("video_trunc", default=None)


def _clean(s: Any, n: int, what: Optional[str] = None) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    if len(s) <= n:
        return s
    out = s[:n - 1].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"
    notes = _TRUNC.get()
    if what and notes is not None:
        notes.append(f"{what} “{s[:70]}{'…' if len(s) > 70 else ''}” was shortened to “{out}” (limit {n} characters)")
    return out


# ábraszövegek, amelyek a dobozukba csak kisebb betűvel fértek be (a check megemlíti)
_FIT: "contextvars.ContextVar[Optional[list[str]]]" = contextvars.ContextVar("video_fit", default=None)


def _fit_size(st: str, size: float, box_w: float, box_h: float, name: str = "") -> int:
    """Elfér-e a szöveg a dobozában (szótördeléssel, ~0,56 em átlagos betűszélesség, 1,22 sormagasság)? Ha nem,
    kisebb betű — legfeljebb az eredeti 55%-áig; az illesztést feljegyzi."""
    s0, size = int(size), float(size)
    words = str(st or "").split() or [""]
    while size > s0 * 0.55:
        cw, lines, cur, wide = 0.56 * size, 1, 0.0, False
        for w in words:
            ww = len(w) * cw
            wide = wide or ww > box_w
            nxt = ww if cur == 0 else cur + cw + ww
            if nxt > box_w and cur > 0:
                lines, cur = lines + 1, ww
            else:
                cur = nxt
        if not wide and lines * size * 1.22 <= box_h * 1.05:
            break
        size *= 0.93
    out = int(round(size))
    notes = _FIT.get()
    if out < s0 and notes is not None:
        notes.append(f"{name or 'text'} {s0}→{out} px")
    return out


def _with_ids(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stabil jelenetazonosítók (s1, s2…): beszúrás/törlés után is ugyanarra a jelenetre mutatnak."""
    used = {str(sc.get("id")) for sc in scenes if sc.get("id")}
    k = max([int(m.group(1)) for u in used if (m := re.fullmatch(r"s(\d+)", u))] or [0])
    for sc in scenes:
        if not sc.get("id") or str(sc["id"]) in ("title", "end"):
            k += 1
            while f"s{k}" in used:
                k += 1
            sc["id"] = f"s{k}"
            used.add(sc["id"])
    return scenes


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


def _decimals(v: float) -> int:
    return 0 if float(v).is_integer() else 1 if round(v, 1) == round(v, 6) else 2


def _fmt_fixed(x: float, dec: int, unit: str, lang: str) -> str:
    """Számláló köztes értéke ugyanannyi tizedessel, mint a végső (ne ugráljon a szélessége)."""
    num = f"{abs(x):,.{dec}f}"
    num = num.replace(",", " ").replace(".", ",") if lang == "hu" else num
    t = ("-" if x < 0 else "") + num
    if unit == "%":
        return t + (" %" if lang == "hu" else "%")
    if not unit:
        return t
    return f"{unit}{t}" if unit in ("$", "€", "£", "¥") and lang == "en" else f"{t} {unit}"


def _fmt_val(v: float, unit: str, lang: str) -> str:
    """Értékcímke a forrás pontosságával (3,35 % marad 3,35 — az infografika fmt-je egy tizedesre kerekítene)."""
    from app.tools.infographic import fmt
    pct = unit == "%"
    t = fmt(v, lang, percent=pct)
    if round(v, 1) != round(v, 2):
        num = f"{abs(v):,.2f}"
        num = num.replace(",", " ").replace(".", ",") if lang == "hu" else num
        t = ("-" if v < 0 else "") + num + ((" %" if lang == "hu" else "%") if pct else "")
    if pct or not unit:
        return t
    return f"{unit}{t}" if unit in ("$", "€", "£", "¥") and lang == "en" else f"{t} {unit}"


def _palette(acc: list[float]) -> list[list[float]]:
    """Szeletszínek az akcentusból: akcentus, világos, sötétebb akcentus, szürke, halvány akcentus, középszürke."""
    mix = lambda a, b, t: [round(a[i] * (1 - t) + b[i] * t, 4) for i in range(3)]
    return [acc, [0.93, 0.94, 0.96], mix(acc, [0, 0, 0], 0.45), [0.55, 0.58, 0.64], mix(acc, [1, 1, 1], 0.5), [0.35, 0.37, 0.42]]


def _arc(out: list, name: str, cx: float, cy: float, r: float, th: float, a0: float, a1: float, color: Any,
         t0: float, dur: float, opacity: int = 100) -> None:
    """Körív apró, elforgatott, lekerekített szakaszokból, amelyek egymás után tűnnek fel (rajzolás-hatás).
    Szögek fokban, 0 = fent, az óramutató járásával."""
    span = a1 - a0
    k = max(3, math.ceil(abs(span) / 5))
    for i in range(k):
        a = math.radians(a0 + span * (i + 0.5) / k - 90)
        chord = 2 * r * math.sin(math.radians(abs(span) / k) / 2)
        out.append({"k": "rect", "name": f"{name} {i + 1}", "size": [round(chord * 1.18 + th * 0.2, 1), round(th, 1)],
                    "pos": [round(cx + r * math.cos(a), 1), round(cy + r * math.sin(a), 1)], "rot": round(math.degrees(a) + 90, 2),
                    "color": color, "opacity": opacity, "round": round(th * 0.25, 1), "grow": "fade",
                    "t0": round(t0 + dur * i / k, 3), "dur": 0.08})


def _gfx_color(v: Any, acc: list[float], default: Any = "accent") -> Any:
    if isinstance(v, (list, tuple)) and len(v) >= 3 and all(isinstance(c, (int, float)) for c in v[:3]):
        return [max(0.0, min(1.0, float(c))) for c in v[:3]]          # [r, g, b] 0–1 (a térkép saját színei)
    v = v or default
    named = {"accent": "accent", "white": [1, 1, 1], "muted": [0.62, 0.65, 0.72], "dark": [0.06, 0.06, 0.08],
             "black": [0, 0, 0], "red": [0.9, 0.2, 0.2], "green": [0.2, 0.7, 0.35], "blue": [0.2, 0.45, 0.9],
             "yellow": [0.95, 0.78, 0.2], "none": None}
    if isinstance(v, str) and v.lower() in named:
        return named[v.lower()]
    if isinstance(v, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", v):
        return _rgb(v if v.startswith("#") else "#" + v)
    return named.get(str(default), "accent")


def _pin_sides(els: list[tuple[int, dict[str, Any]]], W: int, H: int, m: float, k: float) -> dict[int, str]:
    """Tűfeliratok helye: jobbra | balra | fölötte | alatta — ahol a legkevésbé fed más feliratot, tűt, és kint sem lóg
    (10-12: álló országtérképen a „Budapest” a „Debrecen” tűbe futott)."""
    pins = [(i, e) for i, e in els if e.get("shape") == "pin" and e.get("text") and not e.get("_skip")]
    taken = []                                         # (x0, y0, x1, y1): a tűk és a már lerakott feliratok
    for _, e in pins:
        r = e.get("r", 0.03) * m
        x, y = e.get("x", 0.5) * W, e.get("y", 0.5) * H
        taken.append((x - r, y - r, x + r, y + r))
    def overlap(a: tuple, b: tuple) -> float:
        return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    out: dict[int, str] = {}
    for i, e in pins:
        ts = e.get("size", 34) * k
        w, h = min(W * 0.3, 0.56 * ts * len(e["text"]) + 4), ts * 1.25
        r = e.get("r", 0.03) * m
        x, y = e.get("x", 0.5) * W, e.get("y", 0.5) * H
        own = (x - r, y - r, x + r, y + r)
        cands = {"right": (x + r + 6, y - h / 2, x + r + 6 + w, y + h / 2), "left": (x - r - 6 - w, y - h / 2, x - r - 6, y + h / 2),
                 "above": (x - w / 2, y - r - 8 - h, x + w / 2, y - r - 8), "below": (x - w / 2, y + r + 8, x + w / 2, y + r + 8 + h)}
        def cost(bx: tuple) -> float:
            out_ = max(0.0, -bx[0]) + max(0.0, bx[2] - W) + max(0.0, -bx[1]) + max(0.0, bx[3] - H)
            return sum(overlap(bx, t) for t in taken if t != own) + out_ * h * 4
        best = min(("right", "left", "above", "below"), key=lambda sd: (cost(cands[sd]), ("right", "left", "above", "below").index(sd)))
        out[i] = best
        taken.append(cands[best])
    return out


def _graphic_elements(ch: dict[str, Any], W: int, H: int, acc: list[float]) -> list[dict[str, Any]]:
    """A „graphic” elemei builder-rétegekké: arányos koordináták → pixel; méretek 1080-as rövid oldalra értve."""
    m = min(W, H)
    k = m / 1080
    px = lambda q: [round(q[0] * W, 1), round(q[1] * H, 1)]
    out: list[dict[str, Any]] = []
    els = list(enumerate(ch.get("elements") or []))
    sides = _pin_sides(els, W, H, m, k)
    for i, e in sorted(els, key=lambda ie: ie[1].get("shape") != "map"):     # a térkép legalul: ne takarja a tűket
        if e.get("_skip"):
            continue
        sh, name = e["shape"], f"{e['shape'].capitalize()} {i + 1}"
        t0, dur = max(0.0, e.get("at", 0.3 + 0.25 * i)), max(0.1, e.get("dur", 0.6))
        anim = {"anim": e.get("anim", "appear"), "t0": round(t0, 3), "dur": round(dur, 3)}
        if e.get("move_to"):
            anim["move_to"] = px(e["move_to"])
        op = int(max(5, min(100, e.get("opacity", 100))))
        if sh in ("rect", "circle", "pin"):
            if sh == "rect":
                size = [e.get("w", 0.2) * W, e.get("h", 0.1) * H]
                rnd = e.get("radius", 0) * k
            else:
                d = 2 * e.get("r", 0.03 if sh == "pin" else 0.06) * m
                size, rnd = [d, d], d / 2
            el = {"k": "rect", "name": name, "size": [round(max(size[0], 2), 1), round(max(size[1], 2), 1)],
                  "pos": px([e.get("x", 0.5), e.get("y", 0.5)]), "color": _gfx_color(e.get("fill"), acc, "accent" if sh != "rect" or not e.get("stroke") else "none"),
                  "opacity": op, "round": round(rnd, 1), **anim}
            if e.get("stroke"):
                el["stroke"], el["stroke_w"] = _gfx_color(e["stroke"], acc), round(e.get("width", 4) * k, 1)
            out.append(el)
            if sh == "pin" and e.get("text"):
                ts = round(e.get("size", 34) * k)
                side = sides.get(i, "right")
                px_, py_, r_ = e.get("x", 0.5) * W, e.get("y", 0.5) * H, size[0] / 2
                cx_ = {"right": px_ + r_ + 6 + W * 0.15, "left": px_ - r_ - 6 - W * 0.15}.get(side, px_)
                cy_ = {"above": py_ - r_ - 8 - ts * 0.6, "below": py_ + r_ + 8 + ts * 0.6}.get(side, py_)
                out.append({"k": "text", "name": f"Pin label {i + 1}", "str": e["text"], "size": ts, "box": [round(W * 0.3, 1), round(ts * 2.8, 1)],
                            "pos": [round(cx_, 1), round(cy_ + ts * 0.65, 1)],
                            "align": {"right": "left", "left": "right"}.get(side, "center"), "font": "semibold", "color": _gfx_color(e.get("color"), acc, "white"),
                            "anim": "appear", "t0": round(t0 + 0.2, 3), "dur": 0.4})
        elif sh in ("line", "arrow", "path", "polygon"):
            pts = [e["from"], e["to"]] if sh in ("line", "arrow") else e["points"]
            el = {"k": "path", "name": name, "points": [px(q) for q in pts], "closed": sh == "polygon",
                  "fill": _gfx_color(e.get("fill"), acc, "accent" if sh == "polygon" else "none"),
                  "stroke": _gfx_color(e.get("stroke"), acc, "none" if sh == "polygon" and e.get("fill") else "accent"),
                  "stroke_w": round(e.get("width", 8) * k, 1), "opacity": op, **anim}
            if sh == "arrow":           # nyílhegy: kis háromszög a végpontnál, a vonal irányába fordítva
                (ax, ay), (bx, by) = px(pts[0]), px(pts[1])
                ang = math.atan2(by - ay, bx - ax)
                L = max(18, e.get("width", 8) * k * 3.2)
                tip = [bx, by]
                l = [bx - L * math.cos(ang) + L * 0.6 * math.sin(ang), by - L * math.sin(ang) - L * 0.6 * math.cos(ang)]
                r_ = [bx - L * math.cos(ang) - L * 0.6 * math.sin(ang), by - L * math.sin(ang) + L * 0.6 * math.cos(ang)]
                out.append(el)
                out.append({"k": "path", "name": f"Arrowhead {i + 1}", "points": [[round(v, 1) for v in tip], [round(v, 1) for v in l], [round(v, 1) for v in r_]],
                            "closed": True, "fill": el["stroke"] if el["stroke"] else "accent", "stroke": None, "stroke_w": 0, "opacity": op,
                            "anim": "appear", "t0": round(t0 + dur * 0.85, 3), "dur": 0.2})
                continue
            out.append(el)
        elif sh == "text":
            ts = round(e.get("size", 44) * k)
            bw = e.get("w", 0.6) * W
            st = e.get("text") or ""
            fs = _fit_size(st, ts, bw, ts * 3.6, name)
            out.append({"k": "text", "name": name, "str": st, "size": fs, "box": [round(bw, 1), round(fs * 3.6, 1)],
                        "pos": [round(e.get("x", 0.5) * W, 1), round(e.get("y", 0.5) * H + fs * 1.15, 1)],
                        "align": e.get("align") if e.get("align") in ("left", "center", "right") else "center",
                        "font": {"bold": "bold", "regular": "regular"}.get(e.get("weight") or "", "semibold"),
                        "color": _gfx_color(e.get("color"), acc, "white"), **anim})
        elif sh == "map":                # valódi térkép: rétegcsoportok (utak, folyók, országok), a forrás feltüntetve
            if not e.get("_groups"):
                continue
            t0 = max(0.0, e.get("at", 0.1))                  # a térkép a talaj: azonnal indul, nem a sorszáma szerint
            anim = {**anim, "t0": round(t0, 3)}
            pxs = lambda g: [[[round(x * W, 1), round(y * H, 1)] for x, y in path] for path in g["paths"]]
            how = e.get("anim") if e.get("anim") in ("draw", "appear", "none") else "draw"
            # két réteg: a területek (országok, parkok, víz) beúsznak; a vonalak (utak, folyók, határok) kirajzolódnak —
            # a kitöltött sokszögre tett Trim Paths szilánkos foltokat rajzol
            areas = [{"name": g["name"], "closed": True, "fill": _gfx_color(g["fill"], acc), "stroke": None, "stroke_w": 0,
                      "paths": pxs(g)} for g in e["_groups"] if g["closed"] and g.get("fill")]
            lines = [{"name": g["name"] + (" (borders)" if g["closed"] else ""), "closed": g["closed"], "fill": None,
                      "stroke": _gfx_color(g["stroke"], acc), "stroke_w": round(g["width"], 1), "paths": pxs(g)}
                     for g in e["_groups"] if g.get("stroke")]
            if areas:
                out.append({"k": "multipath", "name": "Map areas", "groups": areas, "opacity": op,
                            **{**anim, "anim": "appear" if how == "draw" else how, "dur": 0.8}})
            if lines:
                out.append({"k": "multipath", "name": "Map lines", "groups": lines, "opacity": op,
                            **{**anim, "anim": how, "dur": round(max(0.6, e.get("dur", 1.6)), 3)}})
            bx, by = (e.get("x", 0.5) + e.get("w", 1.0) / 2) * W, (e.get("y", 0.5) + e.get("h", 1.0) / 2) * H
            fs = round(max(14, 17 * k))
            out.append({"k": "text", "name": "Map credit", "str": "OpenFreeMap © OpenMapTiles © OpenStreetMap contributors" if "OpenMapTiles" in (e.get("_credit") or "")
                        else "© OpenStreetMap contributors" if "OpenStreetMap" in (e.get("_credit") or "") else "Natural Earth", "size": fs, "box": [round(W * 0.5, 1), round(fs * 1.6, 1)],
                        "pos": [round(min(bx, W) - W * 0.25 - 12 * k, 1), round(min(by, H) - fs * 0.9 - 8 * k, 1)], "align": "right",
                        "font": "regular", "color": [0.62, 0.65, 0.72], "anim": "appear", "t0": round(t0, 3), "dur": 0.4})
        elif sh in ("image", "lottie"):
            if not e.get("file"):
                continue
            out.append({"k": sh, "name": name, "file": e["file"], "comp": e.get("comp"), "fit": e.get("fit", "contain"),
                        "loop": e.get("loop", True), "pos": px([e.get("x", 0.5), e.get("y", 0.5)]),
                        "size": [round(e.get("w", 0.3) * W, 1), round(e.get("h", 0.3) * H, 1)], "opacity": op, **anim})
    return out


def chart_elements(ch: dict[str, Any], W: int, H: int, lang: str, accent: Optional[list[float]] = None) -> list[dict[str, Any]]:
    """Egy mozgó grafikon rétegei (a builder csak lerakja): oszlop / sáv / vonal, értékcímkék, kategóriacímkék,
    alapvonal. Idő: az elemek egymás után nőnek ki (t0, dur). A felirat-sáv (alul) és a cím (fent) helye szabad."""
    if ch["type"] == "script":           # a jelenetet az ügynök szkriptje rajzolja (builder: runScript)
        return []
    m, vertical = min(W, H), H > W * 1.2
    items = ch["items"]
    n, vals = len(items), [float(i["value"]) if i.get("value") is not None else 0.0 for i in items]
    unit, typ = ch.get("unit") or "", ch["type"]
    x0, x1 = W * 0.09, W * 0.91
    yT, yB = H * (0.2 if vertical else 0.22), H * (0.66 if vertical else 0.7)
    vl, cl = round(m * 0.034), round(m * (0.031 if not vertical else 0.033))
    soft = [0.86, 0.88, 0.92]
    out: list[dict[str, Any]] = []
    rect = lambda name, size, pos, **o: out.append({"k": "rect", "name": name, "size": [round(size[0], 1), round(max(size[1], 2), 1)],
                                                     "pos": [round(pos[0], 1), round(pos[1], 1)], **o})
    text = lambda name, st, size, box, pos, **o: out.append({"k": "text", "name": name, "str": st,
                                                             "size": _fit_size(st, size, box[0], box[1], name),
                                                             "box": [round(box[0], 1), round(box[1], 1)], "pos": [round(pos[0], 1), round(pos[1], 1)], **o})
    stagger, grow = min(0.18, 1.6 / max(n, 1)), 0.7
    if typ == "column":
        vmin, vmax = min(0.0, *vals), max(0.0, *vals)
        top = yT + vl * 1.8
        bottom = yB - (vl * 1.8 if vmin < 0 else 0)
        sc = (bottom - top) / ((vmax - vmin) or 1)
        y0 = top + vmax * sc
        slot = (x1 - x0) / n
        bw = min(slot * 0.6, m * 0.17)
        cat_y = yB + cl * 1.4
        rect("Baseline", (x1 - x0, max(2, m * 0.003)), ((x0 + x1) / 2, y0), color=[1, 1, 1], opacity=45, t0=0.1, dur=0.4, grow="right")
        for k, (it, v) in enumerate(zip(items, vals)):
            cx, h, t0 = x0 + slot * (k + 0.5), abs(v) * sc, 0.35 + k * stagger
            rect(f"Bar – {it['label']}", (bw, h), (cx, y0 - h / 2 if v >= 0 else y0 + h / 2), color="accent", opacity=100,
                 round=min(bw * 0.08, 8), grow="up" if v >= 0 else "down", t0=t0, dur=grow)
            text(f"Value – {it['label']}", _fmt_val(v, unit, lang), vl, (slot, vl * 1.4),
                 (cx, (y0 - h - vl * 0.75) if v >= 0 else (y0 + h + vl * 0.95)), align="center", font="bold", t0=t0 + grow * 0.8)
            text(f"Label – {it['label']}", it["label"], cl, (slot * 0.96, cl * 2.7), (cx, cat_y + cl * 0.6), align="center",
                 font="semibold", color=soft, t0=0.2)
    elif typ == "bar":
        vmin, vmax = min(0.0, *vals), max(0.0, *vals)
        lab_w = (x1 - x0) * (0.34 if not vertical else 0.36)
        px0, px1 = x0 + lab_w + m * 0.02, x1 - vl * 4.2
        sc = (px1 - px0) / ((vmax - vmin) or 1)
        xz = px0 - vmin * sc
        row = (yB - yT) / n
        bh = min(row * 0.62, m * 0.075)
        rect("Baseline", (max(2, m * 0.003), yB - yT), (xz, (yT + yB) / 2), color=[1, 1, 1], opacity=45, t0=0.1, dur=0.4, grow="down")
        for k, (it, v) in enumerate(zip(items, vals)):
            cy, w, t0 = yT + row * (k + 0.5), abs(v) * sc, 0.35 + k * stagger
            rect(f"Bar – {it['label']}", (w, bh), (xz + w / 2 if v >= 0 else xz - w / 2, cy), color="accent", opacity=100,
                 round=min(bh * 0.12, 8), grow="right" if v >= 0 else "left", t0=t0, dur=grow)
            vx = xz + w + vl * 2.1 if v >= 0 else xz - w - vl * 2.1
            text(f"Value – {it['label']}", _fmt_val(v, unit, lang), vl, (vl * 4, vl * 1.4), (vx, cy + vl * 0.1),
                 align="center", font="bold", t0=t0 + grow * 0.8)
            text(f"Label – {it['label']}", it["label"], cl, (lab_w, cl * 1.4), (x0 + lab_w / 2, cy + cl * 0.1), align="right",
                 font="semibold", color=soft, t0=0.2)
    elif typ in ("donut", "ring"):
        pal = _palette(accent or _rgb(None))
        r = m * (0.2 if not vertical else 0.24)
        th = r * (0.32 if typ == "donut" else 0.22)
        cx = W * (0.32 if not vertical and typ == "donut" else 0.5)
        cy = (yT + yB) / 2 if not vertical or typ == "ring" else yT + r + th
        _arc(out, "Track", cx, cy, r, th, 0, 360, [1, 1, 1], 0.1, 0.3, opacity=14)
        if typ == "ring":
            v = max(0.0, min(100.0, vals[0]))
            _arc(out, "Ring", cx, cy, r, th, 0, 360 * v / 100, "accent", 0.4, 1.6)
            big = round(r * 0.62)
            text("Ring value", _fmt_val(vals[0], unit or "%", lang), big, (r * 1.9, big * 1.3), (cx, cy + big * 0.05),
                 align="center", font="bold", t0=0.5)
            text("Ring label", items[0]["label"], round(m * 0.036), (W * 0.8, m * 0.12), (cx, cy + r + th + m * 0.07),
                 align="center", font="semibold", color=soft, t0=0.9)
        else:
            tot = sum(max(v, 0) for v in vals) or 1
            a, leg_x = 0.0, W * 0.58
            leg_y0 = cy - (n - 1) * m * 0.045
            for k, (it, v) in enumerate(zip(items, vals)):
                sweep = 360 * max(v, 0) / tot
                col = pal[k % len(pal)]
                _arc(out, f"Slice {k + 1} – {it['label']}", cx, cy, r, th, a, a + sweep, col, 0.35 + 1.4 * a / 360, 1.4 * sweep / 360)
                a += sweep
                ly = (leg_y0 + k * m * 0.09) if not vertical else (cy + r + th + m * (0.06 + 0.075 * k))
                lx = leg_x if not vertical else W * 0.16
                t1 = 0.35 + 1.4 * (a - sweep / 2) / 360
                rect(f"Key {k + 1}", (m * 0.03, m * 0.03), (lx, ly), color=col, opacity=100, round=m * 0.006, grow="pop", t0=t1, dur=0.25)
                text(f"Key label {k + 1}", f"{it['label']}  ·  {_fmt_val(v, unit, lang)}", round(m * 0.034),
                     (W * (0.36 if not vertical else 0.72), m * 0.06), (lx + m * 0.03 + W * (0.18 if not vertical else 0.36), ly + m * 0.004),
                     align="left", font="semibold", t0=t1)
    elif typ == "icons":
        part, total = vals[0], float(ch.get("total") or (100 if unit == "%" else 0))
        if total <= 0 or part < 0 or part > total:
            return out
        cells = int(total) if total <= 20 and float(total).is_integer() else 100
        on = round(part / total * cells)
        cols = 10 if cells == 100 else (cells if cells <= 10 else math.ceil(cells / 2))
        rows = math.ceil(cells / cols)
        area_w = (x1 - x0) * (0.62 if not vertical else 0.9)
        cell = min(area_w / cols, (yB - yT - m * 0.16) / rows)
        dot = cell * 0.72
        gx0 = W / 2 - cell * cols / 2 + cell / 2
        gy0 = yT + m * 0.02 + cell / 2
        for k in range(cells):
            gx, gy = gx0 + (k % cols) * cell, gy0 + (k // cols) * cell
            hot = k < on
            rect(f"Dot {k + 1}", (dot, dot), (gx, gy), color="accent" if hot else [1, 1, 1], opacity=100 if hot else 22,
                 round=dot / 2, grow="pop", t0=round(0.3 + (1.6 * k / cells if hot else 0.05 * (k % cols)), 3), dur=0.22)
        say = (f"{_fmt_val(part, '', lang)} / {_fmt_val(total, '', lang)}" if cells != 100 or unit != "%" else _fmt_val(part, "%", lang))
        ty = gy0 + rows * cell + m * 0.03
        text("Icons value", say, round(m * 0.07), (W * 0.8, m * 0.1), (W / 2, ty + m * 0.03), align="center", font="bold",
             color=accent or _rgb(None), t0=1.6)
        text("Icons label", items[0]["label"], round(m * 0.034), (W * 0.84, m * 0.11), (W / 2, ty + m * 0.12), align="center",
             font="semibold", color=soft, t0=1.9)
    elif typ in ("timeline", "steps"):
        k_n = len(items)
        hor = not vertical and k_n <= 6
        th = max(4, m * 0.006)
        d = m * (0.03 if typ == "timeline" else 0.07)
        if hor:
            ly, xa, xb = (yT + yB) / 2 - m * 0.03, x0 + m * 0.03, x1 - m * 0.03
            if typ == "timeline":
                out.append({"k": "rect", "name": "Timeline", "size": [round(xb - xa, 1), round(th, 1)], "pos": [round(xa, 1), round(ly, 1)],
                            "rot": 0, "color": [1, 1, 1], "opacity": 55, "grow": "segment", "t0": 0.2, "dur": 0.9})
            slot = (xb - xa) / k_n
            for k, it in enumerate(items):
                px, t0 = xa + slot * (k + 0.5), 0.5 + k * min(0.55, 2.6 / k_n)
                rect(f"Point {k + 1}", (d, d), (px, ly), color="accent", opacity=100, round=d / 2, grow="pop", t0=t0, dur=0.3)
                if typ == "steps":
                    text(f"Step number {k + 1}", str(k + 1), round(d * 0.55), (d, d * 0.8), (px, ly + d * 0.04), align="center",
                         font="bold", t0=t0 + 0.1)
                up = typ == "timeline" and k % 2 == 1
                head_y = ly - m * 0.09 if up else ly + d / 2 + m * 0.05
                text(f"Label {k + 1}", it["label"], round(m * 0.042), (slot * 0.94, m * 0.06), (px, head_y), align="center",
                     font="bold", color=(accent or _rgb(None)) if typ == "timeline" else None, t0=t0 + 0.15)
                if it.get("text"):
                    body_y = head_y - m * 0.07 if up else head_y + m * 0.075
                    text(f"Text {k + 1}", it["text"], round(m * 0.03), (slot * 0.94, m * 0.11), (px, body_y + (0 if up else m * 0.02)),
                         align="center", font="regular", color=soft, t0=t0 + 0.3)
        else:
            lx, ya, yb = x0 + m * 0.06, yT + m * 0.02, yB + m * 0.02
            if typ == "timeline":
                out.append({"k": "rect", "name": "Timeline", "size": [round(yb - ya, 1), round(th, 1)], "pos": [round(lx, 1), round(ya, 1)],
                            "rot": 90, "color": [1, 1, 1], "opacity": 55, "grow": "segment", "t0": 0.2, "dur": 0.9})
            row = (yb - ya) / k_n
            for k, it in enumerate(items):
                py, t0 = ya + row * (k + 0.5), 0.5 + k * min(0.55, 2.6 / k_n)
                rect(f"Point {k + 1}", (d, d), (lx, py), color="accent", opacity=100, round=d / 2, grow="pop", t0=t0, dur=0.3)
                if typ == "steps":
                    text(f"Step number {k + 1}", str(k + 1), round(d * 0.55), (d, d * 0.8), (lx, py + d * 0.04), align="center",
                         font="bold", t0=t0 + 0.1)
                tx = lx + d / 2 + m * 0.04
                tw = x1 - tx
                text(f"Label {k + 1}", it["label"], round(m * 0.044), (tw, m * 0.065), (tx + tw / 2, py - (m * 0.02 if it.get("text") else 0)),
                     align="left", font="bold", color=(accent or _rgb(None)) if typ == "timeline" else None, t0=t0 + 0.15)
                if it.get("text"):
                    text(f"Text {k + 1}", it["text"], round(m * 0.032), (tw, m * 0.1), (tx + tw / 2, py + m * 0.055), align="left",
                         font="regular", color=soft, t0=t0 + 0.3)
    elif typ == "graphic":            # saját grafika: az elemek a képkocka arányában (0–1) → pixel
        out += _graphic_elements(ch, W, H, accent or _rgb(None))
    elif typ == "counter":            # felpörgő szám: 0 → érték, egymást váltó rétegekben, alatta a címke
        v, dec = vals[0], _decimals(vals[0])
        final = _fmt_val(v, unit, lang)
        big = round(min(m * 0.21, W * 0.9 / (0.6 * max(len(final), 1))))
        cy = (yT + yB) / 2 - m * 0.04
        steps, T = 26, 1.7
        for k in range(1, steps + 1):
            x = v * (1 - (1 - k / steps) ** 3)
            st = final if k == steps else _fmt_fixed(x, dec, unit, lang)
            el = {"k": "text", "name": f"Counter {k}", "str": st, "size": big, "box": [round(W * 0.94, 1), round(big * 1.35, 1)],
                  "pos": [round(W / 2, 1), round(cy, 1)], "align": "center", "font": "bold", "color": accent or _rgb(None),
                  "t0": round(0.3 + T * (k - 1) / steps, 3), "cut": True}
            if k < steps:
                el["t1"] = round(0.3 + T * k / steps, 3)
            out.append(el)
        text("Counter label", items[0]["label"], round(m * 0.042), (W * 0.84, m * 0.12), (W / 2, cy + big * 0.85),
             align="center", font="semibold", color=soft, t0=0.5)
    elif typ == "compare":            # A vs B (vs C): nagy számok, alattuk arányos oszlopok
        k_n = len(items)
        xs = [W * x for x in ((0.3, 0.7) if k_n == 2 else (0.2, 0.5, 0.8))]
        mx = max(max(vals), 1e-9)
        bw = min(m * 0.17, W * 0.22)
        big = round(min(m * 0.085, W * 0.3 / (0.6 * max(len(_fmt_val(v, unit, lang)) for v in vals))))
        top = yT + m * 0.02 - big * 0.2            # a legmagasabb oszlop fölött a szám — ennyi a hely
        rect("Baseline", (x1 - x0, max(2, m * 0.003)), ((x0 + x1) / 2, yB), color=[1, 1, 1], opacity=45, t0=0.1, dur=0.4, grow="right")
        for k, (it, v, x) in enumerate(zip(items, vals, xs)):
            h, t0 = max(2.0, max(v, 0) / mx * (yB - top - big * 1.4)), 0.35 + k * 0.35
            hot = v == mx
            rect(f"Bar – {it['label']}", (bw, h), (x, yB - h / 2), color="accent" if hot else [0.86, 0.88, 0.92],
                 opacity=100 if hot else 70, round=min(bw * 0.06, 8), grow="up", t0=t0, dur=0.8)
            text(f"Value – {it['label']}", _fmt_val(v, unit, lang), big, (W * 0.3, big * 1.3), (x, yB - h - big * 0.75),
                 align="center", font="bold", color=(accent or _rgb(None)) if hot else None, t0=t0 + 0.6)
            text(f"Label – {it['label']}", it["label"], round(m * 0.034), (W * 0.3, m * 0.1), (x, yB + m * 0.055),
                 align="center", font="semibold", color=soft, t0=0.2)
        if k_n == 2:
            text("Versus", "vs", round(m * 0.05), (m * 0.2, m * 0.08), (W / 2, (top + yB) / 2), align="center", font="bold",
                 color=soft, t0=0.3)
    elif typ == "progress":           # egymástól független százalékok: sín + kitöltés
        row = (yB - yT) / n
        bh = min(row * 0.22, m * 0.032)
        for k, (it, v) in enumerate(zip(items, vals)):
            cy, t0 = yT + row * (k + 0.55), 0.35 + k * min(0.3, 1.5 / n)
            text(f"Label – {it['label']}", it["label"], round(m * 0.036), ((x1 - x0) * 0.75, m * 0.06),
                 (x0 + (x1 - x0) * 0.375, cy - bh - m * 0.028), align="left", font="semibold", t0=0.2)
            text(f"Value – {it['label']}", _fmt_val(v, unit or "%", lang), round(m * 0.04), ((x1 - x0) * 0.25, m * 0.06),
                 (x1 - (x1 - x0) * 0.125, cy - bh - m * 0.028), align="right", font="bold", color=accent or _rgb(None), t0=t0 + 0.5)
            rect(f"Track – {it['label']}", (x1 - x0, bh), ((x0 + x1) / 2, cy), color=[1, 1, 1], opacity=16, round=bh / 2, t0=0.1, dur=0.3, grow="fade")
            w = max(bh, (x1 - x0) * max(0.0, min(100.0, v)) / 100)
            rect(f"Fill – {it['label']}", (w, bh), (x0 + w / 2, cy), color="accent", opacity=100, round=bh / 2, grow="right", t0=t0, dur=0.9)
    elif typ == "gauge":              # mérőóra: félkör-skála, kitöltés és mutató a helyére fordul
        lo_, hi_ = float(ch.get("min") or 0), float(ch.get("max") or ch.get("total") or 100)
        frac = max(0.0, min(1.0, (vals[0] - lo_) / ((hi_ - lo_) or 1)))
        r = min((x1 - x0) * 0.32, (yB - yT) * 0.78)
        th = r * 0.16
        cx, cy = W / 2, yB - m * 0.05
        _arc(out, "Gauge track", cx, cy, r, th, -90, 90, [1, 1, 1], 0.1, 0.3, opacity=16)
        _arc(out, "Gauge fill", cx, cy, r, th, -90, -90 + 180 * frac, "accent", 0.4, 1.3)
        out.append({"k": "rect", "name": "Needle", "size": [round(r * 0.86, 1), round(max(4, th * 0.28), 1)], "pos": [round(cx, 1), round(cy, 1)],
                    "rot0": -180, "rot": round(-180 + 180 * frac, 2), "color": [0.95, 0.95, 0.97], "opacity": 100,
                    "round": round(th * 0.14, 1), "grow": "rotate", "t0": 0.4, "dur": 1.3})
        rect("Hub", (th * 0.9, th * 0.9), (cx, cy), color=[0.95, 0.95, 0.97], opacity=100, round=th * 0.45, grow="pop", t0=0.3, dur=0.25)
        big = round(m * 0.09)
        text("Gauge value", _fmt_val(vals[0], unit, lang), big, (r * 2, big * 1.3), (cx, cy - r * 0.42), align="center", font="bold",
             color=accent or _rgb(None), t0=1.2)
        text("Gauge label", items[0]["label"], round(m * 0.036), (W * 0.8, m * 0.1), (cx, cy + m * 0.07), align="center",
             font="semibold", color=soft, t0=1.4)
        for nm, val, px in (("Gauge min", lo_, cx - r), ("Gauge max", hi_, cx + r)):
            text(nm, _fmt_val(val, unit, lang), round(m * 0.03), (m * 0.25, m * 0.06), (px, cy + th * 0.9 + m * 0.02), align="center",
                 font="regular", color=soft, t0=0.3)
    elif typ == "funnel":             # tölcsér: szűkülő, középről nyíló sávok
        row = (yB - yT) / n
        mx = max(max(vals), 1e-9)
        bh = row * 0.74
        for k, (it, v) in enumerate(zip(items, vals)):
            w, cy, t0 = max(bh, (x1 - x0) * 0.7 * max(v, 0) / mx), yT + row * (k + 0.5), 0.35 + k * 0.3
            col = _palette(accent or _rgb(None))[0] if k == 0 else [round(c * (1 - 0.13 * k) + 0.05 * k, 4) for c in (accent or _rgb(None))]
            rect(f"Stage – {it['label']}", (w, bh), (W / 2, cy), color=col, opacity=100, round=min(bh * 0.12, 10), grow="center", t0=t0, dur=0.7)
            text(f"Label – {it['label']}", it["label"], round(min(m * 0.036, bh * 0.42)), (w * 0.92, bh * 0.9), (W / 2, cy + m * 0.004),
                 align="center", font="bold", color=[0.06, 0.06, 0.08], t0=t0 + 0.45)
            text(f"Value – {it['label']}", _fmt_val(v, unit, lang), round(m * 0.036), ((x1 - x0) * 0.14, bh), (W / 2 + w / 2 + (x1 - x0) * 0.08, cy),
                 align="center", font="bold", color=accent or _rgb(None), t0=t0 + 0.55)
    elif typ == "proscons":           # mellette / ellene: két oszlop (állóban egymás alatt), tételenként megjelenve
        heads = ch.get("labels") or (["Mellette", "Ellene"] if lang == "hu" else ["For", "Against"])
        pros = [it for it in items if str(it.get("label")).strip() in ("+", "pro", "for", "mellette", "előny")]
        cons = [it for it in items if it not in pros]
        cols_ = [(pros, "+", "accent", heads[0]), (cons, "–", [0.6, 0.62, 0.68], heads[1] if len(heads) > 1 else "")]
        ts = 0.35
        # közös sormagasság (a két fejléc egy vonalban); a szövegdoboz a tetejétől ír — a dobozt úgy tesszük le, hogy az
        # első sor a jel közepére essen (10-12: a pontok szövege a jel fölé csúszott)
        rows_ = max(len(pros), len(cons)) + 1
        row = (yB - yT) / (rows_ * (2 if vertical else 1))
        fs, hs = round(m * 0.034), round(m * 0.045)
        line = lambda name, st, size, w, xl, yc, **o: text(name, st, size, (w, size * 2.8), (xl + w / 2, yc + size * 0.65), **o)
        for ci, (lst, mark, col, head) in enumerate(cols_):
            if vertical:
                hx, hy, colw = x0 + m * 0.02, yT + ci * (yB - yT) / 2, x1 - x0
            else:
                hx, hy, colw = x0 + ci * (x1 - x0) / 2 + m * 0.02, yT, (x1 - x0) / 2 - m * 0.04
            line(f"Heading {ci + 1}", head, hs, colw, hx, hy + row * 0.5, align="left", font="bold",
                 color=(accent or _rgb(None)) if ci == 0 else soft, t0=0.2)
            for k, it in enumerate(lst):
                cy, d = hy + row * (k + 1.5), m * 0.045
                rect(f"Mark {ci + 1}.{k + 1}", (d, d), (hx + d / 2, cy), color=col, opacity=100, round=d / 2, grow="pop", t0=ts, dur=0.25)
                text(f"Mark sign {ci + 1}.{k + 1}", mark, round(d * 0.8), (d, d * 1.1), (hx + d / 2, cy + d * 0.08), align="center",
                     font="bold", color=[0.06, 0.06, 0.08], t0=ts + 0.05)
                line(f"Point {ci + 1}.{k + 1}", it.get("text") or "", fs, colw - d * 1.6, hx + d * 1.4, cy, align="left",
                     font="semibold", t0=ts + 0.1)
                ts += 0.4
    elif typ == "quote":              # idézet: nagy idézőjel, a szöveg, alatta a szerző
        q = items[0].get("text") or ""
        size = round(m * (0.06 if len(q) < 90 else 0.05 if len(q) < 160 else 0.042))
        text("Quote mark", "“", round(m * 0.24), (m * 0.3, m * 0.26), (W / 2, yT + m * 0.06), align="center", font="bold",
             color=accent or _rgb(None), t0=0.2)
        text("Quote", q, size, (W * 0.82, (yB - yT) * 0.7), (W / 2, (yT + yB) / 2 + m * 0.04), align="center", font="semibold", t0=0.5)
        if items[0].get("label"):
            text("Quote author", "— " + str(items[0]["label"]), round(m * 0.036), (W * 0.8, m * 0.08), (W / 2, yB + m * 0.03),
                 align="center", font="regular", color=soft, t0=1.1)
    else:   # line / area
        lo, hi = min(vals), max(vals)
        pad = (hi - lo) * 0.12 or abs(hi) * 0.1 or 1
        lo, hi = lo - pad, hi + pad
        top, bottom = yT + vl * 1.8, yB
        xs = [x0 + (x1 - x0) * (0.06 + 0.88 * k / max(n - 1, 1)) for k in range(n)]
        ys = [bottom - (v - lo) / (hi - lo) * (bottom - top) for v in vals]
        rect("Baseline", (x1 - x0, max(2, m * 0.003)), ((x0 + x1) / 2, bottom), color=[1, 1, 1], opacity=45, t0=0.1, dur=0.4, grow="right")
        seg = min(0.45, 2.4 / max(n - 1, 1))
        if typ == "area":             # kitöltés a vonal alatt: keskeny, halvány oszlopok balról jobbra nőnek
            K = 48
            span = xs[-1] - xs[0]
            for j in range(K):
                xj = xs[0] + span * (j + 0.5) / K
                kk = min(n - 2, int((xj - xs[0]) / (span / max(n - 1, 1))))
                f_ = (xj - xs[kk]) / ((xs[kk + 1] - xs[kk]) or 1)
                yj = ys[kk] + (ys[kk + 1] - ys[kk]) * f_
                rect(f"Area {j + 1}", (span / K * 1.04, bottom - yj), (xj, (yj + bottom) / 2), color="accent", opacity=30, grow="up",
                     t0=round(0.35 + (n - 1) * seg * j / K, 3), dur=0.35)
        th, d = max(4, m * 0.006), max(10, m * 0.016)
        show = set(range(n)) if n <= 8 else {0, n - 1, vals.index(max(vals)), vals.index(min(vals))}
        every = 1 if n <= 8 else math.ceil(n / 8)
        for k in range(n):
            t0 = 0.35 + k * seg
            if k < n - 1:
                dx, dy = xs[k + 1] - xs[k], ys[k + 1] - ys[k]
                length = math.hypot(dx, dy)
                out.append({"k": "rect", "name": f"Line {k + 1}", "size": [round(length, 1), round(th, 1)],
                            "pos": [round(xs[k], 1), round(ys[k], 1)], "rot": round(math.degrees(math.atan2(dy, dx)), 2),
                            "color": "accent", "opacity": 100, "grow": "segment", "t0": t0, "dur": seg})
            rect(f"Point – {items[k]['label']}", (d, d), (xs[k], ys[k]), color="accent", opacity=100, round=d / 2, grow="pop", t0=t0, dur=0.25)
            if k in show:
                text(f"Value – {items[k]['label']}", _fmt_val(vals[k], unit, lang), vl, (vl * 5, vl * 1.4), (xs[k], ys[k] - vl * 1.0),
                     align="center", font="bold", t0=t0 + 0.15)
            if k % every == 0 or k == n - 1:
                text(f"Label – {items[k]['label']}", items[k]["label"], cl, ((x1 - x0) / min(n, 8), cl * 2.7),
                     (xs[k], bottom + cl * 2.0), align="center", font="semibold", color=soft, t0=0.2)
    return out


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
    # a jelenetterv (scenes) adhat fix hosszt jelenetenként; a többi a maradékon osztozik a felirat hossza szerint
    fixed = [float(s["duration"]) if isinstance(s.get("duration"), (int, float)) and s["duration"] > 0 else None for s in scenes]
    if total is None:                        # saját szöveg, hossz nélkül: olvasási tempó (~14 karakter/s)
        durs = [f or max(3.5, w / 14 + 1.0) for f, w in zip(fixed, weights)]
        total = Tt + sum(durs) + Te - X * (n + 1)
    else:
        room = max(total - Tt - Te + X * (n + 1), 3.0 * n)
        free = room - sum(f for f in fixed if f)
        fw = sum(w for f, w in zip(fixed, weights) if not f) or 1
        durs = [f or max(3.0, free * w / fw) for f, w in zip(fixed, weights)]
        loose = [i for i, f in enumerate(fixed) if not f]
        if loose:                            # a szabad jelenetek igazodnak, hogy a teljes hossz kijöjjön
            k = max(0.2, (room - sum(f for f in fixed if f)) / max(sum(durs[i] for i in loose), 1e-6))
            for i in loose:
                durs[i] = max(3.0, durs[i] * k)
        total = Tt + sum(durs) + Te - X * (n + 1)
    total = round(total, 2)
    cpl = L["capW"] / (0.53 * L["capSize"])
    max_chunk = int(cpl * 2 * 0.9)
    captions, t = [], Tt - X
    d_scenes = []
    motions = _motions(len(scenes), ken_burns)
    from app.tools.infographic import _HU
    lang = "hu" if _HU.search(" ".join([story.get("title") or ""] + [x.get("caption") or "" for x in scenes])) else "en"
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
        chart = s.get("chart")
        if chart:
            stat = None
            # cím nélküli graphic/script is a jelenet címét kapja (10-13: a headline nyomtalanul elveszett — az ábrás
            # jelenet a headline-t nem rajzolja, csak az ábra címét)
            chart = {"title": chart.get("title") or head,
                     "elements": chart_elements(chart, W, H, lang, _rgb(accent)),
                     **({"backdrop": chart["backdrop"]} if chart.get("backdrop") is not None else {}),
                     **({"code": chart["code"], "id": s.get("id") or f"s{i + 1}"} if chart["type"] == "script" else {})}
        d_scenes.append({"headline": head, "stat": stat, "chart": chart, "dur": round(d, 3), "image": images[i + 1] if i + 1 < len(images) else None,
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
    tl, t0_ = [{"id": "title", "start": 0.0, "end": round(Tt, 2)}], Tt - X
    for s_, d_ in zip(scenes, durs):
        tl.append({"id": s_.get("id") or "", "start": round(t0_, 2), "end": round(t0_ + d_, 2)})
        t0_ += d_ - X
    tl.append({"id": "end", "start": round(t0_, 2), "end": round(total, 2)})
    D = {
        "timeline": tl,
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


async def _materialize_images(specs: list[Any], fmt: str, workdir: Path,
                              parent: Optional[str] = None) -> tuple[list[Optional[str]], list[str], list[Optional[str]]]:
    """Képleírások (file_id / {url} / {stock} / {generate}, vagy {"asset": név} = a szülőprojekt meglévő képe, letöltés
    nélkül) → fájlnevek a munkamappában + forrásmegjelölések (egyedi) + jelenetenkénti kredit."""
    from app.tools import images as imgs
    W, H = FORMATS[fmt]
    names: list[Optional[str]] = []
    credits: list[str] = []
    per: list[Optional[str]] = []
    parent_assets = assets_of(parent) if parent else {}

    async def one(i: int, spec: Any) -> tuple[Optional[str], Optional[str]]:
        if not spec:
            return None, None
        if isinstance(spec, dict) and spec.get("asset"):
            fid = parent_assets.get(spec["asset"])
            if fid:
                name = f"picture-{i + 1:02d}{Path(spec['asset']).suffix or '.jpg'}"
                shutil.copyfile(crafts.existing(fid), workdir / name)
                return name, spec.get("credit")
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
        per.append(credit if name else None)
        if credit and credit not in credits:
            credits.append(credit)
    return names, credits, per


# a stock-jelöltek látómodell-rangsora közös (videó, pakli, Lottie): app/tools/images.py
from app.tools.images import PICK_N as _PICK_N, contact_sheet as _contact_sheet, rank as _rank  # noqa: E402


async def _choose_stock(jobs: list[tuple[int, str, str]]) -> dict[int, dict[str, Any]]:
    from app.tools import images as imgs
    return await imgs.choose_stock(jobs, medium="video")


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
    trunc_tok, fit_tok = _TRUNC.set([]), _FIT.set([])
    try:
        return await _make_body(body, step, fmt, mode, text, title_hint, og_image, site, doc_pics)
    finally:
        _TRUNC.reset(trunc_tok)
        _FIT.reset(fit_tok)


async def _queries_for(scenes: list[dict[str, Any]]) -> list[str]:
    """Képkereső szavak (angolul, konkrétan) jelenetekhez, amelyekhez a kliens nem adott képet — a magyar cím a
    képtárban mellényúl. Egy modellhívás; hiba esetén a cím."""
    from app.llm.siliconflow import _call_model, _strip_json_fences
    lines = "\n".join(f"{i + 1}. {sc.get('headline')} — {sc.get('caption')}" for i, sc in enumerate(scenes))
    prompt = ("For each scene give a stock-photo search of 2–4 plain, concrete English words (\"wind turbines at sunset\"). "
              f"Reply with json: {{\"queries\": [str, …]}} — exactly {len(scenes)} items, in order.\n{lines}")
    for model in (settings.LLM_MODEL_PRIMARY, settings.LLM_MODEL_FALLBACK):
        try:
            q = json.loads(_strip_json_fences(await _call_model(model, [{"role": "user", "content": prompt}], thinking=False)))["queries"]
            if isinstance(q, list) and len(q) == len(scenes):
                return [_clean(x, 60) for x in q]
        except Exception as e:
            log.info("képkereső szavak (%s): %s", model, e)
    return [sc.get("headline") or "" for sc in scenes]


async def _brand_accent(body: dict[str, Any]) -> Optional[str]:
    """A videó akcentusszíne: a megadott, különben a felhasználó (alapértelmezett / megnevezett) arculatáé; brand="none" = ki."""
    if body.get("accent"):
        return body["accent"]
    b = body.get("brand")
    if str(b or "").strip().lower() in ("none", "off", "no"):
        return None
    try:
        from app.brands import user as user_brands
        kit = await user_brands.resolve(b)
    except Exception as e:
        log.info("videó-arculat: %s", e)
        return None
    cols = (kit or {}).get("colors") or {}
    return cols.get("accent") or cols.get("primary")


async def _make_body(body: dict[str, Any], step: Callable[..., None], fmt: str, mode: str, text: str, title_hint: str,
                     og_image: Optional[str], site: str, doc_pics: list[str]) -> dict[str, Any]:
    pictures = (body.get("pictures") or "stock").lower()
    body = {**body, "accent": await _brand_accent(body)}
    pre_v: list[str] = []
    pre_f: list[str] = []
    if body.get("scenes"):                  # a kliens jelenetterve: nem tervezünk újra, pontosan ez lesz
        return await _make_from_scenes(body, step, fmt, text, title_hint, site, pictures)
    if len(text) < 20:
        raise ValueError("Give the video something to say — an article link, a document, or a few sentences of your own.")
    keep = mode == "text"
    dur = body.get("duration")
    total = None if (keep and not dur) else float(min(max(float(dur or 60), 10), 180))
    # saját szövegnél is a hossz szerint bontunk (10-12: egy bekezdésként jött 60 s → 1 jelenet, a szöveg nagy része
    # kimaradt); a bekezdések száma csak alsó határ — a modell a mondatokat gondolatonként csoportosítja
    by_len = max(2, min(12, round(((total or 60) - 7) / 6.5)))
    n = max(len(_paragraphs(text)), by_len if (total or len(text) > 400) else 1) if keep else by_len
    n = max(1, min(n, 14))

    step(1, 5, "writing the script")
    story = await plan(text, n=n, keep_wording=keep, title_hint=title_hint, language=body.get("language"))
    story["title"] = _clean(story.get("title") or title_hint or "Video", 70, "Title")
    story["kicker"] = _clean(story.get("kicker") or site, 28, "Kicker")
    chart_notes: list[str] = []
    # minden jelenet egy úton: szöveghatárok, ábra a forrás számaival (különben kimarad — a modell nem talál ki számot)
    story["scenes"] = _with_ids([_scene_from(s, text, chart_notes) for s in story.get("scenes") or [] if isinstance(s, dict)][:14])
    if body.get("data_file_id"):           # táblázatból: a cellák értékei, a 2. jelenettől beszúrva
        for k, ch in enumerate(charts_from_table(body["data_file_id"])):
            story["scenes"].insert(min(len(story["scenes"]), 1 + 2 * k),
                                   {"headline": ch["title"], "caption": "", "image_query": story["scenes"][0]["image_query"],
                                    "stat": None, "chart": ch})
            chart_notes.append(f"Chart “{ch['title']}”: {len(ch['items'])} values from the table, cells {ch['cells']}.")
    if not story["scenes"]:
        raise ValueError("Couldn't plan any scenes from that text.")
    story["end"] = {"title": _clean((story.get("end") or {}).get("title"), 60, "End title"),
                    "subtitle": _clean((story.get("end") or {}).get("subtitle"), 90, "End subtitle")}

    step(2, 5, "finding pictures")
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

    # a kliens képei hova kerültek (10-12, ChatGPT: „a képek sorrendje nem volt egyértelmű”)
    if body.get("images") and pictures != "none":
        places = ["title card"] + [f"scene {sc['id']}" for sc in story["scenes"]]
        n_img = len([x for x in body.get("images") or [] if x])
        pre_v.append("Your pictures: " + ", ".join(f"#{k + 1} → {places[k]}" for k in range(min(n_img, len(places))))
                     + (" (then repeated)" if pictures == "mine" and n_img < len(places) else ""))
        if n_img > len(places):
            pre_f.append(f"Pictures #{len(places) + 1}–#{n_img} were not used: the video has {len(places)} places "
                         "(title card + scenes). Use scenes=[…] with an image per scene, or fewer pictures.")

    # a stock-keresések jelöltjeiből a látómodell választ (és egy kép nem kerül két jelenetbe)
    stock_jobs = [(i, sp["stock"], story["title"] if i == 0 else story["scenes"][i - 1]["headline"])
                  for i, sp in enumerate(specs) if isinstance(sp, dict) and sp.get("stock")]
    if stock_jobs:
        for i, chosen in (await _choose_stock(stock_jobs)).items():
            specs[i] = chosen

    opts = {"fmt": fmt, "total": total, "accent": body.get("accent"), "ken_burns": body.get("ken_burns"),
            "voice_file_id": body.get("voice_file_id"), "music_file_id": body.get("music_file_id"), "keep_wording": keep,
            "narration": body.get("narration"), "narration_voice": body.get("narration_voice"), "language": body.get("language")}
    return await _assemble(story, specs, opts, chart_notes=chart_notes, source_text=text,
                           source_file_id=body.get("source_file_id"), step=step, render=body.get("render", True),
                           pre_verified=pre_v, pre_findings=pre_f)


async def _make_from_scenes(body: dict[str, Any], step: Callable[..., None], fmt: str, text: str, title_hint: str,
                            site: str, pictures: str) -> dict[str, Any]:
    """A kliens kész jelenetterve (scenes): szöveg, kép, ábra, hossz jelenetenként — pontosan ez készül el.
    Az ábrák számai a forrásban (text), vagy ha az nincs, a jelenetek saját szövegében legyenek."""
    raw = [s for s in body.get("scenes") or [] if isinstance(s, dict)][:20]
    if not raw:
        raise ValueError("scenes: a list of {headline, caption, image?, image_query?, stat?, chart?, duration?}")
    source = text or "\n".join(f"{s.get('headline') or ''}. {s.get('caption') or ''}" for s in raw)
    chart_notes: list[str] = []
    scenes = _with_ids([_scene_from(s, source, chart_notes) for s in raw])
    story = {"title": _clean(body.get("title") or title_hint or scenes[0].get("headline") or "Video", 70, "Title"),
             "kicker": _clean(body.get("kicker") or site, 28, "Kicker"), "image_query": "", "scenes": scenes,
             "end": {"title": _clean(body.get("end_title") or "", 60, "End title"),
                     "subtitle": _clean(body.get("end_subtitle") or "", 90, "End subtitle")}}
    pre_v = [f"Your scene plan: {len(scenes)} scenes ({', '.join(sc['id'] for sc in scenes)}), used as given."]
    step(2, 5, "finding pictures")
    specs: list[Any] = [None] * (len(scenes) + 1)
    need = [k for k, s in enumerate(raw) if not s.get("image") and pictures != "none"]
    if need and any(not scenes[k].get("image_query") for k in need):
        qs = await _queries_for([scenes[k] for k in need])
        for k, q in zip(need, qs):
            scenes[k]["image_query"] = scenes[k].get("image_query") or q
    for k, s in enumerate(raw):
        if s.get("image") and pictures != "none":
            specs[k + 1] = _img_spec(s["image"])
        elif pictures not in ("none", "mine"):
            specs[k + 1] = {"stock": scenes[k].get("image_query") or scenes[k]["headline"]}
    if body.get("title_image") and pictures != "none":
        specs[0] = _img_spec(body["title_image"])
    elif pictures != "none":
        specs[0] = specs[1] if specs[1] and not (isinstance(specs[1], dict) and specs[1].get("stock")) else \
            {"stock": scenes[0].get("image_query") or story["title"]}
    pre_v.append("Pictures: title card ← " + ("your title_image" if body.get("title_image") else "scene s1's picture" if specs[0] is specs[1] else "stock")
                 + "; " + ", ".join(f"{sc['id']} ← {'yours' if raw[k].get('image') else 'stock' if specs[k + 1] else 'none'}"
                                    for k, sc in enumerate(scenes)))
    stock_jobs = [(i, sp["stock"], story["title"] if i == 0 else scenes[i - 1]["headline"])
                  for i, sp in enumerate(specs) if isinstance(sp, dict) and sp.get("stock")]
    if stock_jobs:
        for i, chosen in (await _choose_stock(stock_jobs)).items():
            specs[i] = chosen
    dur = body.get("duration")
    total = float(min(max(float(dur), 10), 180)) if dur else None
    opts = {"fmt": fmt, "total": total, "accent": body.get("accent"), "ken_burns": body.get("ken_burns"),
            "voice_file_id": body.get("voice_file_id"), "music_file_id": body.get("music_file_id"), "keep_wording": False,
            "narration": body.get("narration"), "narration_voice": body.get("narration_voice"), "language": body.get("language")}
    return await _assemble(story, specs, opts, chart_notes=chart_notes, source_text=text or source,
                           source_file_id=body.get("source_file_id"), step=step, render=body.get("render", True),
                           pre_verified=pre_v, pre_findings=[], scene_plan=True)


def _script_report(out: str, source: str) -> list[str]:
    """A build kimenetéből: a szkriptjelenetek hibái és a szövegeikben a forrásban nem szereplő számok."""
    nums, found = _text_numbers(source), []
    for ln in out.splitlines():
        if ln.startswith("SCRIPT_ERROR "):
            sid, _, msg = ln[13:].partition(": ")
            found.append(f"Scene {sid}: the script failed — {msg[:300]}. The scene shows only its picture and title; "
                         f"fix the code (chart.code) with action=edit.")
        elif ln.startswith("SCRIPT_TEXT "):
            sid, _, raw = ln[12:].partition(" ")
            try:
                texts = [str(t) for t in json.loads(raw)]
            except ValueError:
                continue
            bad = sorted({t for t in texts for x in _text_numbers(t) if x not in nums})
            if bad:
                found.append(f"Scene {sid}: the script shows numbers that aren't in the source: "
                             + "; ".join(f"“{b[:60]}”" for b in bad[:4]) + ".")
    return found


def _without_runtime(story: dict[str, Any]) -> dict[str, Any]:
    """A forgatókönyv mentéshez: a futásidejű kulcsok (_groups, _skip, _credit…) nélkül."""
    def clean(x: Any) -> Any:
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items() if not str(k).startswith("_")}
        if isinstance(x, list):
            return [clean(v) for v in x]
        return x
    return clean(story)


async def _place_on_maps(story: dict[str, Any], fmt: str) -> tuple[list[str], list[str]]:
    """A saját grafikák térképei és földrajzi elemei: helynév → lat/lon → a képkocka arányában (x, y, from, to,
    points); a térkép rétegei az elem "_groups" kulcsába (a mentett forgatókönyvbe nem kerül). → (kreditek, hibák)"""
    from app.tools import geo as geomod
    W, H = FORMATS[fmt]
    credits, problems = [], []
    for si, sc in enumerate(story.get("scenes") or []):
        ch = sc.get("chart") or {}
        if ch.get("type") != "graphic":
            continue
        els = ch.get("elements") or []
        refs = [e for e in els if e.get("geo") or e.get("geo_from") or e.get("geo_to") or e.get("geo_points")]
        maps = [e for e in els if e.get("shape") == "map"]
        if not refs and not maps:
            continue
        sid = sc.get("id") or f"s{si + 1}"
        if not maps:
            problems.append(f"Scene {sid}: elements placed by place name need a map element (shape \"map\") — left out.")
            for e in refs:
                e["_skip"] = True
            continue
        coords: dict[str, tuple[float, float]] = {}

        async def where(q: dict[str, Any]) -> Optional[tuple[float, float]]:
            if "lat" in q:
                return q["lat"], q["lon"]
            if q["place"] not in coords:
                try:
                    g = await geomod.geocode(q["place"])
                    coords[q["place"]] = (g["lat"], g["lon"])
                except Exception as ex:
                    problems.append(f"Scene {sid}: “{q['place']}” — {ex}.")
                    coords[q["place"]] = None          # type: ignore[assignment]
            return coords[q["place"]]
        for e in refs:                                   # előbb minden hely: a térkép mindet mutassa
            e["_ll"] = {k: await where(e[k]) for k in ("geo", "geo_from", "geo_to") if e.get(k)}
            if e.get("geo_points"):
                e["_ll"]["points"] = [await where(q) for q in e["geo_points"]]
        pts = [v for e in refs for k, v in e["_ll"].items() if k != "points" and v] + \
              [v for e in refs for v in (e["_ll"].get("points") or []) if v]
        m = maps[0]
        box = (W * (m.get("x", 0.5) - m.get("w", 1.0) / 2), H * (m.get("y", 0.5) - m.get("h", 1.0) / 2), W * m.get("w", 1.0), H * m.get("h", 1.0))
        # a tűk és az útvonal a cím és a felirat közé (10-12: a vasútállomás a felirat alá csúszott); a jobb szélen
        # álló tű felirata balra kerül (_graphic_elements)
        sx0, sy0, sx1, sy1 = max(box[0], W * 0.07), max(box[1], H * 0.17), min(box[0] + box[2], W * 0.93), min(box[1] + box[3], H * 0.76)
        fit = (sx0, sy0, sx1 - sx0, sy1 - sy0) if sx1 - sx0 > W * 0.2 and sy1 - sy0 > H * 0.2 else None
        try:
            built = await geomod.build(m.get("map") or {}, pts, box, W, H, fit=fit)
        except Exception as ex:
            problems.append(f"Scene {sid}: the map could not be made — {ex}.")
            for e in refs + maps:
                e["_skip"] = True
            continue
        m["_groups"], m["_credit"] = built["groups"], built["credit"]
        if built["credit"] not in credits:
            credits.append(built["credit"])
        pr = built["proj"]
        for e in refs:
            ll = e.pop("_ll")
            if e.get("geo") and ll.get("geo"):
                e["x"], e["y"] = pr.frac(*ll["geo"])
            for k in ("from", "to"):
                if e.get("geo_" + k) and ll.get("geo_" + k):
                    e[k] = pr.frac(*ll["geo_" + k])
            if e.get("geo_points"):
                e["points"] = [pr.frac(*v) for v in ll.get("points") or [] if v]
            if (e.get("geo") and not ll.get("geo")) or (e.get("geo_points") and len(e.get("points") or []) < 2) or \
               any(e.get("geo_" + k) and not ll.get("geo_" + k) for k in ("from", "to")):
                e["_skip"] = True
    return credits, problems


async def _materialize_gfx(story: dict[str, Any], work: Path, parent: Optional[str]) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """A saját grafikák képei és Lottie-animációi → a munkamappába. → (asset-nevek, [(lottie-fájl, kompozíció-név)], kreditek).
    A szülőprojekt meglévő elemei ({"asset"}) letöltés nélkül jönnek; a Lottie JSON-t ellenőrizzük és egyedi nevet kap."""
    from app.tools import images as imgs
    parent_assets = assets_of(parent) if parent else {}
    names, lotties, credits = [], [], []
    n = 0
    for si, sc in enumerate(story.get("scenes") or []):
        ch = sc.get("chart") or {}
        if ch.get("type") != "graphic":
            continue
        for ei, e in enumerate(ch.get("elements") or []):
            if e.get("shape") not in ("image", "lottie"):
                continue
            n += 1
            ext = ".json" if e["shape"] == "lottie" else ".jpg"
            name = e.get("asset") if e.get("asset") and parent_assets.get(e["asset"]) else f"gfx-{n:02d}-{sc.get('id') or si}{ext}"
            try:
                if e.get("asset") and parent_assets.get(e["asset"]):
                    shutil.copyfile(crafts.existing(parent_assets[e["asset"]]), work / name)
                    if e.get("credit"):                                    # újraépítés: a kredit marad
                        credits.append(str(e["credit"])[:200])
                elif e["shape"] == "image":
                    r = await imgs.resolve(_img_spec(e.get("src")), max_px=1600)
                    name = f"gfx-{n:02d}-{sc.get('id') or si}{r.path.suffix or '.jpg'}"
                    shutil.move(str(r.path), work / name)
                    if r.attribution:
                        credits.append(r.attribution)
                        e["credit"] = r.attribution[:200]
                else:
                    from app.tools import lottie as lot
                    src = e.get("src")
                    sid = src.get("file_id") if isinstance(src, dict) else src
                    if isinstance(src, dict) and src.get("url"):
                        sid = src["url"]
                    if isinstance(src, dict) and src.get("search"):      # a LottieFiles könyvtárából, a látómodell választ
                        data, cand = await lot.pick(str(src["search"]), sc.get("headline") or "")
                        e["src"] = {"url": cand["json_url"]}
                        e["credit"] = cand["credit"]
                        credits.append(cand["credit"])
                    elif isinstance(sid, str) and sid.startswith("http"):
                        data = await lot.fetch(sid)
                        if isinstance(src, dict) and src.get("credit"):
                            e["credit"] = str(src["credit"])[:200]
                            credits.append(e["credit"])
                    else:
                        data = lot.check(json.loads(crafts.existing(str(sid)).read_bytes()[:lot.MAX_BYTES].decode("utf-8")))
                    data["nm"] = f"Lottie {n}"
                    (work / name).write_text(json.dumps(data), "utf-8")
                    lotties.append((name, data["nm"]))
                    e["comp"] = data["nm"]
                e["file"] = e["asset"] = name
                names.append(name)
                if e["shape"] == "lottie" and not e.get("comp"):
                    e["comp"] = json.loads((work / name).read_text("utf-8")).get("nm")
                    lotties.append((name, e["comp"]))
            except Exception as ex:
                log.warning("grafika-elem (%s) nem jött le: %s", e.get("shape"), ex)
                e["file"] = None
    return names, lotties, credits


NARRATION_ASSET = "narration.wav"
_NO_NARRATION = {"none", "off", "false", "no", "0"}


def fit_to_narration(story: dict[str, Any], clips: list) -> dict[str, Any]:
    """A forgatókönyv másolata, amelyben minden jelenet legalább a narrációja + 1,4 s (átmenet és lélegzet)."""
    out = json.loads(json.dumps(story))
    for sc, (_, secs) in zip(out.get("scenes") or [], clips[1:]):
        if secs:
            sc["duration"] = round(max(float(sc.get("duration") or 0), secs + 1.4), 2)
    return out


def narration_segments(D: dict[str, Any], clips: list) -> list[tuple[float, Path]]:
    """A narráció darabjai az idővonalon: a cím a címkártyán (ha belefér), a jelenetek a saját elejükön."""
    segs: list[tuple[float, Path]] = []
    title = clips[0] if clips else (None, 0.0)
    if title[0] and title[1] <= D["title"]["dur"] - 0.8:
        segs.append((0.35, title[0]))
    segs += [(tl["start"] + 0.45, clip) for tl, (clip, _) in zip(D["timeline"][1:-1], clips[1:]) if clip]
    return segs


async def _narrate(story: dict[str, Any], opts: dict[str, Any], work: Path) -> tuple[Optional[str], list, Optional[str]]:
    """A cím és a jelenetek felolvasandó szövege → AI-narráció darabjai (offline Piper-hang, app/tools/narration.py).
    → (hang, [(wav, hossz)…] a címmel kezdve, megjegyzés ha kimaradt). Hiba esetén a videó narráció nélkül készül."""
    from app.tools import narration
    if not narration.available():
        return None, [], "AI narration isn't installed on this server — the video has no voice (give voice_file_id)."
    texts = [story.get("title") or ""] + [sc.get("caption") or sc.get("headline") or "" for sc in story.get("scenes") or []]
    mode = str(opts.get("narration") or "ai")
    want = opts.get("narration_voice") or (mode if "-" in mode and "_" in mode else None)     # szerkesztés: ugyanaz a hang
    lang = narration.lang_code(opts.get("language")) or narration.detect(" ".join(texts))
    try:
        key = await asyncio.to_thread(narration.pick, lang, want)
        clips = await narration.speak_all(texts, key, work, float(opts.get("narration_speed") or 1.0))
    except Exception as e:
        log.warning("AI-narráció kimaradt: %s", e)
        return None, [], f"AI narration was skipped ({str(e)[:160]}) — the video has no voice."
    return key, clips, None


async def _assemble(story: dict[str, Any], specs: list[Any], opts: dict[str, Any], *, chart_notes: list[str],
                    source_text: str, source_file_id: Optional[str], step: Callable[..., None], render: bool = True,
                    parent: Optional[str] = None, label_stem: Optional[str] = None, operation: str = "video",
                    pre_verified: Optional[list[str]] = None, pre_findings: Optional[list[str]] = None,
                    scene_plan: bool = False) -> dict[str, Any]:
    """Forgatókönyv + képleírások → .ecproj (+ MP4). A make és az edit közös útja; parent = a szerkesztett projekt
    (a képei és a hangja {"asset": név} hivatkozással, letöltés nélkül jönnek át)."""
    fmt, total = opts["fmt"], opts.get("total")
    work = Path(tempfile.mkdtemp(prefix="video-", dir=str(temp_path("").parent)))
    try:
        names, credit_list, per_credit = await _materialize_images(specs, fmt, work, parent)
        map_credits, gfx_findings = await _place_on_maps(story, fmt)
        gfx_names, lotties, gfx_credits = await _materialize_gfx(story, work, parent)
        credit_list = credit_list + [c for c in gfx_credits + map_credits if c not in credit_list]
        voice = music = None
        voice_s = None
        parent_assets = assets_of(parent) if parent else {}
        for key, base in (("voice_file_id", "voice"), ("music_file_id", "music")):
            fid = opts.get(key)
            kept = opts.get(f"{base}_asset")          # szerkesztés: a szülőprojekt hangja marad
            if base == "voice" and kept == NARRATION_ASSET:
                kept = None                           # …kivéve az AI-narrációt: az a (módosult) szövegből újra készül
            if not fid and kept and parent_assets.get(kept):
                shutil.copyfile(crafts.existing(parent_assets[kept]), work / kept)
                if base == "voice":
                    voice = kept
                else:
                    music = kept
                continue
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
        narr_key, narr_clips, narr_note = None, [], None
        if not voice and str(opts.get("narration") or "ai").lower() not in _NO_NARRATION:
            step(3, 5, "recording the AI narration")
            narr_key, narr_clips, narr_note = await _narrate(story, opts, work)
        story_c = fit_to_narration(story, narr_clips) if narr_clips else story
        anim = any(c.startswith("Animation ") for c in credit_list)
        label = ("Pictures and animations: " if len(credit_list) > 1 else "") if anim else "Pictures: "
        credits = (label + " · ".join(credit_list)) if credit_list else ""     # teljes (linkekkel): a projekt metájába
        # a képernyőn rövid: név + licenc, linkek nélkül („Photo: X (Wikimedia Commons), CC BY 4.0 — https://…”)
        short = [re.sub(r"\s+—\s+https?://\S+", "", re.sub(r"^Photo:\s*", "", c)).strip() for c in credit_list]
        on_screen = (label + " · ".join(dict.fromkeys(s for s in short if s)))[:300] if short else ""
        kb = str(opts.get("ken_burns") or "some").lower()
        kb = kb if kb in ("some", "all", "off") else "some"
        focus = {nm: await asyncio.to_thread(_focus, work / nm) for nm in names if nm} if kb != "off" else {}
        D = compose(story_c, fmt=fmt, total=total, images=names, accent=opts.get("accent"), credits=on_screen, focus=focus, ken_burns=kb,
                    voice=voice, voice_s=voice_s, music=music)
        if narr_clips:                   # a darabok a jelenetek elejére, egy sávban, a videó teljes hosszára
            from app.tools import narration
            segs = narration_segments(D, narr_clips)
            if segs:
                await narration.track(segs, D["total"], work / NARRATION_ASSET)
                voice = NARRATION_ASSET
                D["audio"].update(voice=voice, musicDb=-20)
                pre_verified = list(pre_verified or []) + [
                    f"AI narration ({narr_key}): {len(segs)} part(s) read aloud — each scene lasts as long as its narration."]
        if narr_note:
            pre_findings = list(pre_findings or []) + [narr_note]
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
        base = []
        for lf, _nm in lotties:            # Lottie: előbb beimportáljuk (külön kompozíció), a szkript erre a projektre épít
            args = ["exec", "file.importLottie", "--params", json.dumps({"path": lf})]
            args += ["--project", "base.ecproj", "--save"] if base else ["--empty", "--save-as", "base.ecproj"]
            await _run(*args, cwd=work, timeout=120)
            base = ["base.ecproj"]
        scripted = any(((x.get("chart") or {}).get("type") == "script") for x in story.get("scenes") or [])
        async with _render_gate():
            out = await _run("script", "build.jsx", *base, "--save-as", "project.ecproj", cwd=work,
                             untrusted=scripted, timeout=300 if scripted else None)
        script_findings = _script_report(out, source_text or "") if scripted else []
        log.info("videó felépítve: %s", out.strip().splitlines()[-2:] if out.strip() else "")
        stem = label_stem or _title_stem(story["title"])
        pid = new_file_id("ecproj")
        shutil.copyfile(work / "project.ecproj", output_path(pid))
        from app.routes.make import _new_group
        group = _new_group()
        info_meta = {"format": fmt, "duration": D["total"], "scenes": len(D["scenes"]), "title": story["title"]}
        # a forgatókönyv a projekt mellett: ebből szerkeszt az ügynök (edit) — képek a projekt saját médiájára hivatkozva
        def _src(sp: Any) -> Any:
            sp = sp.get("src") if isinstance(sp, dict) and sp.get("asset") else sp
            return sp if isinstance(sp, (str, dict)) else None
        spec = {"story": _without_runtime(story), "pictures": [({"asset": nm, "credit": cr, "src": _src(sp)} if nm else None)
                                              for nm, cr, sp in zip(names, per_credit, specs)],
                "opts": {"fmt": fmt, "total": total, "accent": opts.get("accent"), "ken_burns": kb, "voice_asset": voice,
                         "music_asset": music, "keep_wording": bool(opts.get("keep_wording")),
                         "narration": narr_key or ("none" if str(opts.get("narration") or "").lower() in _NO_NARRATION else None),
                         "narration_voice": opts.get("narration_voice"), "language": opts.get("language")},
                "source": (source_text or "")[:30000], "chart_notes": [n for n in chart_notes if not n.startswith("⚠ ")],
                "timeline": D.get("timeline")}
        write_meta(pid, label=f"{stem}.ecproj", operation=operation, source_file_id=source_file_id,
                   extra={"video": info_meta, "group": group, "credit": credits or None, "spec": spec})
        for name in [x for x in names if x] + [x for x in (voice, music) if x] + extra + gfx_names:
            add_asset(pid, name, work / name)
        result: dict[str, Any] = {"file_id": pid, "project": _file_entry(pid), "title": story["title"], "format": fmt,
                                  "duration": D["total"], "scenes": len(D["scenes"]), "captions": len(D["captions"]),
                                  "credits": credits or None, "story": _without_runtime(story)}
        if render:
            step(4, 5, "rendering the video")
            vid = await _render_staged(pid, work / "project.ecproj", stem, group=group, want_s=D["total"])
            result["check"] = vid.pop("check", None)          # a közös check-szerződés: a feladat (a videó) ellenőrzése
            chk = result["check"]
            if chk is not None:
                chk["verified"] = list(pre_verified or []) + chk.get("verified", [])
                trunc = _TRUNC.get() or []
                cov_v, cov_f, cov = _coverage(source_text, story, bool(opts.get("keep_wording")), scene_plan)
                result["coverage"] = cov
                chk["verified"] += cov_v
                fitted = _FIT.get() or []
                if fitted:
                    chk["verified"].append(f"Fitted to their boxes: {len(fitted)} graphic text(s) set smaller, e.g. "
                                           + "; ".join(fitted[:4]) + ".")
                chk["findings"] = chk.get("findings", []) + list(pre_findings or []) + script_findings + gfx_findings + cov_f + (
                    [f"Shortened text — {t}. Shorten it yourself, or change it with action=edit." for t in trunc[:6]])
                if chk["findings"] and chk.get("status") == "ok":
                    chk["status"] = "needs_check"
            # technikailag jó, tartalmilag gyanús: kevés jelenet a hosszhoz (10-12: 60 s, 1 jelenet — a szöveg nagy része kimaradt).
            # Az ÁTLAG számít: egy 19 s-os idővonal három eseménnyel és három felirattal rendben van.
            ns = max(1, len(D["scenes"]))
            if result["check"] and (D["total"] / ns > 15 or (ns == 1 and D["total"] > 20)):
                result["check"]["status"] = "needs_check"
                result["check"]["findings"] = result["check"].get("findings", []) + [
                    f"Only {ns} scene(s) for {D['total']:.0f} s, so much of the text may be missing. Add scenes "
                    "(action=edit, add) or make it again without keep_wording."]
            if result["check"] and chart_notes:
                ok_notes = [n for n in chart_notes if not n.startswith("⚠ ")]
                bad = [n[2:] for n in chart_notes if n.startswith("⚠ ")]
                result["check"]["verified"] = result["check"].get("verified", []) + ok_notes
                if bad:
                    result["check"]["findings"] = result["check"].get("findings", []) + bad
                    if result["check"].get("status") == "ok":
                        result["check"]["status"] = "needs_check"
            result["video"] = vid
            result["files"] = [result["project"], vid]
        else:
            result["files"] = [result["project"]]
        step(5, 5, "done")
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)




_STEM = lambda w: w[:5]


def _words(t: str) -> list[str]:
    import unicodedata
    t = unicodedata.normalize("NFKD", (t or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return [w for w in re.findall(r"[a-z0-9]+", t) if len(w) >= 4]


def _video_text(story: dict[str, Any]) -> str:
    parts = [story.get("title") or "", (story.get("end") or {}).get("title") or "", (story.get("end") or {}).get("subtitle") or ""]
    for sc in story.get("scenes") or []:
        parts += [sc.get("headline") or "", sc.get("caption") or ""]
        if sc.get("stat"):
            parts += [sc["stat"].get("value") or "", sc["stat"].get("label") or ""]
        ch = sc.get("chart") or {}
        parts.append(ch.get("title") or "")
        for it in ch.get("items") or []:
            parts += [str(it.get("label") or ""), str(it.get("text") or ""), str(it.get("value") if it.get("value") is not None else "")]
        if ch.get("total"):
            parts.append(str(ch["total"]))
    return " ".join(parts)


def _coverage(source: str, story: dict[str, Any], keep: bool, scene_plan: bool) -> tuple[list[str], list[str], dict[str, Any]]:
    """Tartalmi lefedettség (10-12, ChatGPT: „egy technikailag hibátlan, de tartalmilag hiányos videó is sikeres lett”):
    a forrás mondatai és számai közül mi jelenik meg a videóban (felirat, cím, szám, ábra). Saját szövegnél (keep_wording)
    minden mondatnak benne kell lennie; cikknél a nagy hiány (< 60%) „needs_check”, a kimaradtak listája mindig ott van."""
    vt = _video_text(story)
    vstems = {_STEM(w) for w in _words(vt)}
    sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", source or "") if len(_words(x)) >= 4]
    missing = []
    for snt in sents:
        ws = {_STEM(w) for w in _words(snt)}
        if ws and len(ws & vstems) / len(ws) < 0.4:
            missing.append(snt)
    src_nums = _text_numbers(source or "") - {float(i) for i in range(0, 4)}
    vid_nums = _text_numbers(vt)
    miss_nums = sorted(src_nums - vid_nums)
    cov = {"sentences": {"total": len(sents), "covered": len(sents) - len(missing), "missing": missing[:12]},
           "numbers": {"total": len(src_nums), "covered": len(src_nums) - len(miss_nums),
                       "missing": [(int(x) if float(x).is_integer() else x) for x in miss_nums[:20]]}}
    if scene_plan and not source:
        return [], [], cov
    verified = [f"Covers {cov['sentences']['covered']} of {len(sents)} sentences and {cov['numbers']['covered']} of "
                f"{len(src_nums)} numbers of the source (coverage lists what is left out)."] if sents or src_nums else []
    findings = []
    s_ratio = (len(sents) - len(missing)) / len(sents) if sents else 1
    n_ratio = cov["numbers"]["covered"] / len(src_nums) if src_nums else 1
    if keep and missing:
        findings.append(f"{len(missing)} of your sentences are not in the video, e.g. “{missing[0][:90]}”.")
    elif s_ratio < 0.6 or n_ratio < 0.6:
        findings.append(f"Much of the source is left out ({cov['sentences']['covered']}/{len(sents)} sentences, "
                        f"{cov['numbers']['covered']}/{len(src_nums)} numbers), e.g. “{(missing or ['…'])[0][:90]}”"
                        + (f"; numbers not shown: {', '.join(str(x) for x in cov['numbers']['missing'][:8])}" if miss_nums else "")
                        + ". Add scenes with action=edit, or pass scenes=[…] yourself.")
    return verified, findings, cov


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
    from app.tools import ios_safe
    await ios_safe.finalize(out)                        # iPhone/Safari: a moov elöl (a renderer a végére írja)
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
        src = meta.get("source_file_id")
        out = {"file_id": file_id, "kind": "movie" if ext in MOVIE_EXTS else ext, "size_kb": p.stat().st_size // 1024,
               "made_from": src}
        if src and str(src).lower().endswith(".ecproj"):
            out["project"] = src
            out["note"] = "To change this video, use video(action=\"edit\") with this file or its project; info on the project lists the scenes."
        return out
    data = json.loads(p.read_text("utf-8"))
    footage = [Path(str((i.get("kind") or {}).get("path"))).name for i in (data.get("items") or {}).values()
               if (i.get("kind") or {}).get("type") == "Footage"]
    assets = assets_of(file_id)
    rid = meta.get("render_file_id")
    spec = meta.get("spec") or {}
    pics = spec.get("pictures") or []
    tl = {x["id"]: x for x in spec.get("timeline") or [] if x.get("id")}
    scenes = [{"scene": i, "id": sc.get("id") or f"s{i}", **({"start": tl[sc["id"]]["start"], "end": tl[sc["id"]]["end"]}
                                                              if sc.get("id") in tl else {}),
               "headline": sc.get("headline"), "caption": sc.get("caption"),
               **({"chart": sc["chart"]} if sc.get("chart") else {}),
               **({"stat": sc["stat"]} if sc.get("stat") else {}),
               "picture": ((pics[i] or {}).get("credit") or "yes") if i < len(pics) and pics[i] else None}
              for i, sc in enumerate((spec.get("story") or {}).get("scenes") or [], 1)]
    return {"file_id": file_id, "comps": _comps(data), "footage": footage, "missing": [f for f in footage if f not in assets],
            "video": meta.get("video"), "last_render": _file_entry(rid) if rid and output_path(rid).exists() else None,
            "credits": meta.get("credit"), "editable_by_description": bool(spec),
            **({"title": spec["story"].get("title"), "scenes": scenes, "options": spec.get("opts")} if spec else {}),
            **({"note": "It was also changed by hand in the editor; an edit rebuilds from the script and leaves those hand changes out."}
               if meta.get("hand_edited") else {})}


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
                   "--out", "thumb.png", cwd=work, timeout=120)
        shutil.move(str(work / "thumb.png"), str(raw))
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------- utólagos szerkesztés (az ügynök és az „Ask for a change”) ----------

def project_of(file_id: str) -> str:
    """A videó projektje: .ecproj maga, MP4/WebM/GIF esetén az, amiből renderelték."""
    if file_id.lower().endswith(".ecproj"):
        return file_id
    src = (read_meta(file_id) or {}).get("source_file_id")
    if src and str(src).lower().endswith(".ecproj"):
        return src
    raise ValueError("This video has no project behind it — make one with the video maker (video action=make).")


def _img_spec(x: Any) -> Any:
    """Kép a szerkesztéshez: file_id | {"file_id"} | {"url"} (pl. a kliens saját generált képe) | {"stock": szavak}
    | None / "none" (kép nélkül)."""
    if x in (None, "", "none", False):
        return None
    if isinstance(x, str):
        return {"url": x} if x.startswith("http") else x
    if isinstance(x, dict) and (x.get("file_id") or x.get("url") or x.get("stock")):
        return x.get("file_id") or ({"url": x["url"], **({"credit": x["credit"]} if x.get("credit") else {})} if x.get("url")
                                    else {"stock": _clean(x["stock"], 60)})
    raise ValueError(f"image: a file_id, a URL, {{\"stock\": \"words\"}} or none — not {x!r}")


def _scene_from(ch: dict[str, Any], source: str, notes: list[str], base: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Egy jelenet (a modell terve, a kliens jelenetterve vagy egy szerkesztés) tisztítva: szöveghatárok (a rövidülés
    a check-be kerül), ábra a forrásszámokkal ellenőrizve, stabil id, opcionális fix hossz."""
    sc = dict(base or {"headline": "", "caption": "", "image_query": "", "stat": None, "chart": None})
    if ch.get("id"):
        sc["id"] = re.sub(r"[^\w-]", "", str(ch["id"]))[:20] or sc.get("id")
    if "headline" in ch:
        sc["headline"] = _clean(ch["headline"], 60, "Headline")
    if "caption" in ch:
        sc["caption"] = _clean(ch["caption"], 200, "Caption")
    if ch.get("image_query"):
        sc["image_query"] = _clean(ch["image_query"], 60)
    if "duration" in ch:
        try:
            sc["duration"] = min(30.0, max(2.0, float(ch["duration"]))) if ch["duration"] not in (None, "", "auto") else None
        except (TypeError, ValueError):
            pass
    if "stat" in ch:
        st = ch["stat"]
        sc["stat"] = ({"value": _clean(st.get("value"), 10, "Stat value"), "label": _clean(st.get("label"), 48, "Stat label")}
                      if isinstance(st, dict) and st.get("value") else None)
        if sc["stat"]:
            sc["chart"] = None
    if "chart" in ch:
        cc = ch["chart"]
        if isinstance(cc, dict) and not cc.get("type") and (base or {}).get("chart"):
            cc = {**base["chart"], **cc}          # részleges javítás: csak a megadott mezők változnak (pl. a mértékegység)
        chart, note = _chart_from_plan(cc, source) if cc else (None, None)
        for n in (note if isinstance(note, list) else [note] if note else []):
            # ⚠ = figyelmeztetés (a check findings-be): ami kimaradt, nem mehet el csendben (10-12: a térkép tűi kiestek,
            # a check mégis ok volt — a megjegyzés a „verified” közé került)
            notes.append(n if n.startswith("⚠ ") or chart else "⚠ " + n)
        if cc and chart is None and not note:
            t_ = cc.get("type") if isinstance(cc, dict) else None
            notes.append(f"⚠ The suggested {t_ or 'chart'} “{(cc.get('title') if isinstance(cc, dict) else '') or ''}” was left out: "
                         f"{'unknown type' if t_ not in _CHART_TYPES else 'not enough usable items (check items / elements)'}.")
        sc["chart"] = chart
        if chart:
            sc["stat"] = None
    return sc


async def edit(file_id: str, changes: dict[str, Any], *, extra_source: str = "",
               progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    tok, fit_tok = _TRUNC.set([]), _FIT.set([])
    try:
        return await _edit(file_id, changes, extra_source=extra_source, progress=progress)
    finally:
        _TRUNC.reset(tok)
        _FIT.reset(fit_tok)


async def _edit(file_id: str, changes: dict[str, Any], *, extra_source: str = "",
                progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    """Egy elkészült videó módosítása a forgatókönyvéből → ÚJ változat (projekt + MP4). changes:
    {"scenes": [{"scene": n (1-től), "headline"?, "caption"?, "image"?, "stat"?, "chart"?, "remove"?}],
     "add": [{"after": n (0 = az első elé), "headline", "caption"?, "image"? | "image_query"?, "stat"?, "chart"?}],
     "title"?, "kicker"?, "title_image"?, "end_title"?, "end_subtitle"?,
     "format"?, "duration"?, "ken_burns"?, "accent"?, "music_file_id"? | "none", "voice_file_id"? | "none"}.
    A grafikon számai a forrásból (vagy az extra_source-ból — pl. a kérés szövegéből) jöhetnek."""
    step = progress or (lambda *a, **k: None)
    pid = project_of(file_id)
    meta = read_meta(pid) or {}
    spec = meta.get("spec")
    if not spec:
        raise ValueError("This video wasn't made by the video maker, so it can't be changed by description — change it in "
                         "the video editor (edit_url), or make a new one with video action=make.")
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Say what should change (changes).")
    story = copy.deepcopy(spec["story"])
    story["scenes"] = _with_ids(story["scenes"])          # régi projekt: s1, s2… a mostani sorrendben
    pics = copy.deepcopy(spec.get("pictures") or [])
    pics += [None] * (len(story["scenes"]) + 1 - len(pics))
    opts = dict(spec.get("opts") or {})
    source = (spec.get("source") or "") + "\n" + (extra_source or "")
    notes: list[str] = []
    keep = lambda pic: ({"asset": pic["asset"], "credit": pic.get("credit"), "src": pic.get("src")} if pic else None)

    for key, lim in (("title", 70), ("kicker", 28)):
        if key in changes:
            story[key] = _clean(changes[key], lim, key.capitalize())
    end = dict(story.get("end") or {})
    if "end_title" in changes:
        end["title"] = _clean(changes["end_title"], 60, "End title")
    if "end_subtitle" in changes:
        end["subtitle"] = _clean(changes["end_subtitle"], 90, "End subtitle")
    story["end"] = end

    entries = [{"scene": sc, "pic": keep(pics[i + 1]), "remove": False} for i, sc in enumerate(story["scenes"])]
    title_pic = _img_spec(changes["title_image"]) if "title_image" in changes else keep(pics[0])
    n0 = len(entries)
    ids = [e["scene"].get("id") for e in entries]
    for ch in changes.get("scenes") or []:
        if ch.get("id"):                        # stabil azonosító (s3) — beszúrás/törlés után is ugyanaz a jelenet
            if ch["id"] not in ids:
                raise ValueError(f"There is no scene {ch['id']} (scenes: {', '.join(ids)}).")
            k = ids.index(ch["id"])
        else:
            try:
                k = int(ch.get("scene")) - 1
            except (TypeError, ValueError):
                raise ValueError("scenes[]: give the scene's id (\"s3\") or number (1 = the first scene after the title card)")
            if not 0 <= k < n0:
                raise ValueError(f"There is no scene {k + 1} (the video has {n0}).")
        e = entries[k]
        if ch.get("remove"):
            e["remove"] = True
            continue
        e["scene"] = _scene_from(ch, source, notes, e["scene"])
        if "image" in ch:
            e["pic"] = _img_spec(ch["image"])
        elif ch.get("image_query"):
            e["pic"] = {"stock": _clean(ch["image_query"], 60)}
    adds: dict[int, list[dict[str, Any]]] = {}
    for a in changes.get("add") or []:
        aft = a.get("after", n0)
        after = (ids.index(aft) + 1) if isinstance(aft, str) and aft in ids else 0 if aft == "title" else max(0, min(n0, int(aft)))
        sc = _scene_from(a, source, notes)
        # keresőszó híján a videó (angol) keresőszava — a magyar cím a képtárban mellényúl („Mit tehetsz a méhekért?”)
        near = next((e["scene"].get("image_query") for e in entries[max(0, after - 1):] if e["scene"].get("image_query")), "")
        pic = _img_spec(a["image"]) if a.get("image") else {"stock": _clean(a.get("image_query") or near or story.get("image_query")
                                                                            or sc["headline"] or story["title"], 60)}
        adds.setdefault(after, []).append({"scene": sc, "pic": pic, "remove": False})
    final = list(adds.get(0, []))
    for i, e in enumerate(entries, 1):
        if not e["remove"]:
            final.append(e)
        final += adds.get(i, [])
    if not final:
        raise ValueError("A video needs at least one scene.")

    if "format" in changes:
        opts["fmt"] = fmt_name(changes["format"])
    if "duration" in changes:
        d = changes["duration"]
        opts["total"] = None if d in (None, "", "auto") else float(min(max(float(d), 10), 180))
    for key in ("ken_burns", "accent", "narration", "narration_voice"):
        if key in changes:
            opts[key] = changes[key]
    if changes.get("voice_file_id") not in (None, "", "none"):
        opts["narration"] = "none"                       # saját hang jött: az AI-narráció kimarad
    for key, base in (("music_file_id", "music"), ("voice_file_id", "voice")):
        if key in changes:
            v = changes[key]
            opts[f"{base}_asset"] = None
            opts[key] = None if v in (None, "", "none") else v

    story["scenes"] = _with_ids([e["scene"] for e in final])
    specs: list[Any] = [title_pic] + [e["pic"] for e in final]
    reshape = opts["fmt"] != (spec.get("opts") or {}).get("fmt")
    if reshape:          # más képarány: a képeket az eredeti forrásból újra (különben nagyítva, elmosódva)
        specs = [(sp.get("src") or sp) if isinstance(sp, dict) and sp.get("asset") else sp for sp in specs]
    stock_jobs = [(i, sp["stock"], story["title"] if i == 0 else story["scenes"][i - 1]["headline"])
                  for i, sp in enumerate(specs) if isinstance(sp, dict) and sp.get("stock")]
    if stock_jobs:
        step(1, 5, "finding pictures")
        for i, chosen in (await _choose_stock(stock_jobs)).items():
            specs[i] = chosen
    from app.storage.naming import result_stem
    res = await _assemble(story, specs, opts, chart_notes=_kept_chart_notes(spec, story) + notes, source_text=source,
                          source_file_id=pid, step=step, parent=pid, label_stem=result_stem(pid, "edited"),
                          operation="video edit")
    res["notes"] = notes + (["Rebuilt from the video's script: changes made by hand in the video editor are not in this "
                             "version (they stay in the previous one)."] if meta.get("hand_edited") else [])
    return res


_EDIT_PLAN = """You change a short video made of scenes. Reply with ONE json object of changes — only what the request asks:
{"summary": "what you changed, one short sentence in the request's language",
 "scenes": [{"scene": n, "headline"?: str, "caption"?: str, "image"?: {"stock": "2–4 plain English words"} | "none",
             "stat"?: {"value": str, "label": str} | null, "chart"?: <chart> | null, "remove"?: true}],
 "add": [{"after": n, "headline": str, "caption": str, "image_query": "2–4 plain English words", "stat"?: ..., "chart"?: ...}],
 "title"?: str, "end_title"?: str, "end_subtitle"?: str, "title_image"?: {"stock": "words"},
 "format"?: "vertical|landscape|square", "duration"?: seconds, "ken_burns"?: "some|all|off", "accent"?: "#RRGGBB"}
<chart> = {"type": "column|bar|line|area|donut|ring|icons|timeline|steps|counter|compare|progress|gauge|funnel|proscons|quote", "title": str, "unit": str, "total": number|null, "min"?: number, "max"?: number,
           "items": [{"label": str, "value": number|null, "text": str}]}
Scene numbers are the ones listed below (1 = the first scene after the title card). Numbers in charts and stats must
come from the video's source or from the request itself — never invent them. Keep the language of the video.

The video now:
{video}

Request: {request}"""


def _describe(spec: dict[str, Any]) -> str:
    st, o = spec["story"], spec.get("opts") or {}
    pics = spec.get("pictures") or []
    lines = [f"title: {st.get('title')} · format {o.get('fmt')} · {o.get('total') or 'auto'} s · Ken Burns {o.get('ken_burns')}"]
    for i, sc in enumerate(st["scenes"], 1):
        pic = pics[i] if i < len(pics) else None
        what = f"chart {sc['chart']['type']} “{sc['chart']['title']}”" if sc.get("chart") else (f"stat {sc['stat']['value']}" if sc.get("stat") else "")
        lines.append(f"{i}. {sc.get('headline')} — caption: {sc.get('caption')}" + (f" — {what}" if what else "")
                     + (f" — picture: {(pic or {}).get('credit') or 'yes'}" if pic else " — no picture"))
    lines.append(f"end: {(st.get('end') or {}).get('title')} / {(st.get('end') or {}).get('subtitle')}")
    return "\n".join(lines)


async def revise(file_id: str, request: str, progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    """„Ask for a change" egy videóra: a kérésből változtatás-lista (DeepSeek, JSON) → edit."""
    from app.llm.siliconflow import _call_model, _strip_json_fences
    pid = project_of(file_id)
    spec = (read_meta(pid) or {}).get("spec")
    if not spec:
        raise ValueError("This video wasn't made by the video maker — change it in the video editor instead.")
    (progress or (lambda *a, **k: None))(0, 5, "planning the change")
    prompt = _EDIT_PLAN.replace("{video}", _describe(spec)).replace("{request}", request)
    plan_, last = None, None
    for model in (settings.AGENT_MODEL, settings.AGENT_MODEL_FALLBACK):
        try:
            plan_ = json.loads(_strip_json_fences(await _call_model(model, [{"role": "user", "content": prompt}], thinking=False)))
            break
        except Exception as e:
            last = e
            log.warning("videó-szerkesztési terv %s modellel nem sikerült: %s", model, e)
    if not isinstance(plan_, dict):
        raise ValueError(f"Couldn't work out the change ({last}) — try saying it differently.")
    summary = _clean((plan_ or {}).pop("summary", "") if isinstance(plan_, dict) else "", 200)
    res = await edit(pid, plan_ if isinstance(plan_, dict) else {}, extra_source=request, progress=progress)
    res["summary"] = summary or "Video changed."
    return res


# ---------- videó egy diára ----------

async def to_pptx(file_id: str, deck_file_id: Optional[str] = None, after_slide: Optional[int] = None,
                  title: Optional[str] = None) -> dict[str, Any]:
    """A videó egy diára: új egydiás pakli, vagy egy meglévő pakli új diája (after_slide után, alapból a végén).
    A dián a videó maga (beágyazott MP4, az első kockája a borítóképe), a dia méretéhez igazítva."""
    from pptx import Presentation
    from pptx.util import Emu, Pt
    from app.storage.naming import result_stem, root_name
    from app.tools import previews
    vid = file_id
    if file_id.lower().endswith(".ecproj"):                 # projekt → a legutóbbi renderje (ha nincs, renderel)
        rid = (read_meta(file_id) or {}).get("render_file_id")
        if not rid or not output_path(rid).exists():
            rid = (await render(file_id))["file_id"]
        vid = rid
    vpath = crafts.existing(vid)
    if vpath.suffix.lower() not in (".mp4", ".m4v", ".mov"):
        raise ValueError("Slides take an MP4 video (or a video project, which is rendered first).")
    import cv2
    cap = cv2.VideoCapture(str(vpath))
    vw, vh = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 16, cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 9
    secs = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 30)
    cap.release()
    # borítókép: a címkártya, amikor a cím már teljesen látszik (~1,6 s) — fix arány átúszásra eshet
    poster = temp_path(new_file_id("png"))
    await asyncio.to_thread(previews._video_frame, vpath, min(0.5, 1.6 / secs) if secs > 0 else 0.1, poster)
    if deck_file_id:
        if not deck_file_id.lower().endswith(".pptx"):
            raise ValueError("deck_file_id: a PowerPoint deck (.pptx)")
        prs = Presentation(str(crafts.existing(deck_file_id)))
    else:
        prs = Presentation()
        prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)          # 16:9
    SW, SH = int(prs.slide_width), int(prs.slide_height)
    from app.tools import pptx_deck
    n_before = len(prs.slides)
    # a minta: az a dia, ami után az új jön (alapból az utolsó) — annak hátterét és címstílusát kapja az új dia
    ref_i = int(after_slide) - 1 if after_slide is not None else n_before - 1
    ref = list(prs.slides)[max(0, min(n_before - 1, ref_i))] if n_before else None
    layout = ref.slide_layout if ref is not None else \
        min(prs.slide_layouts, key=lambda lo: (len(lo.placeholders), "blank" not in lo.name.lower()))
    slide = prs.slides.add_slide(layout)
    for ph in list(slide.placeholders):                     # üres helyőrző ne maradjon a dián
        ph._element.getparent().remove(ph._element)
    took_bg = pptx_deck.inherit_look(slide, ref)
    top_room = 0
    if title and title.strip():
        top_room = int(SH * 0.16)
        st = pptx_deck.title_style(ref) if ref is not None else {}
        tb = slide.shapes.add_textbox(int(SW * 0.06), int(SH * 0.04), int(SW * 0.88), int(SH * 0.1))
        tb.text_frame.text = title.strip()[:120]
        run = tb.text_frame.paragraphs[0].runs[0]
        run.font.size = Pt(min(32, st.get("size") or 28))
        run.font.bold = True if st.get("bold") is None else st["bold"]
        if st.get("font"):
            run.font.name = st["font"]
        color = pptx_deck.readable(st.get("color"), slide, prs)
        if color:
            run.font.color.rgb = pptx_deck._rgb(color)
    pad = 0 if (not deck_file_id and not top_room and vw / vh >= SW / SH * 0.98) else int(min(SW, SH) * 0.04)
    box_w, box_h = SW - 2 * pad, SH - top_room - 2 * pad
    k = min(box_w / vw, box_h / vh)
    w, h = int(vw * k), int(vh * k)
    left, top = (SW - w) // 2, top_room + pad + (box_h - h) // 2
    # a dián lévő videó iPhone-on (Keynote) is játszódjon: ha kell, iPhone-biztos másolat kerül be
    from app.tools import ios_safe
    embed = temp_path(new_file_id(vpath.suffix.lstrip(".").lower()))
    shutil.copy(vpath, embed)
    await ios_safe.finalize(embed)
    movie = slide.shapes.add_movie(str(embed), left, top, w, h, poster_frame_image=str(poster),
                                   mime_type="video/quicktime" if embed.suffix.lower() == ".mov" else "video/mp4")
    movie.name = "Video"
    from app.tools import pptx_media              # a PowerPoint saját időzítése: magától indul, koppintásra szünet
    pptx_media.autoplay(slide._element, movie.shape_id, int(secs * 1000))
    n = len(prs.slides)
    pos = n - 1
    if deck_file_id and after_slide is not None:            # a dia helye: after_slide után
        pos = max(0, min(n - 1, int(after_slide)))
        lst = prs.slides._sldIdLst
        el = lst[-1]
        lst.remove(el)
        lst.insert(pos, el)
    if deck_file_id:                                         # a pakli „n / N” oldalszámai az új sorrend szerint
        from app.tools.pptx_deck import renumber_footers
        renumber_footers(prs)
    out = new_file_id("pptx")
    await asyncio.to_thread(prs.save, str(output_path(out)))
    poster.unlink(missing_ok=True)
    embed.unlink(missing_ok=True)
    label = (result_stem(deck_file_id, "with video") if deck_file_id else root_name(display_label(vid), "Video")) + ".pptx"
    write_meta(out, source_file_id=deck_file_id or vid, operation="video on a slide", label=label)
    # ellenőrzés (közös check-szerződés): a mentett fájlban tényleg ott a videó, a megadott dián
    chk_prs = Presentation(str(output_path(out)))
    media = [sh for sh in chk_prs.slides[pos].shapes if sh.shape_type == 16]       # MSO_SHAPE_TYPE.MEDIA
    look = ([f"The new slide has the deck's look: {'background' if took_bg else 'layout'}{' and title style' if title else ''} "
             "from the slide next to it."] if ref is not None else [])
    check = ({"status": "ok", "findings": [], "verified": [f"The MP4 ({secs:.0f} s) is embedded on slide {pos + 1} of {len(chk_prs.slides)}, "
                                                          "with the title card as its poster.", *look]}
             if media else {"status": "failed", "findings": ["The video didn't end up on the slide."], "verified": []})
    return {"file_id": out, "url": public_url(out), "label": label, "slide": pos + 1, "video_file_id": vid, "check": check,
            "note": "The video is embedded and starts by itself when the slide shows (tap or click pauses it) in "
                    "PowerPoint and Keynote, iPhone included; the browser's slide editor shows its first frame."}


# ---------- előnézet jelenetenként ----------

def frame_time(file_id: str, scene: Any = None, at: Optional[float] = None) -> tuple[str, float, str]:
    """→ (a renderelt videó file_id-ja, az időpont másodpercben, címke). scene: id (s3), sorszám, "title", "end";
    a jelenet VÉGE előtt 0,4 s-mal (az animációk már lefutottak); at: tetszőleges másodperc."""
    if file_id.lower().endswith(".ecproj"):              # projekt → a legutóbbi renderje
        pid, vid = file_id, (read_meta(file_id) or {}).get("render_file_id") or ""
        if not vid or not output_path(vid).exists():
            raise ValueError("This video project has no render yet — render it first (video action=render).")
    else:                                                 # videó → a projektje (ha a videókészítő csinálta)
        src = str((read_meta(file_id) or {}).get("source_file_id") or "")
        pid, vid = (src if src.lower().endswith(".ecproj") else ""), file_id
    spec = ((read_meta(pid) or {}).get("spec") or {}) if pid else {}
    if at is not None:
        return vid, max(0.0, float(at)), f"at {float(at):.1f} s"
    tl = spec.get("timeline") or []
    if not tl:
        raise ValueError("This video has no scene timeline (made before it was recorded) — use at=seconds instead.")
    key = str(scene).strip().lower() if scene is not None else "title"
    if key.isdigit():
        key = (tl[int(key)]["id"] if 0 < int(key) < len(tl) - 1 else "")
    hit = next((x for x in tl if str(x.get("id")).lower() == key), None)
    if not hit:
        raise ValueError(f"No scene {scene!r} — scenes: {', '.join(str(x['id']) for x in tl)}")
    return vid, scene_still(tl, tl.index(hit)), f"{hit['id']} ({hit['start']:.1f}–{hit['end']:.1f} s)"


def _kept_chart_notes(spec: dict[str, Any], story: dict[str, Any]) -> list[str]:
    """A mentett ábra-jegyzetek közül csak a VÁLTOZATLAN ábráké öröklődik (10-13: a cím javítása után a check a régi
    „Counter “fős kockázati lista …”” és az új címet is felsorolta)."""
    old = {sc.get("id"): sc.get("chart") for sc in (spec.get("story") or {}).get("scenes") or [] if isinstance(sc, dict)}
    new = {sc.get("id"): sc.get("chart") for sc in story.get("scenes") or [] if isinstance(sc, dict)}
    gone = {(c or {}).get("title") for sid, c in old.items() if c and new.get(sid) != c} - {None, ""}
    return [n for n in dict.fromkeys(spec.get("chart_notes") or []) if not any(f"“{t}”" in n for t in gone)]


def scene_still(tl: list[dict[str, Any]], k: int) -> float:
    """A jelenet „kész” pillanata: az animációk lefutottak, de a KÖVETKEZŐ jelenet áttűnése még nem kezdődött
    (10-13: a jelenet vége előtt 0,4 s a 0,5 s-os áttűnésbe esett — a cím és a felirat a kontaktlapon már elhalványult)."""
    x = tl[k]
    t = x["end"] - 0.4 if x["id"] != "title" else min(x["end"] - 0.4, 2.0)
    nxt = tl[k + 1]["start"] if k + 1 < len(tl) else None
    if nxt is not None and nxt < x["end"]:
        t = min(t, nxt - 0.15)
    return max(x["start"] + 0.1, t, 0.0)


def contact_sheet(file_id: str, width: int = 1400) -> tuple[bytes, list[dict[str, Any]]]:
    """Az összes jelenet egy képen (a címkártya, minden jelenet vége, a zárókép), feliratozva — egy pillantás az egészre."""
    import cv2
    import numpy as np
    pid = project_of(file_id)
    tl = ((read_meta(pid) or {}).get("spec") or {}).get("timeline") or []
    if not tl:
        raise ValueError("This video has no scene timeline (made before it was recorded) — use at=seconds instead.")
    vid = (read_meta(pid) or {}).get("render_file_id") if file_id.lower().endswith(".ecproj") else file_id
    if not vid or not output_path(vid).exists():
        raise ValueError("This video project has no render yet — render it first (video action=render).")
    cap = cv2.VideoCapture(str(crafts.existing(vid)))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    tiles, info_ = [], []
    cols = 4 if len(tl) > 6 else 3
    tw = width // cols
    for k, x in enumerate(tl):
        t = scene_still(tl, k)
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(max(0, t) * fps))
        ok, f = cap.read()
        if not ok:
            continue
        th = int(tw * f.shape[0] / f.shape[1])
        f = cv2.resize(f, (tw, th))
        cv2.rectangle(f, (0, 0), (tw, 30), (0, 0, 0), -1)
        cv2.putText(f, f"{x['id']}  {x['start']:.1f}-{x['end']:.1f}s", (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(f)
        info_.append({"id": x["id"], "start": x["start"], "end": x["end"], "frame_at": round(t, 2)})
    cap.release()
    if not tiles:
        raise ValueError("No frame could be read from the video.")
    while len(tiles) % cols:
        tiles.append(np.zeros_like(tiles[0]))
    grid = np.vstack([np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)])
    ok, buf = cv2.imencode(".jpg", grid, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return buf.tobytes(), info_


def _scene_index(pid: str, scene: Any) -> tuple[int, dict[str, Any], dict[str, Any]]:
    """Jelenet (id / sorszám) → (1-től számolt index, jelenet, idővonal-sor)."""
    spec = (read_meta(pid) or {}).get("spec") or {}
    scenes = _with_ids(copy.deepcopy((spec.get("story") or {}).get("scenes") or []))
    tl = {x["id"]: x for x in spec.get("timeline") or [] if x.get("id")}
    key = str(scene or "").strip()
    k = int(key) if key.isdigit() else next((i + 1 for i, sc in enumerate(scenes) if sc["id"] == key), 0)
    if not 1 <= k <= len(scenes):
        raise ValueError(f"No scene {scene!r} — scenes: {', '.join(sc['id'] for sc in scenes)}")
    sc = scenes[k - 1]
    return k, sc, tl.get(sc["id"]) or {}


def filmstrip(file_id: str, scene: Any, frames: int = 6, width: int = 1400) -> tuple[bytes, dict[str, Any]]:
    """Egy jelenet mozgása egy képen: `frames` kocka a jelenet elejétől a végéig, időponttal feliratozva — így egy
    külső AI is látja, hogyan nő az oszlop, rajzolódik a vonal, fordul a mutató (nem csak a végállapotot)."""
    import cv2
    import numpy as np
    pid = project_of(file_id)
    _, sc, row = _scene_index(pid, scene)
    if not row:
        raise ValueError("This video has no scene timeline (made before it was recorded) — use at=seconds instead.")
    vid = (read_meta(pid) or {}).get("render_file_id") if file_id.lower().endswith(".ecproj") else file_id
    if not vid or not output_path(vid).exists():
        raise ValueError("This video project has no render yet — render it first (video action=render).")
    frames = max(2, min(12, int(frames or 6)))
    a, b = row["start"] + 0.55, row["end"] - 0.25           # az áttűnés (0,5 s) után: ne az előző jelenet látsszon
    times = [a + (b - a) * i / (frames - 1) for i in range(frames)]
    cap = cv2.VideoCapture(str(crafts.existing(vid)))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    cols = frames if frames <= 4 else math.ceil(frames / 2)
    tw = width // cols
    tiles = []
    for t in times:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
        ok, f = cap.read()
        if not ok:
            continue
        f = cv2.resize(f, (tw, int(tw * f.shape[0] / f.shape[1])))
        cv2.rectangle(f, (0, 0), (tw, 26), (0, 0, 0), -1)
        cv2.putText(f, f"{sc['id']}  {t:.1f}s", (6, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(f)
    cap.release()
    if not tiles:
        raise ValueError("No frame could be read from the video.")
    while len(tiles) % cols:
        tiles.append(np.zeros_like(tiles[0]))
    grid = np.vstack([np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)])
    ok, buf = cv2.imencode(".jpg", grid, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return buf.tobytes(), {"scene": sc["id"], "start": row["start"], "end": row["end"], "frames_at": [round(t, 2) for t in times]}


async def render_scene(file_id: str, scene: Any, draft: bool = True) -> dict[str, Any]:
    """Csak egy jelenet renderelése (a saját kompozíciója) egy rövid MP4-be — ellenőrzéshez gyors, az egész videó
    újrarenderelése nélkül. A klip rejtett munkafájl; a teljes videó a render / edit útján készül."""
    pid = project_of(file_id)
    k, sc, _ = _scene_index(pid, scene)
    work = Path(tempfile.mkdtemp(prefix="video-", dir=str(temp_path("").parent)))
    try:
        proj = stage(pid, work)
        names = [c["name"] for c in _comps(json.loads(proj.read_text("utf-8")))]
        comp = next((n for n in names if n.startswith(f"Scene {k} – ")), None)
        if not comp:
            raise ValueError(f"Scene {sc['id']} isn't a separate part of this project (was it rearranged by hand?).")
        args = ["render", "--project", proj.name, "--comp", comp, "--out", "scene.mp4", "--format", "h264", "--json",
                "--bitrate", "2500" if draft else "8000", "--audio", "auto"]
        if draft:
            args += ["--resolution", "half", "--quality", "draft"]
        t0 = time.time()
        async with _render_gate():
            await _run(*args, cwd=work)
        out = work / "scene.mp4"
        if not out.exists():
            raise RuntimeError("The scene render produced no file")
        from app.tools import ios_safe
        await ios_safe.finalize(out)
        vid = new_file_id("mp4")
        shutil.move(str(out), output_path(vid))
        stem = re.sub(r"( \(edited [^)]*\))+$", "", Path(display_label(pid)).stem)
        write_meta(vid, label=f"{stem} – {sc['id']} clip.mp4", operation="video scene render", source_file_id=pid,
                   extra={"ephemeral": True})
        return {**_file_entry(vid), "scene": sc["id"], "headline": sc.get("headline"), "render_seconds": round(time.time() - t0, 1),
                "note": "Just this scene, rendered on its own (draft quality unless draft=false) — to check it quickly."}
    finally:
        shutil.rmtree(work, ignore_errors=True)
