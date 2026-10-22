"""Közösségi szövegek egy videóhoz (10-22, a MoneyPrinterTurbo ötletlistájából): cím, leírás, hashtagek platformonként.

ELŐBB ADAT → DETERMINISZTIKUS → AI:
  1. adat: amit a videó mond — a projekt forgatókönyve (cím, jelenetek, idők), az overlay feliratai / fejezetrétegei,
     nyers felvételnél az átirata (a közös gyorsítótárból);
  2. determinisztikus: a fejezetek (YouTube-szabály: 00:00-tól, legalább 3, mindegyik ≥ 10 s), a kulcsszavak
     (gyakoriság, töltelékszavak nélkül), a platformok korlátai, a kép-/felvételforrások sora;
  3. AI (egy Studio AI-lépés): a szövegek a videó nyelvén, CSAK a videó tényeiből — a számokat a check ellenőrzi.
     Ugyanarra az anyagra a válasz a content_checks táblából jön (AMIT EGYSZER MEGCSINÁLTUNK, NE ÚJRA).
Ha az AI nem elérhető, a determinisztikus változat megy (cím, első mondatok, kulcsszó-hashtagek) — a check megmondja.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections import Counter
from typing import Any, Callable, Optional

from app.config import settings

log = logging.getLogger("aice.social")

VER = "social-v2"
# platform → (cím hossza | None, szöveg hossza, ajánlott hashtagek száma) — a platformok 2026-os korlátai
PLATFORMS: dict[str, tuple[Optional[int], int, int]] = {
    "youtube": (100, 5000, 5), "tiktok": (None, 2200, 5), "instagram": (None, 2200, 5),
    "facebook": (None, 2000, 3), "linkedin": (None, 3000, 3), "x": (None, 280, 2),
}
DEFAULT_PLATFORMS = ("youtube", "tiktok", "instagram")
_ALIASES = {"yt": "youtube", "shorts": "youtube", "reels": "instagram", "ig": "instagram", "insta": "instagram",
            "twitter": "x", "fb": "facebook", "li": "linkedin"}


def platforms_of(raw: Any) -> list[str]:
    if raw in (None, "", []):
        return list(DEFAULT_PLATFORMS)
    items = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw))
    out = []
    for p in items:
        k = _ALIASES.get(str(p).strip().lower(), str(p).strip().lower())
        if k in ("all", "every"):
            return list(PLATFORMS)
        if k and k not in PLATFORMS:
            raise ValueError(f"platforms: {' | '.join(PLATFORMS)} (or \"all\") — not {p!r}.")
        if k and k not in out:
            out.append(k)
    return out or list(DEFAULT_PLATFORMS)


def _tc(t: float) -> str:
    t = int(max(0.0, t))
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def chapters(marks: list[tuple[float, str]], total: float) -> tuple[list[dict[str, Any]], Optional[str]]:
    """YouTube-fejezetek: az első 00:00-kor, legalább 3, mindegyik legalább 10 s. → (fejezetek, miért nincs)."""
    rows = sorted((max(0.0, float(t)), " ".join(str(x).split())[:80]) for t, x in marks if str(x).strip())
    if rows and rows[0][0] > 0.5:
        rows.insert(0, (0.0, "Intro"))
    elif rows:
        rows[0] = (0.0, rows[0][1])
    merged: list[tuple[float, str]] = []
    for t, x in rows:                                   # a 10 s-nál rövidebb fejezet az előzőbe olvad
        if merged and t - merged[-1][0] < 10:
            if len(merged) == 1:                        # …de a rövid címkártya helyett az első valódi fejezet neve áll 00:00-kor
                merged[0] = (0.0, x)
            continue
        merged.append((t, x))
    if merged and total - merged[-1][0] < 10 and len(merged) > 1:
        merged.pop()
    if len(merged) < 3:
        return [], ("no chapters: YouTube needs at least 3, each 10 s or longer" if rows else None)
    return [{"at": _tc(t), "start": round(t, 2), "title": x} for t, x in merged], None


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_][\w'’-]*", text or "")


def keywords(text: str, title: str, lang: Optional[str], n: int = 12) -> list[str]:
    """A videó kulcsszavai: tartalmas szavak (≥ 4 betű, nem töltelékszó) gyakoriság szerint, a cím szavai előnyben."""
    from app.tools import narration
    stop = set().union(*narration._STOPSETS.values()) if not lang else narration._STOPSETS.get(lang, set()) | narration._STOPSETS["en"]
    cnt: Counter = Counter()
    shown: dict[str, str] = {}
    for weight, src in ((3, title), (1, text)):
        for w in _words(src):
            k = w.lower().strip("'’-")
            if len(k) < 4 or k in stop:
                continue
            cnt[k] += weight
            if k not in shown or (w[:1].isupper() and not shown[k][:1].isupper()):
                shown[k] = w.strip("'’-")
    return [shown[k] for k, _ in cnt.most_common(n)]


def hashtag(word: str) -> str:
    """Egy kifejezés hashtagként: szóköz és írásjel nélkül, a szavak nagybetűvel (#KossuthTer — az ékezet marad)."""
    parts = re.findall(r"[^\W_]+", str(word or "").lstrip("#"))
    if not parts:
        return ""
    tag = parts[0] if len(parts) == 1 else "".join(p[:1].upper() + p[1:] for p in parts)
    return "#" + tag[:40]


def _tags(raw: Any, limit: int) -> list[str]:
    out: list[str] = []
    for t in raw if isinstance(raw, list) else re.split(r"[\s,]+", str(raw or "")):
        h = hashtag(t)
        if len(h) > 2 and h.lower() not in {x.lower() for x in out}:
            out.append(h)
    return out[:limit]


def _cut(s: Any, n: Optional[int]) -> str:
    s = re.sub(r"[ \t]+", " ", str(s or "")).strip()
    if not n or len(s) <= n:
        return s
    return s[:n - 1].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _nums(text: str) -> set[str]:
    """A szöveg számai összevethető alakban (az ezres tagolás és a tizedesvessző nem számít)."""
    out = set()
    for m in _NUM.findall(re.sub(r"(?<=\d)[ \u00a0\u202f.](?=\d{3}\b)", "", text or "")):
        v = m.replace(",", ".")
        out.add(v.rstrip("0").rstrip(".") if "." in v else v)
    return out


# ---------- 1. adat: amit a videó mond ----------

async def material(file_id: str, language: Optional[str] = None, step: Callable[..., None] = lambda *a, **k: None) -> dict[str, Any]:
    """{kind, title, text, marks [(mp, felirat)], duration, credits, people} — a projekt forgatókönyvéből, az overlay
    feliratából, vagy (nyers felvételnél, illetve felirat nélküli overlaynél) az átiratból."""
    from app.storage import display_label, read_meta
    from app.storage.naming import root_name
    from app.tools import crafts, video
    p = crafts.existing(file_id)
    ext = p.suffix.lstrip(".").lower()
    pid = None
    if ext == "ecproj":
        pid = file_id
    else:
        src = (read_meta(file_id) or {}).get("source_file_id")
        if src and str(src).lower().endswith(".ecproj"):
            pid = src
    if pid:
        meta = read_meta(pid) or {}
        spec = meta.get("spec") or {}
        story = spec.get("story") or {}
        credits = meta.get("credit") or ""
        if spec.get("kind") == "overlay":
            subs = (spec.get("opts") or {}).get("subtitles") or []
            layers = story.get("layers") or []
            marks = [(float(ly.get("at") or 0), ly.get("text") or ly.get("title") or "") for ly in layers if ly.get("type") == "chapter"]
            people = [" — ".join(x for x in (ly.get("name"), ly.get("role")) if x) for ly in layers if ly.get("type") == "lower_third"]
            text = " ".join(c.get("text") or "" for c in subs)
            if not text:
                text = await _heard(pid, language, step)
            return {"kind": "overlay", "title": story.get("title") or root_name(display_label(pid), "Video"), "text": text,
                    "marks": marks, "duration": float((spec.get("base") or {}).get("duration") or 0), "credits": credits,
                    "people": [x for x in people if x]}
        if spec:
            from app.tools.narration import strip_pauses
            tl = {x["id"]: x for x in spec.get("timeline") or [] if x.get("id")}
            scenes = story.get("scenes") or []
            marks = [(0.0, story.get("title") or "")] + [(tl[sc["id"]]["start"], sc.get("headline") or "") for sc in scenes
                                                        if sc.get("id") in tl and sc.get("headline")]
            end = story.get("end") or {}
            text = " ".join(strip_pauses(" ".join(x for x in (sc.get("headline"), sc.get("caption")) if x)) for sc in scenes)
            text += " " + " ".join(x for x in (end.get("title"), end.get("subtitle")) if x)
            dur = max([x.get("end") or 0 for x in spec.get("timeline") or []] + [0])
            return {"kind": "made", "title": story.get("title") or "", "text": text.strip(), "marks": marks,
                    "duration": float(dur), "credits": credits, "people": []}
    if ext not in video.MOVIE_EXTS | video.AUDIO_EXTS:
        raise ValueError(f"social: give a video (or its project) or a sound file, not .{ext}.")
    from app.tools import mediaprobe
    dur = float((await mediaprobe.info(p)).get("duration") or 0)
    return {"kind": "recording", "title": root_name(display_label(file_id), "Video"), "text": await _heard(file_id, language, step),
            "marks": [], "duration": dur, "credits": (read_meta(file_id) or {}).get("credit") or "", "people": []}


async def _heard(file_id: str, language: Optional[str], step: Callable[..., None]) -> str:
    """A felvétel (overlay-projektnél az alapsávja) átirata — a közös gyorsítótárból, ha már hallottuk; ismert szövegnél
    (avatar, forgatókönyv) annak szavaival."""
    from app.tools import transcript, video
    step(0, 3, "listening (transcript)")
    tr = await transcript.transcribe(video.sound_source(file_id), video.transcript_lang(language), **video.with_script(file_id, {}))
    return " ".join(s["text"] for s in tr.get("sentences") or [])


# ---------- 3. AI: a szövegek ----------

_SYSTEM = """You write the posts that go with a short video on social media. You get what the video says (its title,
its words, the people in it) and the platforms. Write in the video's language (given as a code).
Rules: use ONLY facts from the video — never add numbers, names, claims, promises or prices that aren't in it; no
clickbait that the video doesn't deliver; a strong first line is welcome. Hashtags: specific to the topic (people,
places, the subject), no empty ones like #fyp #viral #trending. Follow each platform's limits.
Each platform gets its OWN text in its own style: youtube — a searchable title (the main subject early) and a 2–4
sentence description; tiktok — one punchy line (about 150 characters show); instagram — a hook line, then 2–3 short
lines; facebook — 2–3 friendly sentences; linkedin — 3–5 sentences, professional, the takeaway first; x — one line.
Reply with ONE JSON object: {"<platform>": {"title": "only for youtube", "text": "the description / caption",
"hashtags": ["#Tag", …]}, …} for exactly the platforms asked."""


async def _ai_posts(mat: dict[str, Any], plats: list[str], lang: str, notes: str, force: bool) -> dict[str, Any]:
    from app import db
    from app.llm.siliconflow import _call_model, _strip_json_fences
    limits = "; ".join(f"{p}: " + (f"title ≤ {PLATFORMS[p][0]}, " if PLATFORMS[p][0] else "")
                       + f"text ≤ {PLATFORMS[p][1]} characters, {PLATFORMS[p][2]} hashtags" for p in plats)
    user = (f"Language: {lang}\nPlatforms: {', '.join(plats)} ({limits})\nVideo title: {mat['title']}\n"
            f"Length: {round(mat['duration'])} s\n" + (f"People in it: {', '.join(mat['people'])}\n" if mat["people"] else "")
            + (f"The person's notes (links, call to action — use them): {notes}\n" if notes else "")
            + f"\nWhat the video says:\n{mat['text'][:12000]}")
    key = hashlib.sha256(json.dumps([VER, user], ensure_ascii=False).encode()).hexdigest()
    if not force:
        try:
            hit = await db.check_get("social", key, VER)
        except Exception:
            hit = None
        if isinstance(hit, dict):
            return hit
    last: Exception = RuntimeError("no model")
    for m in (settings.LLM_MODEL_PRIMARY, settings.LLM_MODEL_FALLBACK):
        try:
            raw = await _call_model(m, [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}], thinking=False)
            data = json.loads(_strip_json_fences(raw))
            if isinstance(data, dict):
                break
        except Exception as e:
            from app import billing
            if isinstance(e, billing.AIStepsExhausted):
                raise
            last = e
            log.warning("közösségi szöveg %s modellel nem sikerült: %s", m, e)
    else:
        raise last
    try:
        await db.check_put("social", key, VER, data)
    except Exception:
        log.warning("közösségi szöveg gyorsítótár írása nem sikerült", exc_info=True)
    return data


def _plain(mat: dict[str, Any], plats: list[str], tags: list[str]) -> dict[str, Any]:
    """AI nélkül: a cím, az első mondatok, a kulcsszavak hashtagként."""
    sents = re.split(r"(?<=[.!?…])\s+", mat["text"])
    out = {}
    for p in plats:
        t_lim, x_lim, n = PLATFORMS[p]
        out[p] = {"title": mat["title"] if t_lim else None, "text": " ".join(sents[:3 if x_lim > 300 else 1]), "hashtags": tags[:n]}
    return out


async def posts(file_id: str, *, platforms: Any = None, language: Optional[str] = None, notes: str = "",
                force: bool = False, progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    """A videó közösségi szövegei platformonként + a fejezetek, a kulcsszavak és egy .md fájl (másoláshoz)."""
    from app import billing
    from app.storage import new_file_id, output_path, public_url, write_meta
    from app.storage.naming import result_stem
    from app.tools import narration
    step = progress or (lambda *a, **k: None)
    plats = platforms_of(platforms)
    mat = await material(file_id, language, step)
    if len(_words(mat["text"])) < 3 and not mat["title"]:
        raise ValueError("social: nothing is said or written in this video to write about — give it a title or captions.")
    lang = narration.lang_code(language) or narration.detect(f"{mat['title']} {mat['text']}") or "en"
    chaps, no_chap = chapters(mat["marks"], mat["duration"])
    kws = keywords(mat["text"], mat["title"], lang)
    tags = _tags(kws, 8)
    notes = " ".join(str(notes or "").split())[:1000]
    findings: list[str] = []
    verified: list[str] = []
    used = [0, 0]
    step(1, 3, "writing the posts")
    outer = billing._steps.get()
    with billing.count_steps() as used:
        try:
            data = await _ai_posts(mat, plats, lang, notes, force)
            by = "ai"
        except billing.AIStepsExhausted as e:
            data, by = _plain(mat, plats, tags), "plain"
            findings.append(f"The AI writing didn't run: {e} — these are plain drafts from the title and the first sentences.")
        except Exception as e:
            log.warning("közösségi szöveg AI nélkül: %s", e)
            data, by = _plain(mat, plats, tags), "plain"
            findings.append("The AI writing isn't available right now — these are plain drafts from the title and the first "
                            "sentences; ask again later for better ones.")
    if used[0] and outer is not None:
        outer[0] += used[0]
        outer[1] += used[1]
    have = _nums(f"{mat['title']} {mat['text']} {notes}")
    out: dict[str, Any] = {}
    for p in plats:
        d = data.get(p) if isinstance(data.get(p), dict) else {}
        t_lim, x_lim, n = PLATFORMS[p]
        title = _cut(d.get("title") or (mat["title"] if t_lim else ""), t_lim) if t_lim else None
        htags = _tags(d.get("hashtags"), n) or tags[:n]
        body = str(d.get("text") or d.get("caption") or d.get("description") or "").strip()
        extra = ""
        if p == "youtube" and chaps:
            extra += "\n\n" + "\n".join(f"{c['at']} {c['title']}" for c in chaps)
        if p in ("youtube", "facebook", "linkedin") and mat["credits"]:
            extra += f"\n\n{mat['credits']}"
        tag_line = " ".join(htags)
        if p == "x":                                    # egy sor: a szöveg, utána a hashtagek, együtt ≤ 280
            text = _cut(body, x_lim - len(tag_line) - 1) + (" " + tag_line if tag_line else "")
        else:
            text = _cut(body, max(40, x_lim - len(extra) - len(tag_line) - 2)) + extra + ("\n\n" + tag_line if tag_line else "")
        new_nums = sorted(_nums(f"{title or ''} {body}") - have)
        if new_nums:
            findings.append(f"{p}: the text has numbers that aren't in the video ({', '.join(new_nums[:5])}) — check them.")
        out[p] = {**({"title": title} if t_lim else {}), "text": text.strip(), "hashtags": htags, "length": len(text.strip())}
    verified.append(f"Posts for {', '.join(plats)} in {lang}, " + ("written by the Studio AI from what the video says"
                                                                  if by == "ai" else "plain drafts") + ".")
    if chaps:
        verified.append(f"{len(chaps)} YouTube chapters from the video's " + ("scenes." if mat["kind"] == "made" else "chapter layers."))
    elif no_chap and "youtube" in plats:
        verified.append(f"YouTube: {no_chap}.")
    if mat["credits"]:
        verified.append("The picture / footage credits are in the YouTube, Facebook and LinkedIn texts.")
    stem = result_stem(file_id, "social posts")
    md = f"# {mat['title']}\n\n" + "\n\n".join(
        f"## {p}\n\n" + (f"**Title:** {v['title']}\n\n" if v.get("title") else "") + v["text"] for p, v in out.items()) + "\n"
    fid = new_file_id("md")
    output_path(fid).write_text(md, "utf-8")
    write_meta(fid, label=f"{stem}.md", source_file_id=file_id, operation="social posts")
    res = {"language": lang, "title": mat["title"], "platforms": out, "chapters": chaps, "keywords": kws,
           "file_id": fid, "name": f"{stem}.md", "url": public_url(fid),
           "check": {"status": "needs_check" if findings else "ok", "findings": findings, "verified": verified}}
    if used[0]:
        res["studio_ai"] = {"steps": used[0], "credits": used[1]}
    return res
