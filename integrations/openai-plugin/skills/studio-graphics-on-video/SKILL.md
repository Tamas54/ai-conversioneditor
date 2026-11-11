---
name: studio-graphics-on-video
description: Put graphics on the user's own video with Studio — name bars, charts, numbers, quotes, split screen, full-screen inserts and subtitles, timed to the footage or to what is said. Use when the user uploads a video and asks for lower thirds, infographics, captions, a chart while they speak, or a punch-in zoom.
---

# Studio graphics on the user's video

The footage stays as it is (size, length, sound); graphics go on top as timed layers.

## Workflow

1. **Get the footage in** — `upload_from_url` (or the file the user attached) → file_id.
2. **Listen first when timing matters** — `video` `action="transcript"` with `file_id` (and `language`): sentences with times plus an .srt subtitle file. Use it to suggest where graphics help.
3. **Overlay** — `video` `action="overlay"`, `file_id`, `text` = the source of every number and quote, `layers=[...]`:
   - `{"at": "00:05", "dur": 5, "type": "lower_third", "name": "…", "role": "…"}`
   - `{"at": {"say": "the words that are said"}, "type": "chart", "chart": {...}}` — starts when they are said (any chart type from the video skill; a ranking race needs about 1.5 s per period: give `dur` accordingly).
   - `type`: title, chapter, source, stat, quote, chart, `fullscreen` (covers the picture, sound goes on), `split` (`side`: the footage shrinks to the other half).
   - `camera=[{"at": "00:12", "zoom": 1.5, "to": [x, y]}, {"at": "00:15", "zoom": 1}]` punches in on the footage.
   - `music` = a mood: it drops under the footage's own speech. `alpha=true` also makes a transparent graphics-only video (ProRes 4444) for an editing program.
4. **Wait** with `action="status"`, **read the `check`**, fix with `action="edit"` — `changes={"layers": [{"id": "L2", ...only what changes}], "add_layers": [...]}`.

## Subtitles

- Subtitles on the user's video: `video` `action="transcript"` → its .srt `file_id` → `file_tools` with `tool="add-subtitles"`, `file_ids=[<video>, <srt>]`, `options={"burn": true}` (burned into the picture; `false` = switchable subtitles). A 30-second video takes about 20 seconds.
- Let the user read the transcript first: speech recognition can mishear names and spell numbers as digits.
