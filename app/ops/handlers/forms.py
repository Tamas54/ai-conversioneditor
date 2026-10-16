"""Űrlap-eszközök: felismerés, kitöltés, tömeges kitöltés, kitölthetővé tétel, ügynöki kitöltés.
A munka az app/formkit csomagban van; itt a bemenet előkészítése (DOC/ODT/RTF → DOCX, XLS/ODS → XLSX,
majd vissza az eredeti formátumba), az előnézet és a kimenetek."""
from __future__ import annotations

import asyncio
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any, Optional

from app.catalog.formats import from_filename
from app.convert.options import OptionError
from app.formkit import detect, fill as F, schema as S
from app.ops.handlers import out_name

_TO_DOCX = {"doc", "odt", "rtf", "dot", "dotx", "docm", "wpd", "wps", "pages"}
_TO_XLSX = {"xls", "ods", "xlsm", "numbers"}
_LANG = {"hu": "hun+eng", "hun": "hun+eng", "hungarian": "hun+eng", "magyar": "hun+eng", "en": "eng", "eng": "eng",
         "english": "eng", "de": "deu+eng", "deu": "deu+eng", "german": "deu+eng", "fr": "fra+eng", "es": "spa+eng",
         "it": "ita+eng", "pl": "pol+eng", "cs": "ces+eng", "sk": "slk+eng", "ro": "ron+eng"}


def _lang(o: dict[str, Any]) -> str:
    return _LANG.get(str(o.get("language") or "hu").lower(), "hun+eng")


async def _native(src: Path, work: Path) -> tuple[Path, Optional[str]]:
    """Az űrlap olyan formában, amit közvetlenül ki tudunk tölteni; vissza: (útvonal, eredeti formátum, ha váltottunk)."""
    fmt = from_filename(src.name) or ""
    if fmt in _TO_DOCX or fmt in _TO_XLSX:
        from app.convert.service import convert_path
        target = "docx" if fmt in _TO_DOCX else "xlsx"
        outs, _ = await convert_path(src, target, {}, work / f"native-{src.stem}")
        return outs[0], fmt
    return src, None


async def _back(path: Path, orig: Optional[str], work: Path) -> Path:
    if not orig:
        return path
    from app.convert.service import convert_path
    outs, _ = await convert_path(path, orig, {}, work / f"back-{path.stem}")
    return outs[0]


async def schema_for(src: Path, o: dict[str, Any]) -> dict[str, Any]:
    from app.formkit.deep import DeepUnavailable
    try:
        sch = await detect.recognize(src, _lang(o), o.get("deep") or ("off" if o.get("schema") else "auto"))
    except DeepUnavailable as e:
        raise OptionError(str(e))
    given = o.get("schema")
    if given:
        if not isinstance(given.get("fields"), list):
            raise OptionError("schema: the object from form-recognize (with a fields list)")
        manual = []
        for f in given["fields"]:
            f = dict(f)
            if not f.get("anchor") and f.get("box") and sch["kind"] in ("flat_pdf", "acroform", "image"):
                f["anchor"] = {"kind": "xy", "page": f.get("page") or 1, "rect": f["box"]}
            elif not f.get("anchor") and f.get("cell") and sch["kind"] == "xlsx":
                f["anchor"] = {"kind": "xlsx_cell", "sheet": f.get("sheet") or sch["pages"][0]["sheet"], "cell": f["cell"]}
            elif not f.get("anchor") and sch["kind"] == "docx" and f.get("label"):
                f["anchor"] = {"kind": "docx_after", "label": f["label"]}
            manual.append(f)
        known = {f["name"] for f in sch["fields"]}
        for f in manual:          # a horgony nélküli, ismeretlen mezőt nem tudnánk hová írni
            if f["name"] not in known and not f.get("anchor") and not f.get("remove"):
                raise OptionError(f"schema: new field {f['name']!r} needs a box (PDF, photo), a cell (XLSX) or a label (DOCX)")
        sch = S.merge_manual(sch, manual)
    return sch


def preview(src: Path, sch: dict[str, Any], dst: Path, filled: bool = False, page: int = 1) -> Optional[Path]:
    """Egy oldal képe a mezők dobozaival és sorszámával (a felismerés ellenőrzéséhez — MCP-n a kliens ügynöke
    ezekből javít)."""
    import pymupdf
    from PIL import Image, ImageDraw, ImageFont
    from app.formkit.fill import FONT
    fmt = from_filename(src.name) or ""
    if fmt == "pdf":
        with pymupdf.open(str(src)) as d:
            if page > d.page_count:
                return None
            pix = d[page - 1].get_pixmap(dpi=100)
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            scale = 100 / 72
    elif sch["kind"] == "image":
        from app.convert.steps import _register_plugins
        _register_plugins()
        with Image.open(src) as im0:
            im = im0.convert("RGB")
        scale = 1.0
        if im.width > 1400:
            scale = 1400 / im.width
            im = im.resize((1400, round(im.height * scale)))
    else:
        return None
    if not filled:
        dr = ImageDraw.Draw(im, "RGBA")
        fnt = ImageFont.truetype(FONT, 12)
        for i, f in enumerate([f for f in sch["fields"] if f.get("box") and (f.get("page") or 1) == page], 1):
            x0, y0, x1, y1 = (v * scale for v in f["box"])
            col = {"signature": (156, 39, 176), "checkbox": (0, 137, 123)}.get(f["type"], (25, 118, 210))
            dr.rectangle((x0, y0, x1, y1), outline=col + (255,), fill=col + (40,), width=2)
            dr.text((x0 + 2, y0 - 14), f"{i}. {f.get('label') or f['name']}"[:40], font=fnt, fill=col + (255,))
    im.save(dst, "PNG", optimize=True)
    return dst


# ---------------------------------------------------------------- eszközök

async def recognize(inputs: list[Path], o: dict[str, Any], out: Path) -> dict[str, Any]:
    src, orig = await _native(inputs[0], out)
    sch = await schema_for(src, o)
    if orig:
        sch["source_format"] = orig
    js = out_name(out, inputs[0], "-fields", "json")
    js.write_text(json.dumps(sch, ensure_ascii=False, indent=1), "utf-8")
    files = [js]
    from app import tenancy
    from app.formkit.deep import MAX_PAGES
    # gyenge eredménynél / MCP-n minden oldal képe kell (a felhasználó vagy a kliens ügynöke ezekből javít)
    every = (sch.get("quality") or {}).get("level") == "low" or tenancy.client_makes_images.get()
    for pg in range(1, (min(len(sch.get("pages") or [1]), MAX_PAGES) if every else 1) + 1):
        pv = await asyncio.to_thread(preview, src, sch, out_name(out, inputs[0], f"-fields-p{pg}", "png"), False, pg)
        if pv:
            files.append(pv)
    return {"files": files, "data": sch}


async def fill(inputs: list[Path], o: dict[str, Any], out: Path) -> dict[str, Any]:
    src, orig = await _native(inputs[0], out)
    sch = await schema_for(src, o)
    if not sch["fields"]:
        raise ValueError("No form fields found. Add them by hand (schema with boxes) or use make-fillable.")
    vals, errors, skipped = F.prepare(sch, o.get("values") or {})
    if not vals and not errors:
        raise OptionError("values: none of the keys match a field. Fields: " + ", ".join(f["name"] for f in sch["fields"][:40]))
    dst, filled = await asyncio.to_thread(F.fill, src, sch, vals, out, _stem(inputs[0], "filled"),
                                          flatten=bool(o.get("flatten")), keep_look=o.get("keep_look", True) is not False)
    dst = await _back(dst, orig, out)
    return {"files": [dst], "data": {"filled": filled, "errors": errors, "skipped": skipped, "kind": sch["kind"]}}


def _stem(src: Path, suffix: str) -> str:
    from app.convert.steps import _stem as st
    import re
    return (re.sub(r"[^\w\-]+", "_", st(src))[:50] or "form") + f"-{suffix}"


async def bulk_fill(inputs: list[Path], o: dict[str, Any], out: Path) -> dict[str, Any]:
    from app.tools import forms as legacy
    data = o["_resolve"](o["data_file"]) if o.get("data_file") else None
    if not data:
        raise OptionError("data_file: a CSV or XLSX file (first row = field names or labels)")
    rows = await asyncio.to_thread(legacy.read_rows, data)
    if not rows:
        raise ValueError("The table has no rows.")
    rows = rows[:500]
    src, orig = await _native(inputs[0], out)
    sch = await schema_for(src, o)
    parts, errors = [], {}
    for i, row in enumerate(rows, 1):
        vals, errs, _ = F.prepare(sch, row)
        if errs:
            errors[i] = errs
        first = next((str(v) for v in row.values() if v not in (None, "")), str(i))
        stem = f"{i:03d}-" + (S.slug(first)[:30] or "row")
        part_dir = out / f"row{i:04d}"
        part_dir.mkdir()
        dst, _ = await asyncio.to_thread(F.fill, src, sch, vals, part_dir, stem, flatten=bool(o.get("flatten")))
        parts.append(await _back(dst, orig, part_dir))
    if all(p.suffix == ".pdf" for p in parts):
        import pymupdf
        res = out_name(out, inputs[0], "-filled", "pdf")
        with pymupdf.open() as merged:
            for p in parts:
                with pymupdf.open(str(p)) as d:
                    merged.insert_pdf(d)
            merged.save(str(res), garbage=3, deflate=True)
    else:
        res = out_name(out, inputs[0], "-filled", "zip")
        with zipfile.ZipFile(res, "w", zipfile.ZIP_DEFLATED) as z:
            for p in parts:
                z.write(p, p.name)
    for p in parts:
        shutil.rmtree(p.parent, ignore_errors=True)
    return {"files": [res], "data": {"copies": len(parts), "errors": errors}}


async def make_fillable(inputs: list[Path], o: dict[str, Any], out: Path) -> dict[str, Any]:
    """Lapos nyomtatvány (PDF, DOCX, fotó, szken) → valódi mezőkkel kitölthető PDF."""
    import pymupdf
    from app.ops.handlers import to_pdf
    src = inputs[0]
    fmt = from_filename(src.name) or ""
    if fmt == "pdf":
        base = src
    elif fmt in ("docx", "doc", "odt", "rtf"):
        base = await to_pdf(src, out)
    else:
        base = out / "page.pdf"
        await asyncio.to_thread(F._image_to_pdf, src, base)
    sch = await detect.recognize(base, _lang(o), o.get("deep") or "auto")
    if sch["kind"] == "acroform":
        raise ValueError("This PDF already has form fields — use fill-form.")
    if not sch["fields"]:
        raise ValueError("No blanks found to turn into fields.")
    dst = out_name(out, src, "-fillable", "pdf")
    with pymupdf.open(str(base)) as d:
        for f in sch["fields"]:
            page = d[(f.get("page") or 1) - 1]
            w = pymupdf.Widget()
            w.field_name = f["name"]
            w.field_label = f.get("label") or f["name"]
            w.rect = pymupdf.Rect(*f["box"])
            if f["type"] == "checkbox":
                w.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
            else:
                w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
                w.text_fontsize = 0 if f["type"] == "multiline" else max(6.0, min(11.0, w.rect.height * 0.7))
                if f["type"] == "multiline":
                    w.field_flags |= pymupdf.PDF_TX_FIELD_IS_MULTILINE
            if f["type"] == "signature":
                w.field_label = (f.get("label") or "Signature")
            page.add_widget(w)
        d.save(str(dst), garbage=3, deflate=True)
    from app.tools import forms as legacy
    await asyncio.to_thread(legacy.unicode_fields, dst)
    return {"files": [dst], "data": {"fields": [{"name": f["name"], "label": f["label"], "type": f["type"]} for f in sch["fields"]]}}


async def agent_fill(inputs: list[Path], o: dict[str, Any], out: Path) -> dict[str, Any]:
    """Profilból + iratokból kitölt, a hiányzót kérdésként adja vissza (status: needs_input), előnézettel."""
    from app import tenancy
    from app.formkit import profile as P
    from app.formkit import signature as G
    uid = tenancy.current_user_id()
    stored = await P.load_profile(uid) if not tenancy.is_guest() else {}
    prof = {**stored, **P.clean_profile(o.get("profile") or {})}
    srcs = [o["_resolve"](fid) for fid in (o.get("source_files") or [])[:10]]
    text = await P.source_text(srcs) if srcs else ""
    src, orig = await _native(inputs[0], out)
    sch = await schema_for(src, o)
    if not sch["fields"]:
        raise ValueError("No form fields found in this file.")
    # MCP-n a következtetést a kliens saját ügynöke végzi — a mi modellünket nem hívjuk
    pl = await P.plan(sch, values=o.get("values"), answers=o.get("answers"), profile=prof, sources=text,
                      use_ai=not tenancy.client_makes_images.get())
    vals = dict(pl["values"])
    sig_id = o.get("signature_id") or prof.get("signature_id")
    if pl["signature_fields"] and sig_id:
        try:
            png = await G.load(uid, sig_id)
            sp = out / "signature.png"
            sp.write_bytes(png)
            for name in pl["signature_fields"]:
                vals[name] = {"image": str(sp)}
            pl["provenance"].update({n: "signature" for n in pl["signature_fields"]})
        except KeyError:
            pl["errors"]["signature"] = f"no saved signature {sig_id}"
    dst, filled = await asyncio.to_thread(F.fill, src, sch, vals, out, _stem(inputs[0], "filled"),
                                          flatten=bool(o.get("flatten")), keep_look=o.get("keep_look", True) is not False)
    files = [await _back(dst, orig, out)]
    pv = await asyncio.to_thread(preview, dst, sch, out_name(out, inputs[0], "-preview", "png"), True) \
        if dst.suffix == ".pdf" or sch["kind"] == "image" else None
    if pv:
        files.append(pv)
    shown = {k: (v if not isinstance(v, dict) else "[signature]") for k, v in vals.items()}
    status = "complete" if not pl["questions"] else "needs_input"
    return {"files": files, "data": {"status": status, "values": shown, "provenance": pl["provenance"],
                                     "questions": pl["questions"], "errors": pl["errors"], "filled": filled,
                                     "kind": sch["kind"], "schema": sch}}
