"""
MCP-over-HTTP endpoint.

Uses the official `mcp` Python SDK in Streamable-HTTP mode. Tools are
registered as async functions; the SDK handles JSON-RPC framing.

Auth:
    If MCP_AUTH_TOKEN is set, clients must send `Authorization: Bearer <token>`.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from pydantic import BaseModel
from typing import Any, Optional, Union

from fastapi import FastAPI, HTTPException, Request
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.config import settings
from app.storage import (
    input_path, new_file_id, save_bytes, public_url, write_meta,
)
from app.tools import (
    convert as t_convert,
    delete_pages as t_delete_pages,
    delete_section as t_delete_section,
    edit_with_instruction as t_edit_ai,
    extract as t_extract,
    find_replace as t_find_replace,
    insert_after as t_insert_after,
    merge as t_merge,
    replace_heading as t_replace_heading,
    insert_image as t_insert_image,
)
from app.tools import vision_ops, chart_image, chart_redraw as chart_redraw_mod

log = logging.getLogger("aice.mcp")

# streamable_http_path="/" — so when this sub-app is mounted at "/mcp"
# in main.py, the effective external URL is just `/mcp` (not `/mcp/mcp`).
# stateless_http=True — no server-side session storage needed; each request
# carries its own context. Required for serverless-style deploys (Railway)
# where memory state can be lost between containers.
# json_response=True — return JSON instead of SSE for simpler clients.
# transport_security: disable DNS-rebinding host validation. Without this,
# FastMCP returns "421 Invalid Host header" for ANY request on a deployed
# domain (Railway, Fly, Render, etc.) that wasn't explicitly whitelisted.
# We're publicly hosted with our own auth (or none, deliberately), so the
# DNS-rebinding protection is irrelevant and only breaks things.
def _icons() -> dict[str, Any]:
    """A kliensek (ChatGPT bővítménylista, Claude connector-lista) a szerver ikonját a serverInfo `icons`-ából veszik —
    enélkül a hoszt faviconját keresik, és a 32 px-es ICO helyett üres „@” jelet mutattak (10-17). Ugyanaz a recept,
    ami az Echolotnál bevált: PNG 512/192 + SVG, belépés nélkül elérhető címen, és website_url."""
    try:
        from mcp.types import Icon
    except Exception:                  # régebbi SDK: ikon nélkül indul
        return {}
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return {"website_url": f"{base}/app",
            "icons": [Icon(src=f"{base}/icon-512.png", mimeType="image/png", sizes=["512x512"]),
                      Icon(src=f"{base}/icon-192.png", mimeType="image/png", sizes=["192x192"]),
                      Icon(src=f"{base}/static/favicon.svg", mimeType="image/svg+xml", sizes=["any"])]}


mcp = FastMCP(
    "ai-conversioneditor",
    **_icons(),
    instructions=(
        "Make and edit real files (Word, PowerPoint, PDF, images/PSD, designs). This is the person's own Studio "
        "workspace, which they connected: files sent here are stored privately in their account (kept "
        f"{settings.FILE_RETENTION_DAYS} days, links signed), never published or shared, and every edit makes a new "
        "version — the original stays. Every file in a result carries "
        "`edit_url`: give it to the person — it opens that file in the matching built-in editor (Word-like, "
        "PowerPoint-like, Photoshop-like, Illustrator-like) for hand finishing. Chains that work well: write and revise "
        "a document (wordcraft) → slides from it (pptx_build from_document) → a Word handout back (convert pptx→docx); "
        "a manuscript → a Word-typeset book (design template=book); posters/posts: design(template=\"auto\") → 3 options "
        "to choose from → design(from_design=…, sizes=[…]) for every platform size; an article, document or the person's "
        "text → a short video with titles, pictures and captions on an editable timeline (video). The person's earlier files: files(action=\"list\", "
        "query=\"words from the name\") — search by name, don't page through everything. Start empty with "
        "files(action=\"new\", kind=document|presentation|design|photo|book|pdf|video) and hand the person its edit_url. PICTURES: this server does NOT "
        "generate AI pictures for you — make them with your own image tool and pass the image URL (image_url / "
        "{\"url\": …}) or upload it (upload_from_url / upload_file → file_id); free stock photos: {\"stock\": \"words\"} or "
        "generate_image(stock_search=…) to show the person candidates. "
        "CHECK YOUR WORK: a Word document, deck or PDF → validate_document(file_id): the pages come back as pictures "
        "with a machine-checked list of problems (\"3 errors, 2 warnings\", page, object, a cut-out of each) — fix every "
        "error and validate again before saying it's done; describe_image is not a check. validate_document(content=true) "
        "also reads a Word document for contradictions between places, tone breaks and the same thing named two ways; "
        "facts(action=\"check\") checks its numbers and facts against sources (StatData, MNB, web search) and "
        "facts(action=\"bind\"/\"refresh\") keeps linked numbers up to date. history(action=\"branches\"/\"merge\") "
        "shows a document's branches and merges two of them idea by idea, as tracked changes. "
        "To SEE any other result call files(action=\"preview\", file_id, page) — the picture comes back in the "
        "tool result (pages of a document, slides, a video's frames: page 1–3); every file result also has `preview_url` "
        "(the same PNG as a link, for the person), and results that make or change a file carry `check` = {status: ok | "
        "needs_check | failed, findings, verified} (decks: the same rendered check as validate_document, each problem "
        "with its slide, shape id and a ready fix). Commands that only read (inspect, *.get, *.list) make no new file. A file existing is not the task done: if status is needs_check, look at the preview; "
        "if failed or a finding says something is missing, fix it before telling the person it's done. "
        "files(action=\"compare\", file_id, other_file_id) shows what changed between two versions and what disappeared. "
        "YOUR THINKING, OUR ENGINES: rendering, checks, layouts, transcripts and music cost the person nothing extra here; "
        "but a step where this server runs its OWN AI model (choosing stock photos with a vision model, finding image search "
        "words, writing a video script from an article, describe_image / ocr_image / extract_table_from_image, "
        "edit_with_instruction, facts checks) is a Studio AI step: a monthly allowance, then credits (result field "
        "`studio_ai`). Do those parts yourself when you can: give scenes their own images or \"image_query\" words, pass a "
        "finished scenes=[…] plan, look at pictures with files(action=\"preview\"), and use the editing commands directly."
    ),
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


_EDITABLE = {"docx", "doc", "odt", "rtf", "md", "txt", "html", "htm", "pptx", "deckcraft", "pdf", "xlsx", "csv", "tsv",
             "png", "jpg", "jpeg", "webp", "psd", "tif", "tiff", "svg", "vectorcraft", "ai", "eps", "designcraft", "idml",
             "ecproj", "fcproj", "mp4", "mov", "webm", "m4v"}


def _exists(fid: str) -> bool:
    from app.tools.crafts import existing
    try:
        existing(fid)
        return True
    except (FileNotFoundError, ValueError):
        return False


def _links(res, pages: Optional[dict] = None):
    """Minden fájl-eredmény mellé `edit_url` (a fájl rögtön a megfelelő szerkesztőben nyílik), `preview_url` (PNG,
    amit egy külső AI megnézhet — `pages`: fájlonként melyik oldal/dia érdemes) és `name` (a beszédes fájlnév)."""
    from app.storage import display_label
    from app.tools import previews
    base = settings.PUBLIC_BASE_URL.rstrip("/")

    def walk(o, depth=0):
        if depth > 5:
            return
        if isinstance(o, dict):
            fid = o.get("file_id")
            if isinstance(fid, str) and "." in fid and fid.rsplit(".", 1)[1].lower() in _EDITABLE and "edit_url" not in o:
                o["edit_url"] = f"{base}/app/file/{fid}/edit"
            if isinstance(fid, str) and "preview_url" not in o and previews.can_preview(fid) and _exists(fid):
                o["preview_url"] = previews.preview_url(fid, (pages or {}).get(fid, 1))
                if "name" not in o and "label" not in o:
                    o["name"] = display_label(fid)
            for v in list(o.values()):
                walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)
    walk(res)
    # háttérmunka (jobs.start): a leírások „call again with job_id”-t mondanak — a kulcs is legyen ott
    if isinstance(res, dict) and res.get("status") in ("running", "done", "failed", "cancelled") and "id" in res \
            and "kind" in res and "job_id" not in res:
        res["job_id"] = res["id"]
    return res


def linked(fn):
    """Eszköz-burkoló: az eredményhez hozzáteszi a szerkesztő-linkeket (az aláírás és a leírás marad)."""
    import functools

    @functools.wraps(fn)
    async def w(*a, **k):
        import time
        from app import tenancy
        from app import billing
        tok = tenancy.client_makes_images.set(True)    # MCP-hívás: AI-képet a kliens csinál, nem a szerver
        t0 = time.monotonic()
        try:
            with billing.count_steps() as used:        # a szerver saját modellhívásai (AI-lépés) ebben a hívásban
                res = await _finalize(fn.__name__, k, await fn(*a, **k))
            if used[0] and isinstance(res, dict):
                res["studio_ai"] = {"steps": used[0], "credits": used[1],
                                    "note": "Steps run on the Studio's own AI model (monthly allowance, then 1 credit per "
                                            f"{billing.AI_STEPS_PER_CREDIT}). You can do most of them yourself — see the server instructions."}
            st = (res.get("check") or {}).get("status") if isinstance(res, dict) else None
            log.info("mcp %s(%s) ok %.1fs%s", fn.__name__, _arg_brief(k), time.monotonic() - t0, f" check={st}" if st else "")
            return res
        except Exception as e:
            log.warning("mcp %s(%s) FAILED %.1fs: %s", fn.__name__, _arg_brief(k), time.monotonic() - t0, str(e)[:300])
            raise
        finally:
            tenancy.client_makes_images.reset(tok)
    return w


# a közvetlen szerkesztők (edits.py) eredménye eddig címke nélkül maradt („3fa8c1e2.docx") — itt kap nevet
_TOOL_WHAT = {"find_replace": "text replaced", "delete_section": "section removed", "insert_after": "text added",
              "replace_heading": "heading renamed", "delete_pages": "pages removed", "merge": "merged",
              "edit_with_instruction": "edited", "insert_image": "picture added", "convert": "converted",
              "chart_redraw": "chart", "generate_chart_image": "chart"}


def _file_dicts(res) -> list[dict]:
    if not isinstance(res, dict):
        return []
    out = [res] if isinstance(res.get("file_id"), str) else []
    out += [f for f in res.get("files") or [] if isinstance(f, dict) and isinstance(f.get("file_id"), str)]
    return out


def _name_unlabeled(tool: str, args: dict, res) -> None:
    what = _TOOL_WHAT.get(tool)
    if not what:
        return
    from app.storage import output_path, read_meta, write_meta
    from app.storage.naming import result_stem
    src = args.get("file_id") or next(iter(args.get("file_ids") or []), None)
    for d in _file_dicts(res):
        fid = d["file_id"]
        meta = read_meta(fid) or {}
        if meta.get("label") or meta.get("original_filename") or not output_path(fid).exists():
            continue
        label = f"{result_stem(src, what, 'file')}.{fid.rsplit('.', 1)[-1]}"
        write_meta(fid, label=label, source_file_id=src if isinstance(src, str) else None, operation=tool)


def _sizes(x, depth: int = 0) -> None:
    """Minden fájl-elem mérete bájtban is (size_bytes), és 10 kB alatt tizedes kB — az 1 kB alatti fájl ne „0” legyen."""
    if depth > 4:
        return
    if isinstance(x, list):
        for y in x:
            _sizes(y, depth + 1)
        return
    if not isinstance(x, dict):
        return
    fid = x.get("file_id")
    if isinstance(fid, str) and ("size_kb" in x or "label" in x or "url" in x):
        from app.storage import input_path, output_path
        try:
            p = output_path(fid) if output_path(fid).exists() else input_path(fid)
            n = p.stat().st_size if p.exists() else None
        except (OSError, ValueError):
            n = None
        if n is not None:
            x["size_bytes"] = n
            if "size_kb" in x:
                x["size_kb"] = round(n / 1024, 1) if n < 10240 else n // 1024
    for k, v in x.items():
        if isinstance(v, (dict, list)) and k not in ("check", "steps", "tasks"):
            _sizes(v, depth + 1)


async def _finalize(tool: str, args: dict, res):
    """Minden MCP-eredmény közös utófeldolgozása: név, méret, ellenőrzés (check), linkek (szerkesztő, előnézet)."""
    if not isinstance(res, dict):
        return res                       # pl. CallToolResult (files preview képpel) — érintetlenül
    try:
        _name_unlabeled(tool, args, res)
    except Exception as e:
        log.info("elnevezés kimaradt (%s): %s", tool, e)
    try:
        _sizes(res)
    except Exception as e:
        log.info("méret kimaradt (%s): %s", tool, e)
    from app.tools import checks
    check, page = await checks.run(tool, args, res)
    pages = {}
    if check is not None:
        res["check"] = check
        main = checks.main_file(res) or (checks.main_file(res["result"]) if isinstance(res.get("result"), dict) else None)
        if main and page:
            pages[main] = page
    return _links(res, pages)


def _arg_brief(k: dict) -> str:
    """Napló: melyik eszköz, milyen paraméterekkel (rövidítve; tartalom/base64/aláírt link nem kerül a naplóba)."""
    out = []
    for key, v in k.items():
        if v is None or key in ("content_base64",):
            continue
        if isinstance(v, str):
            v = v.split("?", 1)[0] if v.startswith("http") else v
            out.append(f"{key}={v[:60]!r}")
        elif isinstance(v, (int, float, bool)):
            out.append(f"{key}={v}")
        else:
            out.append(f"{key}=<{type(v).__name__}>")
    return ", ".join(out)[:400]


_CHUNK_BUFFERS: dict[str, dict[int, bytes]] = {}


def _no_ai_pictures(*specs: Any) -> None:
    """MCP-n a szerver nem készít AI-képet: ha egy spec mégis kér ({"generate": …}, images/pictures="ai", ai_prompt),
    MUNKA ELŐTT ugyanaz az egyértelmű hiba jön, nem félúton egy képnél."""
    from app import tenancy

    def asks(x: Any) -> bool:
        if isinstance(x, dict):
            return isinstance(x.get("generate"), str) or any(asks(v) for v in x.values())
        if isinstance(x, (list, tuple)):
            return any(asks(v) for v in x)
        return False
    if any(asks(s_) for s_ in specs):
        raise ValueError(tenancy.CLIENT_IMAGE_MSG)


def _sniff_ext(data: bytes) -> str:
    """Kiterjesztés a tartalomból (PDF, képek, Office-zip), ha a névből nem derül ki."""
    head = data[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8"):
        return "jpg"
    if head.startswith(b"8BPS"):
        return "psd"
    if head[:4] in (b"RIFF",) and data[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"PK"):
        import io, zipfile
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except Exception:
            return "zip"
        for pre, e in (("ppt/", "pptx"), ("word/", "docx"), ("xl/", "xlsx")):
            if any(n.startswith(pre) for n in names):
                return e
        return "zip"
    return "bin"


class ChatFile(BaseModel):
    """Egy a csetbe feltöltött fájl (ChatGPT Apps SDK „openai/fileParams”: {download_url, file_id})."""
    download_url: str
    file_id: Optional[str] = None
    name: Optional[str] = None


@mcp.tool(meta={"openai/fileParams": ["file"]})
@linked
async def upload_from_url(url: Optional[str] = None, filename: Optional[str] = None, file: Optional[ChatFile] = None) -> dict:
    """
    Bring a file in: a file the person attached to the chat (`file` — ChatGPT passes it automatically),
    or any public URL (`url` — the SERVER does the GET, no base64 round-trip: a gist, S3, github raw,
    an article PDF…). Then edit it with the other tools and give the person its edit_url.

    Args:
      file: a chat attachment {download_url, file_id} (ChatGPT fills this in for attached files).
      url: full HTTP/HTTPS URL the server can GET.
      filename: optional override for the human label (e.g. "Q3 deck.pptx"); default: from the file/URL.

    Returns: same shape as upload_file: {file_id, url, size_kb, original_name, edit_url}.
    """
    import httpx
    from app import net
    from app.api_v1 import operations as O
    if file is not None:
        url = file.download_url
        filename = filename or file.name
    if not url:
        raise ValueError("Give url, or attach the file in the chat (file).")
    try:                                  # a közös kimenő kliens: SSRF-védelem, leíró User-Agent, méretkorlát
        got = await net.fetch(url, max_bytes=O.max_bytes(False), timeout=120)
    except httpx.HTTPStatusError as e:
        raise ValueError(f"The server of that URL answered HTTP {e.response.status_code} — check the link "
                         "(or download it and upload_file it).") from None
    data = got.content
    if not filename:
        filename = got.name if got.name != "download" else "downloaded.bin"
    ext = Path(filename).suffix.lstrip(".").lower()
    if not ext or ext == "bin":            # név kiterjesztés nélkül (csetfájl-link): a tartalomból
        ext = _sniff_ext(data)
        filename = f"{filename.rsplit('.', 1)[0] if filename.endswith('.bin') else filename}.{ext}"
    fid = new_file_id(ext)
    await save_bytes(input_path(fid), data)
    write_meta(
        fid, original_filename=filename, label=filename,
        operation="upload_from_url", extra={"source_url": "chat attachment" if file is not None else url},   # az aláírt, ideiglenes link nem kell
    )
    return {
        "file_id": fid,
        "url": public_url(fid),
        "size_kb": len(data) // 1024,
        "original_name": filename,
    }


# A ChatGPT fájl-paramétere egyszerű objektum-sémát vár (Apps SDK „openai/fileParams”), nem anyOf/$ref-et.
try:
    _t = mcp._tool_manager.get_tool("upload_from_url")
    _t.parameters["properties"]["file"] = {
        "type": "object", "description": "A file the person attached to the chat (ChatGPT fills this in).",
        "properties": {"download_url": {"type": "string"}, "file_id": {"type": "string"}, "name": {"type": "string"}},
        "required": ["download_url"]}
except Exception as _e:  # a séma-finomítás nem kötelező — az eszköz enélkül is működik
    log.warning("upload_from_url séma nem finomítható: %s", _e)


_LIST_KINDS = {
    "image": {"png", "jpg", "jpeg", "webp", "psd", "tif", "tiff", "gif", "heic", "avif", "bmp"},
    "photo": {"png", "jpg", "jpeg", "webp", "psd", "tif", "tiff", "gif", "heic", "avif", "bmp"},
    "picture": {"png", "jpg", "jpeg", "webp", "psd", "tif", "tiff", "gif", "heic", "avif", "bmp"},
    "document": {"docx", "doc", "odt", "rtf", "md", "txt", "html", "htm", "epub"},
    "presentation": {"pptx", "ppt", "odp", "deckcraft"}, "deck": {"pptx", "ppt", "odp", "deckcraft"},
    "slide": {"pptx", "ppt", "odp", "deckcraft"},
    "pdf": {"pdf"}, "spreadsheet": {"xlsx", "xls", "csv", "tsv", "ods"},
    "design": {"vectorcraft", "designcraft", "svg", "ai", "eps", "idml"},
    "video": {"ecproj", "fcproj", "mp4", "mov", "webm", "m4v", "mkv"},
}


# keresésnél nem számító szavak (angol + magyar névelők, kötőszók, birtokos)
_STOP = {"the", "a", "an", "of", "my", "our", "and", "or", "for", "to", "in", "on", "with", "from", "this", "that", "file",
         "az", "egy", "es", "meg", "is", "fajl", "fajlom", "file-om"}


def _name_words(text: str) -> list[str]:
    """Fájlnév / keresőszó → kisbetűs, ékezet nélküli szavak (_ - . szóköz mind határ)."""
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return [w for w in re.split(r"[^0-9a-z]+", t) if w]


def _word_hits(w: str, name_words: list[str]) -> int:
    """Egy keresőszó illik-e a névre: egyezés, vagy közös eleje (ragozás: „fotóját” ~ „foto”, „lapot” ~ „lap”)."""
    for nw in name_words:
        if w == nw:
            return 2
        k = min(len(w), len(nw))
        if k >= 3 and (w.startswith(nw) or nw.startswith(w) or (k >= 5 and w[:5] == nw[:5])):
            return 1
    return 0


@mcp.tool()
@linked
async def files(
    action: str = "list",
    query: Optional[str] = None,
    kind: Optional[str] = None,
    ext: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    size: Optional[str] = None,
    orientation: Optional[str] = None,
    background: Optional[str] = None,
    title: Optional[str] = None,
    file_id: Optional[str] = None,
    page: Optional[int] = None,
    other_file_id: Optional[str] = None,
    scene: Optional[str] = None,
    at: Optional[Union[float, list[float]]] = None,
    frames: Optional[int] = None,
    versions: bool = False,
) -> dict:
    """The person's files, and empty new ones; see a file; compare two versions.

    action="list": their files (uploads and results, newest first) — {file_id, name, ext, size_kb, when, made_from,
      edit_url}, plus total. To FIND a file ("the application form photo", "my contract"), pass query = words from
      its name — the person's own words first, names are often in their language (accents and _/- don't matter;
      best matches first). Narrow with kind = image |
      document | presentation | pdf | spreadsheet | design | video, or ext (e.g. "pptx"). More: offset.
      Versions of the same file come as ONE entry — the newest, with older_versions = how many older ones there are;
      versions=true lists every version.
    action="new": an EMPTY file, ready in the editor — kind = document | presentation | design (social post:
      size square|portrait|story|landscape) | print (size A4|A3|A5|Letter, orientation portrait|landscape) |
      photo (size 1920x1080|1080x1080|1080x1350|3000x2000, background white|transparent) | book (size A5|A4|6x9) |
      pdf (size A4|Letter) | video (size vertical|landscape|square — an empty timeline in the video editor).
      Give the person its edit_url — it opens the blank file in the matching editor.
      title names the file (and the deck's first slide).
    action="preview": SEE a file — returns the picture of page/slide `page` (default 1) of file_id (a video: scene =
      "s3" | its number | "title" | "end" | a layer "L2" — that scene / layer at its end, when the animation has played;
      + frames=6 for a filmstrip of its motion — or scene="all" for every scene and layer on one sheet, or at=seconds, or
      at=[seconds, …] (up to 12 moments on one sheet, each with a faint tenths grid: where the person points or looks →
      a layer's position [x, y, w, h]), so you can
      check your own result (text overflow, a missing object, a wrong crop) before calling it done. A Word document,
      deck or PDF: validate_document checks it for you (pictures + a machine-checked list of problems).
    action="compare": what changed between file_id and other_file_id (default: the file it was made from) —
      changed pages with preview pairs, text added/removed, and things that disappeared (pictures, slides, lines).
    """
    if action in ("list", "find", "search"):
        import datetime
        from app.routes.dashboard import api_files
        d = await api_files()
        items = sorted([*d.get("inputs", []), *d.get("outputs", [])], key=lambda f: f.get("mtime", 0), reverse=True)
        if not versions:          # változatok: csak a legfrissebb (10-13: öt „… diasor (vN)” közül nem látszott, melyik a kész)
            ids = {f["file_id"] for f in items}
            items = [f for f in items if not (f.get("superseded_by") and f["superseded_by"] in ids)]
        if ext:
            items = [f for f in items if f.get("ext") == ext.lower().lstrip(".")]
        kinds = _LIST_KINDS.get((kind or "").lower().rstrip("s")) if kind else None
        if kinds:
            items = [f for f in items if f.get("ext") in kinds]
        if query and query.strip():
            words = [w for w in _name_words(query) if w not in _STOP] or _name_words(query)
            scored = [(sum(_word_hits(w, _name_words(f"{f.get('label') or ''} {f.get('original_filename') or ''}")) for w in words), f)
                      for f in items]
            best = max((sc for sc, _ in scored), default=0)
            # a legjobb találatok (és a majdnem-legjobbak) elöl, azon belül a frissebb
            items = [f for sc, f in sorted(scored, key=lambda t: -t[0]) if sc > 0 and sc >= best * 0.5]
        n, o = max(1, min(100, limit)), max(0, offset)
        page = items[o:o + n]
        hint = ({"hint": "Nothing has those words in its name — names are often in the person's own language (or a "
                         "camera name like IMG_1234): try other words, or list by kind (e.g. kind=\"image\") and look."}
                if query and query.strip() and not items else {})
        return {**hint, "total": len(items), "offset": o, "more": o + n < len(items), "files": [{
            "file_id": f["file_id"], "name": f.get("label"), "ext": f.get("ext"), "size_kb": f.get("size_kb"),
            "when": datetime.datetime.fromtimestamp(f.get("mtime", 0), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "made_from": f.get("source_label"),
            **({"older_versions": f["older_versions"]} if f.get("older_versions") and not versions else {})} for f in page]}
    if action == "preview":
        return await _preview_content(file_id, page or 1, scene, at, frames)
    if action == "compare":
        from app.tools import compare as cmp
        if not file_id:
            raise ValueError("compare needs file_id (and other_file_id, or it compares with the file it was made from)")
        other = other_file_id or cmp.latest_source(file_id)
        if not other:
            raise ValueError("Give other_file_id — this file wasn't made from another one.")
        return await cmp.compare(other, file_id) if not other_file_id else await cmp.compare(file_id, other_file_id)
    if action != "new":
        raise ValueError('action: "list", "new", "preview" or "compare"')
    k = (kind or "").lower()
    if k in ("document", "doc", "word"):
        from app.routes.make import make_document
        res = await make_document({"mode": "template", "template": "blank"})
    elif k in ("presentation", "deck", "slides", "pptx"):
        from app.routes.make import make_deck
        res = await make_deck({"mode": "blank", "title": title or "Presentation title"})
    else:
        from app.tools import blank
        k = {"design": "social", "social_post": "social", "poster": "print", "canvas": "photo", "movie": "video"}.get(k, k)
        res = await blank.make({"kind": k, "size": size, "orientation": orientation, "background": background})
    if title and isinstance(res, dict) and res.get("file_id"):
        # a megadott cím legyen a fájl neve („Untitled design" helyett) — ChatGPT-teszt 10-12
        from app.storage import write_meta
        from app.storage.naming import safe_title
        t = safe_title(title)
        if t:
            label = f"{t}.{res['file_id'].rsplit('.', 1)[-1]}"
            write_meta(res["file_id"], label=label)
            res["label"] = res["name"] = label
    return res


async def _preview_content(file_id: Optional[str], page: int, scene: Optional[str] = None, at: Any = None,
                           frames: Optional[int] = None):
    """files(action="preview"): a kép MAGA az eszköz-válaszban (a kliens-AI látja), mellette a szöveges adatok.
    Videónál jelenetenként is (scene: s3 | sorszám | title | end | all = kontaktlap) vagy időpontra (at)."""
    import asyncio
    import json
    from mcp.types import CallToolResult, ImageContent, TextContent
    from app.storage import display_label
    from app.tools import previews
    from app.tools.crafts import existing
    if not file_id:
        raise ValueError("preview needs file_id")
    src = existing(file_id)
    if (scene is not None or at is not None) and src.suffix.lower().lstrip(".") in ("ecproj", "mp4", "mov", "m4v", "webm"):
        from app.tools import video as vid_mod
        if isinstance(at, list) and len(at) > 1:     # több pillanat egy képen, tizedes ráccsal (hova mutat / néz)
            jpg, shots = await asyncio.to_thread(vid_mod.frames_grid, file_id, at)
            info = {"file_id": file_id, "name": display_label(file_id), "frames": shots,
                    "note": "One tile per moment (its time at the bottom left). The faint grid marks tenths of the frame — "
                            "x left→right, y top→bottom, 0–1 — read a layer's position [x, y, w, h] straight off it."}
        elif frames and scene and str(scene).lower() not in ("all", "title", "end"):     # a jelenet mozgása: filmszalag
            jpg, info = await asyncio.to_thread(vid_mod.filmstrip, file_id, scene, int(frames))
            info = {"file_id": file_id, "name": display_label(file_id), **info,
                    "note": "The scene from start to end — how the graphic moves, frame by frame."}
        elif str(scene or "").lower() == "all":
            jpg, frames = await asyncio.to_thread(vid_mod.contact_sheet, file_id)
            info = {"file_id": file_id, "name": display_label(file_id), "scenes": frames,
                    "note": "Every scene at its end (animations done), with the title card and the end card."}
        else:
            vid, t, label = vid_mod.frame_time(file_id, scene, at[0] if isinstance(at, list) else at)
            out = previews.temp_path_for(vid, t)
            await asyncio.to_thread(previews.video_frame_at, existing(vid), t, out)
            jpg = await asyncio.to_thread(previews.to_jpeg, out, 1000)
            info = {"file_id": file_id, "name": display_label(file_id), "frame": label, "at": round(t, 2)}
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(info, ensure_ascii=False)),
                                       ImageContent(type="image", data=base64.b64encode(jpg).decode(), mimeType="image/jpeg")],
                              structuredContent=info)
    if not previews.can_preview(file_id):
        raise ValueError(f"No preview for .{src.suffix.lstrip('.')} files.")
    n = await previews.page_count(src)
    page = max(1, min(int(page or 1), n or 1))
    jpg = await previews.render_jpeg(src, page, 1000)
    info = {"file_id": file_id, "name": display_label(file_id), "page": page, "pages": n,
            "preview_url": previews.preview_url(file_id, page)}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(info, ensure_ascii=False)),
                                   ImageContent(type="image", data=base64.b64encode(jpg).decode(), mimeType="image/jpeg")],
                          structuredContent=info)


@mcp.tool()
@linked
async def upload_chunk(
    upload_id: str,
    chunk_index: int,
    total_chunks: int,
    filename: str,
    content_base64: str,
) -> dict:
    """
    Upload a large file as multiple base64 chunks across separate tool
    calls — bypasses the per-tool-call input size limit.

    Workflow:
      1. Pick any upload_id (e.g. 'page28-' + uuid). Use the SAME id for
         every chunk of the same file.
      2. Split the file's bytes into N pieces of ≤200KB each (raw bytes;
         after base64 each piece is ≤270KB — fits in tool input).
      3. For each chunk, base64-encode and call:
           upload_chunk(upload_id, chunk_index=i, total_chunks=N,
                        filename='page28.jpeg', content_base64=...)
      4. The LAST chunk's response contains the final file_id. Earlier
         chunks return {pending: i+1/N, received_bytes: ...}.

    Order does NOT matter — chunks can be sent in any order. The file is
    assembled when all `total_chunks` are present.
    """
    if total_chunks < 1:
        raise ValueError("total_chunks must be >= 1")
    if not (0 <= chunk_index < total_chunks):
        raise ValueError(f"chunk_index out of range: {chunk_index} not in [0,{total_chunks})")
    try:
        data = base64.b64decode(content_base64, validate=True)
    except Exception as e:
        raise ValueError(f"content_base64 is not valid base64: {e}")

    buf = _CHUNK_BUFFERS.setdefault(upload_id, {})
    buf[chunk_index] = data

    if len(buf) < total_chunks:
        return {
            "pending": True,
            "chunks_received": len(buf),
            "total_chunks": total_chunks,
            "received_bytes": sum(len(v) for v in buf.values()),
        }

    # All chunks present — assemble in index order and finalize
    full = b"".join(buf[i] for i in range(total_chunks))
    _CHUNK_BUFFERS.pop(upload_id, None)

    ext = Path(filename).suffix.lstrip(".") or "bin"
    fid = new_file_id(ext)
    await save_bytes(input_path(fid), full)
    write_meta(
        fid, original_filename=filename, label=filename,
        operation="upload_chunked",
        extra={"upload_id": upload_id, "n_chunks": total_chunks},
    )
    return {
        "file_id": fid,
        "url": public_url(fid),
        "size_kb": len(full) // 1024,
        "original_name": filename,
        "chunks_assembled": total_chunks,
    }


@mcp.tool()
@linked
async def upload_file(filename: str, content_base64: str) -> dict:
    """
    Upload a file to the editor and get back a `file_id`.

    Use this FIRST when you have a local file you want to process — without
    a file_id, no other tool can act on your file. The returned file_id is
    then passed to convert, find_replace, edit_with_instruction, etc.

    Args:
      filename: original name (used for the human-readable label, e.g.
                "report.docx", "page28.jpeg"). Extension is preserved as
                the file_id's extension.
      content_base64: the file's bytes encoded as base64 (RFC 4648). For a
                local file in your sandbox: read it as bytes and call
                base64.b64encode(data).decode('ascii').

    Returns:
      {file_id, url, size_kb, original_name}

    Examples:
      • OCR a photo:  upload_file("scan.jpg", b64) → file_id → edit_with_instruction(file_id, "extract text and write to a clean DOCX")
      • Convert PDF: upload_file("report.pdf", b64) → file_id → convert(file_id=..., target_format="docx")
    """
    try:
        data = base64.b64decode(content_base64, validate=True)
    except Exception as e:
        raise ValueError(f"content_base64 is not valid base64: {e}")
    if not data:
        raise ValueError("content_base64 decoded to empty bytes")

    ext = Path(filename).suffix.lstrip(".") or "bin"
    fid = new_file_id(ext)
    await save_bytes(input_path(fid), data)
    write_meta(
        fid,
        original_filename=filename,
        label=filename,
        operation="upload",
    )
    return {
        "file_id": fid,
        "url": public_url(fid),
        "size_kb": len(data) // 1024,
        "original_name": filename,
    }


@mcp.tool()
@linked
async def convert(
    target_format: str,
    file_id: Optional[str] = None,
    source_url: Optional[str] = None,
    source_format: Optional[str] = None,
    use_browser: bool = False,
    options: Optional[dict] = None,
    engine: Optional[str] = None,
) -> dict:
    """
    Convert a file between formats — 264 formats, 8800+ directions: documents (docx, doc, odt, rtf, md, html,
    tex, pages, wpd, hwp, xps, djvu, eml, pdf, pdfa…), spreadsheets, slides (pptx → pdf, images or mp4), e-books
    (epub, mobi, azw3, fb2, cbz…), images (heic, webp, avif, jxl, psd, camera RAW…), vectors (svg, eps, ai, emf,
    cdr), video (mp4, mov, mkv, webm, avi, gif…), audio (mp3, wav, flac, m4a, ogg, opus…), archives (zip, rar,
    7z, tar.gz… 43 kinds), CAD (dwg, dxf → pdf/svg/png) and fonts (ttf, otf, woff, woff2, eot).
    catalog(kind="conversions", source_format=…) lists the targets of a format.

    Pass either `file_id` (already uploaded) or `source_url`, and `target_format`.

    options (validated): page_range "1-3,5"; ocr / ocr_language ("hun+eng"); images: width, height, fit,
    quality 1–100, strip_metadata, dpi, background, grayscale, rotate, flip; video: video_codec (h264, h265,
    vp9, av1…), audio_codec, resolution ("1080p", "1280x720"), aspect ("9:16") + aspect_mode, fps, crf, preset,
    video_bitrate, start/end (trim), subtitles (file_id) + subtitles_mode, volume, remove_audio; audio:
    audio_bitrate, sample_rate, channels, bit_depth; e-books: title, author, cover, output_profile; archives:
    password, level; advanced: allow-listed ffmpeg/ImageMagick flags (["-tune", "film"]). Several output files
    (e.g. one JPG per PDF page) come back as one .zip, or separately with options.output = "files".

    Set `use_browser=True` for HTML→PDF when JS-rendering or modern CSS
    (flexbox/grid/recharts/d3) is needed. Defaults to WeasyPrint (faster,
    print-quality CSS only).

    Set `engine="wordcraft"` for the Rust Word engine: no LibreOffice, a few
    ms per call. Sources docx/doc/odt/rtf/html/md/tex/txt; targets
    docx/pdf/odt/rtf/html/md/tex/txt/png (png = first page). md → docx/odt uses
    it by default (tables keep every column); Markdown with footnotes, pictures,
    math or task lists goes through pandoc instead.
    engine="photocraft": images (psd/png/jpg/webp/tif/exr…, PSD layers kept
    when saving psd). engine="vectorcraft": svg/pdf/ai/eps → pdf/svg/png/eps.
    pptx → docx makes a Word HANDOUT: each slide's picture, title, text,
    tables and speaker notes.
    """
    if file_id and file_id.lower().endswith(".pptx") and target_format.lower().lstrip(".") == "docx":
        from app.tools import deck_handout            # pptx → docx = Word-kiosztmány
        return await deck_handout.build(file_id)
    if _universal(file_id, source_url, source_format, target_format, options, engine, use_browser):
        from app.convert import service
        fid = file_id or await _fetch_source(source_url, source_format)
        return await service.convert_file(fid, target_format, options or {}, _pool_or_none())
    return await t_convert(
        _pool(),
        file_id=file_id,
        source_url=source_url,
        source_format=source_format,
        target_format=target_format,
        use_browser=use_browser,
        options=options,
        engine=engine,
    )


# a régi (LibreOffice/pandoc/WeasyPrint) út formátumai — ami ezen kívül esik, vagy új opciót kér, az univerzális gráfra megy
_LEGACY_FMTS = {"pdf", "docx", "doc", "html", "htm", "md", "odt", "pptx", "ppt", "odp", "xlsx", "xls", "ods", "csv", "rtf", "txt"}
_LEGACY_OPTS = {"page_size", "margin", "pandoc_args"}


def _universal(file_id, source_url, source_format, target_format, options, engine, use_browser) -> bool:
    if engine or use_browser:
        return False
    tgt = target_format.lower().lstrip(".")
    src = (source_format or (file_id or source_url or "").split("?", 1)[0].rsplit(".", 1)[-1]).lower()
    new_opts = bool(options) and not set(options) <= _LEGACY_OPTS
    return new_opts or tgt not in _LEGACY_FMTS or src not in _LEGACY_FMTS


async def _fetch_source(url: Optional[str], fmt: Optional[str]) -> str:
    if not url:
        raise ValueError("Either file_id or source_url is required")
    from app import net
    from app.api_v1 import operations as O
    tmp = O._tmpdir() / "src"
    name, _, _ = await net.download(url, tmp, O.max_bytes(False))
    if fmt and not name.lower().endswith("." + fmt.lower().lstrip(".")):
        name = f"{name}.{fmt.lower().lstrip('.')}"
    return O._save_named(tmp, name)


def _pool_or_none():
    try:
        return _pool()
    except RuntimeError:
        return None


@mcp.tool()
@linked
async def wordcraft(
    file_id: Optional[str] = None,
    commands: Optional[list] = None,
    author: Optional[str] = None,
    save_format: str = "docx",
    template: Optional[str] = None,
    read: Optional[str] = None,
    query: Optional[str] = None,
    offset: int = 0,
    max_chars: int = 20000,
    address: Optional[list] = None,
    base_file_id: Optional[str] = None,
) -> dict:
    """
    Edit a document with the WordCraft Word engine: the same ~460 commands
    that drive Word's ribbon (styles, tables, track changes, comments, TOC,
    footnotes…), run headless. The result is a NEW file_id; the source stays.
    Each command's result is in `steps`. Commands that only read (document.inspect, edit.find, styles.list,
    format.state…) save nothing: the same file_id comes back with `results` and no new version.

    REVIEW COMMENTS (someone commented a .docx in Word, e.g. a boss):
    1. read="comments" → each open comment thread: id, author, comment,
       anchor (the highlighted text), paragraph (number) + paragraph_text,
       replies; plus the pending tracked_changes. query="all" adds resolved.
    2. Agree the fixes with the person, then apply ALL in ONE call:
       address=[{"comment": id,
                 "replace": {"find": exact text from the anchor or its
                             paragraph, "with": new text}  (or a list),
                 OR "paragraph": the full new text of that paragraph,
                 "reply": "Fixed: …" (optional), "resolve": false}],
       author = the person's own name. Fixes become word-level tracked
       changes under that name, the reply is threaded under the comment;
       the reviewer's own tracked changes are left as they are. Result:
       NEW file_id, results per comment (fixed | replied | resolved |
       failed + why — retry failed ones on the NEW file_id), not_addressed
       = open comments you skipped. Then give the person the edit_url (or
       url to download) to send back.
    3. Next round: read="changes", file_id = the version that came back,
       base_file_id = the version that was sent → per paragraph before /
       after / diff, tracked or untracked (edited with tracking off), new
       comments and replies, resolved threads.
    More review moves, as items in the same `address` list (mix freely):
       {"new_comment": "your question", "on": "exact text"} (or
         "paragraph": n, or both) → a new comment of your own, Word-native;
       {"accept": "all" | id | [ids] | {"author": "…", "paragraph": n,
         "type": "ins|del|format|paragraph_mark|row", "not_author": "…"}}
         and the same for "reject" → tracked changes decided selectively
         (read="comments" lists them with id, author, paragraph, type);
       {"comment": id, "edit": "new text"} | {"comment": id, "delete": true}
         (whole thread) | {"comment": id, "reopen": true};
       {"final": true} → a clean copy: all changes accepted, all comments
         removed (send it when the review is over).
    Threads, resolved state, durable ids, dates and authors stay Word-native
    (commentsExtended / commentsIds / commentsExtensible / people) — also
    after `commands`, so the reviewer's Word shows exactly what they sent.

    commands: in order, each {"command": id, "params": {...}} or "id={json}".
      [{"command": "review.trackChanges", "params": {"value": true}},
       {"command": "select.text", "params": {"text": "old"}},
       {"command": "text.insert", "params": {"text": "new"}}]
    author: name on tracked changes and comments (e.g. "Claude (copyedit)").
    Files in params: pass "file_id" instead of a path, e.g.
      {"command": "insert.picture", "params": {"file_id": <image>, "width": 430}}.
    Jump to the n-th paragraph (1-based) with edit.goto {"paragraph": n}.
    After insert.picture the picture stays SELECTED: run caret.end before
      text.newParagraph, or Enter replaces (deletes) it.
    para.alignCenter/Left/Right TOGGLE like Word's buttons; to set an
      alignment use para.align {"value": "center"}.
    edit.replaceAll: ALWAYS pass "matchCase" explicitly. Find options carry
      over between commands (like Word's dialog), and a case-insensitive
      replace does not keep the found text's capitals ("az X" → "Az X").
    Build a TOC (references.toc) after review.acceptAll: on a document with
      pending changes it lists deleted and inserted text side by side.
    No file_id → starts from `template` (blank|sample|letter|resume|report).
    save_format: docx (default) | pdf | odt | rtf | html | md | tex | txt | png.

    read instead of editing: "text" | "info" | "inspect" (structure JSON,
    shows tracked-change runs) | "review" (compact list of tracked changes
    and comments: who changed what, in which sentence — read a human's
    edits back cheaply) | "comments" | "changes" (above) | "commands" (ids
    only; `query`, e.g. "table", gives params). text/inspect/comments/
    changes/commands are paged: pass the returned `next_offset` as `offset`
    (chars for text, blocks for inspect, items otherwise);
    `max_chars` caps one reply (default 20000).
    A human can continue by hand in the Word editor: give them the result's edit_url.
    """
    from app.tools import wordcraft as wc
    if read:
        return await wc.read(read, file_id=file_id, query=query, offset=offset, max_chars=max_chars,
                             base_file_id=base_file_id)
    if address:
        if not file_id:
            raise ValueError("address needs file_id: the commented document")
        if commands:
            raise ValueError("Give address or commands, not both — run the commands in a second call on the new file_id")
        from app.tools import review_loop
        return await review_loop.address(file_id, address, author or await review_loop.default_author())
    return await wc.run(
        file_id=file_id, commands=commands, author=author,
        save_format=save_format, template=template,
    )


@mcp.tool()
@linked
async def pptx_build(spec: Optional[dict] = None, from_document: Optional[str] = None,
                     instructions: Optional[str] = None, slides: Optional[int] = None,
                     images: str = "stock", template_file_id: Optional[str] = None) -> dict:
    """
    Build a whole 16:9 PowerPoint deck in ONE call, designed and illustrated.
    EITHER give `spec` (you write the slides, below) OR `from_document` =
    the file_id of a docx/odt/rtf/md/txt/html/pdf: the server turns that
    document into slides — its headings become slides, its facts and its
    own pictures are kept (`instructions` e.g. "10-minute talk for the
    board", `slides` 4–15 or omit for auto, `images` stock|doc|none,
    `template_file_id` a .pptx/.potx to build on). Typical chain: write and
    revise a document, then from_document, then pptx revisions.
    spec = {"title", "footer"?, "brand"? | "theme"? {primary, accent, bg,
            text, muted, font_heading, font_body}, "slides": [...]}
    "brand": the person's saved brand by name, or "mine"; left out, their
            default brand is used when it is set to apply automatically;
            "none" = off (the result says which brand it used). "theme"
            colours go on top. The deck keeps its look inside the file
            (slide master + theme fonts/colours), so slides added later — in
            PowerPoint, by office(app="deck"), or video(action="to_pptx") —
            get the same background and fonts.
    "template_file_id": an uploaded .pptx/.potx used as the FULL template —
            its slide masters, layouts, backgrounds, logo, fonts and colours;
            the slides go into the template's own layouts (theme is ignored).
    Slide layouts (each may carry "notes"):
      cover {title, subtitle?, kicker?, image?}   section {title, subtitle?, number?, image?}
      bullets {title, bullets[], text?, image?}    image_left|image_right {title, bullets[]|text, image}
      full_image {image, title?, caption?}         quote {quote, author?, image?}
      stats {title, stats:[{value, label}] ≤4}     chart {title, chart_type: column | bar | line |
                                                         pie | doughnut | area | scatter, categories,
                                                         series [{name, values}] — scatter: series
                                                         [{name, points: [[x, y, "name"?]…]}] (the name
                                                         labels the dot), x_title?, y_title? (axis
                                                         titles with the unit — a scatter needs both),
                                                         data_labels? (pie/doughnut: % on by default)}
      chart with the video's chart shapes — one object works in both (the video chart object under "chart", or its
              fields on the slide with chart_type), unit? on all: waterfall {items: [{label, value}] (a start, then
              + / − changes), total_label?, total?} | slope {labels: [before, after], items: [{label, from, to}]} |
              band {items: [{label, value, low?, high?}], forecast_from?} | lines {labels, series: [{name, values}]}
              | scatter {x_title, y_title, items: [{label, x, y, size?}]} | race {labels, items: [{label, values}]}.
              waterfall, lines, race: native charts (race = its last period as a ranking; the race moves only in the
              video). slope, band, scatter: native shapes, the same in the editor, PowerPoint and Keynote.
      table {title, headers, rows}                 gallery {title, images[≤4], captions?}
      diagram {title, diagram: {type: decision_tree | swimlane | network, nodes, edges, …}} — the same object as
              a video chart (see video): native, editable shapes (boxes with their text, arrows bound to the
              boxes, the legend); the same actor keeps its colour on every slide
      closing {title?, subtitle?}
    image = "<file_id>" | {"file_id"} | {"url"} (e.g. a picture you made with your own image tool)
            | {"stock": "<search words>"} (free CC photos, credit goes to notes).
    Images are cropped to their frame. This server doesn't make AI pictures for connected AIs.
    The result's `check` is the rendered check validate_document runs (both layouts): each problem names the
    slide and shape id with a ready office(app="deck") fix for just that shape — no rebuild. See the slides as
    pictures with validate_document.
    """
    _no_ai_pictures(spec, {"generate": "ai"} if (images or "").lower() == "ai" else None)
    if from_document:
        from app.routes.make import make_deck
        return await make_deck({"mode": "document", "source_file_id": from_document, "brief": instructions or "",
                                "slides": slides, "images": images, "style_file_id": template_file_id})
    if not spec:
        raise ValueError("Give either spec or from_document.")
    from app.tools import pptx_deck
    if template_file_id and not spec.get("template_file_id"):
        spec["template_file_id"] = template_file_id
    return await pptx_deck.build(spec)


@mcp.tool()
@linked
async def history(
    action: str = "inbox",
    file_id: Optional[str] = None,
    review_id: Optional[int] = None,
    resolution_file_id: Optional[str] = None,
    note: Optional[str] = None,
    other_file_id: Optional[str] = None,
    choices: Optional[dict[str, str]] = None,
) -> dict:
    """
    Version history and the human review inbox (Postgres).
      action="lineage" + file_id   → ancestors (source chain) and descendants
                                     of a file: who made which version how.
      action="inbox"               → open "back to Claude" requests a person
                                     sent from the dashboard (file + note).
      action="request" + file_id, note → ask a person to review a file.
      action="resolve" + review_id (+ resolution_file_id, note) → close one.
    Typical loop: inbox → wordcraft(read="review") on the file → edit with
    tracked changes → resolve with the new file_id.
    Branches (Word documents): several directions from the same version, merged idea by idea.
      action="branches" + file_id  → the version tree: nodes and the branch heads [{file_id, branch, label}].
      action="branch" + file_id, note=name → name a version's branch ("Sales focus"); any version you make
                                     from an older one is a new branch.
      action="compare" + file_id (A), other_file_id (B) → the places where they differ, paired by idea:
                                     units [{id, topic, kind, a: [{text}], b: [{text}]}].
      action="merge" + file_id (A), other_file_id (B), choices {unit id: "a"|"b"|"both"|"merge"} and/or
                                     note = how to combine them ("keep A's opening, add B's data") → a new version
                                     of A with B's parts as tracked changes the person accepts or rejects.
    """
    if action in ("branches", "branch", "compare", "merge"):
        import asyncio as _a
        from app.tools import branches
        if not file_id:
            raise ValueError(f"{action} needs a file_id")
        if action == "branches":
            return await _a.to_thread(branches.tree, file_id)
        if action == "branch":
            return branches.set_branch(file_id, note or "")
        if not other_file_id:
            raise ValueError(f"{action} needs other_file_id (the B branch)")
        if action == "compare":
            return await branches.plan(file_id, other_file_id)
        if not choices and not (note or "").strip():
            raise ValueError("merge needs choices per unit or a note saying how to combine them")
        return _links(await branches.merge(file_id, other_file_id, choices or {}, note or ""))
    from app import db
    if action == "lineage":
        if not file_id:
            raise ValueError("lineage needs a file_id")
        return await db.lineage(file_id)
    if action == "inbox":
        return {"open": await db.review_list("open", file_id=file_id)}
    if action == "request":
        if not file_id:
            raise ValueError("request needs a file_id")
        return await db.review_create(file_id, note, "claude")
    if action == "resolve":
        if review_id is None:
            raise ValueError("resolve needs a review_id")
        return await db.review_resolve(int(review_id), resolution_file_id, note)
    raise ValueError("action: lineage | inbox | request | resolve | branches | branch | compare | merge")


@mcp.tool()
@linked
async def design(
    template: str = "auto",
    data: Optional[dict] = None,
    size: Optional[str] = None,
    theme: Optional[dict] = None,
    brand: Optional[str] = None,
    export: Optional[list[str]] = None,
    variants: int = 3,
    sizes: Optional[list[str]] = None,
    from_design: Optional[str] = None,
    data_file_id: Optional[str] = None,
    design_file_id: Optional[str] = None,
    learn_brand: Optional[list[str]] = None,
    rebuild_from: Optional[str] = None,
) -> dict:
    """
    Designed graphics (VectorCraft — text stays editable; every file has edit_url to the design editor). One tool:

    1) template="auto" — an art director: data {title, goal? ("poster for a summer soul concert"), kicker?, subtitle?,
       body?, date?, place?, cta?, handle?, image?} → `variants` (default 3) CLEARLY different designs (layout,
       type, colours, whitespace) in `size` (default the first of `sizes`, else ig_post). Show the person the
       previews (each variant: name, why, preview PNG, file_id) and let them choose. Then:
    2) from_design=<the chosen variant's file_id>, sizes=[…] → the campaign pack: the same design re-laid out for
       every size (not stretched: the photo re-cropped around the person/subject, type sized to the format).
       Sizes: ig_post 1080×1080 · ig_portrait 1080×1350 · ig_story 1080×1920 · youtube 1280×720 · link 1200×630
       (Facebook/LinkedIn) · x_post 1600×900 · A4 · A3 · A5 · letter (print: PDF too). data may update the texts.
       Shortcut: template="auto", variants=1, sizes=[…] makes one design in all sizes at once.
    3) Templates (one fixed layout, fast): template / data:
      poster        {title, subtitle?, body?, image?, date?, place?, footer?}   size A4|A3|A5
      flyer         {title, kicker?, subtitle?, image?, body (2 columns), highlights[≤3]?, cta?, contact?}
      report_cover  {title, subtitle?, organization?, author?, date?, image?}
      certificate   {recipient, title?="Certificate", lead?, text, place?, date?, signatures[{name, role}]}  (landscape)
      card          {name, role?, organization?, phone?, email?, web?, address?}   (business card 85×55 mm)
      invitation    {title?, recipient? ("Dear Anna"), text, date?, place?, rsvp?, image?}   size A5|A6|A4
      social        {title, subtitle?, handle?, image?}   size landscape (1200×630) | square | story
      booklet       {title, subtitle?, organization?, author?, date?, cover_image?, sections[{heading, text, image?}]}
      book          {file_id (manuscript .docx, Heading 1 chapters), title?, author?, cover_image?, style_file_id?}
                    size A5|A4 → a WORD-typeset book + its PDF; the person checks it in the Word editor
      infographic   {data_file_id (CSV/XLSX) | table_image (a photo of a table), title?, brief?, source?}
                    size portrait (1080×1350) | landscape (1600×900) | both → 2–4 highlighted numbers, 1–2 charts and a
                    short explanation; every number is computed from the cells (check.verified names the cells)
      album         {photos [image, …], manuscript_file_id? (.docx with Heading 1 chapters | .md | .txt), title?,
                    subtitle?, author?, captions? [one per photo], auto_captions? (AI writes them), cover? image}
                    size A4 | A4-landscape | square | A5 → a photo album in the LAYOUT editor: facing pages, photos in
                    their own proportions, captions, contents page, cover; + print PDF (3 mm bleed, crop marks)
    4) MANY COPIES from a spreadsheet: data_file_id = CSV/XLSX (first row = column names, one row per copy) with
       template (certificate, card, invitation, poster, flyer, social) and data using {column} placeholders
       (e.g. {"recipient": "{name}", "text": "for completing {course}"}; empty main fields fill themselves from a
       matching column such as "name") — OR design_file_id = the person's own .vectorcraft design with {placeholders}
       in its texts. → ONE printable PDF (a page per row) + a ZIP of single PDFs (+ the editable all-copies design).
    5) BRAND: brand = the person's brand by name or id, "mine", or "none". Left out: the person's default brand is
       applied automatically when they have one (colours, fonts, logo). learn_brand = [file_ids of examples: posts,
       slides (pptx), PDFs, docx, the logo] → learns and saves their brand (palette, fonts, logo, layout habits) and
       makes it their default; returns it.
    6) REBUILD: rebuild_from = "<file_id>" (template ignored): a photo, screenshot or PDF of a poster / flyer / graphic →
       the SAME design rebuilt as an EDITABLE one: every text a real text box, solid blocks as shapes, photos and logos
       as movable pictures. data {page? (PDF), ai_clean? (AI also wipes the old text off the background, 1 credit),
       hint? (language)}. Returns comparison_url (before / after side by side) — look at it.
    Results carry check {status ok | needs_check | failed, findings, verified} — read it before you say it's done.
    image = "<file_id>" | {"file_id"} | {"url"} (e.g. a picture you made with your own image tool) | {"stock": "<words>"}.
    theme {primary, accent, bg, text, muted} overrides colours on templates. export (templates): pdf|png|svg|jpg|vectorcraft.
    """
    data = dict(data or {})
    if learn_brand:
        from app import db, tenancy
        from app.brands import learn, user as user_brands
        if not db.enabled():
            raise ValueError("Brands are kept in the database, which isn't connected right now.")
        spec = await learn.learn(list(learn_brand), data.get("name") or data.get("brand_name"))
        row = await db.brand_create(tenancy.current_user_id(), spec["name"], spec, make_default=True)
        return {"brand": user_brands.public(user_brands._from_row(row)),
                "note": "Saved and set as the default: it is applied to designs automatically (brand='none' to skip)."}
    if rebuild_from or template == "rebuild":
        from app.tools import rebuild
        return await rebuild.rebuild(rebuild_from or data.get("file_id"), page=int(data.get("page") or 1),
                                     ai_clean=bool(data.get("ai_clean")), hint=data.get("hint"))
    if template == "infographic":
        # a táblázat jöhet a data-ban VAGY a felső szintű data_file_id-ben (az a tömeggyártásé is — itt ugyanazt jelenti)
        from app.brands import user as user_brands
        from app.tools import infographic
        return await infographic.make(data_file_id=data.get("data_file_id") or data.get("file_id") or data_file_id,
                                      table_image_file_id=data.get("table_image"), title=data.get("title"),
                                      brief=data.get("brief"), source=data.get("source"),
                                      orientation=size or data.get("orientation") or "portrait", theme=theme,
                                      brand=await user_brands.resolve(brand))
    if template == "album":
        from app.brands import user as user_brands
        from app.tools import album
        return await album.make(data.get("photos") or [], manuscript_file_id=data.get("manuscript_file_id"),
                                title=data.get("title"), subtitle=data.get("subtitle"), author=data.get("author"), size=size,
                                captions=data.get("captions"), auto_captions=bool(data.get("auto_captions")),
                                cover=data.get("cover"), theme=theme, brand=await user_brands.resolve(brand))
    _no_ai_pictures(data)
    if from_design:
        from app.routes.studio import package
        return await package({"from_file_id": from_design, "sizes": sizes, "content": data})
    if data_file_id:
        from app.brands import user as user_brands
        from app.tools import design_mass
        return await design_mass.run(data_file_id=data_file_id, template=None if design_file_id else (template if template != "auto" else "certificate"),
                                     data=data, design_file_id=design_file_id, size=size, theme=theme,
                                     brand=await user_brands.resolve(brand))
    if template == "auto":
        from app.routes.studio import auto_variants, package
        body = {**data, "image": data.get("image"), "brand": brand, "size": size, "sizes": sizes, "variants": variants}
        res = await auto_variants(body)
        if variants == 1 and sizes:
            return await package({"from_file_id": res["variants"][0]["file_id"], "sizes": sizes})
        return res
    if template == "book":
        from app.routes.make import make_book
        cover = data.get("cover_image")
        return await make_book({"file_id": data.get("file_id"), "title": data.get("title"), "author": data.get("author"),
                                "size": size or "A5", "style_file_id": data.get("style_file_id"),
                                "cover": {"file_id": cover} if isinstance(cover, str) else
                                         ({"stock": cover["stock"]} if isinstance(cover, dict) and cover.get("stock") else
                                          {"ai": cover["generate"]} if isinstance(cover, dict) and cover.get("generate") else None)})
    from app.brands import user as user_brands
    from app.tools import design as dz
    return await dz.render(template, data, size=size, theme=theme, brand=await user_brands.resolve(brand), export=export)


@mcp.tool()
@linked
async def photocraft(
    file_id: Optional[str] = None,
    commands: Optional[list] = None,
    new: Optional[dict] = None,
    remove_background: bool = False,
    background_file_id: Optional[str] = None,
    background_blur: float = 0,
    keep: Optional[list] = None,
    drop: Optional[list] = None,
    mask_file_id: Optional[str] = None,
    refine_file_id: Optional[str] = None,
    save_format: Optional[str] = None,
    quality: Optional[int] = None,
    read: Optional[str] = None,
    query: Optional[str] = None,
    offset: int = 0,
    max_chars: int = 20000,
) -> dict:
    """
    Edit an image with PhotoCraft (Photoshop-like engine, ~860 commands):
    image.autoTone, image.imageSize {width, height}, image.crop, type.create
    {x, y, text, size, color}, filter.blur.gaussianBlur {radius},
    image.adjustments.photoFilter, layers… Result is a NEW file_id.
    Commands that only read (document.inspect, *.get, *.list…) save nothing: the same file_id comes back with `results`.

    remove_background=true cuts the subject out with a neural net (keeps
    hair, thin objects and EVERY person). background_file_id puts that image
    behind the subject, filling the canvas; background_blur (px, e.g. 18)
    fakes a shallow depth of field. One call does "put this animal in front
    of the Parliament": file_id=photo, remove_background=true,
    background_file_id=<image>, background_blur=18.
    The result has `check` {status: ok | needs_check | failed, findings,
    verified, suggest}: people kept, and objects people hold (instrument,
    microphone, bag…) that were left out. needs_check → read the findings;
    usually rerun with refine_file_id=<this result> and keep=[…] (suggest
    lists the names). keep / drop: words ("guitar", "hands",
    "microphone stand"), boxes {"box": [x0,y0,x1,y1]} or exact outlines
    {"polygon": [[x,y],…]} in source pixels — kept objects are added, dropped
    ones taken out. mask_file_id: your own mask (white = keep).
    refine_file_id: fix an earlier cut-out (or the picture made from it)
    without starting over; file_id may be left out then.

    commands: [{"command": id, "params": {...}}]; files in params by
    "file_id" (e.g. file.placeEmbedded). new={"width":1200,"height":800}
    starts a blank canvas. save_format: png (keeps transparency) | jpg |
    webp | psd | tif | pcraft; quality 1-100 for jpg/webp.
    read: "info" (size, layers) | "commands" (ids; `query` for params).
    """
    from app.tools import crafts
    if read:
        return await crafts.photo_read(read, file_id=file_id, query=query, offset=offset, max_chars=max_chars)
    return await crafts.photo_run(
        file_id=file_id, new=new, commands=commands, save_format=save_format, quality=quality,
        remove_background=remove_background, background_file_id=background_file_id,
        background_blur=background_blur, keep=keep, drop=drop, mask_file_id=mask_file_id,
        refine_file_id=refine_file_id,
    )


@mcp.tool()
@linked
async def vectorcraft(
    file_id: Optional[str] = None,
    commands: Optional[list] = None,
    export: Optional[list[str]] = None,
    scale: Optional[float] = None,
    read: Optional[str] = None,
    query: Optional[str] = None,
    offset: int = 0,
    max_chars: int = 20000,
) -> dict:
    """
    Design and lay out with VectorCraft (Illustrator/InDesign-like, ~730
    commands): posters, flyers, brochures, multi-page PDFs, SVG. Opens
    file_id (svg/pdf/ai/eps/.vectorcraft) or starts empty; exports every
    format in `export` (default ["pdf"]; also svg, png, jpg, webp, eps,
    vectorcraft) as new file_ids. Commands that only read (document.inspect, *.get, *.list…) save nothing: the same file_id comes back with `results`.

    Typical: file.new {width:"210 mm", height:"297 mm"} → shape.rectangle
    {x,y,width,height} → text.create {x, y, text, size, font, color,
    area:{width,height}} (area = wrapping text box) → file.place
    {"file_id": <image>} → artboard.new {width,height} (next page).
    Coordinates in points (A4 = 595×842). Fonts: "Source Serif 4",
    "Source Sans 3" are built in. A new shape/placed image stays SELECTED:
    colour it with paint.setFill {color} / paint.setStroke {none: true};
    Place images with file.place {"file_id": <img>, "rect": [x, y, w, h]}
    (fits inside, aspect kept); without rect a photo lands at its physical
    size, and object.setBounds on a placed image does not re-render it.
    Later objects stack on top (draw backgrounds first).
    read: "info" (artboards, fonts, counts) | "commands" (ids; `query`).
    """
    from app.tools import crafts
    if read:
        return await crafts.vector_read(read, file_id=file_id, query=query, offset=offset, max_chars=max_chars)
    return await crafts.vector_run(file_id=file_id, commands=commands, export=export, scale=scale)


@mcp.tool()
@linked
async def office(
    app: str,
    file_id: Optional[str] = None,
    commands: Optional[list] = None,
    save_format: Optional[str] = None,
    read: Optional[str] = None,
    query: Optional[str] = None,
    offset: int = 0,
    max_chars: int = 20000,
    slide: Optional[int] = None,
    shape_id: Optional[int] = None,
) -> dict:
    """
    Office engines, one tool. app = "deck" (PowerPoint-like, pptx) |
    "pdf" (Acrobat-like) | "layout" (InDesign-like, .designcraft/.idml) |
    "sheet" (Excel-like, xlsx/csv). Opens file_id (or starts empty),
    runs `commands`, saves a NEW file_id in save_format (default: the
    source format; deck → pptx, layout → pdf, sheet → xlsx). Every
    command's result comes back in `steps`. Commands that only READ
    (*.inspect, *.get, *.list, format.state, cell.get, edit.find, the PDF
    tools marked read-only…, plus navigation/selection) save nothing: the
    same file_id comes back with `results` and no new version.

    LOOK, THEN FIX ONE THING (decks): read="inspect" (+ slide=n) lists each
    slide's shapes — id, name, kind, box [x, y, w, h] pt, text, fill — with
    no new file; slide + shape_id gives one shape in full (fonts,
    paragraphs, effective box). Then change only that shape, in place:
    {"command": "text.set", "params": {"on_slide": n, "id": <shape id>,
    "text": …}} (keeps the text's formatting), or {"command":
    "edit.select", "params": {"on_slide": n, "ids": [id]}} followed by
    format.size {size} / format.bold / shape.setBounds {x, y, w, h} /
    shape.fill {color}. "on_slide" (1 = first slide) goes to that slide
    first. The result's `check` renders the deck again (the same check as
    validate_document) and names any remaining problem with its shape id
    and a ready fix.

    deck: [{"command": "file.new", "params": {"blank": true}} (no empty
      first slide), {"command": "slide.new", …},
      {"command": "slide.new", "params": {"layout": "titleAndContent",
      "title": …, "body": "line\nline"}}, {"command": "insert.picture",
      "params": {"file_id": <img>, "rect": [x,y,w,h]}}, {"command":
      "insert.chart", "params": {"type": "column", "categories": […],
      "series": [{"name", "values"}]}}, then chart.options {title: null |
      str, legend, dataLabels}]. save_format pdf/png exports.
    sheet: range.setValues {range: "A1", values: [[…]]} (strings starting
      with = are formulas), cell.set {cell, input}, insert.chart {range,
      type}, data.sort. save_format csv/json/html converts.
    pdf: commands are tool steps [{"tool": "redact_mark", "args": {"find":
      "secret"}}, {"tool": "redact_apply", "args": {}}, {"tool":
      "page_add_text", "args": {"page": 1, "text": …, "at": [x, y]}},
      form_fill, page_rotate/delete/move, bookmark_add, page_number…];
      the document is opened and saved for you.
    layout: page layout commands (file.new {preset: "A4", pages,
      facingPages, margins, bleed}…) → pdf/png/designcraft.
    read: "inspect" (the objects, no new file — deck: per slide, see
    above; sheet/layout/pdf: the document's structure) | "info" | "text"
    (pdf text, sheet csv) | "commands" (ids; pass `query` for params and
    descriptions — always look before guessing).
    """
    from app.tools import crafts
    if read:
        return await crafts.office_read(app, read, file_id=file_id, query=query, offset=offset, max_chars=max_chars,
                                        slide=slide, shape_id=shape_id)
    return await crafts.office_run(app, file_id=file_id, commands=commands, save_format=save_format)


@mcp.tool()
@linked
async def digitize(
    file_ids: Optional[list[str]] = None,
    outputs: Optional[list[str]] = None,
    language: Optional[str] = None,
    max_pages: Optional[int] = None,
    job_id: Optional[str] = None,
) -> dict:
    """
    Old book / photographed or scanned pages → editable documents. Each page
    is read by a vision model (old spelling kept, hyphenation and paragraphs
    rejoined, running heads and page numbers dropped, footnotes kept);
    printed illustrations, ornaments and maps are cut out and placed where
    they were; library stamps and handwriting are left out.

    Start: file_ids = page photos/scans in order, or scanned PDFs;
    outputs ⊆ ["docx", "pdf" (new typeset A5 book), "searchable_pdf"
    (original pages + invisible text layer), "review_pdf" (each page's
    image beside its transcript, doubtful readings highlighted — for
    checking a historical source), "txt", "md", "fillable_pdf" (a
    printed form rebuilt with real form fields)] (default docx).
    DOUBTFUL READINGS are marked, never guessed silently: a word read
    with doubt is highlighted in the Word file with a comment (page,
    other possible reading), [reading?] / [reading? / other] in txt/md,
    an illegible word [?] (Word: red). The result's `readings` = {uncertain,
    illegible, pages, items [{page, kind, reading, also, context}]}; the
    typeset PDF and the searchable text layer carry the best reading.
    language = hint, e.g. "Hungarian"; max_pages = only the first N pages
    (a sample chapter). Returns a job — a long book takes
    minutes: call again with job_id until status is "done" (result.files)
    or "failed". Pages already read are cached, so a rerun is cheap.
    """
    from app import jobs
    if job_id:
        return await jobs.job_status(job_id)
    from app.routes.make import make_digitize
    return await make_digitize({"file_ids": file_ids or [], "outputs": outputs or ["docx"], "language": language,
                                "max_pages": max_pages})


@mcp.tool()
@linked
async def pdf_form(
    file_id: str,
    values: Optional[dict] = None,
    data_file_id: Optional[str] = None,
    flatten: bool = False,
) -> dict:
    """
    Fillable PDF forms. With only file_id: lists the fields (name, type,
    value, options, page, semantic_type — date, number, phone, email,
    postcode… from the field's native format or its label: give values in
    that kind). With values {field: value}: fills them (check
    boxes take true/false; keys may be field names or close spellings) and
    returns a NEW file_id. With data_file_id (CSV/XLSX, first row = field
    names): one filled copy per row, combined into one PDF. flatten=true
    bakes the values in for printing (no longer editable). Accented text
    (ő, ű, …) is kept. To get a fillable form from a photo or scan, use
    digitize with outputs=["fillable_pdf"]; for a printed digital PDF use
    pdf tools action make_fillable (/api/make/pdf).
    """
    from app.routes.make import form_fields, make_fill_form
    if not values and not data_file_id:
        return await form_fields(file_id)
    return await make_fill_form({"file_id": file_id, "values": values, "data_file_id": data_file_id, "flatten": flatten})


@mcp.tool()
@linked
async def photo_edit(
    file_ids: Optional[list[str]] = None,
    instruction: Optional[str] = None,
    plan: Optional[list[dict]] = None,
    logo_file_id: Optional[str] = None,
    formats: Optional[list[str]] = None,
    job_id: Optional[str] = None,
) -> dict:
    """
    Precise photo editing from words — NOT a repaint. The photo stays; every
    step becomes its own layer in a PSD (adjustment layers with masks, type
    layers, the logo as an embedded layer), plus a JPG. All file_ids get the
    SAME edit (batch). instruction e.g. "Instagram 4:5, warmer, darker sky,
    headline 'Summer sale' at the top, logo bottom right".
    Or pass your own plan (skip our planner): a list of steps, each {"op": "<name>", …its fields}, e.g.
      [{"op": "crop", "aspect": "4:5"}, {"op": "color", "warmth": 20}, {"op": "text", "text": "Nyári akció"}].
      An unknown op or key is an error that lists the allowed values. ops:
      crop {aspect 1:1|4:5|5:4|16:9|9:16|3:2|2:3|4:3|3:4, focus center|top|bottom|left|right},
      resize {long_edge}, auto, sharpen {amount}, light {exposure, contrast,
      brightness}, color {vibrance, saturation, warmth}, look {name warm|cool|
      tealOrange|bleachBypass|fadedFilm|monoContrast|crossProcess, strength},
      black_white, sky {exposure, saturation, contrast}, vignette {amount},
      background {remove | stock | color, blur}, logo {position,
      width_pct, margin_pct}, text {text, position, size small|medium|large|
      huge, color, weight, shadow}, ai_edit {instruction}.
    Returns a job: call again with job_id until status "done"; result.plan
    is the plan used — tweak it and send it back as plan to adjust.
    """
    from app import jobs
    if job_id:
        return await jobs.job_status(job_id)
    if plan and any(isinstance(st, dict) and st.get("op") == "background" and st.get("ai") for st in plan):
        _no_ai_pictures({"generate": "background"})       # MCP-n generált hátteret nem készítünk
    from app.routes.make import make_photo_edit
    return await make_photo_edit({"file_ids": file_ids or [], "instruction": instruction, "plan": plan,
                                  "logo_file_id": logo_file_id, "formats": formats})


@mcp.tool()
@linked
async def restore_photos(
    file_ids: Optional[list[str]] = None,
    colorize: Optional[bool] = None,
    split_prints: bool = True,
    job_id: Optional[str] = None,
) -> dict:
    """
    Restore old photos (family albums): scratches, dust, stains, tears,
    fading and blur. split_prints=true finds every print on a photographed
    album page and straightens it. colorize: true/false, or omit = colourise
    black-and-white prints only. Each photo becomes a layered PSD — the
    ORIGINAL at the bottom, "Restored (AI)" above it (aligned pixel for
    pixel), "Colour (AI)" on top in Color blend mode — plus a JPG, so a face
    the AI got wrong can be masked back to the original. An album page with
    several prints also gets ONE PSD of the whole page (album_file_id): the
    page at the bottom and a layer group per picture, in place. Costs credits
    (1 per photo, +1 to colourise). Returns a job: call again with job_id.
    """
    from app import jobs
    if job_id:
        return await jobs.job_status(job_id)
    from app.routes.make import make_restore
    return await make_restore({"file_ids": file_ids or [], "colorize": colorize, "split_prints": split_prints})


@mcp.tool()
@linked
async def find_replace(
    file_id: str, pattern: str, replacement: str, regex: bool = False
) -> dict:
    """Replace a string (or regex) everywhere in a DOCX, PPTX (every slide's text), PDF (literal text only, no
    regex), MD, HTML or TXT file — a new version. For one text box on a slide, office(app="deck") text.set is exact."""
    return await t_find_replace(file_id, pattern, replacement, regex)


@mcp.tool()
@linked
async def delete_section(
    file_id: str, heading: str, level: Optional[int] = None
) -> dict:
    """Delete a section by heading title (DOCX or MD: content until the next same- or higher-level heading;
    PPTX: the slides whose title is that heading)."""
    return await t_delete_section(file_id, heading, level)


@mcp.tool()
@linked
async def insert_after(file_id: str, anchor: str, content: str) -> dict:
    """Insert content after a heading (DOCX), after a line (MD), or as new lines at the end of the body text of
    the slide with that title (PPTX)."""
    return await t_insert_after(file_id, anchor, content)


@mcp.tool()
@linked
async def replace_heading(file_id: str, old_title: str, new_title: str) -> dict:
    """Replace a heading's text while preserving its level (DOCX, MD; PPTX: a slide title)."""
    return await t_replace_heading(file_id, old_title, new_title)


@mcp.tool()
@linked
async def delete_pages(file_id: str, page_range: str) -> dict:
    """Delete pages from a PDF, or slides from a PPTX. page_range examples: '3', '1-5', '1,3,5-7'."""
    return await t_delete_pages(file_id, page_range)


@mcp.tool()
@linked
async def merge(file_ids: list[str], target_format: str = "pdf") -> dict:
    """Merge multiple files (currently PDF) into one."""
    return await t_merge(file_ids, target_format)


@mcp.tool()
@linked
async def extract(file_id: str, mode: str = "text") -> dict:
    """Extract text (PDF, DOCX, PPTX, MD, TXT, HTML) or tables (PDF, DOCX) — reads only, no new file.
    mode: 'text' (DOCX: in document order, each table row on its own line, cells tab-separated) | 'tables'
    ([{page, rows}] for PDF and DOCX; a merged cell's continuation is null)."""
    return await t_extract(file_id, mode)


@mcp.tool()
@linked
async def edit_with_instruction(
    file_id: str,
    instruction: str,
    require_confidence: bool = True,
    return_format: Optional[str] = None,
) -> dict:
    """
    Edit a document using a natural-language instruction.

    Pipeline: load → extract text → the AI plans operations →
    deterministic execution → render back to original format. The planning
    runs the Studio's own model (a Studio AI step); for a change you can
    express yourself, wordcraft / office commands or find_replace cost none.

    PDF inputs are auto-round-tripped via DOCX (with a layout-reflow warning).

    If `require_confidence=True` (default) and the LLM's confidence is
    below 0.7, returns the plan WITHOUT executing — letting you review.
    Set `require_confidence=False` to always execute.
    """
    return await t_edit_ai(
        _pool(),
        file_id=file_id,
        instruction=instruction,
        require_confidence=require_confidence,
        return_format=return_format,
    )


# ============================================================
# DOCX builder (build new documents from scratch)
# ============================================================


@mcp.tool()
@linked
async def facts(
    action: str = "check",
    file_id: Optional[str] = None,
    quote: Optional[str] = None,
    source: Optional[dict[str, str]] = None,
) -> dict:
    """
    A Word document's FACTS against sources, and numbers kept up to date.
      action="check" + file_id → every checkable claim (numbers, dates, who did what) with a verdict:
            contradicted | outdated | unverified | supported, the source it rests on (title + url, or the statistics
            office and period), the right value and — when it is wrong — a corrected `replacement` for the `quote`.
            Statistics are checked against StatData (Eurostat, KSH, ECB, FRED…), forint rates against the MNB, other
            facts against a web search: short search queries built from the claims leave the server. "unverified" means
            no source settled it — never say it is false. A statistic about a past period ("in 2024", "at the end of
            2025") is checked against THAT period's figure only; when the source has none, it is "unverified" with
            no_data_for_period (never compared with the latest figure). Costs 1 credit per 10 newly checked facts
            (repeats are free).
      action="bind" + file_id, quote (exact text with the number), source → links that number to its source;
            source = {"kind": "indicator", "country": "HU", "indicator": "cpi"} (cpi, core_cpi, policy_rate,
            unemployment, gdp_growth, gov_debt, bond_yield_10y, …) or {"kind": "fx", "currency": "EUR"} (MNB rate).
            Every `stat`/`fx` claim from check already carries the source to use. → a new version.
      action="list" + file_id → the linked numbers: text in the document, the figure saved at the last bind/refresh
        (checked_at), period, source, up_to_date (= the document still shows that figure). It does not fetch; for
        the newest figures call action="refresh".
      action="refresh" + file_id → every linked number set to its newest figure in the document's own number format,
            and only the words around it adjusted (period, direction, endings), all as tracked changes, with a comment
            where a conclusion may no longer hold → a new version (or "all up to date", no new file).
    """
    from app.routes.checks import _charge_claims
    from app.tools import content_check as cc
    from app.tools import live_data
    from app.tools.crafts import existing
    if not file_id:
        raise ValueError(f"{action} needs a file_id")
    if action == "check":
        r = await cc.facts(existing(file_id), charge=_charge_claims)
        return {"claims": [{k: c.get(k) for k in ("id", "verdict", "claim", "quote", "paragraph", "explanation", "correct",
                                                   "replacement", "source", "stat", "fx", "no_data_for_period")
                            if k != "no_data_for_period" or c.get(k)} for c in r["claims"]],
                "counts": r["counts"], "checked": r["checked"], "new_checks": r["new_checks"]}
    if action == "bind":
        if not quote or not isinstance(source, dict):
            raise ValueError("bind needs the quote (exact text with the number) and a source")
        return _links(await live_data.bind(file_id, quote, source))
    if action == "list":
        return await live_data.listing(file_id)
    if action == "refresh":
        return _links(await live_data.refresh(file_id))
    raise ValueError("action: check | bind | list | refresh")


@mcp.tool()
@linked
async def validate_document(file_id: str, pages: Optional[list[int]] = None, view: str = "word",
                            content: bool = False) -> dict:
    """CHECK a Word document, a deck or a PDF before telling the person it's done. The page pictures come back IN this
    result, together with a machine-checked list of problems — measured on the laid-out pages, not an AI's description.

    Returns status (ok | needs_check | failed), summary ("3 errors, 2 warnings"), issues [{id (E1…, W1…), severity,
    kind, page, object, block | slide + shape_id, message, fix}], and pictures: every page small (problem pages framed red/orange), a
    cut-out of each problem with it marked, and the pages with errors in full. A Word document is laid out twice — in
    the built-in editor and the way the downloaded file looks in Word — and the two are compared.
    It finds: empty list items; odd numbering (repeated, skipped, restarting, continuing under a new heading, typed
    numbers, heading numbers); table rows split across pages and tables continuing without their header row; headings
    left as the last line of a page; captions apart from their picture; text, tables or pictures off the page or into
    the margin; empty pages. Decks: text overflowing its box (as rendered), text drawn over other text, things off the slide, empty placeholders.
    block = the paragraph number in wordcraft(read="inspect"); edit.goto {"paragraph": block + 1} puts the caret there,
    and each issue's `fix` says what to do. Decks: shape_id is the shape's id on that slide, and `fix` is a ready
    office(app="deck") call that changes just that shape (text.set / format.size / shape.setBounds with "on_slide") —
    no rebuild. pptx_build and every deck edit already end with this same rendered check (their `check`), so the
    verdicts agree. Fix every error, then call validate_document again — a file that saved is not proof that the
    layout is right.

    Args:
      file_id: the .docx (or .odt/.rtf/.md), .pptx or .pdf to check.
      pages: page numbers to see in full (default: the pages with errors).
      view: "word" (default — the downloaded file) | "editor" (the built-in editor) | "both" (side by side).
      content: true → also read a Word document's TEXT for contradictions between two places, sudden changes of tone and
        the same thing named or spelled two ways (`content.findings`: kind, places [{paragraph, quote}], note, fix
        {quote, replacement} or null). Each quote is exact text of the document — put a finding in with
        wordcraft (select.text + review.newComment, or a tracked change). For facts against sources use facts(action="check").
    """
    import json
    from mcp.types import CallToolResult, ImageContent, TextContent
    from app.tools import validate as v
    info, images = await v.validate(file_id, pages=pages, view=view)
    if content and file_id.lower().endswith(".docx"):
        from app.tools import content_check as cc
        from app.tools.crafts import existing
        try:
            c = await cc.consistency(existing(file_id))
            info["content"] = {"findings": [{k: x[k] for k in ("id", "kind", "places", "note", "fix")} for x in c["findings"]],
                               "checked": c["checked"], "truncated": c["truncated"]}
            for x in info["content"]["findings"]:
                x["places"] = [{"paragraph": pl["paragraph"], "quote": pl["quote"]} for pl in x["places"]]
                if x["fix"]:
                    x["fix"] = {"quote": x["fix"]["quote"], "replacement": x["fix"]["replacement"]}
        except Exception as e:
            info["content"] = {"error": f"the content check didn't run: {e}"}
    info = _links(info)
    content = [TextContent(type="text", text=json.dumps(info, ensure_ascii=False))]
    for caption, jpg in images:
        content.append(TextContent(type="text", text=caption))
        content.append(ImageContent(type="image", data=base64.b64encode(jpg).decode(), mimeType="image/jpeg"))
    log.info("validate_document %s: %s (%d pictures)", file_id, info["summary"], len(images))
    return CallToolResult(content=content, structuredContent=info)


@mcp.tool()
@linked
async def insert_image(
    file_id: str, anchor: Optional[str] = None, image_file_id: Optional[str] = None,
    width_inches: Optional[float] = None,
    svg_file_id: Optional[str] = None,
    slot: Optional[str] = None,
    image_url: Optional[str] = None,
    credit: Optional[str] = None,
    ai_prompt: Optional[str] = None,
) -> dict:
    """Insert an image into the working DOCX after the anchor paragraph — OR change an existing picture.

    Insert: file_id + anchor + image_file_id (an uploaded PNG/JPG). If svg_file_id is also given, a vector
    SVG layer is attached (Word 2016+ / LO 7+ render it as vector). Combine with chart_redraw(also_svg=true).

    Pictures (decks and Word documents): file_id alone → the file's pictures [{slot, where, query?}].
    file_id + slot + ONE of image_file_id | image_url (+credit, e.g. a candidate from
    generate_image(stock_search=…), or a picture you made with your own image tool) → a NEW version with that
    picture replaced (same frame, nothing else changes). Let the person choose: show them the candidates.
    ai_prompt: not available here — this server doesn't make AI pictures for connected AIs (it returns an error)."""
    from app.tools import pictures
    if ai_prompt:
        _no_ai_pictures({"generate": ai_prompt})
    if slot or not anchor:
        if not slot:
            return {"pictures": await pictures.list_pictures(file_id)}
        pick = ({"url": image_url, "credit": credit} if image_url
                else {"file_id": image_file_id} if image_file_id else None)
        if not pick:
            raise ValueError("Give image_file_id or image_url for the new picture.")
        return await pictures.replace(file_id, slot, pick)
    if not image_file_id:
        raise ValueError("image_file_id is needed to insert a picture after the anchor.")
    return await t_insert_image(
        file_id=file_id, anchor=anchor, image_file_id=image_file_id,
        width_inches=width_inches, svg_file_id=svg_file_id,
    )


# ============================================================
# DOCX revisions / comments (collaborative editing)
# ============================================================


# ============================================================
# PPTX builder
# ============================================================


# ============================================================
# Vision (Kimi K2.6 multimodal — read images)
# ============================================================


@mcp.tool()
@linked
async def ocr_image(image_file_id: str, language_hint: Optional[str] = None) -> dict:
    """Read all visible text from an image (handwriting OK). language_hint
    helps weak-light photos: 'Hungarian', 'English', etc. Runs the Studio's own vision model: one Studio AI step (monthly allowance, then credits). If you can
    read images yourself, look at it with files(action="preview") instead."""
    return await vision_ops.ocr_image(image_file_id, language_hint=language_hint)


@mcp.tool()
@linked
async def describe_image(image_file_id: str, question: Optional[str] = None) -> dict:
    """Free-form Q&A on an image (AI vision). Default: detailed
    description. Pass `question` for targeted queries: 'what colors dominate?',
    'count the people', etc. NOT for checking your own document, deck or PDF:
    a description is not a check — use validate_document (you see the pages
    yourself, plus a machine-checked list of problems). Runs the Studio's own vision model: one Studio AI step (monthly allowance, then credits). If you can see
    images yourself, look at it with files(action="preview") instead."""
    return await vision_ops.describe_image(image_file_id, question=question)


@mcp.tool()
@linked
async def extract_table_from_image(image_file_id: str) -> dict:
    """Extract a table from an image as structured rows. Returns
    {headers: [...], rows: [[...], ...]}. Runs the Studio's own vision model:
    one Studio AI step (monthly allowance, then credits)."""
    return await vision_ops.extract_table_from_image(image_file_id)


@mcp.tool()
@linked
async def image_to_xlsx(image_file_id: str, sheet_name: str = "Sheet1", mode: str = "data") -> dict:
    """A photographed table → a real XLSX.
    mode="data" (default): the table's data only (header + rows), one shot.
    mode="layout": a FAITHFUL, fillable copy of a paper form (e.g. a photographed attendance sheet): perspective and
      skew corrected, the grid measured from the drawn lines — rows, columns, merged cells, proportional column widths
      and row heights, borders; text with bold and alignment; numbers, dates and times typed; empty cells of a
      form-like table marked as fillable fields (unlocked; listed in `fields`); a visible totals row rebuilt as
      formulas (SUM / COUNT / COUNTA, and the daily leave − arrival time) marked inferred and checked against the
      photo's numbers (`formulas`); uncertain readings commented (`uncertain`). Also `comparison_url` (the photo and
      the rendered sheet side by side) and, for an attendance sheet, `template_profile` — ready for
      forms(action="template_register") so the sheet can be filled every month.
    Runs the Studio's own vision model (Studio AI steps)."""
    if mode == "layout":
        from app.storage import display_label
        from app.tools import table_layout
        from app.tools.vision_ops import _resolve_image
        src = _resolve_image(image_file_id)
        return await table_layout.image_to_layout_xlsx(src, Path(display_label(image_file_id)).stem, sheet_name)
    if mode != "data":
        raise ValueError('mode: "data" (the table\'s data) or "layout" (a faithful, fillable copy of the form)')
    return await vision_ops.image_to_xlsx(image_file_id, sheet_name=sheet_name)


@mcp.tool()
@linked
async def generate_image(
    prompt: Optional[str] = None,
    stock_search: Optional[str] = None,
    n: int = 12,
) -> dict:
    """Pictures for a document, deck or video: stock_search="words" → free stock photo CANDIDATES (Wikimedia Commons +
    Openverse, unfiltered) — [{thumb_url, url, credit, title, width, height, source}] AND a numbered picture of them in
    the result, so you see them yourself. Show them to the person and let them pick; then
    insert_image(file_id, slot, image_url=…, credit=…), or {"url": …, "credit": …} in a pptx_build / design / video spec.

    This server doesn't generate AI pictures for connected AIs: `prompt` only returns an error saying so. Make the
    picture with your own image tool and pass its public URL (image_url / {"url": …}) or upload it (upload_from_url /
    upload_file → file_id)."""
    if stock_search:
        import json
        from mcp.types import CallToolResult, ImageContent, TextContent
        from app.tools import images
        cands = await images.search(stock_search, max(1, min(30, n)))
        info = {"candidates": cands}
        sheet = await images.candidate_sheet(cands[:16]) if cands else None
        if not sheet:
            return info
        info["note"] = ("The picture below shows the candidates numbered in the order of this list (1 = the first). "
                        "Look at them before choosing — the search matches file names, so some will be off-topic.")
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(info, ensure_ascii=False)),
                                       ImageContent(type="image", data=base64.b64encode(sheet).decode(), mimeType="image/jpeg")],
                              structuredContent=info)
    if prompt:
        _no_ai_pictures({"generate": prompt})
    raise ValueError("Give stock_search words for free stock photo candidates.")


# ============================================================
# Video
# ============================================================


@mcp.tool()
@linked
async def video(
    action: str = "make",
    text: Optional[str] = None,
    url: Optional[str] = None,
    source_file_id: Optional[str] = None,
    keep_wording: bool = False,
    format: str = "vertical",
    duration: Optional[float] = None,
    images: Optional[list] = None,
    pictures: str = "stock",
    music_file_id: Optional[str] = None,
    music: Optional[str] = None,
    voice_file_id: Optional[str] = None,
    narration: str = "ai",
    narration_voice: Optional[str] = None,
    accent: Optional[str] = None,
    language: Optional[str] = None,
    file_id: Optional[str] = None,
    job_id: Optional[str] = None,
    draft: bool = False,
    ken_burns: str = "some",
    data_file_id: Optional[str] = None,
    changes: Optional[dict] = None,
    deck_file_id: Optional[str] = None,
    after_slide: Optional[int] = None,
    title: Optional[str] = None,
    scenes: Optional[list] = None,
    kicker: Optional[str] = None,
    title_image: Optional[Any] = None,
    end_title: Optional[str] = None,
    end_subtitle: Optional[str] = None,
    scene: Optional[str] = None,
    brand: Optional[str] = None,
    layers: Optional[list] = None,
    alpha: Union[bool, str, None] = None,
    duration_mode: Optional[str] = None,
    camera: Optional[Any] = None,
    subtitles: Optional[Any] = None,
) -> dict:
    """Short videos with an EDITABLE timeline (an After Effects-like video editor opens from edit_url) — made from a
    text, or graphics on the person's OWN footage (action="overlay").

    YOUR OWN SCENE PLAN (most reliable): action="make" with scenes=[{"headline", "caption", "image"? (file_id | URL
      — e.g. a picture you made — | {"stock": "words"}), "image_query"? (English words for a stock photo — give it for every scene without an image: without it the search uses the headline), "stat"?
      {value, label}, "chart"? (see below), "duration"? seconds, "id"? }] + title, kicker?, title_image?, end_title?,
      end_subtitle?, format, duration (total; scenes without one share the rest). It is made exactly as given (no
      re-planning).
      MOVEMENT AND BACKGROUNDS, per scene (in scenes=[…] and in edit changes.scenes): "images": [up to 4 pictures]
      — the scene's backgrounds one after another, crossfading, each moving; "motion": how the picture moves —
      zoom_in | zoom_out (toward / away from its subject, Ken Burns) | pan_left | pan_right | pan_up | pan_down |
      still | auto, "focus"?: [x, y] 0–1 (the point on the picture to move toward; found automatically otherwise),
      "zoom"?: 1.03–1.8 (how far); "camera": a camera on the scene's picture AND graphic together — "push_in" |
      "pull_out" | "drift" | [{"at": seconds, "zoom": 1–3 (1 = the whole frame again), "to"?: [x, y] 0–1 of the
      frame, "dur"?: seconds}] (e.g. punch in on a detail of the photo at 1 s, or on the forecast part of a chart at
      3 s, back out at 6 s); the scene's title, big number and captions stay put. "fit": cover (the picture fills
      the frame, edges may be cut) | contain (the whole picture, its blurred copy filling the rest — e.g. a group
      photo in a vertical video) | auto (default: contain for the person's own photo when its shape is far from the
      frame's, cover otherwise). "background" for a scene without a picture: gradient | motion (light drifting slowly) | dark | "#RRGGBB". A scene with a chart shows the CHART's title at the top, not the headline (a graphic without a
      title shows the headline) — put what should be on screen into the chart title. Put the source in text too: chart numbers are checked against it (without it, against your scenes).

    action="make": an article (url, or its text in text), a document (source_file_id) or the person's own words
      (text + keep_wording=true ONLY when they want their own sentences word for word as the captions; for an
      article or notes to turn into a video, leave it false) → title card, scenes with full-screen pictures
      in gentle motion, headlines, timed captions, end card with picture credits → a video project (.ecproj) + the
      MP4. Takes 1–3 minutes, so it returns a job: call video(action="status", job_id=…) until status is "done"
      (result.project / result.video, each with edit_url) or "failed".
      format: vertical (9:16 — Reels, TikTok, Shorts) | landscape (16:9 — YouTube) | square. duration: seconds,
      4–180 (default 60; with keep_wording and no duration the reading pace sets it). duration_mode: "fixed" (the default
      when you give duration — the video is that long INCLUDING the title card and the end card, which get shorter on a
      short video: scenes hold longer or get shorter (down to 1.2 s), the narration is read up to 1.2× faster; what
      still doesn't fit is said in the check) | "narration" (the default without duration — the voice sets the
      length). Every result has duration = {requested, actual, mode}; a gap beyond a second or so is a check finding
      ("Asked for 24 s, it is 17 s (…)"), never "ok". images: pictures in order —
      file_ids or {"url": …} — images[0] is the TITLE CARD, images[1] scene 1, images[2] scene 2… (the check lists where
      each went; with scenes=[…] give each scene its own image instead); this server doesn't make AI pictures for you, so make them with your own image tool
      and pass their URLs. pictures: stock (free stock photos fill the scenes without one) | mine | none (| ai: AI
      photos — only in the Studio web app, for credits; here, make them yourself and pass them as images).
      AI NARRATION IS ON BY DEFAULT: narration="ai" — a natural AI voice (offline Piper voices, 58 languages, picked
      from the text or `language`) reads the title and every scene's caption aloud.
      narration="none" turns it off (captions only). narration_voice: "female" | "male" | a Piper voice id (e.g.
      "hu_HU-imre-medium", "en_US-ryan-medium").
      MUSIC (none by default): music = a mood — upbeat | calm | cinematic | lofi | corporate | playful | romantic |
      dramatic — or a track id → built-in CC0 music (no credit needed) fitted to the video's length; music_file_id =
      the person's own music. Under speech (the narration, a voice file, or overlay footage's own sound) the music
      drops and comes back in the pauses; "none" = no music.
      voice_file_id: the person's own voice-over (upload it first); it replaces the AI narration; the video follows
      its length (with duration in fixed mode: at least that long). accent: a hex colour for bars and numbers.
      ken_burns: some (default — every other scene a clear Ken Burns move: zoom and pan toward the picture's subject)
      | all | off (only a gentle drift).
      Animated graphics for any topic — chart = {"type", "title", "unit", "total"?, "min"?, "max"?, "items": [{"label",
      "value"?, "text"?}]}: column | bar | line | area (values over time) | donut (parts of a whole) | ring (one %) |
      icons ("1 in 4", x of 100) | counter (one number counting up) | compare (2–3 numbers side by side) | progress
      (separate %s) | gauge (one value on a min–max scale) | funnel (shrinking stages) | timeline (dated events: label =
      date) | steps (how-to) | proscons (label "+"/"-", text) | quote (text word for word from the source, label =
      who) | table ({"columns": [2–5 headings], "rows": [[cells]…] up to 7 rows}). With their own shape: waterfall (a start, then + / − changes; the end is computed and checked against
      "total"? — {"items": [{"label", "value"}…], "total_label"?}) | slope (before → after per item — {"labels": [before,
      after], "items": [{"label", "from", "to"}]}) | scatter (points, bubble when "size" — {"x_title", "y_title" (what
      each axis measures, with the unit), "items": [{"label", "x", "y", "size"?}]}) | band (a line with its low–high
      range, the forecast part dashed — {"items": [{"label": period, "value", "low"?, "high"?}], "forecast_from"?: the
      first forecast period}) | lines (2–4 series over the same periods, e.g. scenarios, named at their ends —
      {"labels": [periods], "series": [{"name", "values"}]}) | race (a ranking re-ordering period by period, the bars
      gliding into place — {"labels": [periods], "items": [{"label", "values"}]}, 3–10 items, 3–12 periods) | sankey
      (amounts flowing between stages, e.g. money from sources → programmes → projects; ribbons as thick as the flow,
      each node labelled with its computed total — {"flows": [{"from", "to", "value"}], "nodes"?: [{"name", "color"?}]
      (order and colour)}, 2–24 flows, at most 14 nodes, one way only: a loop drops it).
      Every number, date and quote is checked against the source — one that isn't there drops the graphic.
      graphic — draw your own (flow diagram, map with pins, labelled photo, org chart, before/after, moving type):
      {"type": "graphic", "title"?, "backdrop"? (0–1 dark veil over the picture, default 0.58; 0 = none), "elements": [
      {"shape": rect | circle | line | arrow | path | polygon | text | pin | image | lottie | map, x, y (centre, 0–1 of
      the frame), w, h (0–1; a text without w stays on ONE line up to 0.92 of the frame width, then wraps — the
      check measures every text and names overlaps, a text running out of its rect, and unexpected line breaks with a
      fitting w / size), r (circle/pin radius, 0–1 of the short side), from / to [x, y] (line, arrow), points [[x, y]…]
      (path, polygon), text (text, pin label), size (px at 1080), weight bold|semibold|regular, align, fill / stroke /
      color ("accent" | "white" | "muted" | "red"… | "#hex" | "none"), width (line px), radius (corner px), opacity (0–1),
      anim: appear | pop | grow (a rect grows from its base: upward when taller than wide — a bar —, else from the left)
      | draw (lines, and rect / circle outlines without fill, draw themselves) | slide-left | slide-right | slide-up |
      slide-down | pulse | none, at (seconds into the scene), dur, move_to [x, y] (then glides there), dash (line, arrow,
      path: "dashed" | "dotted" | [dash, gap] px), dim [[from, to] seconds] (faded while something else is shown),
      image (file_id | URL | {"stock": "words"} — or the person's VIDEO file_id: its picture plays there from when the
      element appears; "clip": [from, to] seconds of it; on an overlay "sound": "replace" | "mix" plays its sound, muted
      by default) + fit cover|contain, lottie (file_id | URL of a Lottie JSON |
      {"search": "bicycle"} — the server picks a free LottieFiles animation and credits it; it loops unless
      loop=false)}]}. video(action="lottie", text="bicycle") lists animations with previews, to choose one yourself.
      map — a REAL map under the other elements: {"shape": "map", "place": "Szeged" (or "bbox": [south, west, north,
      east]), "detail": auto | streets | outline, x/y/w/h = its box (default the whole frame)}. Streets, rivers and
      parks come from OpenStreetMap (a city or district), country borders from Natural Earth (a country or region,
      the named country highlighted); both credited on screen. Then place anything ON it by name or coordinates
      instead of x, y: {"shape": "pin", "place": "Dóm tér, Szeged", "text": "Dóm tér"}; from / to / points take
      {"place": …} or {"lat": …, "lon": …} too (a route between places is drawn straight, point to point). Pins and a
      route may also sit inside the map: {"shape": "map", "place": …, "pins": [{"place", "label"}], "route": [places]}.
      The map widens to show every placed element. Anything left out of a graphic (unknown shape, no position, a
      number not in the source) is listed in the check's findings. Up to 80 elements, each its own layer (editable by hand); a text
      with a number that isn't in the source is left out (the check says which).
      Explainer diagrams — give the relationship, the server lays it out (9:16, 16:9 and 1:1 each their own way) and builds
      it up step by step: {"type": "decision_tree" | "swimlane" | "network", "title"?, "nodes": [{"id", "text" (≤ 70
      characters), "kind"? (tree: question|condition|outcome · swimlane: start|step|decision|end), "actor"? (swimlane: its
      lane) | "group"? (network colour + legend), "order"? (swimlane: same order = same column), "size"? 1–3, "at"? (seconds:
      when it appears)}], "edges": [{"from", "to", "label"? ("yes", "if …", an amount), "kind"? sequence (then — the default)
      | hierarchy (is above) | money (money/goods flow, thicker) | cause | correlation (moves together: never drawn as a
      cause, no arrowhead) | link, "certainty"? certain | uncertain (dashed) | disputed (dotted + ?)}], "actors"? {"Name":
      {"color"?, "short"?}} (the same actor keeps its colour in every scene and edit), "reveal"? steps (default: tree root
      then branch by branch, swimlane in order, network centre then rings — the new part bright, the rest faded) | all,
      "pace"? seconds per step (the scene lengthens to fit), "focus"? false (no fading), "direction"? auto | down | right |
      outline (tree as indented rows), "center"? (network node id)}. Shorthands: tree "root" + nested "branches": [{"label",
      "text", "kind"?, "branches"?}]; swimlane "steps" (= nodes; without edges they follow in order); network edges alone
      (names as from/to). The legend lists the arrow kinds used. Numbers in boxes and labels are checked against the
      source (a label without one is left out, a box without one drops the diagram); a broken diagram is refused with
      what to fix. Change one with action=edit {"chart": {…}} WITHOUT "type": only the nodes (by id) and edges (by
      from + to) you send change — e.g. {"edges": [{"from": "a", "to": "b", "certainty": "uncertain"}]}, {"nodes": [{"id":
      "n3", "branches": [{"label", "text"}]}]} adds branches, {"remove": true} deletes, {"pace": 1.5} slows the build-up.
      threejs — a 3D scene you write in three.js (r170): {"type": "threejs", "title"?, "transparent"? (default true:
      it floats over the scene's picture or background), "code": "…"} — a function body that gets THREE, renderer, W,
      H (px), dur (s), accent (THREE.Color) and label(text, {size, height, color, bg}) (a text sprite in the Studio
      font) and returns {scene, camera, update(t)}. update(t) runs for every frame (t = 0…dur, 30 fps) — animate from
      t, never from the clock; Math.random is seeded. Rendered with software WebGL in a sandbox (no network, nothing
      to load: build textures with canvases); keep it light (~10 s of 1080p renders in a minute or two). Works as a
      scene chart and as a layer chart (on footage, split screen); a code error comes back in the check — fix it
      with action=edit {"chart": {"code": "…"}}. Text you draw in 3D is not checked against the source.
      script — when nothing above fits: your own EffectCraft code for the scene (After Effects-style JavaScript —
      comp.layers.addShape()/addText(), "ADBE …" match names, new Shape(), setValueAtTime keyframes, expressions):
      {"type": "script", "title"?, "backdrop"?, "code": "…"}; the code gets comp (the scene composition), W, H, dur
      (seconds), accent ([r, g, b], 0–1) and D (D.font.bold|semibold|regular). It runs in a sandbox (no files outside
      the project, no network, no processes; 20 000 characters at most). A script error, or a number on screen that
      isn't in the source, comes back in the check; fix it with action=edit {"chart": {"code": "…"}} and look at it
      with files(action="preview", scene=…, frames=6) or video(action="render", scene=…).
      Or pass data_file_id (CSV/XLSX: a chart per column, values from the cells).
      The result's check also reports COVERAGE (which source sentences and numbers made it in — `coverage` lists the
      missing ones), shortened texts, and where your pictures went. See it scene by scene: files(action="preview",
      file_id, scene="all") or scene="s3".
      layers=[…] (see below) put time-coded graphics on the made video too, at absolute times on its timeline.
    action="overlay": graphics on the person's OWN video. file_id = their uploaded mp4 | mov | webm | m4v — it stays
      the base track with its size, fps, length and sound (never re-cut, never forced to 9:16); camera=[{"at": time,
      "zoom", "to"?, "dur"?}] punches in on the footage (and back out with zoom 1), changes.camera in an edit;
      SUBTITLES in the Studio caption style (the dark caption bar of made videos): subtitles=[{"start", "end", "text"}]
      (seconds or "MM:SS" — e.g. the transcript's sentences, corrected with the person) or "auto" (the transcript as
      heard — show it to the person first when names or numbers matter); an .srt comes with the result;
      changes.subtitles in an edit replaces them. Layers are optional when there are subtitles;
      layers=[…] go on top
      → a project (.ecproj, editable in the video editor) + the MP4, as a job (poll status). text = the source: numbers,
      dates and quotes in the layers are checked against it (one that isn't there drops that layer, said in the
      check); without text they are kept and the check says "not checked against a source". title, accent, brand,
      music as for make (the music drops under the footage's own speech). The result's duration is the footage's
      (mode "footage").
    action="transcript": what is said in a video or sound file (or an overlay project's footage), with times →
      sentences [{at, until, text}] + an .srt subtitle file (file_id) — as a job. Offline speech recognition (Whisper
      large-v3-turbo; `language` helps, else detected); a file heard once is not transcribed again. Use it to suggest
      where a graphic would help, then tie layers to what is said (below).
    layers (overlay; make and edit too): [{"id"? ("L1", "L2"… given when left out — stable across edits), "at":
      seconds | "MM:SS" | "HH:MM:SS.ms" | {"say": "words that are said"} (overlay: the layer starts as they are said
      in the footage and stays to the end of that sentence; not said → left out, the check quotes what was heard),
      "dur": seconds | "until": time | {"say": …}, "type", "position"?: auto | top | bottom | left |
      right | center | [x, y, w, h] (centre and size, 0–1 of the frame — a graphic in such a box draws in the box: its
      x/y/w/h are 0–1 of the box and its sizes are px at 1080 on the box's short side), "anim"?: fade | slide | pop | none (or
      {"in", "out"})}]. Types: lower_third {name, role?, side?: left|right} · title {text, subtitle?, kicker?} (a band
      across the frame) · chapter {text, subtitle?, kicker?} · source {text} (a small credit) · stat {value, label} ·
      quote {text, author} (word for word from the source) · chart {chart: any chart above (column … quote, graphic
      with map / lottie / image, script), panel?: false = no dark panel} — type may be the chart type itself, e.g.
      {"type": "column", "title", "unit", "items"}; a graphic without position covers the frame transparently (an
      arrow or a label on the footage) · fullscreen {chart | image | text (+ subtitle, kicker)}: covers the picture,
      the sound goes on (image may be another VIDEO of theirs — a cut-in: + "clip": [from, to] seconds of it; its own
      sound plays instead of the footage's ("sound": "replace", the default here) | "mix" (both) | "off"; split takes a
      video too, muted unless "sound" is given) · split {side: left|right|top|bottom (where the graphic goes), chart | image | text}: the base
      shrinks to the other half. A lower third or source caption without position goes where nothing else is (other
      layers, a made video's headlines and charts). Overlaps that remain, a layer past the end, a dropped number all
      come back in the check's findings.
    alpha (make, overlay, edit): true = also a transparent, graphics-only video: ProRes 4444 .mov (yuva444p12le — for
      Premiere, Resolve, Final Cut) | "webm" (WebM VP9 with alpha — for the web; slower to render) | "both". It has
      no base (footage, pictures, darkening) and no sound; the check proves the alpha (pixel format + a decoded frame).
    action="edit": change a finished video (file_id = its project or its MP4) → a NEW version (project + MP4), as a job.
      changes = {"scenes": [{"id": "s3" (stable — info lists the ids; or "scene": n, 1 = first after the title card), "headline"?, "caption"?,
      "image"? (file_id | URL — e.g. a picture you made with your own image tool | {"stock": "words"} | "none"),
      "stat"? {value, label} | null, "chart"? (any graphic type above; WITHOUT "type" it is a fix of the scene's chart —
      e.g. {"unit": "million"} or {"title": …} changes only that) | null, "duration"?, "remove"? true}],
      "add": [{"after": "s2" | n (0 / "title" = first), "headline", "caption", "image" | "image_query", "stat"?, "chart"?, "duration"?}],
      "title"?, "title_image"?, "end_title"?, "end_subtitle"?, "format"? (vertical|landscape|square — pictures are
      fetched again in the new shape), "duration"?, "ken_burns"?, "accent"?, "music"? (a mood | a track id | "none"),
      "music_file_id"? | "none",
      "voice_file_id"? | "none", "narration"? "ai" | "none", "narration_voice"?, "duration_mode"?,
      "layers"?: [{"id": "L2", …only the fields that change} | {"id": "L2", "remove": true}] (a "chart" without "type" fixes
      just those chart fields), "add_layers"?: [new layers]}. Untouched layers stay as they are. The AI narration is
      recorded again from the changed captions. Numbers in a new chart or stat must be in the video's source or in
      `text` (pass the person's words there). An overlay project takes only layers, add_layers, accent, title and music.
      alpha=… renders the transparent version again (it is kept from the previous version otherwise).
      video(action="info") shows the scenes and layers (id, at, dur, type, text); check and preview_url come with the result.
    action="render" with scene="s3": just that scene as a short draft MP4 (a job, seconds) — check a change without
      rendering the whole video; scene="L2" (a layer): the video's span of that layer, with what is under it.
      (Without scene: re-render the whole project, e.g. after hand edits.) See a layer: files(action="preview",
      file_id, scene="L2") (its finished moment) or + frames=6 (in → out).
    brand: the person's saved brand (name, "mine") colours the video; left out, their default brand is used; "none" = off.
    action="to_pptx": put the video on a slide — a new one-slide deck, or deck_file_id (+ after_slide, title) adds a
      slide to an existing deck; the MP4 is embedded (plays in PowerPoint/Keynote, first frame as its poster). For a
      deck, make the video in the deck's colours (accent = the deck's accent colour, or the same brand) so it fits in.
    action="render": render a project again (file_id = .ecproj, e.g. after hand edits in the editor) → job → MP4;
      draft=true renders half size, fast.
    action="info": a project's compositions, length, scenes, layers, media files and last render (file_id).
    action="status": a make / overlay / edit / render job (job_id).
    """
    from app import jobs
    from app.tools import video as vid
    act = (action or "make").lower()
    if act in ("status", "job") or (act == "info" and job_id):
        if not job_id:
            raise ValueError("status needs job_id")
        return await jobs.job_status(job_id)
    if act == "info":
        if not file_id:
            raise ValueError("info needs file_id (a video project or movie)")
        return vid.info(file_id)
    if act == "render":
        from app.routes.video import render_video
        if scene:                                   # egyetlen jelenet gyors renderje (ellenőrzéshez, az egész nélkül)
            from app import jobs as jobs_mod
            from app.tools import video as vmod

            async def work(progress):
                progress(0, 1, "rendering the scene")
                return await vmod.render_scene(file_id or "", scene, draft=True)
            return jobs_mod.start("video", work)
        return await render_video({"file_id": file_id or "", "draft": draft})
    if act == "edit":
        from app.routes.video import edit_video
        return await edit_video({"file_id": file_id or "", "changes": changes or {}, "source": text or "", "alpha": alpha})
    if act in ("transcript", "subtitles"):           # időbélyeges átirat + .srt (saját felvétel vagy hangfájl)
        from app.routes.video import transcript_video
        return await transcript_video({"file_id": file_id or "", "language": language})
    if act in ("overlay", "footage"):                # a felhasználó felvétele + időkódos rétegek
        from app.routes.video import overlay_video
        return await overlay_video({"file_id": file_id or "", "layers": layers or [], "text": text or "", "title": title,
                                    "accent": accent, "brand": brand, "alpha": alpha, "music": music, "camera": camera,
                                    "subtitles": subtitles, "language": language})
    if act in ("to_pptx", "pptx", "slide"):
        from app.routes.video import video_to_pptx
        return await video_to_pptx({"file_id": file_id or "", "deck_file_id": deck_file_id, "after_slide": after_slide,
                                    "title": title})
    if act in ("lottie", "lottie_search"):          # Lottie-animációk a LottieFiles ingyenes könyvtárából
        from app.tools import lottie
        cands = await lottie.search(text or "", n=10)
        return {"query": text, "animations": [{k: c[k] for k in ("name", "json_url", "thumb_url", "gif_url", "author", "page")}
                                              for c in cands],
                "how": 'Put one in a graphic: {"shape": "lottie", "lottie": {"url": json_url, "credit": …}} — or let the '
                       'server choose: {"shape": "lottie", "lottie": {"search": "words"}}. Free LottieFiles animations '
                       "(Lottie Simple License: commercial use allowed); the author is credited on the end card."}
    if act != "make":
        raise ValueError('action: "make" | "overlay" | "transcript" | "edit" | "to_pptx" | "render" | "info" | "status" | "lottie"')
    from app.routes.video import make_video
    body = {"mode": "text" if keep_wording else "article", "text": text, "url": url, "source_file_id": source_file_id,
            "format": format, "duration": duration, "images": images or [], "pictures": pictures,
            "music_file_id": music_file_id, "music": music, "voice_file_id": voice_file_id, "accent": accent,
            "language": language, "narration": narration, "narration_voice": narration_voice,
            "ken_burns": ken_burns, "data_file_id": data_file_id, "scenes": scenes, "title": title, "kicker": kicker, "brand": brand,
            "title_image": title_image, "end_title": end_title, "end_subtitle": end_subtitle,
            "layers": layers or [], "alpha": alpha, "duration_mode": duration_mode}
    return await make_video(body)


# ============================================================
# Charts
# ============================================================


@mcp.tool()
@linked
async def generate_chart_image(
    chart_type: str,
    categories: list[str],
    series: list[dict],
    title: Optional[str] = None,
    palette: Optional[list[str]] = None,
    x_label: Optional[str] = None, y_label: Optional[str] = None,
    show_data_labels: bool = False,
    also_svg: bool = False,
) -> dict:
    """Render a chart from data into a PNG via matplotlib (200 DPI default).
    chart_type ∈ {column,bar,line,line_markers,area,pie,doughnut,scatter}.
    Pass also_svg=true to ALSO produce a vector SVG copy (svg_file_id).
    Returns the PNG file_id, plus svg_file_id if also_svg."""
    return await chart_image.generate_chart_image(
        chart_type, categories, series,
        title=title, palette=palette,
        x_label=x_label, y_label=y_label,
        show_data_labels=show_data_labels,
        also_svg=also_svg,
    )


@mcp.tool()
@linked
async def chart_redraw(
    image_file_id: str,
    palette: Optional[list[str]] = None,
    brand: Optional[str] = None,
    chart_type_override: Optional[str] = None,
    title_override: Optional[str] = None,
    also_svg: bool = False,
) -> dict:
    """Read an existing chart from an image via AI vision (extracts
    type, axes, categories, series, values), and re-render via matplotlib
    with a new palette or brand (the person's saved brand by name, or "mine"; palette = hex colours wins).
    Returns new image_file_id. Pass also_svg=true
    for a vector copy (use with insert_image's svg_file_id for vector
    charts in DOCX/PDF)."""
    return await chart_redraw_mod.chart_redraw(
        image_file_id,
        palette=palette, brand=brand,
        chart_type_override=chart_type_override,
        title_override=title_override,
        also_svg=also_svg,
    )


# --- Tool-annotációk (MCP ToolAnnotations) ---
# A ChatGPT (Apps SDK) mindhármat KÖTELEZŐNEK veszi; ha hiányzik, a szabvány alapértéke él: openWorld=true,
# destructive=true — vagyis „nyilvános külső világba ír, felülír”. 10-13: a ChatGPT jóváhagyó-ellenőrzése ezért
# nem engedte a tanulmányt a Studióba küldeni („külső szolgáltatásnak továbbítás”). A valóság: a fájlok a
# felhasználó SAJÁT, privát munkaterületére mennek, minden módosítás ÚJ változat (az eredeti megmarad).
# openWorld=true CSAK ott, ahol az eszköz tényleg a nyilvános netre nyúl (URL letöltése, stockkép-/Lottie-/térkép-
# keresés, cikk-URL) — az OpenAI definíciója szerint egy privát fiók nem „open world” attól, hogy külső szerveren fut.
_READ_ONLY = {"extract", "ocr_image", "describe_image", "extract_table_from_image", "validate_document", "catalog", "units"}
_OPEN_WORLD = {"upload_from_url", "pptx_build", "design", "insert_image", "generate_image", "video", "photo_edit", "facts",
               "transfer", "jobs", "convert"}


from app import mcp_ext  # noqa: E402,F401  (csoportosított bővítő eszközök: file_tools, transfer, jobs, catalog, units)


def _annotate_tools() -> None:
    from mcp.types import ToolAnnotations
    for t in mcp._tool_manager.list_tools():
        t.annotations = ToolAnnotations(readOnlyHint=t.name in _READ_ONLY, destructiveHint=False,
                                        openWorldHint=t.name in _OPEN_WORLD)


_annotate_tools()


# --- Plumbing ---


_app_ref: FastAPI | None = None


def _pool():
    if _app_ref is None or not hasattr(_app_ref.state, "pool"):
        raise RuntimeError("Worker pool not initialized")
    return _app_ref.state.pool


def mount_mcp(app: FastAPI):
    """Mount the MCP streamable-HTTP endpoint at /mcp."""
    global _app_ref
    _app_ref = app

    mcp_app = mcp.streamable_http_app()
    # Hitelesítés: app/auth.py (MCP-kulcs Bearer-ben vagy ?key=-ben).
    app.mount("/mcp", mcp_app)
    log.info("MCP endpoint mounted at /mcp")
