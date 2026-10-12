"""„Szerkeszd szavakkal, de pontosan” — utasításból rétegzett PSD, nem újrafestett kép.

A generatív képszerkesztés (ChatGPT, FLUX-edit) az egész képet újrafesti: elcsúszik az arc/termék, a betű
rossz, nincs réteg, és két futás két eredmény. Itt a vision-modell csak TERVET ír (szűk lépéskészlet), a
végrehajtást a PhotoCraft végzi pontosan: minden lépés külön, elnevezett réteg (korrekciós réteg maszkkal,
szövegréteg, beágyazott logó), az eredeti pixelek érintetlenek. Kimenet: PSD (rétegek) + JPG/PNG előnézet.
Tömegesen: EGY terv minden képre → egyforma eredmény (a pozíciók arányosak).

Lépések (plan.steps), sorrendjüket a végrehajtó rendezi (pixel → korrekció → logó → szöveg):
  crop {aspect "4:5"|"1:1"|"16:9"|"9:16"|"3:2"|"2:3"|"4:3", focus "center"|"top"|"bottom"|"left"|"right"}
  resize {long_edge px}                  auto {}                         sharpen {amount 0–100}
  light {exposure −2..2, contrast −50..100, brightness −100..100}
  color {vibrance, saturation −100..100, warmth −100..100 (meleg/hideg fotószűrő)}
  look {name warm|cool|tealOrange|bleachBypass|fadedFilm|monoContrast|crossProcess|dayForNight, strength 0–100}
  black_white {}                         sky {exposure −2..2, saturation −100..100, contrast −50..100}
  vignette {amount 0–100}                background {remove: true | stock "szavak" | ai "prompt" | color "#hex", blur px}
  logo {position "bottom-right"|…, width_pct 5–40, margin_pct 2–10}   (logo_file_id kell)
  text {text, position "top"|"center"|"bottom"|"top-left"|…, size "small"|"medium"|"large"|"huge",
        color "#hex", font, weight 400–900, shadow bool}
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from app.config import settings

log = logging.getLogger("aice.photo_edit")

ASPECTS = {"1:1": (1, 1), "4:5": (4, 5), "5:4": (5, 4), "16:9": (16, 9), "9:16": (9, 16), "3:2": (3, 2), "2:3": (2, 3),
           "4:3": (4, 3), "3:4": (3, 4)}
LOOKS = {"warm", "cool", "tealOrange", "bleachBypass", "fadedFilm", "monoContrast", "crossProcess", "dayForNight"}
TEXT_SIZE = {"small": 0.035, "medium": 0.06, "large": 0.09, "huge": 0.13}
FONT = "Noto Sans"
_ORDER = ["ai_edit", "background", "crop", "resize", "auto", "sharpen", "light", "color", "sky", "look", "black_white",
          "vignette", "logo", "text"]

_PLAN_PROMPT = """You plan precise photo edits for a Photoshop-like engine. You do NOT paint or generate the image;
you choose steps from this list (all optional, use only what the request needs):
  {"op":"crop","aspect":"1:1|4:5|5:4|16:9|9:16|3:2|2:3|4:3|3:4","focus":"center|top|bottom|left|right"}
  {"op":"resize","long_edge":px}
  {"op":"auto"}                                   (automatic tone and colour fix)
  {"op":"sharpen","amount":0-100}
  {"op":"light","exposure":-2..2,"contrast":-50..100,"brightness":-100..100}
  {"op":"color","vibrance":-100..100,"saturation":-100..100,"warmth":-100..100}
  {"op":"look","name":"warm|cool|tealOrange|bleachBypass|fadedFilm|monoContrast|crossProcess|dayForNight","strength":0-100}
  {"op":"black_white"}
  {"op":"sky","exposure":-2..2,"saturation":-100..100,"contrast":-50..100}   (only the sky; negative exposure = more dramatic)
  {"op":"vignette","amount":0-100}
  {"op":"background","remove":true} or {"op":"background","stock":"2-4 words"} or {"op":"background","ai":"prompt"} or {"op":"background","color":"#rrggbb"}, optional "blur":px
  {"op":"logo","position":"top-left|top-right|bottom-left|bottom-right|top|bottom","width_pct":5-40,"margin_pct":2-10}   (only if a logo is provided)
  {"op":"ai_edit","instruction":"..."}   (generative retouch: remove or add objects, change content, e.g. "remove the
      translucent grass blades in front of the animal", "remove the people in the background"; only when the other
      steps cannot do it — it costs a credit. To put something BEHIND the subject prefer background {stock|ai}.)
  {"op":"text","text":"...","position":"top|center|bottom|top-left|top-right|bottom-left|bottom-right","size":"small|medium|large|huge","color":"#rrggbb","weight":400-900,"shadow":true|false}
Look at the photo: put text where it does not cover the subject (e.g. over sky or empty space), pick a text colour
that contrasts with that area, keep the subject in a crop. Use moderate values unless asked for strong ones.
Use "ai" backgrounds only if the user asks for a generated/imagined background.
Logo provided: {logo}.
Request: {request}
Reply with ONE JSON object: {"steps":[...], "summary":"one sentence in the language of the request — English if the request is only a few words or its language is unclear"}"""


# ---------- tervezés ----------

async def plan(image: Path, request: str, has_logo: bool) -> dict[str, Any]:
    from app.llm import vision
    prompt = _PLAN_PROMPT.replace("{logo}", "yes" if has_logo else "no").replace("{request}", request.strip()[:2000])
    msgs = [{"role": "user", "content": [{"type": "text", "text": prompt},
                                         {"type": "image_url", "image_url": {"url": vision.image_to_data_url(image, long_edge=1280)}}]}]
    last: Exception = RuntimeError("no plan")
    for model in (settings.OCR_MODEL, settings.LLM_MODEL_VISION_FALLBACK):
        try:
            raw = await vision._call(msgs, max_tokens=2000, model=model)
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
            data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            if isinstance(data.get("steps"), list):
                data["steps"] = clean_steps(data["steps"], has_logo)
                return data
        except Exception as e:
            last = e
            log.warning("photo_edit terv (%s): %s", model, e)
    raise RuntimeError(f"Could not plan the edit: {last}")


def clean_steps(steps: list[Any], has_logo: bool) -> list[dict[str, Any]]:
    """Ismeretlen lépés/érték ki, határok közé szorítva — a végrehajtó csak ezt kapja."""
    clamp = lambda v, lo, hi, d=0: max(lo, min(hi, float(v))) if isinstance(v, (int, float)) else d
    out: list[dict[str, Any]] = []
    for s in steps:
        if not isinstance(s, dict) or s.get("op") not in _ORDER:
            continue
        op = s["op"]
        if op == "logo" and not has_logo:
            continue
        if op == "crop" and s.get("aspect") not in ASPECTS:
            continue
        if op == "look" and s.get("name") not in LOOKS:
            continue
        if op == "text" and not str(s.get("text") or "").strip():
            continue
        if op == "ai_edit" and not str(s.get("instruction") or "").strip():
            continue
        for k, (lo, hi) in {"exposure": (-2, 2), "contrast": (-50, 100), "brightness": (-100, 100), "vibrance": (-100, 100),
                            "saturation": (-100, 100), "warmth": (-100, 100), "strength": (0, 100), "amount": (0, 100),
                            "width_pct": (3, 50), "margin_pct": (1, 15), "weight": (100, 900), "blur": (0, 80),
                            "long_edge": (256, 8000)}.items():
            if k in s:
                s[k] = clamp(s[k], lo, hi)
        out.append(s)
    return sorted(out, key=lambda s: _ORDER.index(s["op"]))


# ---------- végrehajtás ----------

def _size(path: Path) -> tuple[int, int]:
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        return ImageOps.exif_transpose(im).size


async def _probe_sky(src: Path) -> Optional[int]:
    """A PhotoCraft select.sky küszöbe, ami ésszerű lefedettséget ad (naplementés eget 50-en nem talál)."""
    from app.storage import temp_path
    from app.storage.files import new_file_id
    from app.tools import crafts
    # 15-ös küszöbnél a naplementés égre „domb alakú” sávot és a víz tükröződését is kijelöli (mérve) → nem
    for th in (50, 30):
        out = temp_path(new_file_id("png"))
        try:
            res = await crafts.run_cli(crafts.photo_cli(), "photocraft-cli", "run", str(src), "--cmd", "select.sky",
                                       "--params", json.dumps({"threshold": th}), "--out", str(out))
        except RuntimeError:
            continue
        finally:
            out.unlink(missing_ok=True)
        m = re.search(r'"coverage":\s*([\d.]+)', res)
        if m and 0.08 <= float(m.group(1)) <= 0.7:
            return th
    return None


def _crop_box(w: int, h: int, aspect: str, focus: str) -> tuple[int, int, int, int]:
    aw, ah = ASPECTS[aspect]
    if w / h > aw / ah:
        cw, ch = int(h * aw / ah), h
    else:
        cw, ch = w, int(w * ah / aw)
    x = {"left": 0, "right": w - cw}.get(focus, (w - cw) // 2)
    y = {"top": 0, "bottom": h - ch}.get(focus, (h - ch) // 2)
    return x, y, cw, ch


def _place(position: str, W: int, H: int, bw: float, bh: float, margin: float) -> tuple[float, float]:
    """Doboz bal-felső sarka a pozíció szerint."""
    p = position or "bottom"
    x = margin if "left" in p else (W - margin - bw if "right" in p else (W - bw) / 2)
    y = margin if p.startswith("top") else (H - margin - bh if p.startswith("bottom") else (H - bh) / 2)
    return x, y


async def build_commands(src: Path, steps: list[dict[str, Any]], logo: Optional[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Terv → PhotoCraft-parancsok (a háttér-lépés külön: a photo_run kivágás/háttér paraméterei)."""
    W, H = _size(src)
    cmds: list[dict[str, Any]] = []
    extra: dict[str, Any] = {}
    name = lambda n: cmds.append({"command": "layer.renameLayer", "params": {"name": n}})
    for s in steps:
        op = s["op"]
        if op == "background":
            extra["background"] = s
        elif op == "crop":
            x, y, cw, ch = _crop_box(W, H, s["aspect"], s.get("focus", "center"))
            cmds.append({"command": "image.crop", "params": {"x": x, "y": y, "width": cw, "height": ch}})
            W, H = cw, ch
        elif op == "resize" and s.get("long_edge"):
            k = s["long_edge"] / max(W, H)
            W, H = int(W * k), int(H * k)
            cmds.append({"command": "image.imageSize", "params": {"width": W, "height": H}})
        elif op == "auto":
            cmds += [{"command": "image.autoTone"}, {"command": "image.autoColor"}]
        elif op == "sharpen" and s.get("amount"):
            cmds.append({"command": "filter.sharpen.unsharpMask", "params": {"amount": int(s["amount"] * 1.5), "radius": 1.2, "threshold": 2}})
        elif op == "light":
            if s.get("exposure"):
                cmds.append({"command": "layer.newAdjustmentLayer.exposure", "params": {"exposure": round(s["exposure"], 2)}})
                name(f"Exposure {s['exposure']:+.1f}")
            if s.get("brightness") or s.get("contrast"):
                cmds.append({"command": "layer.newAdjustmentLayer.brightnessContrast",
                             "params": {"brightness": int(s.get("brightness", 0)), "contrast": int(s.get("contrast", 0))}})
                name("Brightness / contrast")
        elif op == "color":
            if s.get("vibrance") or s.get("saturation"):
                cmds.append({"command": "layer.newAdjustmentLayer.vibrance",
                             "params": {"vibrance": int(s.get("vibrance", 0)), "saturation": int(s.get("saturation", 0))}})
                name("Vibrance")
            if s.get("warmth"):
                warm = s["warmth"] > 0
                cmds.append({"command": "layer.newAdjustmentLayer.photoFilter",
                             "params": {"filter": "warming85" if warm else "cooling80", "density": int(abs(s["warmth"]) * 0.6)}})
                name("Warmer" if warm else "Cooler")
        elif op == "sky":
            th = await _probe_sky(src)
            if th is not None:
                sel = {"command": "select.sky", "params": {"threshold": th, "softness": 25}}
                label = "Sky"
            else:   # fokozatos szűrő: a felső 45 % lágy szélű kijelöléssel (amit a fotós is tenne)
                sel = {"command": "select.rect", "params": {"x": 0, "y": -int(H * 0.1), "width": W, "height": int(H * 0.55),
                                                             "feather": int(H * 0.12)}}
                label = "Sky (graduated)"
            if s.get("exposure"):
                cmds += [sel, {"command": "layer.newAdjustmentLayer.exposure", "params": {"exposure": round(s["exposure"], 2)}}]
                name(f"{label}: exposure {s['exposure']:+.1f}")
            if s.get("saturation") or s.get("contrast"):
                cmds += [sel, {"command": "layer.newAdjustmentLayer.vibrance", "params": {"vibrance": int(s.get("saturation", 0))}}]
                name(f"{label}: colour")
                if s.get("contrast"):
                    cmds += [sel, {"command": "layer.newAdjustmentLayer.brightnessContrast", "params": {"contrast": int(s["contrast"])}}]
                    name(f"{label}: contrast")
            cmds += [{"command": "select.all"}, {"command": "select.deselect"}]
        elif op == "look":
            cmds.append({"command": "layer.newAdjustmentLayer.colorLookup", "params": {"lut": s["name"]}})
            strength = int(s.get("strength") or 70)
            if strength < 100:
                cmds.append({"command": "layer.layerStyle.blendingOptions", "params": {"opacity": strength}})
            name(f"Look: {s['name']} {strength}%")
        elif op == "black_white":
            cmds.append({"command": "layer.newAdjustmentLayer.blackWhite", "params": {}})
            name("Black & white")
        elif op == "vignette" and s.get("amount"):
            cmds += [{"command": "layer.newFillLayer.gradient", "params": {"from": "#ffffff", "to": "#000000", "style": "radial"}},
                     {"command": "layer.layerStyle.blendingOptions", "params": {"blend": "multiply", "opacity": int(s["amount"] * 0.6)}}]
            name(f"Vignette {int(s['amount'])}")
        elif op == "logo" and logo:
            lw, lh = _size(logo)
            tw = W * s.get("width_pct", 18) / 100
            th_ = lh * tw / lw
            x, y = _place(s.get("position", "bottom-right"), W, H, tw, th_, min(W, H) * s.get("margin_pct", 4) / 100)
            cmds.append({"command": "file.placeEmbedded", "params": {"path": str(logo), "scale": round(tw / lw * 100, 2),
                                                                      "center": [round(x + tw / 2), round(y + th_ / 2)]}})
            name("Logo")
        elif op == "text":
            size = H * TEXT_SIZE.get(s.get("size"), TEXT_SIZE["large"]) if isinstance(s.get("size"), str) or not s.get("size") \
                else float(s["size"])
            margin = min(W, H) * 0.06
            text = str(s["text"]).strip()
            bw = W - 2 * margin
            per_line = max(4, int(bw / (size * 0.56)))
            lines = sum(max(1, -(-len(part) // per_line)) for part in text.split("\n"))
            bh = lines * size * 1.25 + size * 0.3
            pos = s.get("position", "bottom")
            x, y = _place(pos, W, H, bw, bh, margin)
            align = "left" if "left" in pos else ("right" if "right" in pos else "center")
            cmds.append({"command": "type.create", "params": {
                "x": round(x), "y": round(y), "box": [round(x), round(y), round(bw), round(bh + size)], "text": text,
                "align": align, "font": s.get("font") or FONT, "weight": int(s.get("weight") or 800), "size": round(size, 1),
                "color": s.get("color") or "#ffffff"}})
            if s.get("shadow", True):
                cmds.append({"command": "layer.layerStyle.dropShadow", "params": {
                    "color": "#000000", "opacity": 55, "distance": max(2, round(size * 0.05)), "size": max(4, round(size * 0.18))}})
    return cmds, extra


async def edit_one(file_id: str, steps: list[dict[str, Any]], logo_file_id: Optional[str], formats: list[str]) -> list[dict[str, Any]]:
    """Egy kép: (háttér) + parancsok → PSD és/vagy JPG/PNG. A PSD a rétegeket tartja."""
    from app.routes.make import _image_spec, _materialize
    from app.tools import crafts
    src = crafts.existing(file_id)
    logo = crafts.existing(logo_file_id) if logo_file_id else None
    ai_steps = [s for s in steps if s["op"] == "ai_edit"]
    ai_layers: list[dict[str, Any]] = []
    if ai_steps:
        ai_layers = await _ai_edits(src, [s["instruction"] for s in ai_steps])
    rest = [s for s in steps if s["op"] != "ai_edit"]
    if ai_steps or any(s["op"] == "background" for s in rest):
        # beillesztett réteg (AI-retus / új háttér) után az aktív réteg smart object: a pixelműveletek
        # (auto, élesítés) nem futnak rajta — a korrekciós rétegek igen, azok maradnak
        rest = [s for s in rest if s["op"] not in ("auto", "sharpen")]
    cmds, extra = await build_commands(src, rest, logo)
    cmds = ai_layers + cmds
    bg = extra.get("background") or {}
    kw: dict[str, Any] = {}
    if bg:
        kw["remove_background"] = True
        if bg.get("color"):
            cmds.insert(0, {"command": "layer.newFillLayer.solidColor", "params": {"color": bg["color"]}})
            cmds.insert(1, {"command": "layer.arrange.sendToBack"})
            name_bg = {"command": "layer.renameLayer", "params": {"name": "Background colour"}}
            cmds.insert(2, name_bg)
        elif bg.get("stock") or bg.get("ai"):
            kw["background_file_id"] = await _materialize(_image_spec({"stock": bg.get("stock")} if bg.get("stock") else {"ai": bg["ai"]}))
            kw["background_blur"] = float(bg.get("blur") or 0)
    out: list[dict[str, Any]] = []
    psd = await crafts.photo_run(file_id=file_id, commands=cmds, save_format="psd", **kw)
    out.append(psd)
    for fmt in formats:
        if fmt == "psd":
            continue
        from app.storage import display_label, output_path, write_meta
        from app.storage.files import new_file_id, public_url
        fid = new_file_id(fmt)
        await crafts.run_cli(crafts.photo_cli(), "photocraft-cli", "convert", str(output_path(psd["file_id"])), str(output_path(fid)),
                             *(["--quality", "90"] if fmt in ("jpg", "jpeg", "webp") else []))
        write_meta(fid, source_file_id=file_id, operation="photo edit", label=f"{Path(display_label(file_id)).stem} (edited).{fmt}")
        if ai_steps or (bg.get("ai")):
            from app.tools import ai_label
            ai_label.label(output_path(fid), ai_label.EDITED, "Edited with AI: " + "; ".join(
                [s["instruction"] for s in ai_steps] + (["generated background"] if bg.get("ai") else [])))
        out.append({"file_id": fid, "url": public_url(fid), "size_kb": output_path(fid).stat().st_size // 1024})
    from app.storage import display_label, write_meta
    write_meta(psd["file_id"], source_file_id=file_id, operation="photo edit (layers)",
               label=f"{Path(display_label(file_id)).stem} (edited, layers).psd")
    return out if "psd" in formats else out[1:] or out


async def _ai_edits(src: Path, instructions: list[str]) -> list[dict[str, Any]]:
    """Generatív retus (FLUX.1-Kontext-pro) egymás után; minden eredmény az eredetire illesztve, külön rétegként
    (az eredeti alatta marad → maszkkal bármi visszahozható). Kredit: lépésenként IMAGE_CREDIT_COST."""
    from PIL import Image
    from app.storage import temp_path
    from app.storage.files import new_file_id
    from app.tools import restore
    W, H = _size(src)
    cur = temp_path(new_file_id("png"))
    with Image.open(src) as im:
        from PIL import ImageOps
        ImageOps.exif_transpose(im).convert("RGB").save(cur)
    cmds: list[dict[str, Any]] = []
    for k, ins in enumerate(instructions, 1):
        charged = await restore._charge(1, ins[:80], "AI retouch")
        try:
            raw = await restore._kontext(cur, f"{ins.strip().rstrip('.')}. Change nothing else: keep the same subject, "
                                              "pose, colours, lighting, framing and every other detail exactly as they are.")
        except Exception:
            if charged:
                await restore._refund(1, ins[:80])
            raise
        nxt = temp_path(new_file_id("png"))
        await asyncio.to_thread(restore.align, cur, raw, nxt)
        cmds += [{"command": "file.placeEmbedded", "params": {"path": str(nxt), "scale": 100, "fit": False, "center": [W / 2, H / 2]}},
                 {"command": "layer.renameLayer", "params": {"name": f"AI edit: {ins.strip()[:60]}"}}]
        cur = nxt
    return cmds


async def run(file_ids: list[str], request: str, logo_file_id: Optional[str], formats: list[str],
              steps: Optional[list[dict[str, Any]]] = None, progress=None) -> dict[str, Any]:
    from app.tools import crafts
    if not file_ids:
        raise ValueError("Add at least one photo.")
    has_logo = bool(logo_file_id)
    if steps is None:
        if progress:
            progress(0, len(file_ids), "planning the edit")
        p = await plan(crafts.existing(file_ids[0]), request, has_logo)
        steps, summary = p["steps"], p.get("summary", "")
    else:
        steps, summary = clean_steps(steps, has_logo), "custom plan"
    if not steps:
        raise ValueError("Nothing to do — describe the edit (crop, light, colour, text, logo, background…).")
    files: list[dict[str, Any]] = []
    for i, fid in enumerate(file_ids):
        files += await edit_one(fid, [dict(s) for s in steps], logo_file_id, formats)
        if progress:
            progress(i + 1, len(file_ids), "editing")
    return {"files": files, "file_id": files[0]["file_id"], "plan": steps, "summary": summary, "photos": len(file_ids)}
