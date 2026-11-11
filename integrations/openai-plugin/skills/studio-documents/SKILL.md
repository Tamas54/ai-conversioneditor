---
name: studio-documents
description: Write and edit Word documents precisely with Studio — new documents, tracked changes, review comments, tables of contents, find-and-replace, and checking the layout. Use when the user asks to create, revise, proofread or answer comments in a .docx (or odt/rtf).
---

# Studio documents

Edit exactly what was asked; every edit makes a new version and the original stays.

## Workflow

1. **New document** — write it in Markdown and `convert` to `docx` (headings, lists and tables carry over), or build it with `wordcraft` commands.
2. **Small, exact edits** — `find_replace`, `insert_after`, `replace_heading`, `delete_section`; for an edit in words, `edit_with_instruction`.
3. **Word's own tools** — `wordcraft` runs Word's commands headless (styles, tables, track changes, comments, TOC, footnotes). `read="commands"` with `query` lists them; read-only commands (document.inspect, edit.find…) save nothing.
4. **Review comments** — `wordcraft` `read="comments"` lists the open threads; agree the fixes with the user, then apply all in one call with `address=[{"comment": id, "replace": {"find": …, "with": …}, "reply": "Fixed: …"}]` and `author` = the user's own name: fixes become tracked changes under that name.
5. **Check before handing over** — `validate_document` with the file_id: page pictures plus a machine-checked list (empty list items, broken numbering, split tables, headings at the bottom of a page, things off the page). Each issue has a `fix`; fix every error and check again. `content=true` also reads the text for contradictions.

## Notes

- Give the user `edit_url` (opens the Word-like editor) or `url` (download).
- For facts against sources use `facts` `action="check"`; never change the meaning of what the user wrote without asking.
