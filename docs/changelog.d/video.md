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
