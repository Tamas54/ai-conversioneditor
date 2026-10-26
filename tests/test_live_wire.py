"""Az élő előnézet bekötése a szerkesztésbe (video._preview_and_final → _live_preview): alapból ez fut, a válasz a lejátszó
lapja (/live/<pid>) és a jelenet címe, nincs előnézeti render, a megváltozott grafikák klipjei sorba állnak."""
import asyncio

from app.tools import video, video_preview


def test_the_live_preview_is_the_default(monkeypatch):
    monkeypatch.delenv("VIDEO_PREVIEW_LIVE", raising=False)
    assert video_preview.live()
    for off in ("0", "off", "false"):
        monkeypatch.setenv("VIDEO_PREVIEW_LIVE", off)
        assert not video_preview.live()


async def test_an_edit_answers_with_the_live_page_and_queues_the_changed_layers(monkeypatch):
    from app.tools import live_layers, preview_serve
    pid = "ab" * 16 + ".ecproj"
    queued, metas, steps = [], [], []

    async def fake_layers(p, spec=None, **kw):
        queued.append((p, kw))
        return []
    monkeypatch.setattr(live_layers, "layers_part", fake_layers)
    monkeypatch.setattr(video, "write_meta", lambda p, **kw: metas.append((p, kw)))
    monkeypatch.setattr(preview_serve, "signed", lambda p, h=None: "?t=1.sig")
    pv, note = await video._live_preview(pid, [(10.0, 14.0, "L5 chapter")], 100.0, lambda *a: steps.append(a))
    await asyncio.sleep(0)
    assert pv["mode"] == "live"
    assert pv["page_url"] == f"http://testserver/live/{pid}"
    assert pv["scene_url"] == f"http://testserver/api/video/{pid}/live.json?t=1.sig"
    assert pv["markers"] == [{"from": 10.0, "to": 14.0, "what": "L5 chapter"}]
    assert queued == [(pid, {"enqueue": True})]                       # a megváltozott grafikák már készülnek
    assert metas and metas[0][1]["extra"]["preview"]["mode"] == "live"
    assert "nothing is rendered" in note and steps == [(3, 4, "the live preview")]
