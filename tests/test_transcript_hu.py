"""Magyar átirat (Omlett 3.5): prompt / vocabulary / model / force, és a javítás — adat (szókincs) → determinisztikus →
AI. A fixture a 10-19-i omlett-videó félrehallásai (helyes → amit a Whisper hallott): sonkával → „sokkával”, szép →
„kép”, fakanállal → „fokonállal”, azt ajánlom → „azt jön”, Voilà → „Ala”, Jöhet a tálalás → „Jó natállalás”.
A beszédfelismerő és az LLM itt utánzat (valódi felvétel nincs a tesztben)."""
from __future__ import annotations

import asyncio
import functools
import json
import os
import types

import pytest

VOCAB = ["sonkával", "szép", "fakanállal", "azt ajánlom", "Voilà", "Jöhet a tálalás"]
HEARD = ["sokkával", "kép", "fokonállal", "azt jön", "Ala", "Jó natállalás"]


def _tr() -> dict:
    """Az átirat, ahogy a gép hallotta (mondatok + szóidők) — plusz egy mondat, amit NEM szabad „javítani”
    (a „fakanál” más alak, a „Kanállal” más szó)."""
    said = [(0.0, "Ma omlettet készítünk sokkával."), (3.2, "Nézzétek, milyen kép a tojás!"),
            (6.2, "Most fokonállal keverem meg."), (9.2, "Ehhez azt jön, hogy kevés vajat használjatok."),
            (12.6, "Ala!"), (13.4, "Jó natállalás."), (15.2, "Kanállal nem megy, csak a fakanál jó.")]
    sentences, words = [], []
    for t0, text in said:
        t = t0
        for w in text.split():
            words.append({"w": w, "start": round(t, 2), "end": round(t + 0.3, 2)})
            t += 0.35
        sentences.append({"start": t0, "end": round(t - 0.05, 2), "text": text})
    return {"language": "hu", "model": "large-v3-turbo", "duration": 19.0, "sentences": sentences, "words": words}


def _text(tr: dict) -> str:
    return " ".join(s["text"] for s in tr["sentences"])


# ---------- 1. a paraméterek eljutnak a motorig ----------

class _Seg:
    def __init__(self, text):
        self.start, self.end, self.text = 0.0, 1.0, text
        self.words = [types.SimpleNamespace(word=" " + w, start=0.1 * i, end=0.1 * i + 0.1) for i, w in enumerate(text.split())]


class FakeWhisper:
    """faster-whisper helyett: feljegyzi, milyen modellt töltöttek be és milyen paraméterekkel hívták."""
    def __init__(self):
        self.loads, self.calls = [], []

    def load(self, name=None):
        self.loads.append(name)
        return self

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        return iter([_Seg(f"hallott szöveg {len(self.calls)}.")]), types.SimpleNamespace(language=kw.get("language") or "hu")


@pytest.fixture()
def engine(monkeypatch, tmp_path):
    from app.tools import transcript
    fake = FakeWhisper()
    pcm = os.urandom(32000)                               # 1 s „hang” — a lenyomata egyedi, más teszt nem találja el
    monkeypatch.setattr(transcript, "available", lambda: True)
    monkeypatch.setattr(transcript, "_load", fake.load)

    async def pcm16k(src):
        return pcm
    monkeypatch.setattr(transcript, "_pcm16k", pcm16k)
    src = tmp_path / "felvetel.m4a"
    src.write_bytes(b"x")
    return fake, src


async def test_prompt_vocabulary_and_model_reach_the_engine(engine):
    from app.tools import transcript
    fake, src = engine
    out = await transcript.transcribe(src, "hu", prompt="Főzőműsor: omlett sonkával.", vocabulary="sonkával, fakanállal;Voilà",
                                      model="large-v3", correct=False)
    kw = fake.calls[-1]
    assert kw["initial_prompt"] == "Főzőműsor: omlett sonkával." and kw["language"] == "hu"
    assert kw["hotwords"] == "sonkával, fakanállal, Voilà" and kw["word_timestamps"] is True
    assert fake.loads[-1] == "large-v3" and out["model"] == "large-v3"
    await transcript.transcribe(src, "hu", model="turbo", correct=False)          # alias; paraméterek nélkül nincs prompt
    assert fake.loads[-1] == "large-v3-turbo" and fake.calls[-1]["initial_prompt"] is None and fake.calls[-1]["hotwords"] is None
    with pytest.raises(ValueError, match="large-v3-turbo or large-v3"):
        await transcript.transcribe(src, "hu", model="whisper-9")
    assert transcript.vocab_list(["sonkával", " sonkával ", "", "Voilà"]) == ["sonkával", "Voilà"]


def test_cache_key_has_prompt_vocabulary_and_model_and_force_bypasses_it(engine, db_client):
    from app import db
    from app.tools import transcript
    fake, src = engine
    call = db_client.portal.call

    def tr(**kw):
        return call(functools.partial(transcript.transcribe, src, "hu", correct=False, **kw))
    first = tr(vocabulary=["sonkával"])
    assert len(fake.calls) == 1 and tr(vocabulary=["sonkával"]) == first and len(fake.calls) == 1   # gyorsítótárból
    tr(vocabulary=["sonkával", "szép"])
    assert len(fake.calls) == 2                                                   # más szókincs: más kulcs
    tr(vocabulary=["sonkával"], prompt="Főzőműsor")
    assert len(fake.calls) == 3                                                   # más prompt: más kulcs
    tr(vocabulary=["sonkával"], model="large-v3")
    assert len(fake.calls) == 4                                                   # más modell: más kulcs
    again = tr(vocabulary=["sonkával"], force=True)
    assert len(fake.calls) == 5 and again["sentences"] != first["sentences"]     # force: újrahallgatás…
    assert tr(vocabulary=["sonkával"]) == again and len(fake.calls) == 5         # …és az új eredmény a tárolt
    tr()
    rows = call(db._rows, "SELECT prompt_ver, model FROM ocr_cache WHERE sha256 = (SELECT sha256 FROM ocr_cache "
                          "WHERE prompt_ver = %s ORDER BY created_at DESC LIMIT 1)", (transcript.cache_ver("hu", None, ["sonkával"]),))
    vers = {(r["prompt_ver"], r["model"]) for r in rows}
    assert ("asr-v1:hu", "large-v3-turbo") in vers                                # paraméterek nélkül a régi kulcs marad
    assert len({v for v, _ in vers}) == 4 and ("asr-v1:hu:h" in transcript.cache_ver("hu", "p", None))


# ---------- 2. javítás: előbb a szókincs (AI nélkül), aztán a Studio AI a maradékra ----------

def test_vocabulary_fixes_close_mishearings_without_ai():
    from app.tools import transcript
    tr = _tr()
    fixes = transcript.fix_with_vocabulary(tr, VOCAB)
    text = _text(tr)
    assert [(c["heard"], c["fixed"]) for c in fixes] == [("sokkával.", "sonkával."), ("fokonállal", "fakanállal"),
                                                         ("Jó natállalás.", "Jöhet a tálalás.")]
    assert all(c["by"] == "vocabulary" for c in fixes)
    assert "készítünk sonkával." in text and "Most fakanállal keverem" in text and "Jöhet a tálalás." in text
    assert "Kanállal nem megy, csak a fakanál jó." in text                       # más alak, más szó: marad
    assert "kép a tojás" in text and "azt jön" in text and "Ala!" in text      # rövid / nagyon eltérő: az AI-é
    w = [x for x in tr["words"] if 13.3 < x["start"] < 14.2]
    assert [x["w"] for x in w] == ["Jöhet", "a", "tálalás."] and w[0]["start"] == 13.4 and w[-1]["end"] == 14.05
    cues = transcript.cues(tr)                                                   # a feliratsorok is a javított szavakból
    assert any("Jöhet a tálalás." in c["text"] for c in cues)


def test_vocabulary_leaves_what_was_heard_right():
    """Valódi large-v3-turbo mérés (10-19, Piper-hang): a helyesen hallott „azt ajánlom” utolsó szavát a szókincs-ablak
    „azt azt ajánlom”-ra „javította”. A már pontos előfordulás (és a darabja) érintetlen marad."""
    from app.tools import transcript
    words = [{"w": w, "start": i * 0.4, "end": i * 0.4 + 0.3} for i, w in enumerate(
        "Ehhez azt ajánlom, hogy kevés vajat használjatok. Ojla, Jöhet a tálalás.".split())]
    tr = {"sentences": [{"start": 0.0, "end": 4.0, "text": "Ehhez azt ajánlom, hogy kevés vajat használjatok. Ojla, Jöhet a tálalás."}],
          "words": words}
    assert transcript.fix_with_vocabulary(tr, VOCAB) == []
    assert tr["sentences"][0]["text"].startswith("Ehhez azt ajánlom, hogy")


@pytest.fixture()
def fake_billing(monkeypatch):
    """Egy free fiók (60 AI-lépés/hó) a db helyett, MCP-hívásként (ott számol az AI-lépés)."""
    from app import billing, db, tenancy
    state = {"usage": 0, "credits": 1, "cache": {}}

    async def usage_get(uid, month, kind):
        return state["usage"] if kind == "ai_steps" else 0

    async def usage_add(uid, month, kind, n, limit):
        state["usage"] += n if kind == "ai_steps" else 0
        return state["usage"]

    async def plan_of(uid):
        return "free"

    async def credits_spend(uid, amount, reason, ref=None):
        if state["credits"] < amount:
            raise db.InsufficientCredits("no")
        state["credits"] -= amount
        return state["credits"]

    async def check_get(kind, key, ver, max_age_s=None):
        return state["cache"].get((kind, key, ver))

    async def check_put(kind, key, ver, result):
        state["cache"][(kind, key, ver)] = json.loads(json.dumps(result))
    monkeypatch.setattr(db, "enabled", lambda: True)
    monkeypatch.setattr(db, "usage_get", usage_get)
    monkeypatch.setattr(db, "usage_add", usage_add)
    monkeypatch.setattr(db, "credits_spend", credits_spend)
    monkeypatch.setattr(db, "check_get", check_get)
    monkeypatch.setattr(db, "check_put", check_put)
    monkeypatch.setattr(billing, "plan_of", plan_of)
    tok, tok2 = tenancy.set_user("abc123def"), tenancy.client_makes_images.set(True)
    yield state
    tenancy.client_makes_images.reset(tok2)
    tenancy.reset(tok)


@pytest.fixture()
def fake_llm(monkeypatch):
    """A SiliconFlow HTTP-hívása helyett: a _call_model valódi (benne a billing.ai_step), csak a válasz kitalált."""
    from app.config import settings
    from app.llm import siliconflow
    sent: list[dict] = []
    reply = {"fixes": [{"n": 2, "heard": "kép", "fixed": "szép"}, {"n": 4, "heard": "azt jön", "fixed": "azt ajánlom"},
                       {"n": 5, "heard": "Ala!", "fixed": "Voilà!"},
                       {"n": 1, "heard": "nincs ilyen szó", "fixed": "valami"},                   # nincs a mondatban: kimarad
                       {"n": 7, "heard": "nem megy", "fixed": "szerintem egyáltalán nem igazán működik ez így"}]}  # átfogalmazás: kimarad

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(reply, ensure_ascii=False)}}]}

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.append(json)
            return Resp()
    monkeypatch.setattr(settings, "SILICONFLOW_API_KEY", "teszt")
    monkeypatch.setattr(siliconflow, "httpx", types.SimpleNamespace(AsyncClient=Client))
    return sent


async def test_ai_fixes_the_rest_as_a_studio_ai_step(fake_billing, fake_llm):
    from app import billing
    from app.tools import transcript
    raw = _tr()
    with billing.count_steps() as used:                       # ahogy az MCP-burkoló (linked) számol
        out = await transcript.fix(raw, VOCAB, prompt="Főzőműsor: omlett sonkával", use_ai=True)
    text = _text(out)
    for good in ("sonkával", "szép a tojás", "fakanállal", "azt ajánlom, hogy", "Voilà!", "Jöhet a tálalás."):
        assert good in text, good
    for bad in HEARD:
        assert f" {bad} " not in f" {text} ".replace(".", " ").replace("!", " ").replace(",", " "), bad
    assert "Kanállal nem megy, csak a fakanál jó." in text and "valami" not in text            # a rossz javaslatok kimaradtak
    assert _text(raw) == _text(_tr())                                                           # a nyers átirat érintetlen
    assert sorted(c["by"] for c in out["corrections"]) == ["ai"] * 3 + ["vocabulary"] * 3
    user = fake_llm[0]["messages"][1]["content"]                                                # a szókincs-javítás ELŐBB
    assert "sonkával." in user and "sokkával" not in user and "Voilà" in user
    assert len(fake_llm) == 1 and fake_billing["usage"] == 1                                    # egy AI-lépés, a havi keretből
    assert out["studio_ai"] == {"steps": 1, "credits": 0} and used[0] == 1
    again = await transcript.fix(_tr(), VOCAB, prompt="Főzőműsor: omlett sonkával", use_ai=True)
    assert len(fake_llm) == 1 and _text(again) == text and "studio_ai" not in again             # ugyanarra: nem fizet újra
    await transcript.fix(_tr(), VOCAB, prompt="Főzőműsor: omlett sonkával", use_ai=True, force=True)
    assert len(fake_llm) == 2                                                                   # force: újra kérdez


async def test_no_ai_steps_left_keeps_the_transcript_and_says_why(fake_billing, fake_llm):
    from app.tools import transcript
    fake_billing["usage"], fake_billing["credits"] = 80, 0
    out = await transcript.fix(_tr(), VOCAB, use_ai=True)
    assert "sonkával" in _text(out) and "Ala!" in _text(out) and not fake_llm                   # a szókincs-javítás megvan
    assert "AI proofreading didn't run" in out["correction_notes"][0] and "do that part yourself" in out["correction_notes"][0]


# ---------- 3. ugyanaz mindenhol: MCP, REST, web ----------

@pytest.fixture()
def heard(monkeypatch):
    from app.tools import transcript
    got: list[dict] = []

    async def fake(path, language=None, **kw):
        got.append({"language": language, **kw})
        tr = _tr()
        tr["corrections"] = transcript.fix_with_vocabulary(tr, transcript.vocab_list(kw.get("vocabulary")))
        return tr
    monkeypatch.setattr(transcript, "available", lambda: True)
    monkeypatch.setattr(transcript, "transcribe", fake)
    return got


def _poll(client, job):
    import time
    for _ in range(100):
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job still running")


def test_rest_passes_the_new_params(client, heard, put_file, tmp_path):
    src = tmp_path / "omlett.wav"
    src.write_bytes(b"RIFF")
    fid = put_file(src)
    r = client.post("/api/video/transcript", json={"file_id": fid, "language": "hu", "prompt": "Főzőműsor",
                                                   "vocabulary": "sonkával, fakanállal", "model": "large-v3", "force": True,
                                                   "correct": False})
    assert r.status_code == 200, r.text
    j = _poll(client, r.json())
    assert j["status"] == "done", j
    assert heard[-1] == {"language": "hu", "prompt": "Főzőműsor", "vocabulary": ["sonkával", "fakanállal"],
                         "model": "large-v3", "force": True, "correct": False}
    res = j["result"]
    assert [c["fixed"] for c in res["corrections"]] == ["sonkával.", "fakanállal"]
    assert any("2 misheard word(s) corrected — 2 from your vocabulary" in v for v in res["check"]["verified"])
    bad = client.post("/api/video/transcript", json={"file_id": fid, "model": "tiny"})
    assert bad.status_code == 400 and "large-v3" in bad.json()["detail"]


async def test_mcp_video_transcript_passes_the_new_params(heard, put_file, tmp_path):
    from app import jobs
    from app.mcp_server import video
    src = tmp_path / "omlett.wav"
    src.write_bytes(b"RIFF")
    fid = put_file(src)
    job = await video(action="transcript", file_id=fid, language="hu", vocabulary=["Voilà", "Jöhet a tálalás"],
                      prompt="Főzőműsor", model="large-v3-turbo", correct=True)
    for _ in range(100):
        j = await jobs.job_status(job["job_id"])
        if j["status"] != "running":
            break
        await asyncio.sleep(0.02)
    assert j["status"] == "done", j
    assert heard[-1] == {"language": "hu", "prompt": "Főzőműsor", "vocabulary": ["Voilà", "Jöhet a tálalás"],
                         "model": "large-v3-turbo", "force": False, "correct": True}
    assert j["result"]["corrections"][0]["fixed"] == "Jöhet a tálalás."


def test_mcp_schema_and_web_form_offer_the_params(client):
    from pathlib import Path
    hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    client.post("/mcp/", headers=hdr, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    tools = client.post("/mcp/", headers=hdr, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    props = next(t for t in tools if t["name"] == "video")["inputSchema"]["properties"]
    assert {"prompt", "vocabulary", "model", "force", "correct"} <= set(props)
    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "app.html").read_text("utf-8")
    for name in ("vsubvocab", "vsubprompt", "vsubmodel", "vsubfix", "vsubforce"):
        assert f'name="{name}"' in html, name
    assert 'JSON.stringify({ file_id: vid, language, ...hear })' in html
    assert all(k in html.split("const hear = {", 1)[1].split("};", 1)[0] for k in ("vocabulary", "prompt", "model", "correct", "force"))


def test_schema_reference_for_the_video_lane():
    from app.tools import transcript
    ref = transcript.SCHEMA_REF["transcript"]
    for k in ("prompt", "vocabulary", "model", "force", "correct", "corrections"):
        assert k in ref


# ---------- 4. (opcionális, lassú) valódi Whisper szintetikus beszéddel ----------

@pytest.mark.skipif(os.getenv("ASR_SLOW_TEST") != "1", reason="slow: downloads a Whisper model and a Piper voice (ASR_SLOW_TEST=1)")
async def test_real_whisper_accepts_prompt_and_hotwords(tmp_path, monkeypatch):
    """Piper-hanggal felolvasott magyar mondat → a valódi faster-whisper a prompttal és a hotwords-szel fut (a kis
    modell is elég annak igazolására, hogy a paramétereket a motor elfogadja). ASR_SLOW_MODEL=tiny alapból."""
    from app.tools import narration, transcript
    monkeypatch.setattr(transcript, "MODEL", os.getenv("ASR_SLOW_MODEL", "tiny"))
    wav = tmp_path / "omlett.wav"
    await asyncio.to_thread(narration.synth, "Ma omlettet készítünk sonkával. Jöhet a tálalás.", narration.pick("hu"), wav)
    out = await transcript.transcribe(wav, "hu", prompt="Főzőműsor", vocabulary=VOCAB, force=True)
    assert out["sentences"] and out["model"] == transcript.MODEL and "corrections" in out
