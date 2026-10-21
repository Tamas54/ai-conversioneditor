"""Szavankénti feliratok (10-22, a vanta / MoneyPrinterTurbo ötletlistájából): a feliratsor tokenjeinek kezdőideje az
átiratból (a tapadó írásjel nem új token), a stílusok (box | karaoke | highlight | reveal | bold) darabolása és a
szóidők; a motorban a kimondott szó a kiemelőszínnel."""
from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest


def _w(word, a, b):
    return {"w": word, "start": a, "end": b}


def test_transcript_cues_carry_one_start_per_text_token():
    from app.tools import transcript
    tr = {"words": [_w("Jó", 0.0, 0.3), _w("reggelt", 0.35, 0.8), _w(",", 0.8, 0.82), _w("ilyen", 1.0, 1.3),
                    _w("-olyan", 1.3, 1.7), _w("kávé", 1.8, 2.2), _w(".", 2.2, 2.25)]}
    c = transcript.cues(tr)[0]
    assert c["text"] == "Jó reggelt, ilyen-olyan kávé."
    assert c["words"] == [0.0, 0.35, 1.0, 1.8] and len(c["words"]) == len(c["text"].split())


def test_subtitle_cues_keep_matching_word_times_only():
    from app.tools import video
    ok, bad = video.subtitle_cues([{"start": 1, "end": 4, "text": "egy kettő három", "words": [1.1, 2, 3.2]},
                                   {"start": 5, "end": 7, "text": "négy öt", "words": [5.1]}])
    assert ok["words"] == [1.1, 2.0, 3.2] and "words" not in bad


def test_caption_styles_split_and_time_the_words():
    from app.tools import video
    cues = [{"start": 0.0, "end": 6.0, "text": "Kiöntöm a tojást a serpenyőbe, aztán megfordítom.",
             "words": [0.0, 0.6, 0.8, 1.5, 1.7, 2.9, 3.6]}]
    box = video.caption_track(cues, 1080, 1920, 10)
    assert box and "words" not in box[0]
    hl = video.caption_track(cues, 1080, 1920, 10, "highlight")
    assert [len(r["words"]) for r in hl] == [len(r["text"].split()) for r in hl] and hl[0]["words"][0] == 0.0
    bold = video.caption_track(cues, 1080, 1920, 10, "bold")
    assert [r["text"] for r in bold] == ["Kiöntöm a tojást", "a serpenyőbe,", "aztán megfordítom."]
    assert [r["t0"] for r in bold] == [0.0, 1.5, 2.9] and bold[1]["t1"] == 2.9 and bold[-1]["t1"] == 6.0
    assert bold[0]["words"] == [0.0, 0.6, 0.8] and all(r["size"] > 0 and r["lines"] >= 1 for r in bold)
    est = video.caption_track([{"start": 0, "end": 4, "text": "one two three four"}], 1080, 1920, 10, "karaoke")[0]
    assert est["words"][0] == 0 and est["words"] == sorted(est["words"]) and est["words"][-1] < 4     # becsült idők


def test_caption_style_is_checked():
    from app.tools import video
    assert video.caption_style(None) == "box" and video.caption_style("Bold") == "bold"
    with pytest.raises(ValueError, match="caption_style: box"):
        video.caption_style("tiktok")


def _engine_ok() -> bool:
    from app.tools import video
    return video.available() and bool(shutil.which("ffmpeg"))


@pytest.mark.engines
@pytest.mark.skipif(not _engine_ok(), reason="EffectCraft CLI (EFFECTCRAFT_CLI) and ffmpeg needed")
def test_the_word_being_said_is_in_the_accent_colour(tmp_path, put_file):
    from PIL import Image
    from app.storage import output_path
    from app.tools import video
    clip = tmp_path / "base.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:size=640x360:rate=25:duration=4", "-f",
                    "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(clip)], check=True)
    fid = put_file(clip)

    def green_px(style: str, t: float) -> int:
        res = asyncio.run(video.overlay({"file_id": fid, "accent": "#00FF00", "caption_style": style, "subtitles": [
            {"start": 0.2, "end": 3.8, "text": "ONE TWO THREE FOUR", "words": [0.3, 1.2, 2.1, 3.0]}]}))
        png = tmp_path / f"{style}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(output_path(res["video"]["file_id"])),
                        "-frames:v", "1", str(png)], check=True)
        im = Image.open(png).convert("RGB")
        W, H = im.size
        return sum(1 for x in range(0, W, 2) for y in range(int(H * 0.6), H, 2)
                   if (lambda p: p[1] > 180 and p[0] < 90 and p[2] < 90)(im.getpixel((x, y))))
    assert green_px("highlight", 1.6) > 10                      # a „TWO” a kiemelőszínben (640×360: kis betű)
    assert green_px("bold", 1.6) > 10
    assert green_px("box", 1.6) == 0                            # az alap stílus változatlan
