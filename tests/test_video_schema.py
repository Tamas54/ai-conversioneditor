"""1.4 (OMLETT, 10-19): a `video` MCP-eszköz leírása rövid (a claude.ai a hosszút levágta — épp a layers séma veszett el),
a részletek a video(action="schema", topic=…) referenciában; ábra-objektumos réteg típus nélkül = chart; a hibaüzenet
minimális példát ad."""
from __future__ import annotations

import asyncio

import pytest

CAP = 2000          # a kliensek ennyit biztosan átadnak (a claude.ai a ~2 KB fölötti leírást csonkolta)


def _video_tool():
    from app import mcp_server
    return next(t for t in asyncio.run(mcp_server.mcp.list_tools()) if t.name == "video")


def test_video_description_is_short_and_points_to_the_schema():
    t = _video_tool()
    d = t.description
    assert len(d) <= CAP, len(d)
    for s in ('action="schema"', "topic", "layers", 'action="overlay"', "lower_third", "fullscreen", "split",
              "AI NARRATION IS ON BY DEFAULT", 'narration="none"', "not checked against a source", "render=false"):
        assert s in d, s
    props = t.inputSchema["properties"]
    assert {"topic", "render", "layers", "alpha", "duration_mode"} <= set(props)


def test_schema_returns_the_layers_reference_and_lists_topics():
    from app import mcp_server
    from app.tools import video_schema
    out = asyncio.run(mcp_server.video(action="schema", topic="layers"))
    ref = out["reference"]
    for s in ("lower_third", "until", '{"say"', "position", "[x, y, w, h]", "fullscreen", "split", "add_layers",
              "ProRes 4444", '"webm"', 'scene="L2"', "a layer\n    with a \"chart\" object and no type IS a chart layer"):
        assert s in ref, s
    lst = asyncio.run(mcp_server.video(action="schema"))
    assert {"layers", "graphic", "chart", "subtitles", "make", "overlay"} <= set(lst["topics"])
    for t in ("graphic", "chart", "subtitles", "make", "overlay"):
        assert len(video_schema.reference(t)["reference"]) > 300, t
    assert "0.92" in video_schema.reference("graphic")["reference"]           # a szövegdoboz alapszabálya dokumentálva
    with pytest.raises(ValueError, match='topic="layers"'):
        video_schema.reference("nope")
    video_schema.TOPICS["adjust"] = "changes.adjust — test"                  # más sáv is beírhatja a saját témáját
    try:
        assert video_schema.reference("adjust")["reference"].startswith("changes.adjust")
    finally:
        video_schema.TOPICS.pop("adjust")


def test_a_layer_with_a_chart_object_is_a_chart_and_errors_carry_an_example():
    from app.tools import video
    col = {"type": "column", "title": "Budget", "unit": "bn Ft",
           "items": [{"label": "2024", "value": 44.8}, {"label": "2025", "value": 47.5}]}
    ls = video.layers_from([{"id": "fejlec", "at": 1, "chart": col},
                            {"at": 2, "elements": [{"shape": "text", "text": "Hello", "x": 0.5, "y": 0.2}]}], "", [])
    assert ls[0]["type"] == "chart" and ls[0]["chart"]["type"] == "column"
    assert ls[1]["type"] == "chart" and ls[1]["chart"]["type"] == "graphic"
    with pytest.raises(ValueError) as e:
        video.layers_from([{"id": "fejlec", "at": 1, "text": "x"}], "", [])
    assert '"type": "lower_third"' in str(e.value) and 'action=\\"schema\\"' not in str(e.value) and "schema" in str(e.value)
    with pytest.raises(ValueError, match='"at": "00:02"'):
        video.layers_from([{"type": "title", "text": "x"}], "", [])
