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
import difflib
import json
import os
import re
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


class CliError(RuntimeError):
    """A Craft-CLI hibája; `stdout`: amit a hiba előtt kiírt (a lefutott parancsok eredményei)."""

    def __init__(self, msg: str, stdout: str = ""):
        super().__init__(msg)
        self.stdout = stdout


async def run_cli(exe: Optional[str], name: str, *args: str) -> str:
    if exe is None:
        raise RuntimeError(f"{name} isn't installed on this server")
    # *_NO_PREFS: ne írjon felhasználói beállítást a konténerbe; a napló stderr-re megy.
    env = {**os.environ, **{f"{a}CRAFT_NO_PREFS": "1" for a in ("WORD", "PHOTO", "VECTOR", "DECK", "PDF", "DESIGN", "GRID")}}
    proc = await asyncio.create_subprocess_exec(
        exe, *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=settings.WORDCRAFT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"{name} {args[0]}: timed out after {settings.WORDCRAFT_TIMEOUT_S}s")
    if proc.returncode != 0:
        msg = err.decode("utf-8", "replace").strip() or out.decode("utf-8", "replace").strip()
        raise CliError(msg or f"{name} {args[0]}: exit code {proc.returncode}", out.decode("utf-8", "replace"))
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
        raise ValueError(f"Command id missing: {c!r}")
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


def _rank(item: dict[str, Any], term: str, key: str) -> Optional[int]:
    """Kisebb = jobb: pontos azonosító 0, azonosító-eleje 1, azonosító-rész 2, címke/paraméter/leírás 3."""
    cid = str(item.get(key) or "").lower()
    if cid == term:
        return 0
    if cid.startswith(term):
        return 1
    if term in cid:
        return 2
    if term in json.dumps(item, ensure_ascii=False).lower():
        return 3
    return None


def search_items(items: list[dict[str, Any]], query: str, key: str = "id") -> tuple[list[dict[str, Any]], list[str]]:
    """query: egy vagy több kifejezés vesszővel/szóközzel („select.lasso, file.placeEmbedded") — mindegyikre a
    legjobb találatok, a pontos azonosító elöl; ha semmi: hasonló azonosítók."""
    from app.tools.run_report import similar_ids
    terms = [t for t in re.split(r"[,\s]+", query.lower()) if t]
    hits: dict[str, tuple[int, int, dict[str, Any]]] = {}
    missed: list[str] = []
    for ti, term in enumerate(terms):
        found = False
        for it in items:
            r = _rank(it, term, key)
            if r is not None:
                found = True
                k = str(it.get(key))
                if k not in hits or (r, ti) < hits[k][:2]:
                    hits[k] = (r, ti, it)
        if not found:
            missed.append(term)
    ordered = [it for _, _, it in sorted(hits.values(), key=lambda h: (h[1], h[0]))]
    near: list[str] = []
    ids = [str(i.get(key)) for i in items]
    for term in missed:                   # a nem talált kifejezésekre is javaslat (egy több-kifejezéses kérésben is)
        near += [x for x in similar_ids(term, ids, 6) if x not in near]
    return ordered, near


def _list_commands(raw: list[dict[str, Any]], query: Optional[str], offset: int, max_chars: int, key: str = "id") -> dict[str, Any]:
    if not query:
        return {"count": len(raw), "ids": [c[key] for c in raw],
                "hint": "pass query (one or more ids or words, e.g. 'select.lasso, text') for full params and labels"}
    hits, near = search_items(raw, query, key)
    page, nxt = page_items(hits, max(0, offset), max(1_000, max_chars))
    res: dict[str, Any] = {"count": len(hits), "commands": page, "offset": offset, "next_offset": nxt}
    if near:
        res["did_you_mean"] = near
        res["not_found"] = [x for x in re.split(r"[,\s]+", query) if x and not any(
            _rank(i, x.lower(), key) is not None for i in raw)]
    return res


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
        raise ValueError("Give file_id, or new (e.g. {\"width\": 1200, \"height\": 800})")
    src = existing(file_id) if file_id else None
    src_ext = src.suffix.lstrip(".").lower() if src else "png"
    fmt = (save_format or ("png" if remove_background and not background_file_id else src_ext)).lower().lstrip(".")
    if fmt not in PHOTO_SAVE:
        raise ValueError(f"save_format: {fmt} (choose from {', '.join(sorted(PHOTO_SAVE))})")
    tmp: list[Path] = []
    try:
        if remove_background:
            if not src:
                raise ValueError("remove_background needs a file_id")
            from app.tools import cutout
            cut = temp_path(f"{new_file_id('png')}")
            await cutout.remove_background(src, cut)
            tmp.append(cut)
            src = cut
        args = ["run", str(src)] if src else ["run", "--new", json.dumps(new)]
        steps: list[tuple[str, Optional[dict[str, Any]]]] = []
        if background_file_id:
            if not src:
                raise ValueError("background_file_id needs an existing image (file_id)")
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
        try:
            log_lines = await run_cli(photo_cli(), "photocraft-cli", *args)
        except CliError as e:
            raise ValueError(await _explain(e, "photo", steps)) from None
        before = await _photo_info(src) if src and src.suffix.lower() in (".psd", ".psb", ".pcraft") else None
    finally:
        for t in tmp:
            t.unlink(missing_ok=True)
    what = ("background replaced" if background_file_id else "background removed" if remove_background
            else "edited" if file_id else "")
    res = _finish(fid, file_id, "photocraft", _stem(file_id, what, "image"))
    res.update({"commands_run": len(steps), "removed_background": remove_background,
                "ms_elapsed": int((time.time() - t0) * 1000),
                "steps": run_report.steps(steps, run_report.engine_results(log_lines))})
    after = await _photo_info(output_path(fid))
    if after:
        res["before_after"] = {"before": before, "after": after} if before else {"after": after}
    return res


async def photo_read(mode: str, file_id: Optional[str] = None, query: Optional[str] = None,
                     offset: int = 0, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    if mode == "commands":
        return _list_commands(json.loads(await run_cli(photo_cli(), "photocraft-cli", "commands", "--json")),
                              query, offset, max_chars)
    if mode == "info":
        if not file_id:
            raise ValueError("read=info needs a file_id")
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
        raise ValueError(f"export: {bad} (choose from {', '.join(sorted(VECTOR_SAVE))})")
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
    parsed = [split_command(c) for c in commands or []]
    try:
        log_lines = await run_cli(vector_cli(), "vectorcraft-cli", *args)
    except CliError as e:
        raise ValueError(await _explain(e, "vector", parsed)) from None
    stem = _stem(file_id, "edited" if file_id else "", "design")
    files = [_finish(fid, file_id, "vectorcraft", stem) for fid in outs]
    res = {"files": files, "file_id": files[0]["file_id"], "commands_run": len(commands or []),
           "ms_elapsed": int((time.time() - t0) * 1000),
           "steps": run_report.steps(parsed, run_report.engine_results(log_lines)),
           "exports": [v.get("result") for v in run_report.json_stream(log_lines) if isinstance(v, dict) and v.get("step") == "export"]}
    vf = next((output_path(f["file_id"]) for f in files if f["file_id"].endswith((".vectorcraft", ".pdf", ".svg"))), None)
    if vf is not None:
        try:
            res["after"] = run_report.vector_summary(json.loads(await run_cli(vector_cli(), "vectorcraft-cli", "info", str(vf))))
        except Exception:
            pass
    return res


async def vector_read(mode: str, file_id: Optional[str] = None, query: Optional[str] = None,
                      offset: int = 0, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    if mode == "commands":
        return _list_commands(json.loads(await run_cli(vector_cli(), "vectorcraft-cli", "commands")),
                              query, offset, max_chars)
    if mode == "info":
        if not file_id:
            raise ValueError("read=info needs a file_id")
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


# ---------- „office” motorok: DeckCraft (PowerPoint), PdfCraft (Acrobat), DesignCraft (InDesign), GridCraft (Excel) ----------
# app-kulcsok: deck | pdf | layout (DesignCraft) | sheet (GridCraft)
# Egy közös futtató, alkalmazásonkénti parancssor-szintaxissal (mind kész upstream release, Apache-2.0).

OFFICE: dict[str, dict[str, Any]] = {
    "deck":   {"exe": "deckcraft",   "open": {"pptx", "deckcraft"},
               "save": {"pptx", "deckcraft", "pdf", "png", "jpeg", "jpg"}},
    "pdf":    {"exe": "pdfcraft",    "open": {"pdf"}, "save": {"pdf"}},
    "layout": {"exe": "designcraft", "open": {"designcraft", "idml"}, "save": {"designcraft", "pdf", "png"}},
    "sheet":  {"exe": "gridcraft",   "open": {"xlsx", "csv", "tsv", "json"},
               "save": {"xlsx", "csv", "tsv", "json", "html"}},
}


def office_cli(app: str) -> Optional[str]:
    exe = OFFICE[app]["exe"]
    return cli_path(os.getenv(f"{exe.upper()}_CLI", f"/opt/{exe}/bin/{exe}-cli"), f"{exe}-cli")


def office_web_dir(app_exe: str) -> Path:
    return Path(os.getenv(f"{app_exe.upper()}_WEB_DIR", f"/opt/{app_exe}-web"))


OFFICE_SAVE = set().union(*(s["save"] for s in OFFICE.values()))


async def office_run(app: str, *, file_id: Optional[str] = None, commands: Optional[list[Command]] = None,
                     save_format: Optional[str] = None) -> dict[str, Any]:
    """Parancsok (deck/design/grid) vagy PDF-eszközlépések (pdf) egy dokumentumon → új output file_id."""
    from app import tenancy
    if app not in OFFICE:
        raise ValueError(f"app: {app} (choose from {', '.join(OFFICE)})")
    spec, t0 = OFFICE[app], time.time()
    src = existing(file_id) if file_id else None
    if src and src.suffix.lstrip(".").lower() not in spec["open"]:
        raise ValueError(f"{app} can't open {src.suffix} files (it opens {', '.join(sorted(spec['open']))})")
    fmt = (save_format or (src.suffix.lstrip(".") if src else {"deck": "pptx", "pdf": "pdf", "layout": "pdf", "sheet": "xlsx"}[app])).lower()
    if fmt not in spec["save"]:
        raise ValueError(f"save_format: {fmt} (choose from {', '.join(sorted(spec['save']))})")
    fid, out = _new_output(fmt, file_id, app, "")
    exe, name = office_cli(app), f"{spec['exe']}-cli"
    if app == "pdf":
        if not src:
            raise ValueError("pdf needs a file_id")
        steps = [{"tool": c["tool"], "args": c.get("args") or {}} for c in commands or []
                 if isinstance(c, dict) and c.get("tool")]
        try:
            log_lines = await pdf_script([file_id], steps, out)
        except CliError as e:
            raise ValueError(await _explain(e, "pdf", [(st["tool"], st["args"]) for st in steps], offset=1)) from None
    else:
        args = ["run"] + (["--in", str(src)] if src else [])
        parsed = [split_command(c) for c in commands or []]
        for cid, params in parsed:
            args += ["--cmd", f"{cid}={json.dumps(params, ensure_ascii=False)}" if params else cid]
        if app == "sheet":
            args += ["--out", str(out), "--quiet"]
        elif app == "deck" and fmt in ("pptx", "deckcraft"):
            args += ["--save", str(out)]
        elif app == "layout" and fmt == "designcraft":
            args += ["--cmd", f"file.save={json.dumps({'path': str(out)})}"]
        else:
            args += ["--export", str(out)]
        try:
            log_lines = await run_cli(exe, name, *args)
        except CliError as e:
            raise ValueError(await _explain(e, app, parsed)) from None
    if not out.exists():
        raise RuntimeError(f"{name}: no output was produced ({log_lines[-300:]})")
    res = _finish(fid, file_id, app, _stem(file_id, _office_what(app, commands), _OFFICE_NEW[app]))
    res.update({"steps_run": len(commands or []), "ms_elapsed": int((time.time() - t0) * 1000)})
    try:
        if app == "pdf":
            res["steps"] = run_report.pdf_step_report(log_lines, steps)
            if fmt == "pdf":
                res["before_after"] = {"before": run_report.pdf_summary(src), "after": run_report.pdf_summary(out)}
        else:
            res["steps"] = run_report.steps(parsed, [])
            if app == "deck" and fmt == "pptx":
                ba = {"before": run_report.deck_summary(src)} if src and src.suffix.lower() == ".pptx" else {}
                res["before_after"] = {**ba, "after": run_report.deck_summary(out)}
    except Exception as e:  # a jelentés kényelmi — a kész fájlt nem buktatja
        res["report_error"] = str(e)[:200]
    return res


async def pdf_script(file_ids: list[str], steps: list[dict[str, Any]], out: Path, save: bool = True) -> str:
    """PdfCraft lépéssor: a file_ids sorban doc 1…n-ként nyílik meg; a lépések args-ában a "$out" a
    kimeneti útvonal, a {"file_id": …} paraméter útvonallá oldódik; save=True → a végén doc 1 mentése out-ba."""
    from app import tenancy
    from app.storage import temp_path
    script = [{"tool": "doc_open", "args": {"path": str(existing(f))}} for f in file_ids]
    for st in steps:
        args = resolve_file_ids(dict(st.get("args") or {}))
        args = {k: (str(out) if v == "$out" else v) for k, v in args.items()}
        if file_ids and st["tool"] != "doc_combine":
            args.setdefault("doc", 1)
        script.append({"tool": st["tool"], "args": args})
    if save:
        script.append({"tool": "doc_save", "args": {"doc": 1, "path": str(out), "full": True}})
    tmp = temp_path(new_file_id("json"))
    tmp.write_text(json.dumps(script, ensure_ascii=False))
    try:
        return await run_cli(office_cli("pdf"), "pdfcraft-cli", "run", "--script", str(tmp), "--root", str(tenancy.user_root()))
    finally:
        tmp.unlink(missing_ok=True)


async def office_read(app: str, mode: str, file_id: Optional[str] = None, query: Optional[str] = None,
                      offset: int = 0, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    if app not in OFFICE:
        raise ValueError(f"app: {app} (choose from {', '.join(OFFICE)})")
    exe, name = office_cli(app), f"{OFFICE[app]['exe']}-cli"
    if mode == "commands":
        if app == "pdf":
            raw = json.loads(await run_cli(exe, name, "tools"))
            # teljes leírás és paraméter-séma (típus, enum, leírás) — a csonkolt lista miatt kellett eddig külön keresgélni
            items = [{"tool": t["name"], "description": t.get("description", ""),
                      "params": (t.get("input_schema") or {}).get("properties", {}),
                      "required": (t.get("input_schema") or {}).get("required", []),
                      "read_only": t.get("read_only")} for t in raw]
        elif app == "sheet":
            items = json.loads(await run_cli(exe, name, "commands", "--json"))
        else:
            items = json.loads(await run_cli(exe, name, "commands"))
        key = "tool" if app == "pdf" else "id"
        res = _list_commands(items, query, offset, max_chars, key=key)
        if "commands" in res:
            res["items"] = res.pop("commands")
        return res
    if not file_id:
        raise ValueError(f"read={mode} needs a file_id")
    path = str(existing(file_id))
    if mode == "info":
        out = await run_cli(exe, name, *(["info", path, "--json"] if app == "sheet" else ["info", path]))
        try:
            return json.loads(out)
        except ValueError:
            return {"info": out[:max_chars]}
    if mode == "text":
        out = await run_cli(exe, name, *(["text", path] if app == "pdf" else ["cat", path, "--csv"] if app == "sheet" else ["info", path]))
        chunk = out[offset:offset + max_chars]
        return {"text": chunk, "offset": offset, "total_chars": len(out),
                "next_offset": offset + len(chunk) if offset + len(chunk) < len(out) else None}
    raise ValueError("read: info | text | commands")


async def office_convert(app: str, src: Path, dst: Path) -> None:
    await run_cli(office_cli(app), f"{OFFICE[app]['exe']}-cli", "convert", str(src), str(dst))


# ---------- jelentés-segédek (A3 nevek, A4 részletek) ----------

from app.tools import run_report  # noqa: E402

_OFFICE_NEW = {"deck": "presentation", "pdf": "document", "layout": "layout", "sheet": "spreadsheet"}


def _stem(source_file_id: Optional[str], what: str, fallback: str) -> str:
    from app.storage.naming import result_stem
    return result_stem(source_file_id, what, fallback)


def _office_what(app: str, commands: Optional[list[Command]]) -> str:
    tools = {str(c.get("tool") or c.get("command") or "") for c in commands or [] if isinstance(c, dict)}
    if app == "pdf":
        if any(t.startswith("redact") for t in tools):
            return "redacted"
        if "form_fill" in tools:
            return "form filled"
    return "edited"


async def _photo_info(path: Path) -> Optional[dict[str, Any]]:
    try:
        return run_report.photo_summary(json.loads(await run_cli(photo_cli(), "photocraft-cli", "info", str(path), "--compact")))
    except Exception:
        return None


_IDS_CACHE: dict[str, list[str]] = {}


async def _known_ids(engine: str) -> list[str]:
    """A motor parancsazonosítói (a hibaüzenet „hasonló parancsai"-hoz), folyamatonként egyszer lekérve."""
    if engine not in _IDS_CACHE:
        try:
            if engine == "photo":
                raw = json.loads(await run_cli(photo_cli(), "photocraft-cli", "commands", "--json"))
            elif engine == "vector":
                raw = json.loads(await run_cli(vector_cli(), "vectorcraft-cli", "commands"))
            elif engine == "pdf":
                raw = [{"id": t["name"]} for t in json.loads(await run_cli(office_cli("pdf"), "pdfcraft-cli", "tools"))]
            elif engine == "sheet":
                raw = json.loads(await run_cli(office_cli(engine), "gridcraft-cli", "commands", "--json"))
            else:
                raw = json.loads(await run_cli(office_cli(engine), f"{OFFICE[engine]['exe']}-cli", "commands"))
            _IDS_CACHE[engine] = [str(c.get("id")) for c in raw if c.get("id")]
        except Exception:
            return []
    return _IDS_CACHE[engine]


async def _explain(e: CliError, engine: str, commands: list[tuple[str, Optional[dict[str, Any]]]], offset: int = 0) -> str:
    return run_report.failure(engine, str(e), getattr(e, "stdout", ""), commands, await _known_ids(engine), offset=offset)
