"""WordCraft — a Rust Word-motor (storytold/wordcraft, MIT OR Apache-2.0) CLI-n át.

A Dockerfile a rögzített upstream release-ből teszi a `wordcraft-cli`-t a képbe; itt csak
subprocessként hívjuk. Három dolgot ad:
    - run:     a motor parancsai (ugyanaz a ~460, ami a szalagot hajtja) egy dokumentumon,
               új verzióba mentve; változáskövetés a megadott szerző nevén
    - read:    text | info | inspect (szerkezet JSON) | commands (parancslista)
    - convert: gyors konverzió LibreOffice nélkül (docx/odt/rtf/html/md/tex/txt → … / pdf / png)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional, Union

from app.config import settings
from app.storage import display_label, input_path, output_path, public_url, write_meta
from app.storage.files import new_file_id

log = logging.getLogger("aice.wordcraft")

# `wordcraft-cli convert` / `--save` célformátumai, és amit megnyitni tud.
SAVE_FORMATS = {"docx", "pdf", "odt", "rtf", "html", "md", "tex", "txt", "json", "png"}
OPEN_FORMATS = {"docx", "docm", "dotx", "dotm", "doc", "dot", "odt", "rtf", "html", "htm", "md", "tex", "txt", "json"}
TEMPLATES = {"blank", "sample", "letter", "resume", "report"}
READ_MODES = {"text", "info", "inspect", "commands"}

Command = Union[str, dict[str, Any]]


def cli_path() -> Optional[str]:
    p = settings.WORDCRAFT_CLI
    if p and Path(p).is_file() and os.access(p, os.X_OK):
        return p
    return shutil.which("wordcraft-cli")


def available() -> bool:
    return cli_path() is not None


async def _cli(*args: str) -> str:
    exe = cli_path()
    if exe is None:
        raise RuntimeError("wordcraft-cli nincs telepítve (WORDCRAFT_CLI)")
    # WORDCRAFT_NO_PREFS: ne írjon ~/.config-ot, a napló stderr-re megy.
    env = {**os.environ, "WORDCRAFT_NO_PREFS": "1"}
    proc = await asyncio.create_subprocess_exec(
        exe, *args, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=settings.WORDCRAFT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"wordcraft-cli {args[0]}: időtúllépés ({settings.WORDCRAFT_TIMEOUT_S}s)")
    if proc.returncode != 0:
        msg = err.decode("utf-8", "replace").strip() or out.decode("utf-8", "replace").strip()
        raise RuntimeError(msg or f"wordcraft-cli {args[0]}: kilépési kód {proc.returncode}")
    return out.decode("utf-8", "replace")


def _existing(file_id: str) -> Path:
    for p in (input_path(file_id), output_path(file_id)):
        if p.exists():
            return p
    raise FileNotFoundError(f"file_id not found: {file_id}")


def _cmd_arg(c: Command) -> str:
    """{"command": "select.text", "params": {...}} vagy a CLI saját 'id={json}' alakja."""
    if isinstance(c, str):
        return c.strip()
    cmd = c.get("command") or c.get("id")
    if not cmd:
        raise ValueError(f"parancs id hiányzik: {c!r}")
    params = c.get("params")
    return f"{cmd}={json.dumps(params, ensure_ascii=False)}" if params else str(cmd)


async def run(
    *,
    file_id: Optional[str] = None,
    commands: Optional[list[Command]] = None,
    author: Optional[str] = None,
    save_format: str = "docx",
    template: Optional[str] = None,
) -> dict[str, Any]:
    """Parancsok futtatása egy dokumentumon (vagy sablonból indulva), új output file_id-be."""
    t0 = time.time()
    fmt = save_format.lower().lstrip(".")
    if fmt not in SAVE_FORMATS:
        raise ValueError(f"save_format: {fmt} (lehet: {', '.join(sorted(SAVE_FORMATS))})")
    args = ["run"]
    if file_id:
        args += ["--file", str(_existing(file_id))]
    else:
        tpl = template or "blank"
        if tpl not in TEMPLATES:
            raise ValueError(f"template: {tpl} (lehet: {', '.join(sorted(TEMPLATES))})")
        args += ["--template", tpl]
    if author:
        args += ["--author", author]
    cmd_args = [_cmd_arg(c) for c in (commands or [])]
    for a in cmd_args:
        args += ["--cmd", a]

    out_id = new_file_id(fmt)
    args += ["--save", str(output_path(out_id))]
    await _cli(*args)
    stem = Path(display_label(file_id)).stem if file_id else (template or "dokumentum")
    write_meta(out_id, source_file_id=file_id, operation="wordcraft", label=f"{stem}.{fmt}")
    return {
        "file_id": out_id,
        "url": public_url(out_id),
        "size_kb": output_path(out_id).stat().st_size // 1024,
        "commands_run": len(cmd_args),
        "author": author,
        "ms_elapsed": int((time.time() - t0) * 1000),
    }


async def read(mode: str, file_id: Optional[str] = None, query: Optional[str] = None) -> dict[str, Any]:
    if mode not in READ_MODES:
        raise ValueError(f"read: {mode} (lehet: {', '.join(sorted(READ_MODES))})")
    if mode == "commands":
        cmds = json.loads(await _cli("commands", "--json"))
        if query:
            q = query.lower()
            cmds = [c for c in cmds if q in json.dumps(c, ensure_ascii=False).lower()]
        return {"count": len(cmds), "commands": cmds}
    if not file_id:
        raise ValueError(f"read={mode} file_id-t kér")
    out = await _cli(mode, str(_existing(file_id)))
    if mode == "text":
        return {"text": out}
    return json.loads(out)


async def convert_file(src: Path, dst: Path) -> None:
    """Konverzió a kiterjesztések alapján (a `convert` tool engine='wordcraft' útja)."""
    await _cli("convert", str(src), str(dst))
