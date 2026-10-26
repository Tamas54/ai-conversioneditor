"""A videóprojekt előnézeti lapja (/preview/<projekt>.ecproj, app/routes/preview.py).

Szakasz-gyorsítótáras előnézettel (meta["preview"], VIDEO_PREVIEW_HLS ≠ 0): HLS-lejátszó (aláírt lista, hls.js a
cdnjs-ről, a lejátszó app/static/preview-player.js), a változások jelölői a lapba ágyazott beállításban. Nélküle (vagy
kikapcsolva): a mai lap — a legutóbbi render lejátszója, vagy a címjelenet egy kockája. A böngészős (Chrome) próba:
.claude/qa/pc-ui/ (a gépen, a prototípus listáival)."""
from __future__ import annotations

import json
import re
import sys
import types

import pytest

MARKERS = [{"from": 300.0, "to": 306.0, "what": "cut"}, {"from": 718.0, "to": 724.0, "what": "L7 chart"}]


@pytest.fixture()
def hls(monkeypatch):
    """A gyorsítótár (P1) és a kiszolgálás (P2) moduljai: ha már megvannak, az igaziak (a kapcsoló a tesztben áll),
    különben a szerződés szerinti helyettesek — enabled() és signed(pid) → „?t=<exp>.<sig>”."""
    import app.tools as tools
    state = {"on": True}
    try:
        from app.tools import preview_cache
        monkeypatch.setattr(preview_cache, "enabled", lambda: state["on"])
    except ImportError:
        m = types.ModuleType("app.tools.preview_cache")
        m.enabled = lambda: state["on"]
        monkeypatch.setitem(sys.modules, "app.tools.preview_cache", m)
        monkeypatch.setattr(tools, "preview_cache", m, raising=False)
    try:
        from app.tools import preview_serve  # noqa: F401  (az igazi aláírás)
    except ImportError:
        m = types.ModuleType("app.tools.preview_serve")
        m.signed = lambda pid, h=None: "?t=4102444800.0123456789abcdef0123456789abcdef"
        monkeypatch.setitem(sys.modules, "app.tools.preview_serve", m)
        monkeypatch.setattr(tools, "preview_serve", m, raising=False)
    return state


def _project(tmp_path, put_file, *, preview=True, render=None, draft=False, markers=MARKERS):
    from app.storage import output_path, new_file_id, write_meta
    (tmp_path / "Interview.ecproj").write_text("{}")
    pid = put_file(tmp_path / "Interview.ecproj")
    extra = {"video": {"format": "custom", "width": 1920, "height": 1080, "fps": 30, "duration": 1821.4}}
    if preview:
        extra["preview"] = {"markers": markers, "first_ready_s": 1.8, "dirty": [150, 151, 359]}
    if render:
        rid = new_file_id("mp4")
        output_path(rid).write_bytes(b"\x00\x00\x00\x18ftypmp42")
        write_meta(rid, label="Interview.mp4")
        extra.update(render_file_id=rid, render_draft=draft)
    write_meta(pid, label="Interview.ecproj", extra=extra)
    return pid, extra.get("render_file_id")


def _cfg(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="pv-cfg">(.*?)</script>', html, re.S)
    assert m, "a lejátszó beállítása hiányzik"
    return json.loads(m.group(1))


def test_hls_page_plays_the_signed_playlist_with_markers(client, tmp_path, put_file, hls):
    from app.routes.preview import HLS_JS, HLS_SRI
    pid, _ = _project(tmp_path, put_file)
    r = client.get(f"/preview/{pid}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    html = r.text
    cfg = _cfg(html)
    # aláírt lista és állapot (a lap saját eredetéről), hls.js a cdnjs-ről SRI-vel, a lejátszó a /static/-ból
    assert re.fullmatch(rf"/api/video/{re.escape(pid)}/preview\.m3u8\?t=\d+\.\w+.*", cfg["playlist"]), cfg["playlist"]
    assert re.fullmatch(rf"/api/video/{re.escape(pid)}/preview\.json\?t=\d+\.\w+.*", cfg["json"]), cfg["json"]
    assert f'data-hls-js="{HLS_JS}"' in html and f'data-hls-sri="{HLS_SRI}"' in html
    assert HLS_JS == "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.13/hls.min.js"
    assert re.search(r'<script src="/static/preview-player\.js\?v=\d+" defer>', html)
    # a jelölők, a hossz, a kép aránya, a borítókép; a végleges render még nincs kész → a preview.json mondja meg
    assert cfg["markers"] == MARKERS and cfg["duration"] == 1821.4 and cfg["final"] is None
    assert "aspect-ratio: 1920 / 1080" in html and f'poster="/api/thumb/{pid}"' in html
    assert 'playsinline' in html and 'id="pv-marks"' in html and 'role="group"' in html
    assert "<title>Interview — preview</title>" in html


def test_final_render_already_done_comes_from_the_project(client, tmp_path, put_file, hls):
    pid, rid = _project(tmp_path, put_file, render=True)
    cfg = _cfg(client.get(f"/preview/{pid}").text)
    assert cfg["final"]["status"] == "done" and cfg["final"]["video_url"].startswith(f"/files/{rid}?t=")
    # a gyors vázlat (draft=true) nem a végleges videó
    pid2, _ = _project(tmp_path, put_file, render=True, draft=True)
    assert _cfg(client.get(f"/preview/{pid2}").text)["final"] is None


def test_marker_text_cannot_close_the_script(client, tmp_path, put_file, hls):
    evil = "</script><script>alert(1)</script>"
    pid, _ = _project(tmp_path, put_file, markers=[{"from": 12, "to": 14, "what": evil}, {"what": "no time"}, "junk"])
    html = client.get(f"/preview/{pid}").text
    assert "<script>alert(1)" not in html
    assert _cfg(html)["markers"] == [{"from": 12.0, "to": 14.0, "what": evil}]     # a hibás jelölők kimaradnak


def _today_video(html: str, rid: str, pid: str) -> None:
    assert re.search(rf'<video src="/files/{re.escape(rid)}\?t=[^"]+" poster="/api/thumb/{re.escape(pid)}" controls playsinline', html)
    assert "preview.m3u8" not in html and "hls" not in html.lower() and "pv-cfg" not in html


def test_without_hls_preview_the_page_is_todays(client, tmp_path, put_file, hls):
    pid, rid = _project(tmp_path, put_file, preview=False, render=True)
    _today_video(client.get(f"/preview/{pid}").text, rid, pid)
    pid, _ = _project(tmp_path, put_file, preview=False)                       # még nincs render: egy kocka
    html = client.get(f"/preview/{pid}").text
    assert f'<img src="/api/thumb/{pid}"' in html and "preview.m3u8" not in html


def test_switched_off_the_page_is_todays(client, tmp_path, put_file, hls):
    hls["on"] = False                                                            # VIDEO_PREVIEW_HLS=0
    pid, rid = _project(tmp_path, put_file, render=True)
    _today_video(client.get(f"/preview/{pid}").text, rid, pid)


def test_plain_video_file_unchanged(client, tmp_path, put_file, hls):
    (tmp_path / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    vid = put_file(tmp_path / "clip.mp4")
    html = client.get(f"/preview/{vid}").text
    assert f'<video src="/files/{vid}?t=' in html and "preview.m3u8" not in html


def test_player_script_is_served(client):
    r = client.get("/static/preview-player.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    js = r.text
    # Safari/iOS natívan, máshol hls.js; jelölő-kattintás → tekerés + lejátszás; 2 s-os figyelés; átváltás a végleges MP4-re
    for needle in ('canPlayType("application/vnd.apple.mpegurl")', "new Hls(", "attachMedia(v)", "segments ready",
                   "Final video: rendering", "ready around", "video_url", "jump(m.from, true)", "filling ? 2000 : 10000"):
        assert needle in js, needle
