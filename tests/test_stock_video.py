"""Ingyenes stock videó a Pexels-ről (Kommandant 10-21: „ingyenes videobejátszás valódi képpel”), hálózat nélkül:
keresés → jelöltek (kredit: „Video: Név / Pexels”, a Pexels-hivatkozással), gyorsítótár (200 kérés / óra), a doboznak
megfelelő felbontás, letöltés a munkaterületre a licenccel és a kredittel, rétegben {"stock_video": …}."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

_REAL = httpx.AsyncClient


def _video(vid: int, name: str, dur: int = 12, w: int = 3840, h: int = 2160) -> dict:
    files = [{"quality": "sd", "file_type": "video/mp4", "width": int(w / 6), "height": int(h / 6), "link": f"https://videos.pexels.com/{vid}_sd.mp4"},
             {"quality": "hd", "file_type": "video/mp4", "width": int(w / 2), "height": int(h / 2), "link": f"https://videos.pexels.com/{vid}_hd.mp4"},
             {"quality": "uhd", "file_type": "video/mp4", "width": w, "height": h, "link": f"https://videos.pexels.com/{vid}_uhd.mp4"}]
    return {"id": vid, "width": w, "height": h, "duration": dur, "url": f"https://www.pexels.com/video/{name}-{vid}/",
            "image": f"https://images.pexels.com/videos/{vid}/x.jpeg", "user": {"name": "Ada Lovelace", "url": "https://www.pexels.com/@ada"},
            "video_files": files}


VIDS = [_video(111, "warship-at-sea"), _video(222, "short-clip", dur=2), _video(333, "harbour-at-night", w=1080, h=1920)]


@pytest.fixture()
def pexels(monkeypatch, tmp_path):
    import app.mcp_server  # noqa: F401 — előbb betöltve: a httpx.AsyncClient cseréje (lent) globális
    from app.tools import stock_video
    monkeypatch.setenv("PEXELS_API_KEY", "pk_test")
    stock_video._cache.clear()
    seen: list[httpx.Request] = []
    mp4 = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=navy:size=640x360:rate=25:duration=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mp4)], check=True)

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "api.pexels.com":
            assert req.headers["authorization"] == "pk_test"
            if req.url.path == "/videos/search":
                return httpx.Response(200, json={"videos": VIDS, "total_results": 3})
            vid = int(req.url.path.rsplit("/", 1)[1])
            v = next((x for x in VIDS if x["id"] == vid), None)
            return httpx.Response(200, json=v) if v else httpx.Response(404, json={})
        return httpx.Response(200, content=mp4.read_bytes())          # a videofájl
    monkeypatch.setattr(stock_video.httpx, "AsyncClient", lambda **kw: _REAL(transport=httpx.MockTransport(handler), **kw))
    return seen


def test_search_gives_credited_candidates_and_is_cached(pexels):
    from app.tools import stock_video
    c = asyncio.run(stock_video.search("warship at sea", 5, "landscape", min_seconds=3))
    assert [x["stock_video"] for x in c] == ["pexels:111", "pexels:333"]               # a 2 s-os kimarad
    assert c[0]["title"] == "Warship at sea" and c[0]["credit"] == "Video: Ada Lovelace / Pexels"
    assert c[0]["license"].startswith("Pexels License") and c[0]["thumb_url"].startswith("https://images.pexels.com/")
    q = pexels[0].url.params
    assert q["query"] == "warship at sea" and q["orientation"] == "landscape"
    asyncio.run(stock_video.search("warship at sea", 5, "landscape", min_seconds=3))
    assert len(pexels) == 1                                                            # gyorsítótárból (200 kérés / óra)
    assert "pexels.com" in stock_video.ATTRIBUTION


def test_no_key_says_so(monkeypatch):
    from app.tools import stock_video
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    stock_video._cache.clear()
    with pytest.raises(stock_video.StockVideoError, match="PEXELS_API_KEY"):
        asyncio.run(stock_video.search("sea"))


def test_the_file_fits_the_box_and_refs_are_understood():
    from app.tools import stock_video
    files = VIDS[0]["video_files"]
    assert stock_video._pick_file(files, 1080)["width"] == 1920          # a legkisebb, ami legalább 1080 magas
    assert stock_video._pick_file(files, 300)["width"] == 640
    assert stock_video._pick_file(files, 4000)["width"] == 3840          # nincs elég nagy: a legnagyobb
    for ref, want in (("pexels:111", "111"), ("111", "111"), ({"stock_video": "pexels:111"}, "111"),
                      ("https://www.pexels.com/video/warship-at-sea-111/", "111"),
                      ("https://www.pexels.com/hu-hu/video/hajo-111/", "111"), ("warship at sea", None)):
        assert stock_video._ref_id(ref) == want, ref


def test_fetch_puts_the_video_in_the_workspace_with_its_licence(pexels):
    from app.storage import read_meta
    from app.tools import crafts, stock_video
    fid, credit = asyncio.run(stock_video.fetch({"stock_video": "warship at sea"}, short=1080, orientation="landscape"))
    assert credit == "Video: Ada Lovelace / Pexels" and crafts.existing(fid).stat().st_size > 1000
    m = read_meta(fid)
    assert m["label"] == "Warship at sea (Pexels).mp4" and m["license"].startswith("Pexels License")
    assert m["source_url"] == "https://www.pexels.com/video/warship-at-sea-111/" and m["width"] == 1920
    assert any(r.url.host == "videos.pexels.com" and r.url.path == "/111_hd.mp4" for r in pexels)
    with pytest.raises(stock_video.StockVideoError, match="No such"):
        asyncio.run(stock_video.fetch("pexels:999"))


def test_a_layer_takes_a_stock_video_or_a_pexels_link():
    from app.tools import video
    assert video._img_spec({"stock_video": "pexels:111"}) == {"stock_video": "pexels:111"}
    assert video._img_spec("https://www.pexels.com/video/warship-at-sea-111/") == {"stock_video": "https://www.pexels.com/video/warship-at-sea-111/"}
    assert video._img_spec("https://example.com/a.jpg") == {"url": "https://example.com/a.jpg"}
    ly = video.layers_from([{"at": 1, "dur": 4, "type": "fullscreen", "image": {"stock_video": "warship at sea"}}], "", [])[0]
    assert ly["chart"]["elements"][0]["src"] == {"stock_video": "warship at sea"}


def test_mcp_stock_action_shows_numbered_candidates_with_the_pexels_link(pexels, monkeypatch):
    from app import mcp_server
    from app.tools import images

    async def sheet(c):
        return b"\xff\xd8jpg"
    monkeypatch.setattr(images, "candidate_sheet", sheet)
    res = asyncio.run(mcp_server._stock_videos("warship at sea"))
    info = res.structuredContent
    assert info["videos"][0]["stock_video"] == "pexels:111" and "pexels.com" in info["attribution"]
    assert '"stock_video": "pexels:<id>"' in info["how"] and "Pexels License" in info["how"]
    assert res.content[1].mimeType == "image/jpeg"


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) and ffmpeg needed")
def test_overlay_cut_in_with_a_stock_video_is_credited(pexels, tmp_path, put_file):
    from app.storage import read_meta
    from app.tools import video
    base = tmp_path / "base.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:size=640x360:rate=25:duration=5", "-f",
                    "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(base)], check=True)
    res = asyncio.run(video.overlay({"file_id": put_file(base), "layers": [
        {"at": 1, "dur": 3, "type": "fullscreen", "image": {"stock_video": "warship at sea"}, "sound": "off"}]}))
    assert res["check"]["status"] in ("ok", "needs_check"), res["check"]
    spec = read_meta(res["file_id"])["spec"]
    el = spec["story"]["layers"][0]["chart"]["elements"][0]
    assert el["src"].endswith(".mp4") and el["credit"] == "Video: Ada Lovelace / Pexels"      # letöltve, a kredit rajta
    assert "Ada Lovelace / Pexels" in json.dumps(res, ensure_ascii=False)
