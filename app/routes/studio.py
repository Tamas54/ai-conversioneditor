"""Dizájnstúdió végpontjai: automata tervező (3 változat → választás → kampánycsomag), tömeges személyre szabás,
és a felhasználó arculatai (tanulás mintákból, szerkesztés, törlés).

    POST   /api/make/design/auto      {goal?, kicker?, title, subtitle?, body?, date?, place?, cta?, handle?, image?,
                                       sizes?[], size?, variants?=3, brand?}            → {variants: [...]}  (a változatok rejtettek)
    POST   /api/make/design/package   {from_file_id, sizes?[], content?{...}}          → {posts: [{size, files}], file_id}
    POST   /api/make/design/mass      {data_file_id, template? | design_file_id?, data?, size?, brand?}   → munka (job)
    GET    /api/design/columns?file_id=…                                                → {columns, rows, sample}
    GET    /api/brand-kits · POST /api/brand-kits/learn {file_ids[], name?} (munka) · POST /api/brand-kits {name, colors, fonts}
    PATCH  /api/brand-kits/{id} {name?, colors?, fonts?, rules?, logo_file_id?, auto_apply?, is_default?} · DELETE /api/brand-kits/{id}
    GET    /api/brand-kits/fonts                                                           → a választható betűcsaládok

A választott terv (spec), a tartalom, a kép és az arculat a változat fájljainak metájában utazik ("design"), így a
csomag bármikor, bármely méretben újra elkészíthető — a felületről és az MCP-ről ugyanígy.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app import db, events, jobs, tenancy

log = logging.getLogger("aice.studio")
router = APIRouter()
CONTENT_KEYS = {"goal": 400, "kicker": 60, "title": 160, "subtitle": 200, "body": 900, "date": 80, "place": 120, "cta": 80,
                "handle": 60}


def _content(body: dict[str, Any]) -> dict[str, Any]:
    return {k: str(body[k]).strip()[:n] for k, n in CONTENT_KEYS.items() if str(body.get(k) or "").strip()}


def _photo_dict(p) -> Optional[dict[str, Any]]:
    if not p:
        return None
    return {"file_id": p.file_id, "w": p.w, "h": p.h, "focus": list(p.focus), "box": list(p.box) if p.box else None,
            "people": p.people, "colors": p.colors}


def _photo_from(d: Optional[dict[str, Any]]):
    from app.tools import crafts, design_auto as da
    if not d:
        return None
    try:
        path = crafts.existing(d["file_id"])
    except Exception:
        return None
    return da.Photo(file_id=d["file_id"], path=path, w=int(d["w"]), h=int(d["h"]), focus=tuple(d.get("focus") or (0.5, 0.45)),
                    box=tuple(d["box"]) if d.get("box") else None, people=bool(d.get("people")), colors=list(d.get("colors") or []))


def _brand_ref(brand: Optional[dict[str, Any]]) -> Any:
    if not brand:
        return "none"
    return brand.get("id") or brand.get("_registry") or "none"


def _short(label: str) -> str:
    return label.split(" · ")[0]


async def auto_variants(body: dict[str, Any]) -> dict[str, Any]:
    """3 (1–4) eltérő terv ugyanarra a tartalomra, az előnézeti méretben — a fájljaik rejtettek, amíg nem választanak."""
    from app.brands import user as user_brands
    from app.routes.make import _image_spec, _materialize, _new_group
    from app.storage import write_meta
    from app.tools import design_auto as da
    content = _content(body)
    if not content.get("title"):
        raise ValueError("Add a headline.")
    sizes = [da.size_key(s) for s in (body.get("sizes") or [body.get("size") or "ig_post"])]
    preview = da.size_key(body.get("size") or sizes[0])
    n = max(1, min(4, int(body.get("variants") or 3)))
    ai = (not tenancy.is_guest()) and body.get("ai", True) is not False
    img = body.get("image")             # felület: {file_id}|{url}|{stock}|{ai}; MCP: "<file_id>"|{"url"}|{"stock"}|{"generate"}
    image = await _materialize(_image_spec(img) if isinstance(img, dict) and not img.get("generate") else img)
    if image and body.get("remove_background") and body.get("background"):
        # alany kivágása + új háttér egyszer — minden változat és méret ezt kapja
        from app.tools import crafts
        bg = body["background"]
        bg_id = await _materialize(_image_spec(bg) if isinstance(bg, dict) and not bg.get("generate") else bg)
        comp = await crafts.photo_run(file_id=image, remove_background=True, background_file_id=bg_id,
                                      background_blur=float(body.get("blur") or 12), save_format="jpg", quality=90)
        image = comp["file_id"]
    brand = await user_brands.resolve(body.get("brand"))
    photo = await da.analyze(image) if image else None
    specs = await da.plan(content, photo, brand, n=n, ai=ai)
    tmp: list[Path] = []
    try:
        results = await asyncio.gather(*[da.render(s, content, preview, photo=photo, brand=brand, tmp=tmp) for s in specs])
    finally:
        for p in tmp:
            p.unlink(missing_ok=True)
    out = []
    title = content["title"][:50]
    for s, r in zip(specs, results):
        g = _new_group()
        ids = [f["file_id"] for f in r["files"]]
        info = {"spec": s, "content": content, "image": image, "brand": _brand_ref(brand), "photo": _photo_dict(photo),
                "sizes": sizes, "preview": preview, "files": ids}
        for f in r["files"]:
            ext = f["file_id"].rsplit(".", 1)[-1]
            f["label"] = f"{title} – {s['name']} ({_short(da.SIZES[preview][2])}).{ext}"
            write_meta(f["file_id"], label=f["label"], operation="design (option)",
                       extra={"design": info, "group": g, "variant": preview, "ephemeral": True})
        vc = next((x for x in r["files"] if x["file_id"].endswith(".vectorcraft")), r["files"][0])
        png = next((x for x in r["files"] if x["file_id"].endswith(".png")), None)
        out.append({"name": s["name"], "why": s.get("why"), "archetype": s["archetype"], "fonts": s["fonts"],
                    "palette": s["palette"], "file_id": vc["file_id"], "preview_file_id": png and png["file_id"],
                    "preview": png and png["url"], "files": r["files"]})
    events.emit("tool_done", tool="design_auto", result={"variants": [v["file_id"] for v in out]})
    return {"variants": out, "size": preview, "sizes": sizes, "ai": ai, "brand": brand and brand.get("name"),
            "next": "Pick one, then make every size: POST /api/make/design/package {from_file_id, sizes} "
                    "(MCP: design(from_design=<file_id>, sizes=[…]))."}


async def package(body: dict[str, Any]) -> dict[str, Any]:
    """A választott terv minden kért méretben (újratördelve) — egy csoport, az első méret nyílik a szerkesztőben."""
    from app.brands import user as user_brands
    from app.routes.make import _new_group
    from app.storage import read_meta, write_meta
    from app.tools import design_auto as da
    fid = body.get("from_file_id") or body.get("file_id")
    info = (read_meta(fid) or {}).get("design") if fid else None
    if not info:
        raise ValueError("Pick one of the designs first (its file_id) — this file isn't an auto design.")
    sizes = [da.size_key(s) for s in (body.get("sizes") or info.get("sizes") or [info.get("preview") or "ig_post"])]
    sizes = list(dict.fromkeys(sizes))
    content = {**info["content"], **_content(body.get("content") or {})}
    spec = info["spec"]
    try:
        brand = await user_brands.resolve(info.get("brand"))
    except ValueError:
        brand = None                                       # az arculatot azóta törölték
    photo = _photo_from(info.get("photo"))
    group = _new_group()
    title = (content.get("title") or "Design")[:60]
    posts = []
    reuse = info.get("preview") in sizes and content == info["content"]
    tmp: list[Path] = []
    try:
        for key in sizes:
            if reuse and key == info.get("preview"):         # az előnézet már kész: csak láthatóvá válik
                files = []
                for x in info.get("files") or []:
                    ext = x.rsplit(".", 1)[-1]
                    label = f"{title} ({_short(da.SIZES[key][2])}).{ext}"
                    write_meta(x, label=label, operation="design", extra={"ephemeral": False, "group": group, "variant": key})
                    from app.storage import public_url
                    files.append({"file_id": x, "url": public_url(x), "label": label})
            else:
                r = await da.render(spec, content, key, photo=photo, brand=brand, tmp=tmp)
                files = r["files"]
                for f in files:
                    ext = f["file_id"].rsplit(".", 1)[-1]
                    f["label"] = f"{title} ({_short(da.SIZES[key][2])}).{ext}"
                    f["name"] = f["label"]          # a craft-futás „design.*” neve helyett a valódi
                    write_meta(f["file_id"], label=f["label"], operation="design",
                               extra={"design": {**info, "content": content, "sizes": sizes, "preview": key,
                                                 "files": [x["file_id"] for x in files]},
                                      "group": group, "variant": key})
            posts.append({"size": key, "size_label": da.SIZES[key][2], "files": files})
    finally:
        for p in tmp:
            p.unlink(missing_ok=True)
    first = next((f for f in posts[0]["files"] if f["file_id"].endswith(".vectorcraft")), posts[0]["files"][0])
    n = sum(len(p["files"]) for p in posts)
    events.emit("tool_done", tool="design_package", result={"file_id": first["file_id"], "n": n})
    return {"posts": posts, "file_id": first["file_id"], "n": n}


def _err(e: Exception) -> HTTPException:
    if isinstance(e, ValueError):
        return HTTPException(400, str(e))
    from app.routes.make import _fail
    return _fail(e)


@router.post("/api/make/design/auto")
async def api_design_auto(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return await auto_variants(body)
    except HTTPException:
        raise
    except Exception as e:
        raise _err(e)


@router.post("/api/make/design/package")
async def api_design_package(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return await package(body)
    except HTTPException:
        raise
    except Exception as e:
        raise _err(e)


@router.post("/api/make/design/mass")
async def api_design_mass(body: dict[str, Any]) -> dict[str, Any]:
    from app.brands import user as user_brands
    from app.routes.make import _image_spec
    from app.tools import design_mass
    if not body.get("data_file_id"):
        raise HTTPException(400, "Add the spreadsheet — a CSV or Excel file, one row per copy, column names in the first row.")
    try:
        brand = await user_brands.resolve(body.get("brand"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    data = dict(body.get("data") or {})
    if isinstance(data.get("image"), dict):
        data["image"] = _image_spec(data["image"])

    async def work(progress):
        r = await design_mass.run(data_file_id=body["data_file_id"], template=body.get("template"), data=data,
                                  design_file_id=body.get("design_file_id"), size=body.get("size"), brand=brand,
                                  theme={"accent": body["accent"]} if body.get("accent") else None, progress=progress)
        events.emit("tool_done", tool="design_mass", result={"file_id": r["file_id"], "copies": r["copies"]})
        return r
    return jobs.start("design_mass", work)


@router.get("/api/design/columns")
async def api_design_columns(file_id: str) -> dict[str, Any]:
    from app.tools import crafts, design_mass, forms
    try:
        rows = await asyncio.to_thread(forms.read_rows, crafts.existing(file_id))
    except Exception as e:
        raise HTTPException(400, f"Can't read this spreadsheet: {e}")
    rows = [r for r in rows if any(str(v or "").strip() for v in r.values())]
    return {"columns": design_mass.columns_of(rows), "rows": len(rows), "sample": rows[0] if rows else {}}


# ---------- arculatok ----------

def _need_db() -> None:
    if not db.enabled():
        raise HTTPException(503, "Brands are kept in the database, which isn't connected right now.")


def _uid() -> str:
    return tenancy.current_user_id()


@router.get("/api/brand-kits")
async def api_brands() -> dict[str, Any]:
    from app.brands import user as user_brands
    if not db.enabled():
        return {"brands": [], "enabled": False}
    rows = await db.brand_list(_uid())
    return {"brands": [user_brands.public(user_brands._from_row(r)) for r in rows], "enabled": True}


@router.get("/api/brand-kits/fonts")
async def api_brand_fonts() -> dict[str, Any]:
    from app.tools import design_auto as da
    return {"heading": [{"family": f, "kind": v["kind"]} for f, v in da.FAMILIES.items()],
            "body": [{"family": f} for f in da.BODY_FAMILIES]}


@router.post("/api/brand-kits/learn")
async def api_brand_learn(body: dict[str, Any]):
    from app.auth import ACCOUNT_NEEDED
    from app.brands import learn, user as user_brands
    if tenancy.is_guest():
        return JSONResponse({"detail": ACCOUNT_NEEDED, "account_required": True}, status_code=401)
    _need_db()
    ids = [x for x in body.get("file_ids") or [] if x]
    if not ids:
        raise HTTPException(400, "Add a few examples of the brand — posts, slides, a PDF or the logo.")
    uid = _uid()

    async def work(progress):
        spec = await learn.learn(ids, body.get("name"), progress)
        row = await db.brand_create(uid, spec["name"], spec, make_default=True if body.get("make_default", True) else None)
        return {"brand": user_brands.public(user_brands._from_row(row))}
    return jobs.start("brand_learn", work)


@router.post("/api/brand-kits")
async def api_brand_create(body: dict[str, Any]) -> dict[str, Any]:
    from app.brands import user as user_brands
    _need_db()
    spec = _clean_spec(body)
    row = await db.brand_create(_uid(), spec.get("name") or "My brand", spec)
    return {"brand": user_brands.public(user_brands._from_row(row))}


def _clean_spec(body: dict[str, Any], base: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Kézi szerkesztés: csak érvényes színek és elérhető betűcsaládok kerülnek be."""
    from app.tools import design_auto as da
    import re
    hexre = re.compile(r"^#[0-9A-Fa-f]{6}$")
    spec = {k: v for k, v in (base or {}).items() if not k.startswith("_") and k not in ("id", "is_default", "auto_apply", "logo_url")}
    if body.get("name"):
        spec["name"] = str(body["name"]).strip()[:80]
    if isinstance(body.get("colors"), dict):
        cols = dict(spec.get("colors") or {})
        for k in ("primary", "accent", "bg", "text"):
            v = str(body["colors"].get(k) or "")
            if hexre.match(v):
                cols[k] = v.upper()
        if isinstance(body["colors"].get("extra"), list):
            cols["extra"] = [c.upper() for c in body["colors"]["extra"] if hexre.match(str(c))][:4]
        spec["colors"] = cols
    if isinstance(body.get("fonts"), dict):
        f = dict(spec.get("fonts") or {})
        if body["fonts"].get("heading") in da.FAMILIES:
            f["heading"] = body["fonts"]["heading"]
        if body["fonts"].get("body") in da.BODY_FAMILIES:
            f["body"] = body["fonts"]["body"]
        spec["fonts"] = f
    if isinstance(body.get("rules"), dict):
        spec["rules"] = {**(spec.get("rules") or {}), **{k: v for k, v in body["rules"].items() if isinstance(v, (str, list))}}
    if "logo_file_id" in body:
        spec["logo_file_id"] = body["logo_file_id"] or None
    return spec


@router.patch("/api/brand-kits/{brand_id}")
async def api_brand_update(brand_id: int, body: dict[str, Any]) -> dict[str, Any]:
    from app.brands import user as user_brands
    _need_db()
    row = await db.brand_get(_uid(), brand_id)
    if not row:
        raise HTTPException(404, "No such brand.")
    spec = _clean_spec(body, dict(row.get("spec") or {}))
    row = await db.brand_update(_uid(), brand_id, name=spec.get("name"), spec=spec,
                                auto_apply=body.get("auto_apply") if isinstance(body.get("auto_apply"), bool) else None,
                                is_default=True if body.get("is_default") is True else None)
    return {"brand": user_brands.public(user_brands._from_row(row))}


@router.delete("/api/brand-kits/{brand_id}")
async def api_brand_delete(brand_id: int) -> dict[str, Any]:
    _need_db()
    if not await db.brand_delete(_uid(), brand_id):
        raise HTTPException(404, "No such brand.")
    return {"deleted": brand_id}
