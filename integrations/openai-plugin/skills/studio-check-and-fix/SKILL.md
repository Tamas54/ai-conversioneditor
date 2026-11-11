---
name: studio-check-and-fix
description: Check a Studio file before saying it is done, and fix only what is wrong. Use after making or editing any deck, document, PDF or video with Studio, or when the user says something looks wrong.
---

# Check, then fix one thing

A file that saved is not proof that it looks right. Every Studio result carries a `check`: {status ok | needs_check | failed, findings, verified}.

## Workflow

1. **Read the `check`** of every result. `findings` say exactly what is wrong; `verified` says what was confirmed.
2. **See it** — `validate_document` (decks, documents, PDFs) returns the pages as pictures with the problems marked; for videos use `files` `action="preview"` (`scene`, `frames`).
3. **Fix one thing** — use the ready fix in the finding:
   - Decks: `office(app="deck", read="inspect", slide=n)` lists shape ids; then `text.set` / `format.size` / `shape.setBounds` with `on_slide` and the id. Reading (`inspect`, `*.get`, `*.list`) never makes a new file.
   - Documents: the finding's `block` is the paragraph; `edit.goto {"paragraph": block + 1}` puts the caret there.
   - Videos: `video` `action="edit"` with only the scene or layer that changes.
4. **Check again** — the edit's own `check` re-renders; repeat until it is ok, then tell the user what you checked.

## Notes

- Do not rebuild a whole file to change one shape, sentence or scene.
- If a picture is wrong (wrong place or person, text or watermark in it), replace that picture only.
- `studio_ai` in a result shows how many of Studio's own AI steps a call used; prefer giving your own image words, pictures and plans — they cost nothing.
