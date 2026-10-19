"""A `video` MCP-eszköz részletes referenciája: video(action="schema", topic=…).

A tool-leírás rövid (a claude.ai kliens a hosszú leírást levágja — 10-19: épp a `layers` séma veszett el, és az első
overlay-hívás „type None”-nal állt meg); a részletek itt vannak, témánként. Bővíthető: egy másik modul a saját témáját
egyszerűen beírja a TOPICS szótárba (pl. TOPICS["adjust"] = video_fx.SCHEMA) — a kulcs a téma neve, az érték a szöveg.
"""
from __future__ import annotations

from typing import Any, Optional

TOPICS: dict[str, str] = {}

TOPICS["make"] = """\
action="make" — a short video from a text: an article (url, or its text in text), a document (source_file_id) or the
person's own words (text + keep_wording=true ONLY when they want their own sentences word for word as the captions)
→ title card, scenes with full-screen pictures in gentle motion, headlines, timed captions, end card with picture
credits → a video project (.ecproj) + the MP4. Takes 1–3 minutes, so it returns a job: video(action="status",
job_id=…) until status is "done" (result.project / result.video, each with edit_url) or "failed".

YOUR OWN SCENE PLAN (most reliable): scenes=[{"headline", "caption", "image"? (file_id | URL — e.g. a picture you made —
| {"stock": "words"}), "image_query"? (English words for a stock photo — give it for every scene without an image:
without it the search uses the headline), "stat"? {value, label}, "chart"? (topic "chart"), "duration"? seconds,
"id"?}] + title, kicker?, title_image?, end_title?, end_subtitle?, format, duration (total; scenes without one share the
rest). It is made exactly as given (no re-planning).

MOVEMENT AND BACKGROUNDS, per scene (in scenes=[…] and in edit changes.scenes): "images": [up to 4 pictures] — the
scene's backgrounds one after another, crossfading, each moving; "motion": zoom_in | zoom_out (Ken Burns, toward / away
from its subject) | pan_left | pan_right | pan_up | pan_down | still | auto, "focus"?: [x, y] 0–1 (the point to move
toward; found automatically otherwise), "zoom"?: 1.03–1.8; "camera": a camera on the scene's picture AND graphic
together — "push_in" | "pull_out" | "drift" | [{"at": seconds, "zoom": 1–3 (1 = the whole frame again), "to"?: [x, y]
0–1 of the frame, "dur"?: seconds}]; the scene's title, big number and captions stay put. "fit": cover | contain (the
whole picture, its blurred copy filling the rest) | auto (default: contain for the person's own photo when its shape is
far from the frame's). "background" for a scene without a picture: gradient | motion | dark | "#RRGGBB". A scene with a
chart shows the CHART's title at the top, not the headline (a graphic without a title shows the headline). Put the
source in text too: chart numbers are checked against it (without it, against your scenes).

format: vertical (9:16 — Reels, TikTok, Shorts) | landscape (16:9 — YouTube) | square. duration: seconds, 4–180
(default 60; with keep_wording and no duration the reading pace sets it). duration_mode: "fixed" (the default when you
give duration — the video is that long INCLUDING the title and end cards; scenes hold longer or get shorter, down to
1.2 s; the narration is read up to 1.2× faster; what still doesn't fit is said in
the check) | "narration" (the default without duration — the voice sets the length). Every result has duration =
{requested, actual, mode}; a gap beyond a second or so is a check finding, never "ok".
images: pictures in order — file_ids or {"url": …} — images[0] is the TITLE CARD, images[1] scene 1… (the check lists
where each went). This server doesn't make AI pictures for you: make them with your own image tool and pass their URLs.
pictures: stock (free stock photos fill the scenes without one) | mine | none (| ai: only in the Studio web app).
AI NARRATION IS ON BY DEFAULT: narration="ai" — a natural AI voice (offline Piper voices, 58 languages, picked from the
text or `language`) reads the title and every caption aloud. narration="none" turns it off (captions only).
narration_voice: "female" | "male" | a Piper voice id (e.g. "hu_HU-imre-medium", "en_US-ryan-medium").
MUSIC (none by default): music = a mood — upbeat | calm | cinematic | lofi | corporate | playful | romantic | dramatic —
or a track id → built-in CC0 music fitted to the length; music_file_id = the person's own music. Under speech the music
drops and comes back in the pauses; "none" = no music. voice_file_id: the person's own voice-over (replaces the AI
narration). accent: a hex colour for bars and numbers. brand: a saved brand (name, "mine"); "none" = off.
ken_burns: some (default — every other scene a clear Ken Burns move) | all | off.
data_file_id: CSV/XLSX → a chart per column, values from the cells.
layers=[…] (topic "layers") put time-coded graphics on the made video too, at absolute times on its timeline.
The result's check also reports COVERAGE (which source sentences and numbers made it in), shortened texts, where your
pictures went, and the measured text boxes of your graphics. See it scene by scene: files(action="preview", file_id,
scene="all") or scene="s3". render=false saves the project without rendering (the layout check still comes back)."""

TOPICS["overlay"] = """\
action="overlay" — graphics on the person's OWN video. file_id = their uploaded mp4 | mov | webm | m4v — it stays the
base track with its size, fps, length and sound (never re-cut, never forced to 9:16; a phone video stored turned is set
upright). → a project (.ecproj, editable in the video editor) + the MP4, as a job (poll status).
layers=[…] (topic "layers") and/or subtitles=[…] | "auto" (topic "subtitles"). text = the source: numbers, dates and
quotes in the layers are checked against it (one that isn't there drops that layer, said in the check); without text
they are kept and the check says "not checked against a source". title, accent, brand, music as for make (the music
drops under the footage's own speech). camera=[{"at": time, "zoom", "to"?, "dur"?}] punches in on the footage (back
out with zoom 1); changes.camera in an edit.
The result's duration is the footage's (mode "footage"). render=false saves the project only (the layout check still
comes back).
Example: video(action="overlay", file_id="ab12.mov", text="…", layers=[{"at": "00:02", "type": "lower_third", "name":
"Anna Kovács", "role": "Mayor"}, {"at": 9, "dur": 5, "type": "column", "title": "Budget", "unit": "bn Ft", "items":
[{"label": "2024", "value": 44.8}, {"label": "2025", "value": 47.5}]}])"""

TOPICS["layers"] = """\
layers (overlay; make and edit too): [{"id"? ("L1", "L2"… given when left out — stable across edits), "at": seconds |
"MM:SS" | "HH:MM:SS.ms" | {"say": "words that are said"} (overlay: the layer starts as they are said in the footage and
stays to the end of that sentence; not said → left out, the check quotes what was heard), "dur": seconds | "until":
time | {"say": …}, "type", "position"?: auto | top | bottom | left | right | center | [x, y, w, h] (centre and size,
0–1 of the frame — a graphic in such a box draws in the box: its x/y/w/h are 0–1 of the box and its sizes are px at 1080
on the box's short side), "anim"?: fade | slide | pop | none (or {"in", "out"})}].
Types:
  lower_third {name, role?, side?: left|right}
  title {text, subtitle?, kicker?} (a band across the frame) · chapter {text, subtitle?, kicker?}
  source {text} (a small credit) · stat {value, label} · quote {text, author} (word for word from the source)
  chart {chart: any chart (topic "chart") or a graphic (topic "graphic"), panel?: false = no dark panel} — a layer
    with a "chart" object and no type IS a chart layer; type may also be the chart type itself, e.g. {"type": "column",
    "title", "unit", "items"} or {"type": "graphic", "elements": […]}; a graphic without position covers the frame
    transparently (an arrow or a label on the footage)
  fullscreen {chart | image | text (+ subtitle, kicker)}: covers the picture, the sound goes on (image may be another
    VIDEO of theirs — a cut-in: + "clip": [from, to] seconds of it; its sound plays instead of the footage's ("sound":
    "replace", the default here) | "mix" | "off")
  split {side: left|right|top|bottom (where the graphic goes), chart | image | text}: the base shrinks to the other half
    (a video plays muted unless "sound" is given).
A lower third or source caption without position goes where nothing else is (other layers, a made video's headlines and
charts). Overlaps that remain, a layer past the end, a dropped number, a text that collides or runs out of its box all
come back in the check's findings. Edit by id: changes={"layers": [{"id": "L2", …only the fields that change} | {"id":
"L2", "remove": true}], "add_layers": [new layers]} (a "chart" without "type" fixes just those chart fields).
See one: files(action="preview", file_id, scene="L2") (+ frames=6: in → out); video(action="render", scene="L2"): a
short clip of its span.
alpha (make, overlay, edit): true = also a transparent, graphics-only video: ProRes 4444 .mov (yuva444p12le — Premiere,
Resolve, Final Cut) | "webm" (VP9 with alpha — for the web; slower) | "both". No base, no sound; the check proves the
alpha (pixel format + a decoded frame).
Minimal examples: {"at": 2, "type": "lower_third", "name": "Anna Kovács", "role": "Mayor"} · {"at": "00:05", "dur": 4,
"type": "graphic", "elements": [{"shape": "text", "text": "Hello", "x": 0.5, "y": 0.2, "size": 60}]} · {"at": 9,
"chart": {"type": "donut", "title": "Share", "unit": "%", "items": [{"label": "A", "value": 60}, {"label": "B",
"value": 40}]}}"""

TOPICS["graphic"] = """\
graphic — draw your own (flow diagram, map with pins, labelled photo, org chart, before/after, moving type) as a scene
chart or a layer: {"type": "graphic", "title"?, "backdrop"? (0–1 dark veil over the picture, default 0.58; 0 = none),
"elements": [{…}]} — up to 80 elements, each its own layer (editable by hand).
Element: {"shape": rect | circle | line | arrow | path | polygon | text | pin | image | lottie | map,
  x, y (centre, 0–1 of the frame / the layer's box), w, h (0–1), r (circle/pin radius, 0–1 of the short side),
  from / to [x, y] (line, arrow), points [[x, y]…] (path, polygon),
  text (text, pin label), size (px at 1080 on the short side), weight bold|semibold|regular, align left|center|right,
  fill / stroke / color ("accent" | "white" | "muted" | "red" | "green" | "blue" | "yellow" | "dark" | "black" | "#hex" |
  "none"), width (line px), radius (corner px), opacity (0–1, 1 = solid; 0–100 percent is read as percent too),
  anim: appear | pop | grow (a rect grows from its base: upward when taller than wide — a bar —, else from the left) |
    draw (lines, and rect / circle outlines without fill, draw themselves) | slide-left | slide-right | slide-up |
    slide-down | pulse | none,
  at (seconds into the scene / layer when it appears; default one after another), dur (seconds the appearing takes),
  move_to [x, y] (then glides there: it starts once the element has appeared — after the fade-in (≤ 0.5 s) for
    appear, after dur for pop / grow / slide — and takes move_dur seconds (default: dur), eased in and out; e.g.
    "at": 1.5, "dur": 2.2 → it is at move_to by 4.2 s),
  dash (line, arrow, path: "dashed" | "dotted" | [dash, gap] px), dim [[from, to] seconds] (faded while something else
  is shown)}.
TEXT BOX: a text without w stays on ONE line as long as it fits in 0.92 of the frame width (a short one keeps a 0.6
  wide box); longer text wraps there. Give w to wrap it in a narrower box. The check measures every text as the engine
  draws it and names overlaps, a text running out of its rect (or off the frame) and unexpected line breaks, with a w /
  size that fits.
image: file_id | URL | {"stock": "words"} — or the person's VIDEO file_id: its picture plays there from when the element
  appears; "clip": [from, to] seconds of it; on an overlay "sound": "replace" | "mix" plays its sound (muted by
  default) + fit cover|contain.
lottie: file_id | URL of a Lottie JSON | {"search": "confetti", "pick"?: n} — the server orders free LottieFiles
  animations: transparent ones first, then those whose shape fits the element's box (w × h), then by a look at their
  previews; pick = the n-th of that order (default 1). The result's lottie_picks names the one taken (name, author,
  thumb_url preview, its pick and the alternatives with their previews); it is credited, and loops unless loop=false.
  video(action="lottie", text="confetti") lists candidates with previews to choose one yourself (then {"url": json_url,
  "credit": …}). The check samples every Lottie element at 3 moments: an opaque background or a Lottie sitting on a text
  is a finding.
map — a REAL map under the other elements: {"shape": "map", "place": "Szeged" (or "bbox": [south, west, north, east]),
  "detail": auto | streets | outline, x/y/w/h = its box (default the whole frame)}. Streets, rivers and parks from
  OpenStreetMap, country borders from Natural Earth; both credited on screen. Place anything ON it by name or
  coordinates instead of x, y: {"shape": "pin", "place": "Dóm tér, Szeged", "text": "Dóm tér"}; from / to / points take
  {"place": …} or {"lat": …, "lon": …} too. Pins and a route may sit inside the map: {"shape": "map", "place": …,
  "pins": [{"place", "label"}], "route": [places]}. The map widens to show every placed element.
Anything left out (unknown shape, no position, a number in a text that isn't in the source) is listed in the check."""

TOPICS["chart"] = """\
chart = {"type", "title", "unit", "total"?, "min"?, "max"?, "items": [{"label", "value"?, "text"?}]}:
column | bar | line | area (values over time) | donut (parts of a whole) | ring (one %) | icons ("1 in 4", x of 100) |
counter (one number counting up) | compare (2–3 numbers side by side) | progress (separate %s) | gauge (one value on a
min–max scale) | funnel (shrinking stages) | timeline (dated events: label = date) | steps (how-to) | proscons (label
"+"/"-", text) | quote (text word for word from the source, label = who) | table ({"columns": [2–5 headings], "rows":
[[cells]…] up to 7 rows}).
With their own shape: waterfall (a start, then + / − changes; the end is computed and checked against "total"? —
{"items": [{"label", "value"}…], "total_label"?}) | slope ({"labels": [before, after], "items": [{"label", "from",
"to"}]}) | scatter ({"x_title", "y_title", "items": [{"label", "x", "y", "size"?}]}) | band (a line with its low–high
range, the forecast part dashed — {"items": [{"label", "value", "low"?, "high"?}], "forecast_from"?}) | lines (2–4
series — {"labels": [periods], "series": [{"name", "values"}]}) | race ({"labels": [periods], "items": [{"label",
"values"}]}, 3–10 items, 3–12 periods) | sankey ({"flows": [{"from", "to", "value"}], "nodes"?: [{"name", "color"?}]},
2–24 flows, at most 14 nodes, one way only).
Every number, date and quote is checked against the source — one that isn't there drops the chart.
graphic: your own drawing — topic "graphic".
Explainer diagrams — give the relationship, the server lays it out and builds it up step by step: {"type":
"decision_tree" | "swimlane" | "network", "title"?, "nodes": [{"id", "text" (≤ 70 characters), "kind"? (tree:
question|condition|outcome · swimlane: start|step|decision|end), "actor"? (swimlane lane) | "group"? (network colour +
legend), "order"?, "size"? 1–3, "at"?}], "edges": [{"from", "to", "label"?, "kind"? sequence | hierarchy | money | cause
| correlation | link, "certainty"? certain | uncertain (dashed) | disputed (dotted + ?)}], "actors"? {"Name": {"color"?,
"short"?}}, "reveal"? steps | all, "pace"? seconds per step, "focus"? false, "direction"? auto | down | right | outline,
"center"?}. Shorthands: tree "root" + nested "branches": [{"label", "text", "kind"?, "branches"?}]; swimlane "steps";
network edges alone. Change one with action=edit {"chart": {…}} WITHOUT "type": only the nodes (by id) and edges (by
from + to) you send change; {"remove": true} deletes, {"pace": 1.5} slows the build-up.
threejs — a 3D scene in three.js (r170): {"type": "threejs", "title"?, "transparent"? (default true), "code": "…"} — a
function body that gets THREE, renderer, W, H, dur, accent (THREE.Color) and label(text, {size, height, color, bg}) and
returns {scene, camera, update(t)}; animate from t (0…dur, 30 fps), never from the clock; no network. A code error comes
back in the check — fix it with action=edit {"chart": {"code": "…"}}.
script — your own EffectCraft code (After Effects-style JavaScript): {"type": "script", "title"?, "backdrop"?, "code":
"…"}; gets comp, W, H, dur, accent ([r, g, b] 0–1) and D (D.font.bold|semibold|regular); sandboxed (no files outside the
project, no network, 20 000 characters at most). Errors and numbers not in the source come back in the check."""

TOPICS["subtitles"] = """\
SUBTITLES on the person's own footage (overlay), in the Studio caption style (the dark caption bar of made videos):
subtitles=[{"start", "end", "text"}] (seconds or "MM:SS" — e.g. the transcript's sentences, corrected with the person)
or "auto" (the transcript as heard — show it to the person first when names or numbers matter); an .srt comes with the
result; changes.subtitles in an edit replaces them. Layers are optional when there are subtitles; layers stay above the
caption bar. Long lines are split at sentence / clause boundaries into two-line parts.
action="transcript": what is said in a video or sound file (or an overlay project's footage), with times → sentences
[{at, until, text}] + an .srt (file_id), as a job. Offline speech recognition (Whisper large-v3-turbo; `language`
helps, else detected). Tie a layer to what is said: {"at": {"say": "words from a sentence"}, …}."""

TOPICS["edit"] = """\
action="edit": change a finished video (file_id = its project or its MP4) → a NEW version (project + MP4), as a job.
changes = {"scenes": [{"id": "s3" (stable — info lists the ids; or "scene": n, 1 = first after the title card),
"headline"?, "caption"?, "image"? (file_id | URL | {"stock": "words"} | "none"), "stat"? {value, label} | null, "chart"?
(WITHOUT "type" it is a fix of the scene's chart — e.g. {"unit": "million"}) | null, "duration"?, "remove"?
true}], "add": [{"after": "s2" | n (0 / "title" = first), "headline", "caption", "image" | "image_query", "stat"?,
"chart"?, "duration"?}], "title"?, "title_image"?, "end_title"?, "end_subtitle"?, "format"?, "duration"?, "ken_burns"?,
"accent"?, "music"? | "music_file_id"? | "none", "voice_file_id"? | "none", "narration"? "ai" | "none",
"narration_voice"?, "duration_mode"?, "layers"?: [{"id": "L2", …} | {"id": "L2", "remove": true}], "add_layers"?: […]}.
Untouched layers stay as they are; the AI narration is recorded again from the changed captions. Numbers in a new chart
or stat must be in the video's source or in `text`. An overlay project takes layers, add_layers, accent, title, music,
camera and subtitles. alpha=… renders the transparent version again.
Faster turns: render=false saves the new version without rendering (render it later with action="render");
action="render" with scene="s3" (a scene) or scene="L2" (a layer's span) renders just that part as a short draft clip.
video(action="info") shows the scenes and layers (id, at, dur, type, text); files(action="preview", file_id,
scene="s3" | "L2" | "all") shows them.
action="to_pptx": put the video on a slide — a new one-slide deck, or deck_file_id (+ after_slide, title) adds a slide
to an existing deck; the MP4 is embedded (first frame as its poster)."""

_ABOUT = {"make": "a video from a text / your scene plan: formats, duration, pictures, narration, music",
          "overlay": "graphics and subtitles on the person's own footage",
          "layers": "time-coded layers: types, timing, position, alpha — read before your first overlay",
          "graphic": "draw your own: shapes, the text box rule, move_to, images, Lottie (search + pick), real maps",
          "chart": "every chart type, explainer diagrams, three.js, script",
          "subtitles": "subtitles on footage and the transcript",
          "edit": "changing a finished video, faster re-renders, to_pptx"}


def reference(topic: Optional[str] = None) -> dict[str, Any]:
    """video(action="schema"): téma nélkül a témák listája, témával a részletes referencia (a TOPICS bővíthető)."""
    t = str(topic or "").strip().lower()
    if not t:
        return {"topics": {k: _ABOUT.get(k, TOPICS[k].split("\n", 1)[0][:120]) for k in TOPICS},
                "how": 'video(action="schema", topic="layers") — the full reference of one topic.'}
    if t not in TOPICS:
        raise ValueError(f"schema topic {topic!r} — use {' | '.join(TOPICS)}, e.g. video(action=\"schema\", topic=\"layers\").")
    return {"topic": t, "reference": TOPICS[t]}
