"""PhotoCraft (képek) és VectorCraft (dizájn/layout) — a storytold Craft-motorok CLI-n át.

Mint a WordCraft (app/tools/wordcraft.py): a Dockerfile a rögzített upstream release-ből teszi a
CLI-ket a képbe, itt subprocessként hívjuk őket. Közös segédek is itt laknak (CLI-futtatás,
file_id → útvonal a parancsparaméterekben, lapozás), a WordCraft is ezeket használja.

    photocraft  Photoshop-szerű: autoTone, imageSize, crop, select.subject/sky, removeBackground,
                type.create, szűrők, rétegek; PSD/PNG/JPG/WebP/TIFF/EXR, ~860 parancs
    vectorcraft Illustrator/InDesign-szerű: file.new (A4…), artboard.new, text.create (area: tördel),
                file.place (kép), shape.*, változók; PDF/SVG/PNG/EPS export, ~730 parancs
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional, Union

from app.config import settings
from app.storage import display_label, input_path, output_path, public_url, write_meta
from app.storage.files import new_file_id

Command = Union[str, dict[str, Any]]
DEFAULT_MAX_CHARS = 20_000

PHOTO_OPEN = {"psd", "psb", "pcraft", "png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp", "gif", "exr", "tga"}
PHOTO_SAVE = {"pcraft", "psd", "png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp", "gif", "exr"}
VECTOR_OPEN = {"vectorcraft", "vctemplate", "svg", "svgz", "pdf", "ai", "eps", "dxf"}
VECTOR_SAVE = {"vectorcraft", "svg", "svgz", "pdf", "png", "jpg", "webp", "eps", "dxf", "tiff"}


# ---------- közös segédek ----------

def cli_path(configured: str, name: str) -> Optional[str]:
    if configured and Path(configured).is_file() and os.access(configured, os.X_OK):
        return configured
    return shutil.which(name)


async def run_cli(exe: Optional[str], name: str, *args: str) -> str:
    if exe is None:
        raise RuntimeError(f"{name} nincs telepítve")
    # *_NO_PREFS: ne írjon felhasználói beállítást a konténerbe; a napló stderr-re megy.
    env = {**os.environ, "WORDCRAFT_NO_PREFS": "1", "PHOTOCRAFT_NO_PREFS": "1", "VECTORCRAFT_NO_PREFS": "1"}
    proc = await asyncio.create_subprocess_exec(
        exe, *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=settings.WORDCRAFT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"{name} {args[0]}: időtúllépés ({settings.WORDCRAFT_TIMEOUT_S}s)")
    if proc.returncode != 0:
        msg = err.decode("utf-8", "replace").strip() or out.decode("utf-8", "replace").strip()
        raise RuntimeError(msg or f"{name} {args[0]}: kilépési kód {proc.returncode}")
    return out.decode("utf-8", "replace")


def existing(file_id: str) -> Path:
    for p in (input_path(file_id), output_path(file_id)):
        if p.exists():
            return p
    raise FileNotFoundError(f"file_id not found: {file_id}")


def resolve_file_ids(params: Any) -> Any:
    """Parancsparaméterben "file_id" → "path" (az ügynök nem ismeri a szerver fájlrendszerét,
    base64 pedig nem fér a parancssorba: Linuxon 128 KB / argumentum)."""
    if isinstance(params, dict) and params.get("file_id"):
        return {k: v for k, v in params.items() if k != "file_id"} | {"path": str(existing(params["file_id"]))}
    return params


def split_command(c: Command) -> tuple[str, Optional[dict[str, Any]]]:
    """{"command": id, "params": {...}} vagy "id={json}" vagy "id" → (id, params)."""
    if isinstance(c, str):
        cid, _, raw = c.strip().partition("=")
        return cid, (json.loads(raw) if raw else None)
    cid = c.get("command") or c.get("id")
    if not cid:
        raise ValueError(f"parancs id hiányzik: {c!r}")
    return str(cid), resolve_file_ids(c.get("params"))


def page_items(items: list[Any], offset: int, max_chars: int) -> tuple[list[Any], Optional[int]]:
    """items[offset:]-ből annyit, amennyi JSON-ban max_chars alá fér (legalább egyet)."""
    page, size, i = [], 0, offset
    while i < len(items):
        n = len(json.dumps(items[i], ensure_ascii=False))
        if page and size + n > max_chars:
            break
        page.append(items[i]); size += n; i += 1
    return page, (i if i < len(items) else None)


def _list_commands(raw: list[dict[str, Any]], query: Optional[str], offset: int, max_chars: int) -> dict[str, Any]:
    if not query:
        return {"count": len(raw), "ids": [c["id"] for c in raw],
                "hint": "pass query (e.g. 'text', 'crop') for params and labels"}
    q = query.lower()
    hits = [c for c in raw if q in json.dumps(c, ensure_ascii=False).lower()]
    page, nxt = page_items(hits, max(0, offset), max(1_000, max_chars))
    return {"count": len(hits), "commands": page, "offset": offset, "next_offset": nxt}


def _new_output(fmt: str, source_file_id: Optional[str], op: str, stem: str) -> tuple[str, Path]:
    fid = new_file_id(fmt)
    return fid, output_path(fid)


def _finish(fid: str, source_file_id: Optional[str], op: str, stem: str) -> dict[str, Any]:
    write_meta(fid, source_file_id=source_file_id, operation=op, label=f"{stem}.{fid.rsplit('.', 1)[1]}")
    return {"file_id": fid, "url": public_url(fid), "size_kb": output_path(fid).stat().st_size // 1024}


# ---------- PhotoCraft ----------

def photo_cli() -> Optional[str]:
    return cli_path(settings.PHOTOCRAFT_CLI, "photocraft-cli")


async def photo_run(
    *, file_id: Optional[str] = None, new: Optional[dict[str, Any]] = None,
    commands: Optional[list[Command]] = None, save_format: Optional[str] = None,
    quality: Optional[int] = None, remove_background: bool = False,
    background_file_id: Optional[str] = None, background_blur: float = 0,
) -> dict[str, Any]:
    """PhotoCraft-parancsok egy képen (vagy új vásznon), új output file_id-be.
    remove_background: előbb ISNet-kivágás (az alany marad, átlátszó háttér).
    background_file_id: ez a kép kerül az alany mögé, a vásznat kitöltve (+ background_blur px elmosás)."""
    from app.storage import temp_path
    t0 = time.time()
    if not file_id and not new:
        raise ValueError("file_id vagy new (pl. {\"width\": 1200, \"height\": 800}) kell")
    src = existing(file_id) if file_id else None
    src_ext = src.suffix.lstrip(".").lower() if src else "png"
    fmt = (save_format or ("png" if remove_background and not background_file_id else src_ext)).lower().lstrip(".")
    if fmt not in PHOTO_SAVE:
        raise ValueError(f"save_format: {fmt} (lehet: {', '.join(sorted(PHOTO_SAVE))})")
    tmp: list[Path] = []
    try:
        if remove_background:
            if not src:
                raise ValueError("remove_background file_id-t kér")
            from app.tools import cutout
            cut = temp_path(f"{new_file_id('png')}")
            await cutout.remove_background(src, cut)
            tmp.append(cut)
            src = cut
        args = ["run", str(src)] if src else ["run", "--new", json.dumps(new)]
        steps: list[tuple[str, Optional[dict[str, Any]]]] = []
        if background_file_id:
            if not src:
                raise ValueError("background_file_id egy meglévő képhez (file_id) kell")
            from PIL import Image
            with Image.open(src) as a, Image.open(existing(background_file_id)) as b:
                # „cover”: a háttér a teljes vásznat kitölti; +8 %, hogy az elmosás széle ne látsszon
                scale = max(a.width / b.width, a.height / b.height) * 108
            steps.append(("file.placeEmbedded", {"path": str(existing(background_file_id)), "scale": round(scale, 1)}))
            if background_blur:
                steps.append(("filter.blur.gaussianBlur", {"radius": float(background_blur)}))
            steps.append(("layer.arrange.sendToBack", None))
        steps += [split_command(c) for c in commands or []]
        for cid, params in steps:
            args += ["--cmd", cid] + (["--params", json.dumps(params, ensure_ascii=False)] if params else [])
        fid, out = _new_output(fmt, file_id, "photocraft", "")
        args += ["--out", str(out)]
        if quality:
            args += ["--quality", str(int(quality))]
        log_lines = await run_cli(photo_cli(), "photocraft-cli", *args)
    finally:
        for t in tmp:
            t.unlink(missing_ok=True)
    stem = Path(display_label(file_id)).stem if file_id else "kep"
    res = _finish(fid, file_id, "photocraft", stem)
    res.update({"commands_run": len(steps), "removed_background": remove_background,
                "ms_elapsed": int((time.time() - t0) * 1000), "results": _json_lines(log_lines)[-10:]})
    return res


async def photo_read(mode: str, file_id: Optional[str] = None, query: Optional[str] = None,
                     offset: int = 0, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    if mode == "commands":
        return _list_commands(json.loads(await run_cli(photo_cli(), "photocraft-cli", "commands", "--json")),
                              query, offset, max_chars)
    if mode == "info":
        if not file_id:
            raise ValueError("read=info file_id-t kér")
        return json.loads(await run_cli(photo_cli(), "photocraft-cli", "info", str(existing(file_id)), "--compact"))
    raise ValueError("read: info | commands")


# ---------- VectorCraft ----------

def vector_cli() -> Optional[str]:
    return cli_path(settings.VECTORCRAFT_CLI, "vectorcraft-cli")


async def vector_run(
    *, file_id: Optional[str] = None, commands: Optional[list[Command]] = None,
    export: Optional[list[str]] = None, scale: Optional[float] = None,
) -> dict[str, Any]:
    t0 = time.time()
    formats = [f.lower().lstrip(".") for f in (export or ["pdf"])]
    bad = [f for f in formats if f not in VECTOR_SAVE]
    if bad:
        raise ValueError(f"export: {bad} (lehet: {', '.join(sorted(VECTOR_SAVE))})")
    args = ["run"] + (["--in", str(existing(file_id))] if file_id else [])
    for c in commands or []:
        cid, params = split_command(c)
        if cid == "file.place" and isinstance(params, dict) and "link" not in params:
            # Beágyazva: egy szerveroldali útvonalra mutató link a böngésző-szerkesztőben és
            # máshol olvashatatlan („linked file can't be read here”) — a dizájn legyen önhordó.
            params = {**params, "link": False}
        args += ["--cmd", cid] + (["--params", json.dumps(params, ensure_ascii=False)] if params else [])
    outs = []
    for fmt in formats:
        fid, out = _new_output(fmt, file_id, "vectorcraft", "")
        outs.append(fid)
        args += ["--export", str(out)]
    if scale:
        args += ["--scale", str(scale)]
    log_lines = await run_cli(vector_cli(), "vectorcraft-cli", *args)
    stem = Path(display_label(file_id)).stem if file_id else "dizajn"
    files = [_finish(fid, file_id, "vectorcraft", stem) for fid in outs]
    return {"files": files, "file_id": files[0]["file_id"], "commands_run": len(commands or []),
            "ms_elapsed": int((time.time() - t0) * 1000),
            "results": [r for r in _json_lines(log_lines) if r.get("step") == "cmd"][-10:]}


async def vector_read(mode: str, file_id: Optional[str] = None, query: Optional[str] = None,
                      offset: int = 0, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    if mode == "commands":
        return _list_commands(json.loads(await run_cli(vector_cli(), "vectorcraft-cli", "commands")),
                              query, offset, max_chars)
    if mode == "info":
        if not file_id:
            raise ValueError("read=info file_id-t kér")
        return json.loads(await run_cli(vector_cli(), "vectorcraft-cli", "info", str(existing(file_id))))
    raise ValueError("read: info | commands")


async def convert_file(engine: str, src: Path, dst: Path) -> None:
    """A `convert` tool engine='photocraft'|'vectorcraft' útja (formátum a kiterjesztésekből)."""
    if engine == "photocraft":
        await run_cli(photo_cli(), "photocraft-cli", "convert", str(src), str(dst))
    else:
        await run_cli(vector_cli(), "vectorcraft-cli", "convert", str(src), str(dst))


def _json_lines(text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out
