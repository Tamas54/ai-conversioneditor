---
name: studio-scan-book
description: Turn photographed or scanned pages of an old book, letter or document into an editable Word file with Studio, marking doubtful readings. Use when the user uploads page photos or a scanned PDF and asks for the text, a Word file, a searchable PDF or a new typeset edition.
---

# Studio scan to document

## Workflow

1. **Upload the pages in order** (photos or a scanned PDF) → file_ids.
2. **Start** — `digitize` with `file_ids`, `language` (e.g. "Hungarian"), `outputs` from docx | pdf (a new typeset book) | searchable_pdf | review_pdf (page image beside its transcript, doubtful readings highlighted) | txt | md. For a long book try `max_pages` first (a sample chapter).
3. **Wait** — call `digitize` with `job_id` until `done`.
4. **Report the doubtful readings** — the result's `readings` lists every uncertain or illegible word with its page and context. They are highlighted (with a comment) in the Word file and never guessed silently; offer the `review_pdf` for checking a historical source.

## Notes

- Old spelling is kept; running heads and page numbers are dropped; illustrations are cut out and placed where they were.
- Pages already read are cached, so a rerun is cheap.
