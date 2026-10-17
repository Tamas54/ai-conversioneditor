"""Feliratozó (10-18, Kommandant: „webes feliratozó, szövegjavítással és a saját stílusunkkal”): a javított átirat
feliratként a saját felvételen, a make-videók feliratsávjával (ugyanaz a builder-kód), .srt is; a gyors ráégetés
(file_tools add-subtitles) is a Studio stílusában."""
from __future__ import annotations

import pytest


def test_cues_are_cleaned_ordered_and_never_overlap():
    from app.tools import video
    cues = video.subtitle_cues([{"at": "00:05", "until": "00:08", "text": "  második   sor "},
                                {"start": 1, "end": 6, "text": "első"},
                                {"start": 9, "text": "idő nélkül a vége a hosszából"},
                                {"start": 10, "end": 11, "text": "   "}])
    assert [c["text"] for c in cues] == ["első", "második sor", "idő nélkül a vége a hosszából"]
    assert cues[0]["end"] == 5.0                                   # egyszerre egy felirat: az előző véget ér
    assert cues[2]["end"] > cues[2]["start"]
    with pytest.raises(ValueError):
        video.subtitle_cues([{"start": "később", "text": "x"}])
    with pytest.raises(ValueError):
        video.subtitle_cues("csak szöveg")


def test_long_lines_become_two_line_parts_within_their_time():
    from app.tools import video
    long = ("Ez egy hosszabb mondat, hogy lássuk, hogyan törik két sorra a felirat a feliratsávban, ha nem fér ki egy "
            "sorban, és ha még két sorba sem fér, akkor egymás után több részben jelenik meg, a mondat ideje alatt.")
    track = video.caption_track([{"start": 2.0, "end": 8.0, "text": long}], 1920, 1080, 30.0)
    assert len(track) >= 2 and track[0]["t0"] == 2.0 and abs(track[-1]["t1"] - 8.0) < 0.01
    assert all(c["lines"] in (1, 2) for c in track) and " ".join(c["text"] for c in track) == long


def test_quick_burn_uses_the_studio_caption_style():
    from app.tools import video
    st = video.subtitle_force_style(1920, 1080)
    assert "FontName=Inter" in st and "BorderStyle=3" in st and "Alignment=2" in st
    assert video.subtitle_force_style(1080, 1920) != st                 # állóban a saját méretével


def test_overlay_takes_subtitles_without_layers(put_file, tmp_path):
    from app.tools import video
    p = tmp_path / "v.mp4"
    p.write_bytes(b"\\x00" * 100)
    fid = put_file(p)
    assert video.overlay_input({"file_id": fid, "subtitles": [{"start": 0, "end": 2, "text": "Szia"}]})[0] == fid
    with pytest.raises(ValueError, match="subtitles"):
        video.overlay_input({"file_id": fid})


def test_srt_endpoint_writes_the_corrected_lines(client):
    r = client.post("/api/video/srt", json={"cues": [{"start": "00:01", "end": "00:03.5", "text": "Kijavított név: Kovács Anna"}]})
    assert r.status_code == 200, r.text
    fid = r.json()["file_id"]
    from app.storage import output_path
    txt = output_path(fid).read_text("utf-8")
    assert "00:00:01,000 --> 00:00:03,500" in txt and "Kovács Anna" in txt
    assert client.post("/api/video/srt", json={"cues": []}).status_code == 400
