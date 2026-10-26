"""Live preview, captions (lane L3): app/tools/live_captions.py, app/routes/live_captions.py, app/static/live-captions.js.

The ASS must be the segment preview's own (preview_cache._build → pc-subs.ass): the fixture is the 30-minute S4 show's
caption spec with the sha256 of the pc-subs.ass the segment preview wrote for it (10-26) — byte for byte the same. The
browser side (JASSUB, vendored) is measured against ffmpeg's ass filter in .claude/qa/lp-captions/ (fid.py, cap.mjs);
here a Chromium test checks that it loads without COOP/COEP and draws the highlighted word on the right frame.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
S4 = json.loads((Path(__file__).parent / "fixtures" / "live_captions_s4.json").read_text("utf-8"))
VENDOR = ROOT / "app" / "static" / "vendor" / "jassub"
SUBS = [{"start": 0.5, "end": 4.0, "text": "Az első mondat itt van, rövid."},
        {"start": 4.2, "end": 9.0, "text": "A második mondat kicsit hosszabb, hogy két sorba törjön a feliratsávban."}]


def spec(style="highlight", accent="#2a9df4", subs=SUBS, **base):
    b = {"width": 1920, "height": 1080, "fps": 29.993, "duration": 12.0, "proxy": {"w": 960, "h": 540}, **base}
    return {"kind": "overlay", "base": b, "opts": {"accent": accent, "caption_style": style, "subtitles": subs}}


def segment_preview_ass(sp):
    """What preview_cache._build writes into pc-subs.ass for this spec (its own inputs: plan_ctx, opts, video._rgb)."""
    from app.tools import fast_render, preview_cache, video
    c0 = preview_cache.plan_ctx(sp, {})
    opts = sp.get("opts") or {}
    subs = opts.get("subtitles") or (sp.get("story") or {}).get("subtitles") or []
    style = opts.get("caption_style") or "box"
    return fast_render.ass_subtitles(subs, style, c0["W"], c0["H"], c0["total"], video._rgb(opts.get("accent")), fps=c0["fps"])


@pytest.fixture(autouse=True)
def _fresh():
    from app.tools import live_captions as lc
    lc._ASS.clear()
    lc._MEMO.clear()
    yield


def _project(sp, **extra):
    from app.storage import new_file_id, output_path, write_meta
    pid = new_file_id("ecproj")
    output_path(pid).write_text("{}", "utf-8")
    write_meta(pid, label="Show.ecproj", extra={"spec": sp, **extra})
    return pid


# ---------- the ASS: the segment preview's own ----------

def test_s4_ass_is_byte_identical_to_the_segment_previews_pc_subs():
    from app.tools import live_captions as lc
    h, inp = lc.ass_for(S4["spec"])
    text, gz = lc.cached(h)
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == S4["pc_subs_sha256"]
    assert h == S4["pc_subs_sha256"][:24] and gzip.decompress(gz).decode("utf-8") == text
    assert (inp["W"], inp["H"], inp["fps"], inp["style"]) == (1920, 1080, 29.993, "highlight")


@pytest.mark.parametrize("sp", [
    spec(),
    spec("box"), spec("karaoke"), spec("reveal"), spec("bold", accent="#ff6a00"),
    spec(fps=25), spec(fps=0), spec(fps=240), spec(proxy=None), spec(width=1080, height=1920, proxy={"w": 540, "h": 960}),
    {**spec(), "opts": {"accent": "#2a9df4", "caption_style": "highlight"}, "story": {"subtitles": SUBS}},   # story's subs
])
def test_ass_equals_the_segment_preview_for_every_style_and_geometry(sp):
    from app.tools import live_captions as lc, preview_cache
    got = lc.ass_for(sp)
    assert got is not None
    assert lc.cached(got[0])[0] == segment_preview_ass(sp)
    c0 = preview_cache.plan_ctx(sp, {})
    g = lc.geometry(sp)
    assert (g["W"], g["H"], g["fps"], g["total"], g["pw"], g["ph"]) == (c0["W"], c0["H"], c0["fps"], c0["total"], c0["pw"], c0["ph"])


def test_frame_snapped_times():
    """The events start on the frame the segment preview shows them (fast_render._ass_time with fps)."""
    from app.tools import live_captions as lc
    text = lc.cached(lc.ass_for(spec())[0])[0]
    starts = re.findall(r"^Dialogue: \d+,(\d+):(\d\d):(\d\d)\.(\d\d),", text, re.M)
    fps = 29.993
    for h, m, s, c in starts:
        ms = ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(c) * 10
        k = next(k for k in range(int(ms / 1000 * fps) - 2, int(ms / 1000 * fps) + 3) if int(k * (1000 / 29993) * 1000) >= ms)
        assert k * 1000 / fps - ms < 33.4          # the first frame at/after the start is less than a frame later


def test_no_captions_no_part():
    from app.tools import live_captions as lc
    assert lc.captions_part("x.ecproj", spec(subs=[])) is None
    assert lc.captions_part("x.ecproj", {"kind": "story", "opts": {"subtitles": SUBS}}) is None


# ---------- the part ----------

def test_part_shape_and_fonts():
    from app.tools import live_captions as lc
    pid = _project(spec())
    p = lc.captions_part(pid)
    assert p["v"] == 1 and p["W"] == 1920 and p["H"] == 1080 and p["frame"] == [960, 540] and p["fps"] == 29.993
    assert p["style"] == "highlight" and p["hand_edited"] is False and p["events"] > 10
    assert re.fullmatch(rf"http://testserver/api/video/live/captions/{p['hash']}\.ass\?p={re.escape(pid)}&t=\d+\.[0-9a-f]{{32}}",
                        p["ass_url"]), p["ass_url"]
    assert [(f["name"], f["file"]) for f in p["fonts"]] == [("Inter", "Inter-Regular.otf"), ("Inter Semi Bold", "Inter-SemiBold.otf")]
    for f in p["fonts"]:
        assert re.fullmatch(r"http://testserver/api/video/live/font/Inter-[A-Za-z]+-[0-9a-f]{12}\.otf\?t=\d+\.[0-9a-f]{32}", f["url"])
    bold = lc.captions_part(pid, spec("bold"))
    assert ("Inter Bold", "Inter-Bold.otf") in [(f["name"], f["file"]) for f in bold["fonts"]]
    assert bold["hash"] != p["hash"]


def test_ass_fonts_parses_styles_and_overrides():
    from app.tools import live_captions as lc
    ass = ("Style: Cap,Inter Semi Bold,50,&H00FFFFFF,&H00FFFFFF,&H0,&H0,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n"
           "Style: Bar,Inter,20,&H470F0B0A,&H00FFFFFF,&H0,&H0,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n"
           "Style: Unused,Inter Black,20,&H0,&H0,&H0,&H0,-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n"
           "Dialogue: 1,0:00:00.00,0:00:01.00,Cap,,0,0,0,,{\\an5\\fnInter\\b1\\fs90}SZIA{\\1c&HFFFFFF&} VILÁG\n"
           "Dialogue: 0,0:00:00.00,0:00:01.00,Bar,,0,0,0,,{\\p1}m 0 0 l 1 1{\\p0}\n"
           "Dialogue: 1,0:00:00.00,0:00:01.00,Cap,,0,0,0,,{\\fnArial}x\n")
    assert lc.ass_fonts(ass) == [("Inter Semi Bold", "Inter-SemiBold.otf"), ("Inter Bold", "Inter-Bold.otf"),
                                 ("Inter", "Inter-Regular.otf")]


def test_url_is_stable_within_the_bucket_and_follows_the_content():
    from app.tools import live_captions as lc
    pid = _project(spec())
    now = (int(time.time()) // lc.BUCKET_S) * lc.BUCKET_S + 10
    a = lc.captions_part(pid, now=now)
    b = lc.captions_part(pid, now=now + 3600)
    c = lc.captions_part(pid, spec(accent="#ff6a00"), now=now)
    d = lc.captions_part(pid, spec(subs=[{**SUBS[0], "text": "Más szöveg."}, SUBS[1]]), now=now)
    assert a == b                                            # the same captions: the same URLs (no reload, cache hit)
    assert len({a["hash"], c["hash"], d["hash"]}) == 3        # accent / text: a new ASS
    assert c["fonts"] == a["fonts"]                           # the same fonts: the same URLs across versions


def test_memo_skips_the_rebuild(monkeypatch):
    from app.tools import live_captions as lc
    sp = spec()
    lc.ass_for(sp)
    calls = []
    monkeypatch.setattr(lc, "build", lambda inp: calls.append(1) or "x")
    lc.captions_part("p.ecproj", sp)
    assert calls == []


# ---------- the routes ----------

def test_ass_route_plain_gzip_and_rebuild_after_restart(client):
    from app.tools import live_captions as lc
    sp = spec()
    pid = _project(sp)
    p = lc.captions_part(pid)
    url = p["ass_url"].replace("http://testserver", "")
    r = client.get(url, headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/x-ssa")
    assert r.text == segment_preview_ass(sp)
    assert "immutable" in r.headers["cache-control"] and "content-encoding" not in r.headers
    rz = client.get(url, headers={"Accept-Encoding": "gzip"})
    assert rz.status_code == 200 and rz.headers.get("content-encoding") == "gzip" and rz.text == r.text
    lc._ASS.clear()
    lc._MEMO.clear()                                          # a restart: rebuilt from the project's spec
    assert client.get(url).text == r.text
    from app.storage import write_meta
    write_meta(pid, extra={"spec": spec(accent="#ff6a00")})   # the project changed since: that URL is gone
    lc._ASS.clear()
    lc._MEMO.clear()
    assert client.get(url).status_code == 404


def test_ass_route_rejects_bad_tokens(client):
    from app.tools import live_captions as lc
    pid = _project(spec())
    p = lc.captions_part(pid)
    url = p["ass_url"].replace("http://testserver", "")
    assert client.get(re.sub(r"t=\d+\.\w+", "t=1.abc", url)).status_code == 403
    other = re.sub(r"/captions/\w+\.ass", "/captions/" + "0" * 24 + ".ass", url)
    assert client.get(other).status_code == 403                    # the token is for that hash only
    font = p["fonts"][0]["url"].replace("http://testserver", "")
    tok = font.split("?t=")[1]
    assert client.get(f"/api/video/live/captions/{p['hash']}.ass?p={pid}&t={tok}").status_code == 403   # a font's token


def test_font_route(client):
    from app.tools import live_captions as lc
    p = lc.captions_part(_project(spec()))
    f = next(x for x in p["fonts"] if x["file"] == "Inter-SemiBold.otf")
    r = client.get(f["url"].replace("http://testserver", ""))
    assert r.status_code == 200 and r.headers["content-type"] == "font/otf" and "immutable" in r.headers["cache-control"]
    assert r.content == (lc.font_dir() / "Inter-SemiBold.otf").read_bytes()
    key = f["url"].split("/font/")[1].split("?")[0]
    assert client.get(f"/api/video/live/font/{key}?t=1.abc").status_code == 403
    from app import tenancy
    from app.tools import preview_serve as ps
    exp = int(time.time()) + 3600
    for bad in ("Inter-SemiBold-000000000000.otf", "Inter-Nope-0123456789ab.otf", "Arial-0123456789ab.otf"):
        t = f"{exp}.{ps._sig(lc.FONT_PID, lc.font_token_h(bad), exp, tenancy.ADMIN_ID)}"
        assert client.get(f"/api/video/live/font/{bad}?t={t}").status_code == 404, bad


# ---------- the vendored JASSUB ----------

def test_vendored_jassub_is_self_contained_and_licensed(client):
    js, worker = (VENDOR / "jassub.js").read_text("utf-8"), (VENDOR / "jassub-worker.js").read_text("utf-8")
    assert "JASSUB 2.5.20" in js and "JASSUB 2.5.20" in worker
    for src in (js, worker):                     # no bare imports (a module worker has no import map)
        assert not re.search(r"""^\s*import\b[^'"]*['"](?![./])""", src, re.M)
        assert not re.search(r"""\bfrom\s*['"](?![./])""", src)
    assert "MIT License" in (VENDOR / "LICENSE").read_text("utf-8")
    assert "FriBidi (LGPL-2.1-or-later" in worker and "FreeType" in worker
    r = client.get("/static/vendor/jassub/jassub-worker.wasm")
    assert r.status_code == 200 and r.headers["content-type"] == "application/wasm" and r.content[:4] == b"\0asm"
    r = client.get("/static/live-captions.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "export class LiveCaptions" in r.text


# ---------- in Chromium: no COOP/COEP, the right word on the right frame ----------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_app():
    uvicorn = pytest.importorskip("uvicorn")
    from app.main import app
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(10)


@pytest.mark.skipif(not os.getenv("CHROMIUM_EXECUTABLE"), reason="Chromium needed")
def test_browser_draws_the_highlighted_word_on_its_frame(live_app):
    from app.tools import live_captions as lc
    pid = _project(spec())
    part = lc.captions_part(pid, base=live_app)
    text = lc.cached(part["hash"])[0]
    # two consecutive word states of the second sentence: the frame where the 2nd word lights up, and the one before
    ev = [ln.split(",", 9) for ln in text.splitlines() if ln.startswith("Dialogue: 1,")]
    cs = lambda s: (int(s[0]) * 3600 + int(s[2:4]) * 60 + int(s[5:7])) * 1000 + int(s[8:10]) * 10   # noqa: E731
    j = next(j for j in range(1, len(ev)) if ev[j - 1][2] == ev[j][1] and "\\1c" in ev[j - 1][9] and "\\1c" in ev[j][9]
             and ev[j - 1][9].split("}")[0] == ev[j][9].split("}")[0])          # the next word of the same row
    on_ms = cs(ev[j][1])
    fps = part["fps"]
    k = next(k for k in range(int(on_ms / 1000 * fps) - 2, int(on_ms / 1000 * fps) + 3) if int(k * (1000 / 29993) * 1000) >= on_ms)
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        br = p.chromium.launch(executable_path=os.environ["CHROMIUM_EXECUTABLE"])
        page = br.new_page(viewport={"width": 960, "height": 540})
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(live_app + "/health")
        page.set_content('<div id="box" style="position:relative;width:960px;height:540px;background:#000"></div>')
        page.add_script_tag(type="module", content=(
            "import { LiveCaptions } from '/static/live-captions.js';"
            "window.CAP = new LiveCaptions(document.getElementById('box'), { width: 960, height: 540 });"
            f"window.CAP.load({json.dumps(part)}).then(() => {{ window.READY = true }})"))
        page.wait_for_function("window.READY === true", timeout=60000)
        res = page.evaluate("""async ([k, fps]) => {
            const r = window.CAP._j.renderer
            const acc = async t => {                       // the worker's own frame (readback), as the canvas shows it
              await window.CAP.setTime(t)
              const c = await r._drawCapture(Math.floor(t * fps + 1e-3) / fps)
              let n = 0, x0 = 1e9, x1 = -1
              for (let i = 0; i < c.data.length; i += 4) {
                const R = c.data[i], B = c.data[i + 2], A = c.data[i + 3]
                if (A > 200 && B > 140 && B - R > 70) { n++; const x = (i / 4) % c.width; x0 = Math.min(x0, x); x1 = Math.max(x1, x) }
              }
              return { n, x0, x1 }
            }
            return { coi: self.crossOriginIsolated, before: await acc((k - 1) / fps), on: await acc(k / fps),
                     mid: await acc(k / fps + 0.6 / fps), stats: window.CAP.stats }
        }""", [k, fps])
        br.close()
    assert not errs, errs
    assert res["coi"] is False                                # no COOP/COEP: the single-threaded mode
    assert res["before"]["n"] > 50 and res["on"]["n"] > 50    # a highlighted word on both frames …
    assert res["on"]["x0"] > res["before"]["x1"]              # … the next one (to the right) exactly from frame k
    assert res["mid"] == res["on"]                            # a clock inside the frame draws that frame
    assert res["stats"]["errors"] == 0
