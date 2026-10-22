"""Videó végpontok (a logika: app/tools/video.py).

    POST /api/make/video           automata videó (cikk / URL / dokumentum / saját szöveg) → job; mode=new → üres projekt
    POST /api/video/overlay        a felhasználó felvétele + időkódos rétegek (névsáv, fejezetcím, ábra…) → job
    POST /api/video/render         egy videóprojekt (.ecproj) renderelése MP4-re → job
    POST /api/video/edit           egy elkészült videó módosítása a forgatókönyvéből (changes) → job, új változat
    POST /api/video/to-pptx        a videó egy diára (új pakli vagy egy meglévő új diája)
    GET  /api/video/assets         a projekt médiafájljai (név + letöltési link) — a böngészős szerkesztőnek (bridge)
    POST /api/video/asset          a böngészőben a projekthez adott médiafájl feltöltése (mentés után a bridge küldi)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app import events, tenancy
from app.tools import crafts
from app.config import settings
from app.storage import output_path, public_url, temp_path

router = APIRouter()
log = logging.getLogger("aice.video.routes")


def _fail(e: Exception) -> HTTPException:
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, (ValueError, FileNotFoundError)):
        return HTTPException(400, str(e))
    from app.routes.make import _fail as make_fail
    return make_fail(e)


@router.post("/api/make/video")
async def make_video(body: dict[str, Any]) -> dict[str, Any]:
    """{mode: article|text|new, text?, url?, source_file_id?, format: vertical|landscape|square, duration?, duration_mode?,
    images?, pictures?: stock|mine|none, music?: <mood|track id|none>, music_file_id?, voice_file_id?, narration?: ai|none,
    narration_voice?, accent?,
    language?, layers?, alpha?}"""
    from app import jobs
    from app.tools import video
    mode = (body.get("mode") or "article").lower()
    if mode == "new":
        try:
            out = await video.blank(body.get("format"), body.get("duration"))
        except Exception as e:
            raise _fail(e)
        events.emit("tool_done", tool="make_video", params={"mode": "new"}, result={"file_id": out["file_id"]})
        return out
    if tenancy.is_guest():
        from app.auth import ACCOUNT_NEEDED
        raise HTTPException(401, ACCOUNT_NEEDED)
    if not video.available():
        raise HTTPException(503, "The video engine isn't installed on this server.")
    try:
        video.fmt_name(body.get("format"))
        video.check_music(body.get("music"))
        video.scrim_of(body.get("scrim"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if str(body.get("pictures") or "").lower() == "ai" and not tenancy.client_makes_images.get():
        # AI-fotók: a munka ELŐTT kiderüljön, ha nincs rá kredit (a pakli ugyanígy) — becsült darabszám: címkép + jelenetek
        from app.routes.make import _credits_left
        from app.tools import ai_image
        n = 1 + (len(body.get("scenes") or []) or max(2, min(12, round(((float(body.get("duration") or 60)) - 7) / 6.5))))
        left, per = await _credits_left(), ai_image.picture_price()
        if left is not None and left < n * per:
            raise HTTPException(402, f"AI photos for this video need about {n * per} credits "
                                     f"({per} per picture); you have {left}. Choose stock photos or add credits.")

    async def work(progress):
        try:
            res = await video.make(body, progress)
        except Exception as e:
            raise _fail(e)
        events.emit("tool_done", tool="make_video", params={"mode": mode},
                    result={"files": [f["file_id"] for f in res.get("files") or []]})
        return res
    return jobs.start("video", work)


@router.post("/api/video/overlay")
async def overlay_video(body: dict[str, Any]) -> dict[str, Any]:
    """{file_id: <mp4|mov|webm|m4v>, layers: [...], text?: a forrás, title?, accent?, brand?, alpha?} → job
    (a projekt + MP4, alpha esetén az átlátszó grafika is)."""
    from app import jobs
    from app.tools import video
    if tenancy.is_guest():
        from app.auth import ACCOUNT_NEEDED
        raise HTTPException(401, ACCOUNT_NEEDED)
    if not video.available():
        raise HTTPException(503, "The video engine isn't installed on this server.")
    try:
        video.check_music(body.get("music"))
        fid, _ = video.overlay_input(body)
    except FileNotFoundError:
        raise HTTPException(400, "That file isn't in the workspace — upload the video first (upload_file), then pass its file_id.")
    except ValueError as e:
        raise HTTPException(400, str(e))

    async def work(progress):
        try:
            res = await video.overlay(body, progress)
        except Exception as e:
            raise _fail(e)
        events.emit("tool_done", tool="overlay_video", params={"file_id": fid},
                    result={"files": [f["file_id"] for f in res.get("files") or []]})
        return res
    return jobs.start("video", work)


@router.post("/api/video/cutout")
async def cutout_video(body: dict[str, Any]) -> dict[str, Any]:
    """{file_id: zöld / kék háttér előtt felvett videó, background?, key?, alpha?} → job (új hátterű MP4 és/vagy átlátszó
    videó) — app/tools/video_key.py."""
    from app import jobs
    from app.tools import video, video_key
    if tenancy.is_guest():
        from app.auth import ACCOUNT_NEEDED
        raise HTTPException(401, ACCOUNT_NEEDED)
    try:
        crafts.existing(body.get("file_id") or "")
        video_key.key_of(body.get("key"))
        video_key.background_of(body.get("background"))
        if body.get("alpha") not in (None, ""):
            video.alpha_kinds(body.get("alpha"))
    except FileNotFoundError:
        raise HTTPException(400, "That file isn't in the workspace — upload the video first.")
    except ValueError as e:
        raise HTTPException(400, str(e))

    async def work(progress):
        try:
            return await video_key.cutout(body, progress)
        except Exception as e:
            raise _fail(e)
    return jobs.start("video", work)


@router.post("/api/video/social")
async def social_video(body: dict[str, Any]) -> dict[str, Any]:
    """{file_id: videó | projekt | hang, platforms?, language?, notes? (link, felhívás), force?} → job (a posztok
    platformonként, fejezetek, kulcsszavak, .md)."""
    from app import jobs
    from app.tools import social
    if tenancy.is_guest():
        from app.auth import ACCOUNT_NEEDED
        raise HTTPException(401, ACCOUNT_NEEDED)
    fid = body.get("file_id") or ""
    try:
        crafts.existing(fid)
        social.platforms_of(body.get("platforms"))
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, str(e) if isinstance(e, ValueError) else "That file isn't in the workspace.")

    async def work(progress):
        try:
            return await social.posts(fid, platforms=body.get("platforms"), language=body.get("language"),
                                      notes=body.get("notes") or "", force=bool(body.get("force")), progress=progress)
        except Exception as e:
            raise _fail(e)
    return jobs.start("video", work)


@router.post("/api/video/variants")
async def variants_video(body: dict[str, Any]) -> dict[str, Any]:
    """{file_id: <projekt vagy a renderje>, changes: {variants: [changes…]} | {count: 1–3}, draft?} → job (A/B változatok)."""
    from app import jobs
    from app.tools import video
    if tenancy.is_guest():
        from app.auth import ACCOUNT_NEEDED
        raise HTTPException(401, ACCOUNT_NEEDED)
    fid = body.get("file_id") or ""
    try:
        pid = video.project_of(fid)
        video.variant_plan((video.read_meta(pid) or {}).get("spec") or {}, body.get("changes"))   # a hibás kérés most derüljön ki
    except ValueError as e:
        raise HTTPException(400, str(e))

    async def work(progress):
        try:
            return await video.variants(fid, body.get("changes"), progress, draft=bool(body.get("draft")))
        except Exception as e:
            raise _fail(e)
    return jobs.start("video", work)


@router.post("/api/video/audiogram")
async def audiogram_video(body: dict[str, Any]) -> dict[str, Any]:
    """{file_id: hang vagy videó, image?, format?, title?, style?, captions?, caption_style?, language?} → job (podcast-videó:
    borító, hullámforma, cím, felirat — szerkeszthető projekt)."""
    from app import jobs
    from app.tools import video
    if tenancy.is_guest():
        from app.auth import ACCOUNT_NEEDED
        raise HTTPException(401, ACCOUNT_NEEDED)
    if not video.available():
        raise HTTPException(503, "The video engine isn't installed on this server.")

    async def work(progress):
        try:
            return await video.audiogram(body, progress)
        except Exception as e:
            raise _fail(e)
    return jobs.start("video", work)


@router.post("/api/video/render")
async def render_video(body: dict[str, Any]) -> dict[str, Any]:
    """{file_id: <.ecproj>, format?: mp4|webm|gif, draft?: bool} → job (result: a renderelt fájl)."""
    from app import jobs
    from app.tools import video
    fid = body.get("file_id") or ""
    if not fid.lower().endswith(".ecproj"):
        raise HTTPException(400, "Pick a video project (.ecproj) to render.")

    async def work(progress):
        progress(0, 1, "rendering the video")
        try:
            res = await video.render(fid, (body.get("format") or "mp4").lower(), bool(body.get("draft")))
        except Exception as e:
            raise _fail(e)
        events.emit("tool_done", tool="render_video", params={"file_id": fid}, result={"file_id": res["file_id"]})
        return res
    return jobs.start("render", work)


@router.post("/api/video/edit")
async def edit_video(body: dict[str, Any]) -> dict[str, Any]:
    """{file_id: <.ecproj vagy a renderje>, changes: {...}, source?: str, alpha?} → job (az új projekt + MP4)."""
    from app import jobs
    from app.tools import video
    if tenancy.is_guest():
        from app.auth import ACCOUNT_NEEDED
        raise HTTPException(401, ACCOUNT_NEEDED)
    fid = body.get("file_id") or ""
    try:
        video.project_of(fid)
        video.check_music((body.get("changes") or {}).get("music"))
    except ValueError as e:
        raise HTTPException(400, str(e))

    async def work(progress):
        try:
            res = await video.edit(fid, body.get("changes") or {}, extra_source=body.get("source") or "", progress=progress,
                                   alpha=body.get("alpha"), render=body.get("render", True) is not False,
                                   draft=bool(body.get("draft")))
        except Exception as e:
            raise _fail(e)
        events.emit("tool_done", tool="edit_video", params={"file_id": fid}, result={"files": [f["file_id"] for f in res.get("files") or []]})
        return res
    return jobs.start("video", work)


@router.post("/api/video/transcript")
async def transcript_video(body: dict[str, Any]) -> dict[str, Any]:
    """{file_id: <videó | hang | overlay-projekt>, language?, prompt?, vocabulary?, model?, force?, correct?, script?} → job
    (időbélyeges mondatok + .srt + a javítások) — a paraméterek: app/tools/transcript.py."""
    from app import jobs
    from app.tools import transcript, video
    if tenancy.is_guest():
        from app.auth import ACCOUNT_NEEDED
        raise HTTPException(401, ACCOUNT_NEEDED)
    if not transcript.available():
        raise HTTPException(503, "Speech recognition isn't installed on this server.")
    fid = body.get("file_id") or ""
    try:
        crafts.existing(fid)
    except (FileNotFoundError, ValueError):
        raise HTTPException(400, "That file isn't in the workspace — upload it first (upload_file), then pass its file_id.")
    try:
        flag = lambda v: None if v is None or v == "" else str(v).strip().lower() not in ("0", "false", "no", "off")  # noqa: E731
        opts = {"prompt": body.get("prompt"), "vocabulary": transcript.vocab_list(body.get("vocabulary")),
                "model": transcript.model_name(body.get("model")), "force": bool(flag(body.get("force"))),
                "correct": flag(body.get("correct"))}
        if str(body.get("script") or "").strip():          # a kimondott szöveg: a szavak abból, az idők a hallásból
            opts["script"] = str(body["script"]).strip()[:20_000]
    except ValueError as e:
        raise HTTPException(400, str(e))

    async def work(progress):
        try:
            return await video.transcribe_file(fid, body.get("language"), progress, **opts)
        except Exception as e:
            raise _fail(e)
    return jobs.start("video", work)


@router.post("/api/video/srt")
async def video_srt(body: dict[str, Any]) -> dict[str, Any]:
    """{cues: [{start, end, text}], source_file_id?} → a (javított) feliratok .srt-fájlként (a kikapcsolható felirathoz)."""
    from app.storage import display_label, output_path, write_meta
    from app.storage.files import new_file_id
    from app.storage.naming import root_name
    from app.tools import video
    if tenancy.is_guest():
        from app.auth import ACCOUNT_NEEDED
        raise HTTPException(401, ACCOUNT_NEEDED)
    try:
        cues = video.subtitle_cues(body.get("cues"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not cues:
        raise HTTPException(400, "No subtitle lines — give cues with start, end and text.")
    src = body.get("source_file_id")
    sid = new_file_id("srt")
    output_path(sid).write_text(video._srt_text(cues), "utf-8")
    stem = root_name(display_label(src), "subtitles") if src else "subtitles"
    write_meta(sid, label=f"{stem}.srt", operation="subtitles", source_file_id=src)
    return video._file_entry(sid)


@router.post("/api/video/to-pptx")
async def video_to_pptx(body: dict[str, Any]) -> dict[str, Any]:
    """{file_id: videó vagy projekt, deck_file_id?, after_slide?, title?} → az új pptx."""
    from app.tools import video
    try:
        res = await video.to_pptx(body.get("file_id") or "", body.get("deck_file_id"), body.get("after_slide"), body.get("title"))
    except Exception as e:
        raise _fail(e)
    events.emit("tool_done", tool="video_to_pptx", params={"file_id": body.get("file_id")}, result={"file_id": res["file_id"]})
    return res


@router.get("/api/video/assets")
async def video_assets(file_id: str) -> dict[str, Any]:
    from app.tools import video
    if not file_id.lower().endswith(".ecproj"):
        raise HTTPException(400, "not a video project")
    out = []
    for name, fid in video.assets_of(file_id).items():
        if output_path(fid).exists():
            u = public_url(fid)
            out.append({"name": name, "url": u[u.index("/files/"):] if "/files/" in u else u})
    return {"assets": out}


@router.post("/api/video/asset")
async def video_asset(request: Request, project: str = Query(...), name: str = Query(...)) -> dict[str, Any]:
    from app.tools import video
    if not project.lower().endswith(".ecproj") or not output_path(project).exists():
        raise HTTPException(404, "no such video project")
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty file.")
    if len(data) > settings.EDITOR_MAX_SAVE_MB * 1024 * 1024:
        raise HTTPException(413, f"larger than {settings.EDITOR_MAX_SAVE_MB} MB")
    tmp = temp_path()
    tmp.write_bytes(data)
    try:
        fid = video.add_asset(project, name, Path(tmp))
    finally:
        tmp.unlink(missing_ok=True)
    return {"file_id": fid, "name": video._asset_name(name)}
