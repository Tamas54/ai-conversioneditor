---
name: studio-video
description: Make a short video with motion graphics with Studio — from an article, a document, the user's words or the user's photos. Use when the user asks for a video, a reel, a short, an explainer or an infographic video, with animated charts, narration, music or camera moves.
---

# Studio video

The most reliable path is your own scene plan: you decide every scene, the server builds it exactly.

## Workflow

1. **Plan** — `video` with `action="make"`, `format` (vertical 9:16 for phones, landscape 16:9, square), `title`, `kicker`?, `end_title`?, `end_subtitle`?, and `scenes=[...]`, each `{"headline", "caption", ...}`:
   - Picture: `image` (file_id / URL — e.g. the user's photo, or one you generated) or `images` (up to 4, they crossfade), or `image_query` (2–4 plain English words for a free stock photo). Without either, give `background`: gradient | motion | dark | "#RRGGBB".
   - Movement: `motion` zoom_in | zoom_out | pan_left | pan_right | pan_up | pan_down | still, `focus` [x, y], `zoom`; `camera` push_in | pull_out | drift | `[{"at": s, "zoom": 1.6, "to": [x, y]}, {"at": s, "zoom": 1}]` to punch in on a detail (the title and captions stay put).
   - `fit`: contain shows a whole photo with a blurred fill (group photos in a vertical video); auto does it for the user's own photos.
   - Graphics: `chart` — column, bar, line, area, donut, ring, icons, counter, compare, progress, gauge, funnel, timeline, steps, proscons, quote, waterfall, slope, scatter, band, lines, race, sankey, explainer diagrams (decision_tree, swimlane, network), your own `graphic`, a real `map`.
   - `stat` {value, label} for one big number; `duration` seconds for a fixed scene length.
2. **Source text** — put the facts in `text`. Every number, date and quote on screen must be in it (or in the scenes), otherwise that graphic is left out and the check says why. Computed values are fine when they follow from the source (say how in the caption).
3. **Sound** — `narration` "ai" (natural voice, `language`) or "none"; `music` = a mood (upbeat, calm, cinematic, lofi, corporate, playful, romantic, dramatic): it drops under speech.
4. **Wait** — the call returns a `job_id`; call `video` `action="status"` with it until `done`.
5. **Read the `check`** and fix with `action="edit"`: `changes={"scenes": [{"id": "s3", ...only what changes}]}` — untouched scenes stay as they are. To add numbers, pass the extra facts in `text`.
6. **Look** — `files` `action="preview"` with `scene="s3"` (and `frames=6` for the motion) before telling the user it is done.

## Notes

- Pictures of real people: use the user's own photos; never present a stock photo of a stranger as someone the user means.
- Sending to a phone: make it vertical; the result is a full MP4 plus an editable project (`edit_url` opens the timeline editor).
