---
name: studio-presentation
description: Make a finished, checked PowerPoint deck with Studio. Use when the user asks for a presentation, slides, a pitch deck or a board deck — from an outline, notes, an article or an uploaded document — including charts, tables, explainer diagrams or a video on a slide.
---

# Studio presentation

Build the whole deck in one call, look at it, then fix single shapes in place. Never rebuild a deck to change one thing.

## Workflow

1. **Source**
   - The user's text or outline: write the slides yourself as a `spec` (below).
   - An uploaded document (docx, pdf, md…): `pptx_build` with `from_document` = its file_id, `instructions` (audience, length), `slides` 4–15 or leave it out.
   - A file from a link: `upload_from_url` first, then use its file_id.

2. **Build** — `pptx_build` with `spec` = `{"title", "footer"?, "brand"? | "theme"? {primary, accent, bg, text, muted}, "slides": [...]}`.
   Slide layouts: `cover`, `section`, `bullets`, `image_left` / `image_right`, `full_image`, `quote`, `stats` (≤ 4 numbers), `chart`, `table`, `gallery`, `diagram`, `closing`. Each may carry `notes`.
   - Pictures: `{"stock": "2–4 plain English words"}` (free photos, credited in the notes) or a file_id / URL of a picture you made. Name the place or thing in the words ("Szeged city aerial", not "city aerial").
   - Charts: `chart_type` column | bar | line | pie | doughnut | area | scatter (with `x_title`, `y_title` and point names `[x, y, "name"]`) | waterfall | slope | band | lines | race — the last ones take the same object as a video chart.
   - Diagrams: `{"layout": "diagram", "diagram": {"type": "decision_tree" | "swimlane" | "network", ...}}` — native, editable shapes that build up click by click in the slideshow.
   - A video on a slide: make the video first (see the video skill, in the deck's colours: `accent`), then `video` `action="to_pptx"` with `deck_file_id` and `after_slide`.

3. **Read the `check`** in the result. It is the rendered check of both layouts (the built-in editor and PowerPoint).
   - `ok`: show the person `preview_url` and `edit_url`.
   - `needs_check` / `failed`: every finding names the slide and the shape id and carries a ready `office(app="deck", …)` fix. Run that fix — it changes only that shape and checks the deck again.

4. **Look at the slides yourself**: `validate_document` with `file_id` (and `pages` for slides to see large). Look at every picture: is it the right place, person or thing?

5. **Targeted changes**: `office` with `app="deck"`, `read="inspect"` (+ `slide`) lists the shapes with their ids and boxes — nothing is saved. Then change one shape: `{"command": "text.set", "params": {"on_slide": n, "id": <id>, "text": "…"}}`, or `edit.select` + `format.size` / `shape.setBounds`.

## Notes

- Numbers on slides are the user's; do not invent figures. If a number is an estimate, say so on the slide.
- Hand over `edit_url` — it opens the deck in the built-in PowerPoint-like editor for the final touches; `url` downloads the .pptx (it opens in PowerPoint and Keynote).
