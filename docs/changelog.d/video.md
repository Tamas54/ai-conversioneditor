# Lane "video" (OPERATION OMLETT)

## 1.3 — the video check sees text collisions
- A graphic's `text` without `w` no longer wraps at a hidden 0.6 of the frame: it stays on one line while it fits in
  0.92 of the frame width (measured with Inter, the renderer's font — ő/ű included; short texts keep their 0.6 box, so
  older videos look the same; left/right-aligned text keeps its old anchor edge).
- The builder reports every graphic text's drawn (ink) box from the engine (`sourceRectAtTime`); the check turns them into
  findings: two texts overlapping (same layer or two layers on screen together), a text running out of its enclosing
  `rect` or off the frame, an unexpected line break (no `w`). Each finding names the layer/scene, the element and a
  suggested `w` / `size` (or `y`). Status becomes `needs_check`.
- `render=false` (overlay / make / edit) still returns this layout check.
- Measured on the reconstructed 10-19 layers (`rendkivuli`, `tudta`, `hir3`, main's 0.6 box): main 0 findings / `ok`,
  now 3 named findings / `needs_check`. Without `w` the new default keeps "RENDKÍVÜLI KIADÁS" (66 px, ~663 px wide) and
  the Tudta line on one line; only the over-long ticker text is still reported (with a size that fits).

## 1.4 — short `video` tool description + `action="schema"`
- The `video` MCP description went from 21 902 to 1 945 characters (cap 2 000, tested; sent without indentation). It
  lists the main actions and points to `video(action="schema", topic=…)`.
- New `action="schema"`: topics make | overlay | layers | graphic | chart | subtitles | edit (no topic = the list). The
  registry is a plain dict, `app.tools.video_schema.TOPICS` — another lane adds a topic with
  `TOPICS["adjust"] = video_fx.SCHEMA` (no other wiring needed).
- A layer with a `chart` object and no `type` is a chart layer (a list of `elements` without `type` is a graphic); the
  type / `at` / chart errors carry a minimal example and point to the schema.
- New MCP params: `topic`, `render` (`render=false` for make / overlay / edit: save the project only).
- Other tool descriptions over 2 000 characters (not changed here): wordcraft 4 829, design 4 785, pptx_build 4 246,
  forms 3 698, file_tools 3 046, office 2 901, validate_document 2 531, files 2 297, convert 2 255, facts 2 080.

## 3.6 — `move_to` timing
- The glide starts once the element has appeared (after the ≤ 0.5 s fade-in for `appear`, after `dur` for pop / grow /
  slide, 0.3 s for pulse) and lasts `move_dur` (new; default `dur`), eased. It starts from the element's own place
  (a slide-in no longer makes it start from the slide offset).
- Regression (rendered frames, marker at x 0.2, `at` 1.5, `dur` 2.2, `move_to` [0.88, y]): at 4.3 s main had it at
  x ≈ 0.2 (the move started at 3.9 s and lasted 4.4 s); now x = 0.88 (arrives at 4.2 s).

## 3.7 — Lottie search
- `{"search": …}` now loads up to five candidates and orders them: transparent background first (a solid layer or a
  filled rect covering the canvas counts as opaque), then the shape that fits the element's box, then the vision
  ranking. `"pick": n` takes the n-th of that order.
- The result has `lottie_picks`: where it went, name, author, `thumb_url` (preview), `gif_url`, page, its pick / of,
  transparent, size, and the alternatives with their previews. The chosen URL, name and preview are kept in the spec.
- The check samples every Lottie element (its comp rendered transparent at 25/50/75 % of its loop): an animation that
  fills its whole box (opaque background) or sits on a measured text is a named finding; a clean one is in `verified`.
- Tests mock LottieFiles (no network); the sampling test renders synthetic Lottie JSONs with the engine.

## 3.4 (overlay side) — end card that extends the footage
- `video(action="overlay", …, end_card={"title", "subtitle"?, "kicker"?, "dur"?})` (or `end_title` / `end_subtitle`):
  the footage is extended by `dur` seconds (default 4) — last frame frozen with `tpad=stop_mode=clone`, sound padded
  with `apad=pad_dur` (one x264 CRF 17 pass, a hidden work file) — and a full-frame card (80 % veil over the frozen
  frame, kicker / title / accent bar / subtitle, the picture and animation credits) sits on the added seconds.
  An overlay may have only an end card.
- Edits: `changes.end_card` changes only the given fields (a new length re-extends from the plain footage; the same
  length reuses the extension); `"none"` removes it and the video is back to the footage's length.
- Measured: 3.0 s clip + `dur: 2` → 5.0 s video, audio 5.0 s; frames at 3.6 s and 4.6 s are identical under the card.
- The general extend-video / image-to-video operations belong to lane "media".

## Cross-lane hooks (schema registry, transcript hints)
- `video_schema.register({topic: text})` adds any lane's reference; `app.tools.transcript.SCHEMA_REF` (lane
  transcript) is picked up automatically when present, so `video(action="schema", topic="transcript")` works after the
  merge without wiring.
- Overlay `transcript={"prompt", "vocabulary", "model"}` (MCP param `transcript`) is passed to the speech recognition
  behind `subtitles="auto"` and `at: {"say": …}` (also kept for edits) — only the keys `transcript.transcribe` accepts,
  so it is a no-op until lane transcript's new parameters are merged.

## Phase 5 — video tool details
- `make`: new `scrim` (0–1, also per scene; `0` = no darkening) controls the veil over title card, scenes, chart / number
  scenes and end card — `backdrop: 0` alone left a 70 % shade before. The title card's kicker gets a dark pill sized to
  its rendered text (engine `sourceRectAtTime`) over a photo, so the red accent is readable.
- Fixed length compensates the crossfades: when every scene has its own `duration` and they come out short, they are
  stretched to the asked length (main: 10 s asked, 3 × 2.5 s scenes → 8.9 s; now 10.0 s). Scene lengths stay minimums
  (too long is still reported, never cut).
- `opacity` is 0–1 everywhere and stored that way (0.94 was stored as 94.0); 0–100 is read as percent; old saved specs
  keep working. Diagram graphics now emit 0–1 too. Documented in the `graphic` schema topic.
- Layer listings / comp names: a graphic is labelled by its title or first text (`graphic “RENDKÍVÜLI KIADÁS”`), else
  `graphic (3 elements)` — no more `graphic “”`.
- `video(action="status", job_id, wait=30)` waits up to 30 s (≤ 55) while the job runs. The `jobs` MCP tool now finds
  Studio jobs (video, render, transcript) too, with a note, instead of "No such job.".
- Faster turns: `render=false` (save only, layout check still returned), `draft=true` on make / overlay / edit (half size,
  2.5 Mbps draft; previews use it until a full render; `info.last_render.draft`). Re-rendering only the changed section
  into the existing MP4 is NOT done: the engine renders spans, but splicing separately encoded H.264 pieces without
  re-encoding needs keyframe-aligned cuts and identical encoder parameters, which the CLI doesn't expose — the existing
  `action="render", scene="L2"` clip covers checking one part.
- Credits: "Pictures and animations:" whenever a Lottie is in the video (main only looked for a credit starting with
  "Animation ", so a Lottie with its own credit got "Pictures:").
- A graphic image / Lottie / cut-in that couldn't be loaded is now a named finding (it was only logged; the check said
  `ok`).
