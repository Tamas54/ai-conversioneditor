"""Komplex vagy rosszul felismert űrlap: több lépéses látó modell (settings.OCR_MODEL — DeepSeek V4.1 Flash).

Alapból mindig az ingyenes, helyi felismerés fut (detect.py). Ha az eredmény gyenge (nincs mező, sok felirat
mező nélkül, felirat nélküli mezők, nehezen olvasható szken) vagy az űrlap összetett, és a felhasználónak
fizetős csomagja van (Plus, Pro), jön a mély felismerés, oldalanként kreditért:

    1. lépés  a helyi eredmény számozott dobozokkal az oldalképre rajzolva → a modell javít (felirat, típus),
              töröl (nem mező), pótol (kimaradt mező, 0–1000 ezrelékes dobozzal); ha a helyi nem talált semmit,
              a modell az egész lapot olvassa (a meglévő forms.read_layout)
    2. lépés  ha az 1. sokat változtatott: még egy ellenőrző kör az új dobozokkal

MCP-n NEM fut: ott a kliens saját ügynöke dolgozik — megkapja az oldalképeket és a sémát, és a javított
mezőket `schema`-ként küldi vissza (a mi modellünket nem terheljük, a felhasználó a saját AI-ját használja).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from app.formkit import schema as S

log = logging.getLogger("aice.forms.deep")

CREDITS_PER_PAGE = int(os.getenv("DEEP_FORM_CREDITS_PER_PAGE", "1"))
MAX_PAGES = int(os.getenv("DEEP_FORM_MAX_PAGES", "10"))
PAID_PLANS = ("plus", "pro")
MCP_HINT = ("Automatic recognition looks incomplete. Look at the page images (the preview PNGs) with your own vision, "
            "then call again with schema = the corrected field list: fix labels/types, add missing fields with a box "
            "[x0, y0, x1, y1] in page units (PDF points or image pixels, origin top left), or {\"name\": …, \"remove\": true}.")
UPGRADE = "AI recognition for complex forms is part of the Plus and Pro plans — see Plans."


class DeepUnavailable(ValueError):
    pass


# ---------------------------------------------------------------- minőség

def quality(sch: dict[str, Any]) -> dict[str, Any]:
    f = sch.get("fields") or []
    reasons: list[str] = []
    if not f:
        reasons.append("no fields found")
    unnamed = sum(1 for x in f if re.fullmatch(r"Field \d+", x.get("label") or ""))
    if f and unnamed / len(f) > 0.3:
        reasons.append(f"{unnamed} of {len(f)} fields have no label")
    labels = sch.get("label_hints") or 0
    if labels >= 3 and len([x for x in f if x["type"] != "checkbox"]) < 0.6 * labels:
        reasons.append(f"{labels} printed labels but only {len(f)} fields")
    conf = sch.get("ocr_confidence")
    if conf is not None and conf < 70:
        reasons.append(f"hard-to-read scan (OCR confidence {conf:.0f}%)")
    complex_ = len(sch.get("pages") or []) > 3 or len(f) > 60
    return {"level": "low" if reasons else "good", "reasons": reasons, "complex": complex_}


async def eligibility() -> tuple[bool, str]:
    from app import billing, tenancy
    from app.config import settings
    if tenancy.client_makes_images.get():
        return False, "mcp"
    if not settings.SILICONFLOW_API_KEY:
        return False, "no_model"
    uid = tenancy.current_user_id()
    if tenancy.is_guest(uid):
        return False, "account"
    if await billing.plan_of(uid) not in PAID_PLANS:
        return False, "plan"
    return True, ""


def why_text(reason: str) -> str:
    return {"mcp": MCP_HINT, "plan": UPGRADE, "account": "AI recognition needs an account on the Plus or Pro plan.",
            "no_model": "AI recognition isn't configured on this server."}.get(reason, reason)


async def charge(pages: int) -> int:
    from app import db, tenancy
    uid = tenancy.current_user_id()
    if not db.enabled() or uid == tenancy.ADMIN_ID:
        return 0
    n = pages * CREDITS_PER_PAGE
    try:
        await db.credits_spend(uid, n, "AI form recognition", ref=f"{pages} pages")
    except db.InsufficientCredits:
        raise DeepUnavailable(f"AI form recognition needs {n} credits — top up in Plans.")
    return n


# ---------------------------------------------------------------- lépések

_REVIEW = """This is page {page} of a form. The numbered boxes are the fill-in fields our layout analysis found.
Check them against the printed form and reply with ONE JSON object:
{{"fix": [{{"n": 3, "label": "printed label of the field", "kind": "text|multiline|checkbox|date|signature"}}],
 "remove": [5, 7],
 "add": [{{"label": "…", "kind": "text|multiline|checkbox|date|signature", "box": [x0, y0, x1, y1]}}]}}
fix = wrong label or kind (keep the box). remove = not a fill-in field (a printed line, a table border, a filled-in
value). add = a blank, empty box, table cell to fill or tick box that has no number yet; box = the blank area in
integers 0–1000, thousandths of the page width and height, origin top left. Labels exactly as printed (keep the
language and accents). Empty lists when everything is right. Output only the JSON."""


def _overlay(img: Path, fields: list[dict[str, Any]], W: float, H: float, dst: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont
    from app.formkit.fill import FONT
    with Image.open(img) as im:
        im = im.convert("RGB")
    sx, sy = im.width / W, im.height / H
    dr = ImageDraw.Draw(im, "RGBA")
    fnt = ImageFont.truetype(FONT, max(12, im.width // 90))
    for i, f in enumerate(fields, 1):
        x0, y0, x1, y1 = f["box"]
        box = (x0 * sx, y0 * sy, x1 * sx, y1 * sy)
        dr.rectangle(box, outline=(220, 30, 30, 255), width=3, fill=(220, 30, 30, 30))
        dr.text((box[0] + 2, box[1] - fnt.size - 2), str(i), font=fnt, fill=(220, 30, 30, 255))
    im.save(dst, "PNG")


def _parse(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    a, b = text.find("{"), text.rfind("}")
    return json.loads(text[a:b + 1])


async def _ask(img: Path, prompt: str) -> dict[str, Any]:
    from app.config import settings
    from app.llm import vision
    msgs = [{"role": "user", "content": [{"type": "text", "text": prompt},
                                         {"type": "image_url", "image_url": {"url": vision.image_to_data_url(img, long_edge=2048)}}]}]
    last: Exception = RuntimeError("no answer")
    for model in (settings.OCR_MODEL, settings.LLM_MODEL_VISION_FALLBACK):
        try:
            return _parse(await vision._call(msgs, max_tokens=6000, model=model))
        except Exception as e:     # rossz JSON / nem válaszolt → tartalék modell
            last = e
            log.warning("deep form review %s: %s", model, e)
    raise RuntimeError(f"AI form recognition failed: {last}")


def _apply(fields: list[dict[str, Any]], ans: dict[str, Any], page: int, W: float, H: float) -> tuple[list[dict[str, Any]], int]:
    out = [dict(f) for f in fields]
    changes = 0
    for fx in ans.get("fix") or []:
        try:
            f = out[int(fx["n"]) - 1]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if fx.get("label"):
            f["label"] = S.clean_label(fx["label"])
        kind = fx.get("kind")
        typ = kind if kind in ("checkbox", "signature", "multiline", "date") else S.guess_type(f["label"])
        if typ != f["type"] or fx.get("label"):
            f.update(type=typ, check=S.field("x", "x", typ)["check"])
            changes += 1
    drop = {int(n) - 1 for n in (ans.get("remove") or []) if str(n).isdigit()}
    changes += len(drop)
    out = [f for i, f in enumerate(out) if i not in drop]
    for a in ans.get("add") or []:
        b = a.get("box") or []
        if len(b) != 4:
            continue
        box = [b[0] * W / 1000, b[1] * H / 1000, b[2] * W / 1000, b[3] * H / 1000]
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        label = S.clean_label(a.get("label") or "") or f"Field {len(out) + 1}"
        kind = a.get("kind")
        typ = kind if kind in ("checkbox", "signature", "multiline", "date") else S.guess_type(label)
        out.append(S.field(S.slug(label), label, typ, page=page, box=box,
                           anchor={"kind": "xy", "page": page, "rect": [round(v, 2) for v in box]}))
        changes += 1
    return out, changes


async def refine_page(img: Path, fields: list[dict[str, Any]], page: int, W: float, H: float, work: Path,
                      hint: Optional[str] = None) -> tuple[list[dict[str, Any]], int]:
    """Egy oldal: (ha nincs mit javítani: teljes olvasás) → javító kör → ha sokat változott, még egy."""
    from app.tools import forms as legacy
    steps = 0
    if not fields:
        lay = await legacy.read_layout(img, hint)
        steps += 1
        for it in lay.get("items", []):
            if it.get("type") != "field":
                continue
            b = it.get("box") or [0, 0, 0, 0]
            box = [b[0] * W / 1000, b[1] * H / 1000, b[2] * W / 1000, b[3] * H / 1000]
            label = S.clean_label(it.get("label") or it.get("name") or "")
            kind = it.get("kind", "text")
            typ = kind if kind in ("checkbox", "signature", "multiline", "date") else S.guess_type(label)
            fields.append(S.field(S.slug(it.get("name") or label), label or it.get("name", ""), typ, page=page, box=box,
                                  anchor={"kind": "xy", "page": page, "rect": [round(v, 2) for v in box]}))
    for round_ in range(2):
        ov = work / f"review-p{page}-{round_}.png"
        await asyncio.to_thread(_overlay, img, fields, W, H, ov)
        ans = await _ask(ov, _REVIEW.format(page=page))
        steps += 1
        fields, changes = _apply(fields, ans, page, W, H)
        if changes <= max(1, len(fields) // 5):     # kevés változás: kész
            break
    return fields, steps


async def recognize(sch: dict[str, Any], page_images: list[tuple[Path, float, float]], work: Path,
                    hint: Optional[str] = None) -> dict[str, Any]:
    """page_images: (kép, lapszélesség, lapmagasság a séma egységében). A séma mezőit oldalanként finomítja."""
    pages = page_images[:MAX_PAGES]
    credits = await charge(len(pages))
    by_page: dict[int, list[dict[str, Any]]] = {}
    for f in sch["fields"]:
        by_page.setdefault(f.get("page") or 1, []).append(f)
    gate = asyncio.Semaphore(4)

    async def one(i: int, item: tuple[Path, float, float]):
        img, W, H = item
        async with gate:
            return await refine_page(img, [dict(f) for f in by_page.get(i, [])], i, W, H, work, hint)
    results = await asyncio.gather(*(one(i, it) for i, it in enumerate(pages, 1)))
    fields = [f for fl, _ in results for f in fl]
    fields += [f for p, fl in by_page.items() if p > len(pages) for f in fl]       # a MAX_PAGES utániak maradnak
    out = dict(sch)
    out["fields"] = S.unique_names(fields)
    out["engine"] = "vision"
    out["deep"] = {"used": True, "pages": len(pages), "steps": sum(s for _, s in results), "credits": credits}
    return out
