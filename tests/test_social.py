"""Közösségi szövegek egy videóhoz (10-22, a MoneyPrinterTurbo ötletlistájából): adat (a forgatókönyv / átirat) →
determinisztikus (fejezetek, kulcsszavak, korlátok, forrássor) → AI (a szövegek, csak a videó tényeiből). Az LLM itt
utánzat."""
from __future__ import annotations

import asyncio
import json
import subprocess

import pytest


def test_chapters_follow_youtubes_rules():
    from app.tools import social
    ch, why = social.chapters([(0, "Cím"), (12, "Első"), (15, "Túl rövid"), (31, "Második"), (55, "Vége")], 60)
    assert [(c["at"], c["title"]) for c in ch] == [("00:00", "Cím"), ("00:12", "Első"), ("00:31", "Második")]  # a 10 s alatti kiesik
    assert social.chapters([(3, "A"), (20, "B"), (40, "C")], 70)[0][0] == {"at": "00:00", "start": 0.0, "title": "A"}   # 00:00-tól
    assert social.chapters([(15, "A"), (30, "B"), (45, "C")], 70)[0][0]["title"] == "Intro"
    none, why = social.chapters([(0, "A"), (8, "B")], 20)
    assert none == [] and "at least 3" in why


def test_keywords_hashtags_and_platforms():
    from app.tools import social
    text = ("A Kossuth téren ma tüntetés volt. A tüntetés békés maradt, a rendőrség figyelte a Kossuth teret. "
            "A szervezők szerint ezren voltak.")
    kws = social.keywords(text, "Tüntetés a Kossuth téren", "hu")
    assert kws[0].lower().startswith("tüntetés") and "Kossuth" in kws and "volt" not in kws
    assert social.hashtag("Kossuth tér") == "#KossuthTér" and social.hashtag("#már #van") == "#MárVan" and social.hashtag("!!") == ""
    assert social.platforms_of(None) == ["youtube", "tiktok", "instagram"]
    assert social.platforms_of("yt, ig, twitter") == ["youtube", "instagram", "x"] and len(social.platforms_of("all")) == 6
    with pytest.raises(ValueError, match="youtube | tiktok"):
        social.platforms_of(["myspace"])
    assert social._nums("ezren, 1 000 fő, 3,5%") == {"1000", "3.5"}


def _made(tmp_path, put_file):
    """Egy kész (make) videó projektje: forgatókönyv, idővonal, képforrások."""
    from app.storage import write_meta
    p = tmp_path / "v.ecproj"
    p.write_text("{}")
    pid = put_file(p)
    write_meta(pid, label="Méhek.ecproj", extra={"credit": "Photos: Pexels (Anna K.)", "spec": {
        "story": {"title": "Mit tehetsz a méhekért?", "scenes": [
            {"id": "s1", "headline": "Virágok", "caption": "Ültess őshonos virágokat. [pause: 1s] A méhek 70 fajt kedvelnek."},
            {"id": "s2", "headline": "Víz", "caption": "Tegyél ki egy tál vizet kövekkel."},
            {"id": "s3", "headline": "Vegyszer nélkül", "caption": "Ne permetezz virágzáskor."}],
            "end": {"title": "Kezdd ma!", "subtitle": ""}},
        "timeline": [{"id": "title", "start": 0, "end": 4}, {"id": "s1", "start": 3.5, "end": 20}, {"id": "s2", "start": 19.5, "end": 35},
                     {"id": "s3", "start": 34.5, "end": 50}, {"id": "end", "start": 49.5, "end": 54}]}})
    return pid


def test_posts_from_a_made_video(monkeypatch, tmp_path, put_file):
    from app.llm import siliconflow
    from app.storage import output_path
    from app.tools import social
    pid = _made(tmp_path, put_file)
    asked = []

    async def llm(model, messages, thinking=True, json_mode=True):
        asked.append(messages[1]["content"])
        return json.dumps({
            "youtube": {"title": "Így segíthetsz a méheknek — 3 egyszerű lépés " + "x" * 120, "text": "Három dolog, amit ma megtehetsz.",
                        "hashtags": ["#méhek", "#kert", "fyp", "#méhek", "őshonos virágok"]},
            "tiktok": {"text": "Ültess virágot, tegyél ki vizet! 500 méh hálás lesz.", "hashtags": ["#méhek"]},
            "x": {"text": "Méhbarát kert három lépésben: virág, víz, vegyszer nélkül. " * 8, "hashtags": ["#méhek", "#kert", "#x3"]}})
    monkeypatch.setattr(siliconflow, "_call_model", llm)
    res = asyncio.run(social.posts(pid, platforms="youtube,tiktok,x"))
    assert "[pause" not in asked[0] and "A méhek 70 fajt kedvelnek." in asked[0] and res["language"] == "hu"
    yt, tt, x = res["platforms"]["youtube"], res["platforms"]["tiktok"], res["platforms"]["x"]
    assert len(yt["title"]) <= 100 and yt["hashtags"] == ["#méhek", "#kert", "#fyp", "#ŐshonosVirágok"]
    assert "00:00 Virágok\n00:19 Víz\n00:34 Vegyszer nélkül" in yt["text"]          # a 4 s-os címkártya helyett az első jelenet
    assert "Photos: Pexels (Anna K.)" in yt["text"] and "Pexels" not in tt["text"]
    assert len(x["text"]) <= 280 and x["text"].endswith("#méhek #kert")
    assert any("tiktok" in f and "500" in f for f in res["check"]["findings"])        # a kitalált szám a check-be
    assert output_path(res["file_id"]).read_text("utf-8").startswith("# Mit tehetsz a méhekért?")


def test_without_the_ai_plain_drafts_and_a_note(monkeypatch, tmp_path, put_file):
    from app.llm import siliconflow
    from app.tools import social
    pid = _made(tmp_path, put_file)

    async def down(*a, **k):
        raise siliconflow.LLMServiceError(503, "m")
    monkeypatch.setattr(siliconflow, "_call_model", down)
    res = asyncio.run(social.posts(pid, platforms=["instagram"]))
    ig = res["platforms"]["instagram"]
    assert ig["text"].startswith("Virágok Ültess őshonos virágokat.") and ig["hashtags"]
    assert res["check"]["status"] == "needs_check" and "isn't available" in res["check"]["findings"][0]


def test_posts_from_a_recording_use_its_transcript(monkeypatch, tmp_path, put_file):
    from app.llm import siliconflow
    from app.tools import social, transcript
    src = tmp_path / "beszed.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=200:duration=2", str(src)], check=True)
    fid = put_file(src)

    async def fake(path, language=None, **kw):
        return {"language": "en", "duration": 2.0, "words": [], "sentences": [{"start": 0, "end": 2, "text": "Bees need water."}]}
    monkeypatch.setattr(transcript, "transcribe", fake)
    got = []

    async def llm(model, messages, thinking=True, json_mode=True):
        got.append(messages[1]["content"])
        return json.dumps({"youtube": {"title": "Bees need water", "text": "A short tip.", "hashtags": ["#bees"]}})
    monkeypatch.setattr(siliconflow, "_call_model", llm)
    res = asyncio.run(social.posts(fid, platforms="youtube", notes="Link: https://example.org/bees"))
    assert "Bees need water." in got[0] and "https://example.org/bees" in got[0] and res["chapters"] == []
    assert res["platforms"]["youtube"]["title"] == "Bees need water"


async def test_over_mcp_the_agent_writes_the_posts(monkeypatch, tmp_path, put_file):
    """Kommandant 10-22: „az agent irányít” — MCP-n a szerver az anyagot adja, a posztokat az ügynök írja; a szerver
    ellenőrzi (hossz, a videóban nem szereplő szám), hozzáteszi a fejezeteket és a forrássort. Studio AI nem fut."""
    from app.llm import siliconflow
    from app.mcp_server import video
    from app.storage import output_path
    called = []

    async def llm(*a, **k):
        called.append(1)
        return "{}"
    monkeypatch.setattr(siliconflow, "_call_model", llm)
    pid = _made(tmp_path, put_file)
    pack = await video(action="social", file_id=pid, ai={"platforms": "youtube,x"}, text="Link: https://example.org")
    assert set(pack["platforms"]) == {"youtube", "x"} and pack["platforms"]["x"]["text_max"] == 280
    assert "A méhek 70 fajt kedvelnek." in pack["video"]["says"] and "[pause" not in pack["video"]["says"]
    assert [c["at"] for c in pack["chapters"]] == ["00:00", "00:19", "00:34"] and "NOTHING WRITTEN YET" in pack["how"]
    assert any("https://example.org" in r for r in pack["rules"]) and not called
    res = await video(action="social", file_id=pid, ai={"posts": {
        "YouTube": {"title": "Méhbarát kert három lépésben", "text": "Virág, víz, vegyszer nélkül — 99 tipp.",
                    "hashtags": ["#méhek", "kert"]},
        "x": {"text": "Méhbarát kert: virág, víz, vegyszer nélkül.", "hashtags": ["#méhek"]}}})
    yt = res["platforms"]["youtube"]
    assert not called and yt["title"] == "Méhbarát kert három lépésben" and yt["hashtags"] == ["#méhek", "#kert"]
    assert "00:00 Virágok" in yt["text"] and "Photos: Pexels (Anna K.)" in yt["text"]
    assert any("99" in f for f in res["check"]["findings"]) and "yours, checked" in res["check"]["verified"][0]
    assert output_path(res["file_id"]).read_text("utf-8").startswith("# Mit tehetsz a méhekért?")
