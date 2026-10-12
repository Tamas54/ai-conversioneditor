"""Tömeges személyre szabás: sablon + táblázat (CSV/XLSX) → N darab (oklevél, névjegy, meghívó, termékadatlap…).

Két forrás:
  • design-sablon (design.FNS: certificate, card, invitation, poster, flyer, social…): a data mezőiben {oszlop}
    helyőrzők; ha egy fő mező üres, és van hozzá illő oszlop (név → recipient/name), magától kitöltődik.
    Soronként egy oldal; 40 oldalanként EGY VectorCraft-futás (a parancssor hossza miatt), a részek PDF-jei egybe.
  • a felhasználó saját VectorCraft-terve (.vectorcraft / .svg) {mező} helyőrzőkkel a szövegekben: soronként
    edit.findReplace + PDF-export (párhuzamosan).
Kimenet: EGY közös, nyomtatható PDF (soronként egy oldal) + ZIP egyenként (a fájlnév az első névszerű oszlopból),
és sablon-útnál a szerkeszthető .vectorcraft is (minden példány egy rajztábla), ha nem túl sok.
A pdf_form CSV-útjának mintájára (forms.read_rows ugyanaz).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("aice.design_mass")
PH = re.compile(r"\{([^{}\n]{1,48})\}")
LIMIT = 500
CHUNK = 40
NAMEISH = ("name", "nev", "név", "recipient", "full name", "fullname", "participant", "guest", "vendeg", "vendég", "student",
           "employee", "attendee", "title", "product", "termek", "termék")
# sablonmező → az oszlopnevek, amelyekből magától kitöltődik, ha a mezőt üresen hagyják
AUTO = {
    "recipient": ("name", "full name", "fullname", "recipient", "participant", "guest", "student", "attendee", "nev", "név", "vendeg"),
    "name": ("name", "full name", "fullname", "nev", "név", "employee"),
    "role": ("role", "title", "position", "job title", "beosztas", "beosztás"),
    "organization": ("organization", "organisation", "company", "ceg", "cég", "szervezet"),
    "phone": ("phone", "mobile", "tel", "telefon"), "email": ("email", "e-mail", "mail"), "web": ("web", "website", "url"),
    "date": ("date", "datum", "dátum"), "place": ("place", "location", "venue", "helyszin", "helyszín"),
    "title": ("title", "product", "termek", "termék", "headline"), "subtitle": ("subtitle", "tagline"),
    "body": ("body", "description", "leiras", "leírás", "text"), "text": ("text", "for", "reason", "course", "achievement"),
    "image": ("image", "photo", "picture", "kep", "kép", "image_url", "photo_url"),
}


def _key(s: str) -> str:
    t = unicodedata.normalize("NFKD", str(s).strip().lower())
    return "".join(c for c in t if not unicodedata.combining(c)).replace("_", " ")


def columns_of(rows: list[dict[str, Any]]) -> list[str]:
    return [c for c in (rows[0].keys() if rows else []) if c and str(c).strip()]


def fill_text(text: str, row: dict[str, Any]) -> str:
    """{oszlop} → az érték (kis/nagybetű, ékezet, _ nem számít); ismeretlen helyőrző üresen marad."""
    look = {_key(k): ("" if v is None else str(v).strip()) for k, v in row.items() if k}
    return PH.sub(lambda m: look.get(_key(m.group(1)), ""), text)


def _fill(data: Any, row: dict[str, Any]) -> Any:
    if isinstance(data, str):
        return fill_text(data, row)
    if isinstance(data, list):
        return [_fill(x, row) for x in data]
    if isinstance(data, dict):
        return {k: _fill(v, row) for k, v in data.items()}
    return data


def _auto(template: str, data: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    look = {_key(k): ("" if v is None else str(v).strip()) for k, v in row.items() if k}
    out = dict(data)
    for field, names in AUTO.items():
        if field in ("title",) and template in ("certificate", "invitation"):
            continue                                       # az oklevél címe a sablon szövege, nem a táblázaté
        if str(out.get(field) or "").strip():
            continue
        v = next((look[n] for n in names if look.get(n)), None)
        if v:
            out[field] = v
    return out


def _stem(row: dict[str, Any], i: int) -> str:
    look = {_key(k): ("" if v is None else str(v).strip()) for k, v in row.items() if k}
    v = next((look[n] for n in NAMEISH if look.get(_key(n))), "") or next((x for x in look.values() if x), "")
    v = re.sub(r"[\\\\/:*?\"<>|\n\r\t]+", " ", v).strip()[:60]
    return f"{i + 1:03d} {v}".strip()


def _placeholders_in(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1) for m in PH.finditer(text or "")))


def _merge_pdfs(parts: list[Path], dst: Path) -> int:
    import fitz
    out = fitz.open()
    for p in parts:
        with fitz.open(str(p)) as d:
            out.insert_pdf(d)
    n = out.page_count
    out.save(str(dst), garbage=3, deflate=True)
    out.close()
    return n


def _zip_pages(combined: Path, names: list[str], dst: Path) -> None:
    """A közös PDF oldalai külön PDF-ekként egy ZIP-be (oldal = példány)."""
    import fitz
    with fitz.open(str(combined)) as doc, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        seen: set[str] = set()
        for i in range(doc.page_count):
            one = fitz.open()
            one.insert_pdf(doc, from_page=i, to_page=i)
            name = names[i] if i < len(names) else f"{i + 1:03d}"
            while name in seen:
                name += "_"
            seen.add(name)
            z.writestr(f"{name}.pdf", one.tobytes(garbage=3, deflate=True))
            one.close()


async def run(*, data_file_id: str, template: Optional[str] = None, data: Optional[dict[str, Any]] = None,
              design_file_id: Optional[str] = None, size: Optional[str] = None, theme: Optional[dict] = None,
              brand: Optional[dict[str, Any]] = None, progress=None, limit: int = LIMIT) -> dict[str, Any]:
    from app.storage import delete_meta, display_label, output_path, public_url, temp_path, write_meta
    from app.storage.files import new_file_id
    from app.tools import crafts, design, forms, images
    rows = await asyncio.to_thread(forms.read_rows, crafts.existing(data_file_id))
    rows = [r for r in rows if any(str(v or "").strip() for v in r.values())]
    if not rows:
        raise ValueError("The spreadsheet has no rows — the first row should be the column names, then one row per copy.")
    truncated = len(rows) > limit
    rows = rows[:limit]
    cols = columns_of(rows)
    names = [_stem(r, i) for i, r in enumerate(rows)]
    tmp: list[Path] = []
    parts: list[Path] = []
    part_ids: list[str] = []                                 # a részek kimeneti fájljai (az összefűzés után törlődnek)
    used: set[str] = set()
    missing: list[str] = []
    editable: Optional[str] = None
    try:
        if design_file_id:                                   # a felhasználó saját VectorCraft-terve
            src = crafts.existing(design_file_id)
            probe = temp_path(new_file_id("txt"))
            tmp.append(probe)
            await crafts.run_cli(crafts.vector_cli(), "vectorcraft-cli", "convert", str(src), str(probe))
            phs = _placeholders_in(probe.read_text("utf-8", "replace"))
            if not phs:
                raise ValueError("No {placeholders} in the design — type e.g. {name} where each copy's own text goes.")
            look = {_key(c) for c in cols}
            used = {p for p in phs if _key(p) in look}
            missing = [p for p in phs if _key(p) not in look]
            sem = asyncio.Semaphore(4)
            done = 0

            async def one(i: int, row: dict[str, Any]) -> Path:
                nonlocal done
                out = temp_path(new_file_id("pdf"))
                args = ["run", "--in", str(src)]
                for ph in phs:
                    args += ["--cmd", "edit.findReplace", "--params",
                             json.dumps({"find": "{" + ph + "}", "replace": fill_text("{" + ph + "}", row)}, ensure_ascii=False)]
                args += ["--export", str(out)]
                async with sem:
                    await crafts.run_cli(crafts.vector_cli(), "vectorcraft-cli", *args)
                done += 1
                if progress:
                    progress(done, len(rows), "making the copies")
                return out
            parts = await asyncio.gather(*[one(i, r) for i, r in enumerate(rows)])
            tmp.extend(parts)
        else:
            template = template or "certificate"
            if template not in design.FNS:
                raise ValueError(f"template: one of {', '.join(sorted(design.FNS))} (or your own design file)")
            data = dict(data or {})
            for v in data.values():
                for ph in _placeholders_in(v) if isinstance(v, str) else []:
                    (used.add(ph) if _key(ph) in {_key(c) for c in cols} else missing.append(ph))
            t = design._theme(data, theme, brand)
            w, h = design.size_for(template, size)
            per_row_img = isinstance(data.get("image"), str) and bool(PH.search(data["image"]))
            static_img = None
            if data.get("image") and not per_row_img:
                static_img = await images.resolve(data["image"], aspect=design._frame_aspect(template, w, h))
            fn = design.FNS[template]
            chunks = [rows[i:i + CHUNK] for i in range(0, len(rows), CHUNK)]
            for ci, chunk in enumerate(chunks):
                d = design.Doc(w, h, (data.get("title") or template)[:60])
                row_imgs = []
                for k, row in enumerate(chunk):
                    rd = _auto(template, _fill(data, row), row)
                    img = static_img
                    if per_row_img and rd.get("image"):
                        try:
                            img = await images.resolve(rd["image"], aspect=design._frame_aspect(template, w, h))
                            row_imgs.append(img)
                        except Exception as e:
                            log.info("sor %d képe: %s", k, e)
                            img = None
                    if k:
                        d.new_page()
                    fn(d, rd, t, img)
                    design._logo(d, brand, template)
                export = ["pdf"] + (["vectorcraft"] if (ci == 0 and len(rows) <= CHUNK) else [])
                res = await crafts.vector_run(commands=d.cmds, export=export)
                for x in row_imgs:
                    x.path.unlink(missing_ok=True)
                for f in res["files"]:
                    if f["file_id"].endswith(".pdf"):
                        parts.append(crafts.existing(f["file_id"]))
                        part_ids.append(f["file_id"])
                    else:
                        editable = f["file_id"]
                if progress:
                    progress(min(len(rows), (ci + 1) * CHUNK), len(rows), "making the copies")
            if static_img:
                static_img.path.unlink(missing_ok=True)
        stem = (data or {}).get("title") if not design_file_id else Path(display_label(design_file_id)).stem
        stem = re.sub(r"\{[^}]*\}", "", str(stem or template or "copies")).strip(" -–·") or "copies"
        combined_id = new_file_id("pdf")
        n = await asyncio.to_thread(_merge_pdfs, parts, output_path(combined_id))
        zip_id = new_file_id("zip")
        await asyncio.to_thread(_zip_pages, output_path(combined_id), names, output_path(zip_id))
        src_id = design_file_id or data_file_id
        write_meta(combined_id, label=f"{stem[:50]} ({n} copies).pdf", source_file_id=src_id, operation="mass personalise")
        write_meta(zip_id, label=f"{stem[:50]} ({n} copies, one file each).zip", source_file_id=src_id, operation="mass personalise")
        files = [{"file_id": combined_id, "url": public_url(combined_id), "label": f"{stem[:50]} ({n} copies).pdf"},
                 {"file_id": zip_id, "url": public_url(zip_id), "label": f"{stem[:50]} ({n} copies, one file each).zip"}]
        if editable:
            write_meta(editable, label=f"{stem[:50]} (all copies).vectorcraft", source_file_id=src_id, operation="mass personalise")
            files.append({"file_id": editable, "url": public_url(editable), "label": f"{stem[:50]} (all copies).vectorcraft"})
        miss = sorted(set(missing))
        # ellenőrzés (közös check-szerződés): minden sorból lap lett, minden helyőrzőnek volt oszlopa
        findings = ([f"No column for: {', '.join('{' + m + '}' for m in miss)} — those places stay empty or literal."] if miss else []) + \
                   ([f"Only the first {limit} rows were made."] if truncated else [])
        check = {"status": "needs_check" if findings else "ok", "findings": findings,
                 "verified": [f"{n} copies in one PDF (a page each) and a ZIP of single files.",
                              f"Columns used: {', '.join(sorted(used)) or 'none'}."]}
        return {"file_id": combined_id, "files": files, "copies": n, "columns": cols, "used": sorted(used),
                "missing": miss, "truncated": truncated, "check": check,
                "note": (f"Only the first {limit} rows were made." if truncated else None)}
    finally:
        for p in tmp:
            p.unlink(missing_ok=True)
        for fid in part_ids:
            crafts.existing(fid).unlink(missing_ok=True) if output_path(fid).exists() else None
            delete_meta(fid)
