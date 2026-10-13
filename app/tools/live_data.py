"""Élő adatok: egy szám a dokumentumban a forrásához kötve, egy gombbal frissíthető (10-14).

A horgony a dokumentumban egy könyvjelző (aice_<12 hex>) pontosan a szám körül — a Word és a WordCraft is megtartja
(mérve: a mező-kód és a könyvjelző-név túléli a WordCraft mentését, a dokumentum-változó és a customXml NEM). A kötés
(forrás, számformátum, utolsó forrásérték) Postgresben (data_bindings), a könyvjelző nevével kulcsolva — így a kötés a
dokumentummal utazik: minden változat, másolat, Wordben szerkesztett és visszatöltött példány ugyanazt találja meg.

Frissítéskor (refresh):
  • a forrás legújabb értéke a dokumentum saját formátumában (tizedesjel, tizedesek, ezres tagolás) — követett cserével;
  • a modell CSAK a szám körüli szavakat igazítja (időszak, irány, a számhoz tapadó rag) — követett cserével;
    számot nem ír (a cserében csak a régi szöveg, az új érték és az időszak számjegyei állhatnak);
  • ha egy következtetés a bekezdésben az új számmal már nem biztos, megjegyzés kerül mellé — a mondatot nem írja át.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from app.config import settings
from app.tools import datasources as ds
from app.tools.docx_marks import Docx, RevisionError

log = logging.getLogger("aice.live_data")

PREFIX = "aice_"
_NUM = re.compile(r"(?<![\w.,])[-−]?\d{1,3}(?:[   .]\d{3})+(?:,\d+)?(?![\w])|(?<![\w.,])[-−]?\d+(?:[.,]\d+)?(?![\w])")


def _parse(tok: str, ref: Optional[float]) -> tuple[float, dict[str, Any]]:
    """Egy számjel értéke és formátuma. A kétértelmű „1,234” / „1.234” a forrásértékhez közelebbi olvasat szerint."""
    t = tok.replace("−", "-").replace(" ", " ").replace(" ", " ")
    cands = []
    if " " in t:
        g = t.replace(" ", "")
        cands.append((g.replace(",", "."), {"dec": ",", "group": " "}))
    elif "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        grp = "." if dec == "," else ","
        cands.append((t.replace(grp, "").replace(dec, "."), {"dec": dec, "group": grp}))
    elif "," in t:
        cands.append((t.replace(",", "."), {"dec": ",", "group": ""}))
        if re.fullmatch(r"-?\d{1,3}(,\d{3})+", t):
            cands.append((t.replace(",", ""), {"dec": ".", "group": ","}))
    elif "." in t:
        cands.append((t, {"dec": ".", "group": ""}))
        if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
            cands.append((t.replace(".", ""), {"dec": ",", "group": "."}))
    else:
        cands.append((t, {"dec": ",", "group": ""}))
    best = None
    for raw, fmt in cands:
        try:
            v = float(raw)
        except ValueError:
            continue
        frac = raw.split(".", 1)[1] if "." in raw else ""
        fmt = {**fmt, "decimals": len(frac)}
        score = abs(v - ref) / max(abs(ref), 1e-9) if ref is not None else 0
        if best is None or score < best[0]:
            best = (score, v, fmt)
    if best is None:
        raise ValueError(f"not a number: {tok!r}")
    return best[1], best[2]


def fmt_number(v: float, fmt: dict[str, Any]) -> str:
    n = int(fmt.get("decimals") or 0)
    s = f"{abs(v):,.{n}f}"                       # 1,234.56
    ip, _, fp = s.partition(".")
    ip = ip.replace(",", fmt.get("group") or "")
    out = ip + ((fmt.get("dec") or ",") + fp if n else "")
    return ("-" if v < 0 else "") + out


def _pick_number(text: str, ref: Optional[float]) -> Optional[tuple[int, int, float, dict[str, Any]]]:
    """Az idézet melyik száma a kötendő: a forrásértékhez legközelebbi (az évszám-szerű egész csak végső esetben)."""
    best = None
    for m in _NUM.finditer(text):
        try:
            v, fmt = _parse(m.group(0), ref)
        except ValueError:
            continue
        yearish = fmt["decimals"] == 0 and 1900 <= abs(v) <= 2100
        score = (1 if yearish else 0, abs(v - ref) / max(abs(ref), 1e-9) if ref is not None else 0)
        if best is None or score < best[0]:
            best = (score, m.start(), m.end(), v, fmt)
    return best and best[1:]


def _row_public(r: dict[str, Any], text: Optional[str]) -> dict[str, Any]:
    fmt = r.get("fmt") or {}
    disp = fmt_number(r["value"], fmt) if r.get("value") is not None else r.get("display")
    return {"id": r["id"], "label": r.get("label"), "text": text, "display": disp, "period": r.get("period"),
            "source_name": r.get("source_name"), "source_url": r.get("source_url"), "checked_at": r.get("checked_at"),
            "spec": r.get("spec"), "up_to_date": text is not None and disp == text}


async def listing(file_id: str) -> dict[str, Any]:
    from app import db
    from app.tools import crafts
    src = crafts.existing(file_id)
    if src.suffix.lower() != ".docx":
        return {"bindings": []}
    d = await asyncio.to_thread(Docx, src)
    bms = d.bookmarks(PREFIX)
    rows = await db.bindings_get(list(bms))
    texts = d.texts()
    out = [_row_public(rows[k], texts[i][a:b]) for k, (i, a, b) in bms.items() if k in rows]
    return {"bindings": out}


def _new_version(file_id: str, out_id: str, op: str, instruction: str, summary: str) -> str:
    from app.storage import display_label, write_meta
    from app.tools.revise import history
    stem = Path(display_label(file_id)).stem.split(" (")[0]
    label = f"{stem} (v{len(history(file_id)) + 1}).docx"
    write_meta(out_id, source_file_id=file_id, operation=op, label=label, extra={"instruction": instruction, "summary": summary})
    return label


async def bind(file_id: str, quote: str, spec: dict[str, Any], paragraph: Optional[int] = None,
               label: Optional[str] = None) -> dict[str, Any]:
    """A szám (az idézetben a forrásértékhez legközelebbi) könyvjelzőt kap, a kötés a Postgresbe — új változat."""
    from app import db
    from app.storage import output_path, public_url
    from app.storage.files import new_file_id
    from app.tools import crafts
    if not db.enabled():
        raise ValueError("Live data needs the database — it isn't set up on this server.")
    spec = {k: v for k, v in (spec or {}).items() if k in ("kind", "country", "indicator", "currency")}
    got = await ds.fetch(spec)                         # a forrás él-e (és a kétértelmű számformához a viszonyítás)
    src = crafts.existing(file_id)
    if src.suffix.lower() != ".docx":
        raise ValueError("Live numbers work in Word documents (.docx).")
    bid = PREFIX + uuid.uuid4().hex[:12]

    def work():
        d = Docx(src)
        hit = d.find(quote, paragraph)
        if hit is None:
            raise ValueError("That text isn't in this version of the document any more.")
        i, a, b = hit
        text = d.texts()[i]
        num = _pick_number(text[a:b], got["value"])
        if num is None:
            raise ValueError("There's no number in that text to keep up to date.")
        s, e, v, fmt = num
        inside = [k for k, (pi, x, y) in d.bookmarks(PREFIX).items() if pi == i and x < a + e and a + s < y]
        if inside:
            raise ValueError("That number is already linked to its source.")
        d.bookmark(d.paragraphs()[i], a + s, a + e, bid)
        out = new_file_id("docx")
        d.save(output_path(out))
        return out, text[a + s:a + e], v, fmt
    out_id, shown, v, fmt = await asyncio.to_thread(work)
    lab = label or ds.describe(spec).replace(", latest", "")
    await db.binding_put({"id": bid, "label": lab, "spec": spec, "fmt": fmt, "value": got["value"], "display": fmt_number(got["value"], fmt),
                          "period": got.get("period"), "source_name": got.get("source_name"), "source_url": got.get("source_url"),
                          "file_id": out_id})
    newest = fmt_number(got["value"], fmt)
    summary = (f"“{shown}” is now linked to {got.get('source_name') or 'its source'}"
               + (f" — the newest figure is {newest} ({got.get('period')}); Refresh all puts it in." if newest != shown else " and is up to date."))
    label_v = _new_version(file_id, out_id, "live link", f"Keep “{shown}” up to date ({lab})", summary)
    return {"file_id": out_id, "url": public_url(out_id), "label": label_v, "binding_id": bid, "summary": summary,
            "text": shown, "newest": newest, "period": got.get("period")}


_WORDING_PROMPT = """A figure in a document was updated from its source. Adjust ONLY the words around the figure that must
change so the paragraph stays true and grammatical:
  • the period it refers to ("in September" → "in October", "2025" → "2026" when the source's period says so),
  • the direction or size words when the new figure reverses or changes them ("rose" → "fell", "above" → "below"),
  • a grammatical ending attached to the figure (Hungarian: the case ending after a number may change with it).
Never write any other number, never rephrase, never touch anything else. Each edit's "old" is copied exactly from the
paragraph and must not include the figure itself. If nothing needs to change, give no edits.
Also: if a conclusion or claim in the paragraph may no longer hold with the new figure, say so in one sentence in the
document's language ("conclusion"); otherwise null.
Paragraph: "{para}"
The figure: was "{old}"{old_p}, now "{new}" ({new_p}, {source}).
Reply with JSON only: {{"edits": [{{"old": "…", "new": "…"}}], "conclusion": "…" or null}}"""


async def refresh(file_id: str, progress=None) -> dict[str, Any]:
    """Minden kötött szám a forrás legújabb értékére + a szöveg igazítása — követett változásként, új változatban.
    Ha minden naprakész: nincs új változat (file_id None), csak az ellenőrzés ideje frissül."""
    from app import db
    from app.storage import output_path, public_url
    from app.storage.files import new_file_id
    from app.tools import crafts
    from app.tools.content_check import _DIGITS, _ask, minimal_change
    say = progress or (lambda *_: None)
    src = crafts.existing(file_id)
    d = await asyncio.to_thread(Docx, src, f"{settings.PRODUCT_NAME} data")
    bms = d.bookmarks(PREFIX)
    rows = await db.bindings_get(list(bms))
    if not rows:
        return {"file_id": None, "summary": "No numbers in this document are linked to a source yet.", "bindings": []}
    say(0, len(rows), "getting the newest figures")
    texts = d.texts()
    got = await asyncio.gather(*(ds.fetch(rows[k]["spec"]) for k in rows), return_exceptions=True)
    changes, failed = [], []
    for (k, r), g in zip(rows.items(), got):
        i, a, b = bms[k]
        if isinstance(g, Exception):
            failed.append({"id": k, "label": r.get("label"), "why": str(g)})
            continue
        new = fmt_number(g["value"], r["fmt"])
        prev = r.get("period")
        r.update(value=g["value"], display=new, period=g.get("period"), source_name=g.get("source_name"), source_url=g.get("source_url"))
        if new != texts[i][a:b]:
            changes.append((k, r, i, texts[i][a:b], new, {**g, "_prev_period": prev}))
    if not changes:
        for k, r in rows.items():
            if not any(f["id"] == k for f in failed):
                await db.binding_put({**r, "file_id": file_id})
        n = len(rows) - len(failed)
        return {"file_id": None, "bindings": (await listing(file_id))["bindings"], "failed": failed,
                "summary": f"All {n} linked number{'s are' if n != 1 else ' is'} up to date." + (
                    f" {len(failed)} source{'s' if len(failed) != 1 else ''} didn't answer." if failed else "")}
    say(0, len(changes), "adjusting the wording")

    async def wording(c):
        k, r, i, old, new, g = c
        prev = g.get("_prev_period") if g.get("_prev_period") != g.get("period") else None
        try:
            return await _ask(_WORDING_PROMPT.format(para=" ".join(texts[i].split())[:3000], old=old, new=new,
                                                     old_p=f" ({prev})" if prev else "", new_p=g.get("period") or "latest",
                                                     source=g.get("source_name") or "source"))
        except Exception as e:
            log.warning("szöveg-igazítás (%s): %s", k, e)
            return {"edits": [], "conclusion": None}
    plans = await asyncio.gather(*(wording(c) for c in changes))

    def work():
        notes = []
        for (k, r, i, old, new, g), plan in zip(changes, plans):
            p = d.paragraphs()[i]
            a, b = d.bookmarks(PREFIX)[k][1:]
            d.replace(p, a, b, new)                          # a szám: törlés + beszúrás, a könyvjelzőn belül
            pos = d.bookmarks(PREFIX)[k][1]
            allowed = set(_DIGITS.findall(old + " " + new + " " + str(g.get("period") or "")))
            for ed in (plan.get("edits") or [])[:4]:
                o, n_ = str(ed.get("old") or ""), str(ed.get("new") or "")
                if not o or o == n_ or old in o or new in o:
                    continue
                if not set(_DIGITS.findall(n_)) <= (allowed | set(_DIGITS.findall(o))):
                    log.info("igazítás elvetve (új szám): %s → %s", o, n_)
                    continue
                t = d.text(p)
                hits = [m.start() for m in re.finditer(re.escape(o), t)]
                if not hits:
                    continue
                at = min(hits, key=lambda h: abs(h - pos))         # a számhoz legközelebbi előfordulás
                bm = d.bookmarks(PREFIX)[k]
                if at < bm[2] and at + len(o) > bm[1]:
                    continue                                       # a számba nyúlna
                qa, qb, rep = minimal_change(o, n_)
                try:
                    d.replace(p, at + qa, at + qb, rep)
                except RevisionError as e:
                    log.info("igazítás kihagyva: %s", e)
            if plan.get("conclusion"):
                bm = d.bookmarks(PREFIX)[k]
                try:
                    d.comment(p, bm[1], bm[2], f"{r.get('label')}: {old} → {new} ({g.get('period') or 'latest'}, "
                                                f"{g.get('source_name') or 'source'}). {plan['conclusion']}")
                    notes.append(k)
                except RevisionError:
                    pass
        out = new_file_id("docx")
        d.save(output_path(out))
        return out, notes
    out_id, notes = await asyncio.to_thread(work)
    for k, r in rows.items():
        if not any(f["id"] == k for f in failed):
            await db.binding_put({**r, "file_id": out_id})
    parts = [f"{c[1].get('label')}: {c[3]} → {c[4]}" for c in changes]
    summary = (f"Updated {len(changes)} number{'s' if len(changes) != 1 else ''} as tracked changes — " + "; ".join(parts) + "."
               + (f" {len(notes)} comment{'s' if len(notes) != 1 else ''} where a conclusion may no longer hold." if notes else "")
               + (f" {len(failed)} source{'s' if len(failed) != 1 else ''} didn't answer." if failed else ""))
    label = _new_version(file_id, out_id, "live data", "Refresh the linked numbers", summary)
    return {"file_id": out_id, "url": public_url(out_id), "label": label, "summary": summary,
            "changed": [{"id": c[0], "label": c[1].get("label"), "old": c[3], "new": c[4], "period": c[5].get("period")} for c in changes],
            "failed": failed}
