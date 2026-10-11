"""A termék „Make” végpontjai — egy rövid űrlapból kész fájl (a webes app csempéi mögött).

    POST /api/make/deck     {brief, slides?, images?: stock|ai|none, accent?}   → PPTX (a tartalmat a GLM írja)
    POST /api/make/social   {headline, subtitle?, handle?, sizes[], image}        → PNG-k (+PDF) méretenként
    POST /api/make/design   {template, data, size?}                               → PDF + PNG
    POST /api/make/photo    {file_id, action, background?, blur?}                 → új kép
    POST /api/make/book     {file_id, title?, author?, cover?}                    → könyv-PDF (borító + fejezetek)
    POST /api/make/pdf      {action, file_id | file_ids, …}                      → PDF-műveletek (PdfCraft)

Képforrás (image/background/cover): {"file_id"} | {"stock": "words"} | {"ai": "prompt"}. Az AI-kép kreditbe kerül
(vision_ops.generate_image vonja le); a pakli előre ellenőrzi, hogy elég-e az egyenleg.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from app import events, tenancy
from app.config import settings

log = logging.getLogger("aice.make")
router = APIRouter()

SOCIAL_SIZES = {"landscape", "square", "story"}


def _image_spec(src: Optional[dict]) -> Optional[Any]:
    """A felület képválasztója → images.resolve formátum."""
    if not src:
        return None
    if src.get("file_id"):
        return src["file_id"]
    if src.get("stock"):
        return {"stock": src["stock"]}
    if src.get("ai"):
        return {"generate": src["ai"]}
    return None


async def _materialize(spec: Any) -> Optional[str]:
    """Stock/AI képet EGYSZER letölt/generál és fájlként ment — több méret/felhasználás ugyanazt kapja
    (különben méretenként újra generálna és újra kreditet vonna le)."""
    if spec is None or isinstance(spec, str):
        return spec
    from app.storage import output_path, write_meta
    from app.storage.files import new_file_id
    from app.tools import images
    r = await images.resolve(spec, aspect=None, max_px=2400)
    try:
        if r.source_file_id:          # generált kép: már fájl
            return r.source_file_id
        fid = new_file_id("jpg")
        output_path(fid).write_bytes(r.path.read_bytes())
        write_meta(fid, operation="stock photo", label="stock.jpg", extra={"credit": r.attribution})
        return fid
    finally:
        r.path.unlink(missing_ok=True)


async def _credits_left() -> Optional[int]:
    from app import db
    uid = tenancy.current_user_id()
    if uid == tenancy.ADMIN_ID or not db.enabled():
        return None
    u = await db.user_get(uid)
    return (u or {}).get("credits", 0)


def _fail(e: Exception) -> HTTPException:
    from app import db
    if isinstance(e, db.InsufficientCredits):
        return HTTPException(402, "Not enough credits for AI images. Use stock photos or your own images, or add credits.")
    log.exception("make failed")
    return HTTPException(500, str(e)[:300])


async def _llm_json(system: str, user: str) -> dict[str, Any]:
    from app.llm.siliconflow import _call_model, _strip_json_fences
    last: Exception = RuntimeError("no model")
    for model in (settings.LLM_MODEL_PRIMARY, settings.LLM_MODEL_FALLBACK):
        try:
            raw = await _call_model(model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                    thinking=False)
            return json.loads(_strip_json_fences(raw))
        except Exception as e:  # következő modell
            last = e
            log.warning("deck-vázlat %s modellel nem sikerült: %s", model, e)
    raise last


_DECK_SYSTEM = """You write presentation decks as JSON for a deck builder. Reply with ONE JSON object:
{"title": str, "footer": str, "slides": [ ... ]}
Each slide has "layout" and fields:
  cover {title, subtitle, kicker}            section {title, subtitle, number}
  bullets {title, bullets[3-5], image?}      image_left|image_right {title, bullets[2-4] or text, image}
  full_image {title, caption, image}         quote {quote, author}
  stats {title, stats:[{value, label}] 2-4}  chart {title, chart_type: column|bar|line|pie, categories[], series:[{name, values[]}]}
  table {title, headers[], rows[][]}         closing {title, subtitle}
Rules: first slide cover, last slide closing; vary layouts; bullets ≤ 70 characters, max 5 per slide;
only use numbers the brief gives or well-known facts; write in the language of the brief.
For each slide that should have a picture set "image": {"query": "2-4 plain English words"} — a stock-photo
search, concrete and visual (e.g. "wind turbines", "team meeting"). Cover and full_image always get an image."""


@router.post("/api/make/deck")
async def make_deck(body: dict[str, Any]) -> dict[str, Any]:
    from app.tools import pptx_deck
    brief = (body.get("brief") or "").strip()
    if len(brief) < 10:
        raise HTTPException(400, "Describe the presentation in a sentence or two.")
    n = max(4, min(15, int(body.get("slides") or 8)))
    mode = body.get("images") or "stock"
    if mode == "ai":  # az LLM-vázlat ELŐTT: kredit nélkül ne várjon fél percet a semmire
        left = await _credits_left()
        if left is not None and left < settings.IMAGE_CREDIT_COST:
            raise HTTPException(402, f"AI images need credits (you have {left}). Choose stock photos or add credits.")
    try:
        spec = await _llm_json(_DECK_SYSTEM, f"Brief:\n{brief}\n\nMake exactly {n} slides.")
        slides = [s for s in spec.get("slides") or [] if isinstance(s, dict) and s.get("layout") in pptx_deck.LAYOUTS]
        if not slides:
            raise HTTPException(502, "The assistant returned no usable slides — try again or rephrase the brief.")
        wanted = [s for s in slides if isinstance(s.get("image"), dict)]
        if mode == "ai":
            left = await _credits_left()
            if left is not None and left < len(wanted) * settings.IMAGE_CREDIT_COST:
                raise HTTPException(402, f"This deck needs {len(wanted) * settings.IMAGE_CREDIT_COST} credits for AI images; "
                                         f"you have {left}. Choose stock photos or add credits.")
        for s in slides:
            q = (s.get("image") or {}).get("query") if isinstance(s.get("image"), dict) else None
            if mode == "none" or not q:
                s.pop("image", None)
            elif mode == "ai":
                s["image"] = {"generate": f"{q}, editorial photograph, natural light, no text"}
            else:
                s["image"] = {"stock": q}
        deck = {"title": spec.get("title") or slides[0].get("title"), "footer": spec.get("footer"), "slides": slides}
        if body.get("accent"):
            deck["theme"] = {"accent": body["accent"]}
        out = await pptx_deck.build(deck)
    except HTTPException:
        raise
    except Exception as e:
        raise _fail(e)
    events.emit("tool_done", tool="make_deck", result={"file_id": out["file_id"]})
    return out


@router.post("/api/make/social")
async def make_social(body: dict[str, Any]) -> dict[str, Any]:
    from app.tools import design
    headline = (body.get("headline") or "").strip()
    if not headline:
        raise HTTPException(400, "Add a headline.")
    sizes = [s for s in body.get("sizes") or ["square"] if s in SOCIAL_SIZES] or ["square"]
    files = []
    try:
        image = await _materialize(_image_spec(body.get("image")))
        if image and body.get("remove_background") and body.get("background"):
            # alany kivágása + új háttér, egyszer — minden méret ezt használja
            from app.tools import crafts
            bg_id = await _materialize(_image_spec(body["background"]))
            comp = await crafts.photo_run(file_id=image, remove_background=True, background_file_id=bg_id,
                                          background_blur=float(body.get("blur") or 12), save_format="jpg", quality=90)
            image = comp["file_id"]
        for size in sizes:
            res = await design.render("social", {"title": headline, "subtitle": body.get("subtitle"),
                                                 "handle": body.get("handle"), "image": image},
                                      size=size, theme={"accent": body["accent"]} if body.get("accent") else None,
                                      export=["png", "pdf"])
            files.append({"size": size, **res})
    except HTTPException:
        raise
    except Exception as e:
        raise _fail(e)
    events.emit("tool_done", tool="make_social", result={"files": [f["file_id"] for f in files]})
    return {"posts": files}


@router.post("/api/make/design")
async def make_design(body: dict[str, Any]) -> dict[str, Any]:
    from app.tools import design
    data = dict(body.get("data") or {})
    for k in ("image", "cover_image"):
        if isinstance(data.get(k), dict):
            data[k] = _image_spec(data[k])
    try:
        out = await design.render(body.get("template") or "poster", data, size=body.get("size"),
                                  theme={"accent": body["accent"]} if body.get("accent") else None)
    except Exception as e:
        raise _fail(e)
    events.emit("tool_done", tool="make_design", result={"file_id": out["file_id"]})
    return out


@router.post("/api/make/photo")
async def make_photo(body: dict[str, Any]) -> dict[str, Any]:
    from app.tools import crafts
    fid = body.get("file_id")
    if not fid:
        raise HTTPException(400, "Choose a photo first.")
    action = body.get("action") or "remove_background"
    try:
        if action == "remove_background":
            out = await crafts.photo_run(file_id=fid, remove_background=True, save_format="png")
        elif action == "replace_background":
            bg_id = await _materialize(_image_spec(body.get("background")))
            if not bg_id:
                raise HTTPException(400, "Pick a background: a stock search, an AI prompt or one of your images.")
            out = await crafts.photo_run(file_id=fid, remove_background=True, background_file_id=bg_id,
                                         background_blur=float(body.get("blur") or 12), save_format="jpg", quality=90)
        elif action == "enhance":
            out = await crafts.photo_run(file_id=fid, commands=[{"command": "image.autoTone"}, {"command": "image.autoColor"}])
        else:
            raise HTTPException(400, f"unknown action: {action}")
    except HTTPException:
        raise
    except Exception as e:
        raise _fail(e)
    events.emit("tool_done", tool="make_photo", result={"file_id": out["file_id"]})
    return out


@router.post("/api/make/book")
async def make_book(body: dict[str, Any]) -> dict[str, Any]:
    """Kézirat (DOCX) → könyv-PDF: borító + fejezetenként oldal(ak), folyó szöveggel."""
    from docx import Document
    from app.tools import design
    from app.tools.crafts import existing
    fid = body.get("file_id")
    if not fid:
        raise HTTPException(400, "Upload your manuscript (.docx) first.")
    try:
        doc = Document(str(existing(fid)))
    except Exception:
        raise HTTPException(400, "The manuscript must be a .docx file.")
    sections: list[dict[str, Any]] = []
    for p in doc.paragraphs:
        txt = p.text.strip()
        if not txt:
            continue
        if p.style.name.lower().startswith(("heading 1", "heading 2", "title")) or (not sections and len(txt) < 80):
            sections.append({"heading": txt, "text": ""})
        elif sections:
            sections[-1]["text"] += ("\n\n" if sections[-1]["text"] else "") + txt
    sections = [s for s in sections if s["text"]][:40]
    if not sections:
        raise HTTPException(400, "No chapters found — use Heading 1 for chapter titles.")
    data = {"title": body.get("title") or sections[0]["heading"], "subtitle": body.get("subtitle"),
            "author": body.get("author"), "organization": body.get("author"), "sections": sections,
            "cover_image": _image_spec(body.get("cover"))}
    try:
        out = await design.render("booklet", data, size=body.get("size") or "A5", export=["pdf"])
    except Exception as e:
        raise _fail(e)
    events.emit("tool_done", tool="make_book", result={"file_id": out["file_id"]})
    return out


# PDF-műveletek (PdfCraft): action → (lépések, kimeneti formátum, mentsen-e a végén doc 1-et)
def _pdf_plan(action: str, body: dict[str, Any]) -> tuple[list[dict[str, Any]], str, bool]:
    if action == "merge":
        return [{"tool": "doc_combine", "args": {"paths": "$paths", "out": "$out"}}], "pdf", False
    if action == "compress":
        return [{"tool": "doc_reduce", "args": {"path": "$out"}}], "pdf", False
    if action == "to_word":
        return [{"tool": "doc_export_office", "args": {"path": "$out"}}], "docx", False
    if action == "page_numbers":
        pos = body.get("position") or "center"
        return [{"tool": "doc_header_footer", "args": {f"footer_{pos}": body.get("format") or "<<1 of n>>"}}], "pdf", True
    if action == "watermark":
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "Type the watermark text.")
        return [{"tool": "doc_watermark", "args": {"text": text, "opacity": 0.25}}], "pdf", True
    if action == "redact":
        words = [w.strip() for w in body.get("words") or [] if w and w.strip()]
        patterns = [p for p in body.get("patterns") or [] if p in ("email", "phone", "credit-card", "ssn", "date")]
        if not words and not patterns:
            raise HTTPException(400, "Add words to black out, or pick a pattern (emails, phone numbers…).")
        steps = ([{"tool": "redact_mark", "args": {"words": words}}] if words else []) + \
                [{"tool": "redact_mark", "args": {"pattern": p}} for p in patterns]
        return steps + [{"tool": "redact_apply", "args": {}}, {"tool": "doc_remove_hidden", "args": {}}], "pdf", True
    if action == "ocr":
        return [{"tool": "ocr_recognize", "args": {"language": "en"}}], "pdf", True
    if action == "protect":
        pw = body.get("password") or ""
        if len(pw) < 4:
            raise HTTPException(400, "Choose a password of at least 4 characters.")
        return [{"tool": "doc_protect", "args": {"open_password": pw}}], "pdf", True
    if action == "compare":
        return [{"tool": "doc_compare_report", "args": {"doc": 2, "other": 1, "path": "$out"}}], "pdf", False
    if action == "make_fillable":   # digitális (nem fotózott) nyomtatvány: vonalak/dobozok → valódi mezők
        return [{"tool": "form_detect_fields", "args": {"add": True}}], "pdf", True
    raise HTTPException(400, f"unknown action: {action}")


PDF_LABELS = {"merge": "merged", "compress": "compressed", "to_word": "", "page_numbers": "numbered",
              "watermark": "watermarked", "redact": "redacted", "ocr": "searchable", "protect": "protected",
              "compare": "comparison", "make_fillable": "fillable"}


@router.post("/api/make/pdf")
async def make_pdf(body: dict[str, Any]) -> dict[str, Any]:
    from pathlib import Path
    from app.storage import display_label, output_path, public_url, write_meta
    from app.storage.files import new_file_id
    from app.tools import crafts
    action = body.get("action") or ""
    ids = [i for i in (body.get("file_ids") or ([body["file_id"]] if body.get("file_id") else [])) if i]
    if not ids:
        raise HTTPException(400, "Choose a PDF first.")
    need = 2 if action in ("merge", "compare") else 1
    if len(ids) < need:
        raise HTTPException(400, "Choose at least two PDFs." if action == "merge" else "Choose the old and the new version.")
    if any(not i.lower().endswith(".pdf") for i in ids):
        raise HTTPException(400, "Only PDF files here — convert other files to PDF first.")
    steps, fmt, save = _pdf_plan(action, body)
    fid = new_file_id(fmt)
    out = output_path(fid)
    try:
        if action == "merge":
            steps[0]["args"]["paths"] = [str(crafts.existing(i)) for i in ids]
            await crafts.pdf_script([], steps, out, save=False)
        elif action == "redact":
            # a nem illeszkedő szó/minta a PdfCraftban hiba („nothing matched”) → előbb egyenként kipróbáljuk
            marks = []
            for st in steps[:-2]:
                try:
                    await crafts.pdf_script(ids[:1], [st], out, save=False)
                    marks.append(st)
                except RuntimeError as e:
                    if "nothing matched" not in str(e):
                        raise
            if not marks:
                raise HTTPException(422, "Nothing to black out — none of those words or patterns appear in this PDF.")
            await crafts.pdf_script(ids[:1], marks + steps[-2:], out, save=True)
        else:
            await crafts.pdf_script(ids[:need], steps, out, save=save)
        if not out.exists():
            raise RuntimeError("no output")
    except HTTPException:
        raise
    except Exception as e:
        raise _fail(e)
    stem = Path(display_label(ids[-1] if action == "compare" else ids[0])).stem
    tag = PDF_LABELS.get(action, action)
    write_meta(fid, source_file_id=ids[0], operation=f"pdf {action}", label=f"{stem}{' (' + tag + ')' if tag else ''}.{fmt}")
    res = {"file_id": fid, "url": public_url(fid), "size_kb": out.stat().st_size // 1024}
    events.emit("tool_done", tool="make_pdf", params={"action": action}, result=res)
    return res


# ---------- Convert (bármi → bármi) ----------

@router.get("/api/convert/targets")
async def convert_targets(ext: str) -> dict[str, Any]:
    from app.tools import anyconvert
    return {"targets": [{"fmt": t, "label": anyconvert.LABELS.get(t, t.upper())} for t in anyconvert.targets_for(ext)]}


@router.post("/api/make/convert")
async def make_convert(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    from app.tools import anyconvert
    fid, target = body.get("file_id"), (body.get("target") or "").strip()
    if not fid or not target:
        raise HTTPException(400, "Choose a file and a format.")
    try:
        out = await anyconvert.convert_any(getattr(request.app.state, "pool", None), fid, target)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise _fail(e)
    events.emit("tool_done", tool="make_convert", params={"target": target}, result={"file_id": out["file_id"]})
    return out


# ---------- Digitalizálás: régi könyv / fotózott oldal → szöveg, Word, új PDF, kereshető PDF ----------

@router.post("/api/make/digitize")
async def make_digitize(body: dict[str, Any]) -> dict[str, Any]:
    from app import jobs
    from app.tools import digitize
    ids = [i for i in body.get("file_ids") or [] if i]
    if not ids:
        raise HTTPException(400, "Add photos or scans of the pages.")
    outputs = [o for o in body.get("outputs") or ["docx"] if o in digitize.OUTPUTS]
    if not outputs:
        raise HTTPException(400, "Pick at least one result: Word, new PDF, searchable PDF or text.")
    lang = (body.get("language") or "").strip() or None
    limit = 1000 if tenancy.current_user_id() == tenancy.ADMIN_ID else settings.OCR_MAX_PAGES
    if body.get("max_pages"):            # „csak az első N oldal” (próba, mintafejezet)
        limit = max(1, min(limit, int(body["max_pages"])))

    async def work(progress):
        res = await digitize.digitize(ids, outputs, lang, limit, progress)
        events.emit("tool_done", tool="make_digitize", result={"files": [f["file_id"] for f in res["files"]],
                                                               "pages": res["pages"]})
        return res
    return jobs.start("digitize", work)


# ---------- Űrlapok: mezők, kitöltés (egyenként vagy táblázatból) ----------

@router.get("/api/forms/fields")
async def form_fields(file_id: str) -> dict[str, Any]:
    from app.tools import forms
    if not file_id.lower().endswith(".pdf"):
        return {"fields": []}
    try:
        return {"fields": await forms.fields(file_id)}
    except FileNotFoundError:
        raise HTTPException(404, "File not found.")
    except Exception as e:
        log.warning("form_fields %s: %s", file_id, e)
        return {"fields": []}


@router.post("/api/make/fill_form")
async def make_fill_form(body: dict[str, Any]) -> dict[str, Any]:
    """{file_id, values: {mező: érték}} vagy {file_id, data_file_id: csv/xlsx} (soronként egy példány); flatten?"""
    from pathlib import Path
    from app.storage import display_label, output_path, public_url, write_meta
    from app.storage.files import new_file_id
    from app.tools import crafts, forms
    fid = body.get("file_id")
    if not fid:
        raise HTTPException(400, "Choose the form (a PDF with fields).")
    flatten = bool(body.get("flatten"))
    out_id = new_file_id("pdf")
    stem = Path(display_label(fid)).stem
    try:
        if body.get("data_file_id"):
            rows = forms.read_rows(crafts.existing(body["data_file_id"]))
            info = await forms.fill_batch(fid, rows, flatten, output_path(out_id))
            label = f"{stem} ({info['copies']} filled).pdf"
        else:
            info = await forms.fill(fid, dict(body.get("values") or {}), flatten, output_path(out_id))
            label = f"{stem} (filled).pdf"
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise _fail(e)
    write_meta(out_id, source_file_id=fid, operation="fill form", label=label)
    res = {"file_id": out_id, "url": public_url(out_id), "label": label, "size_kb": output_path(out_id).stat().st_size // 1024, **info}
    events.emit("tool_done", tool="make_fill_form", result={"file_id": out_id})
    return res
