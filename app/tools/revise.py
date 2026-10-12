"""„Kérj változtatást” — iteratív szerkesztés egy kész fájlon. Minden kérés ÚJ verzió (az előző megmarad, vissza
lehet lépni); a verzió meta-adatában a kérés és az összefoglaló, így a felület a láncból „beszélgetést” rajzol.

Fájltípus szerint:
  kép / PSD  → photo_edit (pontos lépések + AI-retus rétegként)                  „a vidra mögé tedd a Parlamentet”
  docx/odt   → doc_revise (DeepSeek terv + python-docx), ami nem fér bele → régi ügynök   „a 22. oldalra egy fotó”
  pptx       → saját paklinál a leírás (deck_spec) módosul és újraépül; idegennél → régi ügynök
  pdf        → ha Word-forrásból készült (könyv / digitalizálás): a forrást szerkeszti és újra PDF-et csinál;
               különben → régi ügynök
  stílusminta (style_file_id) → a pakli témája a mintából (pptx téma / PDF / kép)
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Optional

from app.config import settings

log = logging.getLogger("aice.revise")

IMAGES = {"jpg", "jpeg", "png", "webp", "psd", "tif", "tiff"}
DOCS = {"docx", "odt", "doc", "rtf", "md"}


def _meta(fid: str) -> dict[str, Any]:
    from app.storage import read_meta
    return read_meta(fid) or {}


def history(file_id: str, limit: int = 40) -> list[dict[str, Any]]:
    """A verzió-lánc a legrégebbitől: [{file_id, label, instruction?, summary?}] — a forrásig visszafelé."""
    from app.storage import display_label
    from app.tools.crafts import existing
    chain, fid = [], file_id
    while fid and len(chain) < limit:
        try:
            existing(fid)
        except FileNotFoundError:
            break
        m = _meta(fid)
        chain.append({"file_id": fid, "label": display_label(fid, m), "instruction": m.get("instruction"),
                      "summary": m.get("summary"), "operation": m.get("operation")})
        if m.get("operation") != "revise":
            break
        fid = m.get("source_file_id")
    return list(reversed(chain))


# ---------- pakli: a leírás módosítása ----------

_DECK_PROMPT = """You revise a presentation given as JSON. Apply the user's request and reply with the COMPLETE updated JSON
object (same structure). Keep everything the request does not mention exactly as it is.
Layouts: cover {{title, subtitle, kicker}} · section {{title, subtitle, number}} · bullets {{title, bullets[], image?}} ·
image_left|image_right {{title, bullets[] or text, image}} · full_image {{title, caption, image}} · quote {{quote, author}} ·
stats {{title, stats:[{{value,label}}]}} · chart {{title, chart_type: column|bar|line|pie, categories[], series:[{{name, values[]}}]}} ·
table {{title, headers[], rows[][]}} · gallery {{title, images[]}} · closing {{title, subtitle}}.
Pictures: strings like "IMAGE#3" are existing pictures — keep them unless asked to change them. For a NEW picture use
{{"query": "2-4 plain English words"}} (stock photo, free) or {{"ai": "prompt"}} (generated, costs a credit — only if asked).
Theme colours are under "theme" ({{primary, accent, bg, panel, text, muted, font_heading, font_body}}) if the user asks
for other colours or fonts.{style}
Request: {request}
Deck JSON:
{deck}"""


async def _revise_deck(spec: dict[str, Any], request: str, theme: Optional[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    from app.llm.siliconflow import _call_model, _strip_json_fences
    work = copy.deepcopy(spec)
    refs: dict[str, Any] = {}
    for s in work.get("slides") or []:
        for key in ("image",):
            if isinstance(s.get(key), dict) and s[key].get("file_id"):
                tok = f"IMAGE#{len(refs) + 1}"; refs[tok] = s[key]; s[key] = tok
        if isinstance(s.get("images"), list):
            for j, im in enumerate(s["images"]):
                if isinstance(im, dict) and im.get("file_id"):
                    tok = f"IMAGE#{len(refs) + 1}"; refs[tok] = im; s["images"][j] = tok
    style = ""
    if theme:
        work["theme"] = {k: v for k, v in theme.items() if k != "mood"}
        style = f"\nThe user gave a style example: {theme.get('mood', '')}. Its theme is already set in the JSON; match the tone."
    prompt = _DECK_PROMPT.format(request=request.strip()[:2000], deck=json.dumps(work, ensure_ascii=False), style=style)
    last: Exception = RuntimeError("no answer")
    for model in (settings.AGENT_MODEL, settings.AGENT_MODEL_FALLBACK):
        try:
            raw = _strip_json_fences(await _call_model(model, [{"role": "user", "content": prompt}], thinking=False))
            new = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            if isinstance(new.get("slides"), list) and new["slides"]:
                break
        except Exception as e:
            last = e
            log.warning("pakli-revízió (%s): %s", model, e)
    else:
        raise RuntimeError(f"Could not revise the deck: {last}")

    def back(v: Any) -> Any:
        if isinstance(v, str) and v in refs:
            return refs[v]
        if isinstance(v, dict) and v.get("query"):
            return {"stock": v["query"]}
        if isinstance(v, dict) and v.get("ai"):
            return {"generate": v["ai"]}
        return v
    for s in new["slides"]:
        if "image" in s:
            s["image"] = back(s["image"])
        if isinstance(s.get("images"), list):
            s["images"] = [back(x) for x in s["images"]]
    if theme:
        new["theme"] = {**(new.get("theme") or {}), **{k: v for k, v in theme.items() if k != "mood"}}
    return new, f"Updated the deck ({len(new['slides'])} slides)."


# ---------- irányító ----------

async def revise(pool, file_id: str, request: str, style_file_id: Optional[str] = None) -> dict[str, Any]:
    from app.storage import display_label, output_path, public_url, write_meta
    from app.storage.files import new_file_id
    from app.tools import crafts
    src = crafts.existing(file_id)
    ext = src.suffix.lstrip(".").lower()
    stem = Path(display_label(file_id)).stem.split(" (")[0]
    request = request.strip()
    theme = None
    template_id = None
    if ext == "pptx" and str(style_file_id or "").lower().endswith((".pptx", ".potx")):
        from app.tools import pptx_template
        if pptx_template.usable(crafts.existing(style_file_id)):   # dizájn a mesterdián → teljes sablon; különben téma
            template_id = style_file_id
    if style_file_id and ext == "pptx" and not template_id:
        from app.tools import style_ref
        theme = await style_ref.theme_from(style_file_id)
    if style_file_id:
        request = request or "Restyle it like the example."
    if not request:
        raise ValueError("Say what should change.")
    out_id, summary, extra_files = None, "", []

    if ext in IMAGES:
        from app.tools import photo_edit
        res = await photo_edit.run([file_id], request, None, ["psd", "jpg"])
        out = [f for f in res["files"] if not f["file_id"].endswith(".psd")] or res["files"]
        out_id, summary = out[0]["file_id"], res.get("summary") or ""
        extra_files = [f["file_id"] for f in res["files"] if f["file_id"] != out_id]
    elif ext in DOCS or (ext == "pdf" and _docx_source(file_id)):
        from app.tools import doc_revise
        docx_id = file_id if ext in DOCS else _docx_source(file_id)
        if ext not in ("docx",) and ext in DOCS:      # odt/rtf/md → docx munkapéldány
            from app.tools import wordcraft
            conv = new_file_id("docx")
            await wordcraft.convert_file(crafts.existing(docx_id), output_path(conv))
            write_meta(conv, source_file_id=docx_id, operation="convert", extra={"ephemeral": True})
            docx_id = conv
        styled_note = ""
        if style_file_id:                              # „olyan stílusú, mint ez” — a minta stílusai + oldalbeállítása
            import asyncio
            from app.tools import doc_style
            ref = await doc_style.reference_for(style_file_id)
            styled = new_file_id("docx")
            await asyncio.to_thread(doc_style.apply_style, crafts.existing(docx_id), ref, output_path(styled))
            write_meta(styled, source_file_id=docx_id, operation="restyle", extra={"ephemeral": True})
            docx_id, styled_note = styled, "Restyled like the example. "
        new_docx = new_file_id("docx")
        if style_file_id and request.strip() == "Restyle it like the example.":
            import shutil
            shutil.copy(crafts.existing(docx_id), output_path(new_docx))
            r = {"summary": ""}
        else:
            r = await doc_revise.revise(docx_id, request, output_path(new_docx))
            if not r["handled"]:
                new_docx = await _agent(pool, docx_id, request)
                r["summary"] = "Done by the editing agent."
        summary = styled_note + (r.get("summary") or "")
        if ext == "pdf":                                  # a PDF a Word-forrásból újra (ugyanazzal a szedéssel)
            out_id = new_file_id("pdf")
            await _docx_to_pdf(new_docx, output_path(out_id), _meta(file_id))
            write_meta(new_docx, source_file_id=docx_id, operation="revise", label=f"{stem}.docx",
                       extra={"instruction": request, "summary": summary})
            extra_files = [new_docx]
        else:
            out_id = new_docx
    elif ext == "pptx" and _meta(file_id).get("deck_spec"):
        from app.tools import pptx_deck
        base = copy.deepcopy(_meta(file_id)["deck_spec"])
        if template_id:                          # új sablon: a minta-pptx mesterdiái, elrendezései, háttere
            base["template_file_id"] = template_id
            base.pop("theme", None)
        elif theme:                              # a minta csak témát ad (PDF / kép / dizájn nélküli pptx): a régi sablon el
            base.pop("template_file_id", None)
        if template_id and request.strip() == "Restyle it like the example.":
            spec, summary = base, "Rebuilt on the example's template."
        else:
            spec, summary = await _revise_deck(base, request, theme)
            if base.get("template_file_id"):      # a modell a JSON-ból kihagyhatja — a sablon marad
                spec["template_file_id"] = base["template_file_id"]
        built = await pptx_deck.build(spec)
        out_id = built["file_id"]
    else:
        out_id = await _agent(pool, file_id, request)
        summary = "Done by the editing agent."
    label = f"{stem} (v{len(history(file_id)) + 1}).{out_id.rsplit('.', 1)[1]}"
    write_meta(out_id, source_file_id=file_id, operation="revise", label=label,
               extra={"instruction": request, "summary": summary, **({"style_file_id": style_file_id} if style_file_id else {})})
    return {"file_id": out_id, "url": public_url(out_id), "label": label, "summary": summary, "also": extra_files}


def _docx_source(pdf_id: str) -> Optional[str]:
    """Egy általunk Wordből szedett PDF Word-forrása (könyv: a kézirat; digitalizálás: a testvér-docx)."""
    m = _meta(pdf_id)
    src = m.get("source_file_id")
    if src and src.lower().endswith(".docx"):
        return src
    if m.get("operation") == "digitize" and src:
        from app import tenancy
        out = tenancy.user_root() / "outputs"
        for p in out.glob("*.docx"):
            mm = _meta(p.name)
            if mm.get("source_file_id") == src and mm.get("operation") == "digitize":
                return p.name
    return None


async def _docx_to_pdf(docx_id: str, dst: Path, pdf_meta: dict[str, Any]) -> None:
    """Ugyanaz a szedés, ami az eredeti PDF-et adta: könyv → booklet-sablon; különben WordCraft."""
    from app.tools import crafts, wordcraft
    if pdf_meta.get("operation") in ("design", "booklet"):     # a régi dizájnos füzet (az új könyv már Word-szedés)
        from app.routes.make import make_book
        r = await make_book({"file_id": docx_id, "layout": "booklet"})
        dst.write_bytes(crafts.existing(r["file_id"]).read_bytes())
        return
    await wordcraft.convert_file(crafts.existing(docx_id), dst)


async def _agent(pool, file_id: str, request: str) -> str:
    from app.llm.agent import run_agent
    r = await run_agent(pool=pool, file_id=file_id, instruction=request, max_iters=14, wall_budget_s=240)
    if not r.get("file_id") or r["file_id"] == file_id:
        raise RuntimeError("The editing agent could not make this change — try saying it differently.")
    return r["file_id"]
