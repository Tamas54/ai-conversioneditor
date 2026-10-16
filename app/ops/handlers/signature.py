"""Aláírás-eszközök: létrehozás (rajz, fotó, szken, feltöltött kép → tiszta, átlátszó PNG, titkosítva mentve)
és elhelyezés PDF-be vagy DOCX-be (aláírásmező, „Aláírás:” sor, felirat, vagy megadott hely)."""
from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path
from typing import Any

from app.convert.options import OptionError
from app.ops.handlers import out_name


async def create(inputs: list[Path], o: dict[str, Any], out: Path) -> dict[str, Any]:
    from app import tenancy
    from app.formkit import signature as G
    if inputs:
        src = inputs[0]
    elif o.get("image_base64"):
        raw = re.sub(r"^data:image/[a-z]+;base64,", "", o["image_base64"])
        try:
            data = base64.b64decode(re.sub(r"\s", "", raw), validate=True)
        except ValueError:
            raise OptionError("image_base64: not valid base64")
        if len(data) > 5 * 1024 * 1024:
            raise OptionError("image_base64: at most 5 MB")
        src = out / "drawn.png"
        src.write_bytes(data)
    else:
        raise OptionError("Give an image of the signature (a photo, a scan, or a drawing as image_base64).")
    try:
        im = await asyncio.to_thread(G.clean, src, o.get("color"))
    except ValueError as e:
        raise OptionError(str(e))
    png = G.to_png(im)
    kind = o.get("kind") or "signature"
    name = o.get("name") or ("Initials" if kind == "initials" else "Signature")
    meta = await G.save(tenancy.current_user_id(), png, name, kind)
    safe = re.sub(r"[^\w-]+", "_", name)[:40] or "signature"
    dst = out / f"{safe}.png"
    dst.write_bytes(png)
    return {"files": [dst], "data": {"signature_id": meta["id"], "name": meta["name"], "kind": kind,
                                     "width": meta["width"], "height": meta["height"]}}


async def place(inputs: list[Path], o: dict[str, Any], out: Path) -> dict[str, Any]:
    from app import tenancy
    from app.catalog.formats import from_filename
    from app.formkit import signature as G
    src = inputs[0]
    png = out / "sig.png"
    if o.get("signature_id"):
        try:
            png.write_bytes(await G.load(tenancy.current_user_id(), o["signature_id"]))
        except KeyError:
            raise OptionError(f"signature_id: no saved signature {o['signature_id']} (create one with create-signature)")
    elif o.get("image_file"):
        img = o["_resolve"](o["image_file"])
        im = await asyncio.to_thread(G.clean, img, None)
        png.write_bytes(G.to_png(im))
    else:
        raise OptionError("signature_id (a saved signature) or image_file is required")
    fmt = from_filename(src.name)
    if fmt == "pdf":
        dst = out_name(out, src, "-signed", "pdf")
        try:
            info = await asyncio.to_thread(G.place_pdf, src, dst, png, placement=o.get("placement") or "auto",
                                           page=o.get("page"), x=o.get("x"), y=o.get("y"), width=o.get("width"),
                                           field=o.get("field"))
        except ValueError as e:
            raise OptionError(str(e))
    elif fmt == "docx":
        dst = out_name(out, src, "-signed", "docx")
        info = await asyncio.to_thread(G.place_docx, src, dst, png, field=o.get("field"),
                                       width_cm=(o["width"] / 28.35) if o.get("width") else None)
    else:
        raise OptionError("sign-document takes PDF or DOCX")
    return {"files": [dst], "data": info}
