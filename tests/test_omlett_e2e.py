"""OMLETT regressziós végpont-teszt (a sávok együtt, valódi ffmpeg-gel, Postgresen): A két álló (rotation 90, GPS-címkés)
és egy fekvő szintetikus klipet fűz → 1080×1920, a fekvő elmosott kitöltéssel; megosztja B-vel (editor); B MCP-n
meghosszabbítja A felvételét (a saját kreditjéből fizet, A nem), az új változat A munkaterületén jön létre; C-t az MCP
elutasítja; a letöltött (exportált) fájlban nincs helyadat. A 2. (25 réteg + 15 felirat + ütköző szöveg → needs_check)
és a 3. pont (kódolási profil, SSIM) a sávok saját tesztjeiben: test_video_text_fit.py, test_media_omlett.py; a 6. pont
(navigator.share) a test_share_ui.py-ban (Chromium kell hozzá)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_share import world  # noqa: F401  (a három fiók bekapcsolt megosztással)

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg kell")

GPS = "+47.4979+019.0402/"


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def _clip(path: Path, w: int, h: int, rotate: int | None = None) -> bytes:
    """Szintetikus telefonos klip: testsrc2 + hang, a felvétel helyével (QuickTime location + Apple ISO6709)."""
    raw = path.with_name("raw-" + path.name)
    _ff("-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=10:duration=1", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000:duration=1", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(raw))
    _ff("-i", str(raw), "-c", "copy", "-movflags", "use_metadata_tags", "-metadata", f"location={GPS}",
        "-metadata", f"com.apple.quicktime.location.ISO6709={GPS}",
        *(["-metadata:s:v:0", f"rotate={rotate}"] if rotate else []), str(path))
    return path.read_bytes()


def _probe(p: Path) -> dict:
    return json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                                      str(p)], capture_output=True, text=True, check=True).stdout)


def _has_location(p: Path) -> bool:
    tags = {k.lower(): v for k, v in (_probe(p)["format"].get("tags") or {}).items()}
    return any("location" in k and v for k, v in tags.items())


def test_omlett_chain_merge_share_extend_pay_export(world, tmp_path):  # noqa: F811
    from app import credits, db
    w = world
    call = w.call
    # 1. A: két álló iPhone-szerű klip (tárolva 320×180, rotation 90) + egy fekvő → merge-files → álló vászon
    ids = [w.upload("a", n, _clip(tmp_path / n, 320, 180, rot)) for n, rot in (("a1.mov", 90), ("a2.mov", 90), ("f.mov", None))]
    assert _has_location(tmp_path / "a1.mov")
    err, res = w.mcp("a", "file_tools", {"tool": "merge-files", "file_ids": ids, "options": {"output_format": "mp4"}})
    assert not err, res
    merged = res["files"][0]["file_id"]
    from app.config import settings
    a_root = settings.DATA_DIR / "u" / w.u["a"]["id"]
    mp = a_root / "outputs" / merged
    v = next(s for s in _probe(mp)["streams"] if s["codec_type"] == "video")
    assert v["height"] > v["width"]                         # álló kimenet (régen 320×180-as fekvő, oldalsávos)

    # 4–5. A megosztja B-vel; B MCP-n meghosszabbítja (díjas szabály: 2 kredit) → B fizet, A nem; az új fájl A-nál
    call(lambda: credits.rule_save({"operation": "mcp:file_tools", "provider_cost": 2, "multiplier": 1, "unit": "job"}))
    try:
        r = w.req("a", "POST", "/api/share", json={"file_id": merged, "email": w.u["b"]["email"], "role": "editor"})
        assert r.status_code == 200, r.text
        a_before, b_before = (call(db.user_get, w.u[k]["id"])["credits"] for k in ("a", "b"))
        err, res = w.mcp("b", "file_tools", {"tool": "extend-video", "file_ids": [merged], "options": {"duration": 1}})
        assert not err, res
        assert res["studio_ai"]["credits_used"] == 2
        assert call(db.user_get, w.u["b"]["id"])["credits"] == b_before - 2
        assert call(db.user_get, w.u["a"]["id"])["credits"] == a_before
        ext, ext_url = res["files"][0]["file_id"], res["files"][0]["url"]
        assert (a_root / "outputs" / ext).exists()
        assert float(_probe(a_root / "outputs" / ext)["format"]["duration"]) == pytest.approx(
            float(_probe(mp)["format"]["duration"]) + 1, abs=0.3)

        # C: a megosztás nélkül az MCP elutasítja (nem fizet, nem fut)
        c_before = call(db.user_get, w.u["c"]["id"])["credits"]
        err, res = w.mcp("c", "file_tools", {"tool": "extend-video", "file_ids": [merged], "options": {"duration": 1}})
        assert err and "isn't shared with you" in res
        assert call(db.user_get, w.u["c"]["id"])["credits"] == c_before
    finally:
        call(lambda: db._rows("DELETE FROM pricing_rules WHERE operation = 'mcp:file_tools'"))

    # export: B letölti a megosztott eredményt — a kimenő példányban nincs helyadat
    from urllib.parse import urlsplit
    assert any(f["file_id"] == ext for f in w.req("b", "GET", "/api/files").json()["shared"])
    parts = urlsplit(ext_url)
    got = w.req("b", "GET", parts.path + ("?" + parts.query if parts.query else ""))
    assert got.status_code == 200 and len(got.content) > 1000
    out = tmp_path / "export.mp4"
    out.write_bytes(got.content)
    assert not _has_location(out)
