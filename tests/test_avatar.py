"""Beszélő avatar (Kling AI Avatar via kie.ai, 10-22): egy arcfotó + hang (felvétel vagy szövegből AI-hang) →
szájszinkronos videó. Csak beleegyezéssel, csak fizetett kreditből, a beszéd másodpercei szerint árazva; hálózat nélkül."""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import pytest

from test_ai_clip import ClipProv, _as, _job, _put_as, _user


class AvatarProv(ClipProv):
    def finish_all(self, ok=True):
        from app.providers.base import FAIL, SUCCESS
        for model, inp, tid in self.created:
            if self.tasks[tid].state not in (SUCCESS, FAIL):
                per_s = 16 if model.endswith("pro") else 8
                self.tasks[tid] = self._ts(task_id=tid, state=SUCCESS if ok else FAIL, cost=per_s * 6.0 if ok else 0.0,
                                           urls=[f"https://tempfile.example/{tid}.mp4"] if ok else [], error=None if ok else "x")


@pytest.fixture()
def prov(db_client, monkeypatch):
    from app import credits, providers
    from app.tools import ai_video
    p = AvatarProv()
    monkeypatch.setattr(providers, "get", lambda name="kie": p)
    monkeypatch.setattr(providers, "enabled_for", lambda uid=None: True)
    monkeypatch.setattr(ai_video, "POLL_FIRST_S", 0.05)
    db_client.portal.call(credits.seed_rules)
    return p


def _wav(path: Path, secs: float) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=44100:duration={secs}",
                    str(path)], check=True)
    return path


def _face(path: Path) -> Path:
    from PIL import Image
    Image.new("RGB", (600, 800), "tan").save(path)
    return path


@pytest.mark.parametrize("opts, msg", [
    ({"image": "a.png", "text": "Szia"}, "consent"),
    ({"image": "a.png", "text": "Szia", "consent": "yes"}, "consent"),
    ({"text": "Szia", "consent": True}, "photo of the face"),
    ({"image": "a.png", "consent": True}, "one of them"),
    ({"image": "a.png", "audio": "v.wav", "text": "x", "consent": True}, "one of them"),
    ({"image": "a.png", "text": "x", "quality": "4k", "consent": True}, "quality"),
])
def test_avatar_options_refuse_clearly(opts, msg):
    from app.tools import ai_video
    with pytest.raises(ai_video.AIVideoError, match=msg):
        ai_video.avatar_options(opts)


def test_avatar_from_a_recording_is_priced_by_its_seconds_and_charged_the_real_cost(db_client, prov, tmp_path):
    from app import db
    from app.providers.kie import map_input
    from app.tools import ai_video
    call = db_client.portal.call
    uid, _ = _user(call, 100)
    face = _put_as(uid, _face(tmp_path / "face.png"), "png")
    voice = _put_as(uid, _wav(tmp_path / "v.wav", 5.6), "wav")
    est = call(_as(uid, lambda: ai_video.estimate_avatar({"image": face, "audio": voice, "consent": True})))
    assert est["units"] == 6 and est["credits"] == 16 and est["seconds"] == 5.6      # 6 s × 8 kie × padló = 15,3 → 16
    job, j = _job(call, prov, uid, lambda: ai_video.start_avatar({"image": face, "audio": voice, "consent": True,
                                                                   "prompt": "A calm newsreader"}))
    assert j["status"] == "done", j.get("error")
    model, inp, _ = prov.created[0]
    native = map_input(model, inp)
    assert model == "kling/ai-avatar-standard" and set(native) == {"image_url", "audio_url", "prompt"}
    assert native["prompt"] == "A calm newsreader" and all(u.startswith("https://upload.example/") for u in
                                                           (native["image_url"], native["audio_url"]))
    r = j["result"]
    assert r["credits_charged"] == 16 and r["label"].startswith("AI avatar") and r["audio"] is True
    assert call(db.user_get, uid)["credits"] == 84


def test_avatar_from_text_uses_the_ai_voice_and_needs_paid_credits(db_client, prov, tmp_path, monkeypatch):
    from app import credits
    from app.tools import ai_video, narration
    call = db_client.portal.call
    said = []

    def synth(text, key, dst, speed=1.0):
        said.append((text, key))
        _wav(Path(dst), 3.2)
        with wave.open(str(dst)) as w:
            return w.getnframes() / w.getframerate()
    monkeypatch.setattr(narration, "synth", synth)
    monkeypatch.setattr(narration, "pick", lambda lang, want=None: f"{lang}-{want or 'default'}")
    uid, _ = _user(call, 100)
    face = _put_as(uid, _face(tmp_path / "face.png"), "png")
    opts = {"image": face, "text": "Jó reggelt, itt a Tamás TV.", "language": "hu", "voice": "male", "quality": "1080p",
            "consent": True}
    est = call(_as(uid, lambda: ai_video.estimate_avatar(opts)))
    assert est["units"] == 4 and est["model"] == "kling/ai-avatar-pro" and said[-1] == ("Jó reggelt, itt a Tamás TV.", "hu-male")
    free, _ = _user(call, 100, paid=False)
    face2 = _put_as(free, _face(tmp_path / "face2.png"), "png")
    with pytest.raises(credits.PaidCreditsOnly):
        call(_as(free, lambda: ai_video.start_avatar({**opts, "image": face2})))
    assert prov.created == []


def test_ai_label_is_off_unless_switched_on(tmp_path, monkeypatch):
    """Kommandant 10-22: az AI-jelölés a felhasználó dolga — alapból nem írunk XMP-t; AI_LABEL=1 visszakapcsolja."""
    from PIL import Image
    from app.tools import ai_label
    p = tmp_path / "x.jpg"
    Image.new("RGB", (40, 40), "red").save(p)
    before = p.read_bytes()
    monkeypatch.delenv("AI_LABEL", raising=False)
    assert ai_label.label(p, ai_label.GENERATED, "x") is False and p.read_bytes() == before
    monkeypatch.setenv("AI_LABEL", "1")
    assert ai_label.label(p, ai_label.GENERATED, "x") is True and b"trainedAlgorithmicMedia" in p.read_bytes()
