"""Footnote marks survive the simple edit tools (11-09).

A Lexonomics study came back with "[1]" marks instead of footnotes; checking the Studio for the same gap found three
paths that lose real footnotes:
 1. find_replace / replace_heading wrote the text with python-docx's run.text setter, which clears the run, and put
    the paragraph's whole text into its first run: every footnote mark of a paragraph went with one replaced word;
 2. docx_track_replace_paragraph moved the mark's run into the deletion — an Accept took the note's mark away;
 3. docx_accept_all_revisions / docx_reject_all_revisions walked the body only: the changes inside a note stayed
    pending and the note of deleted text stayed in the file (LibreOffice then shows a neighbour's note).
"""
from __future__ import annotations

import asyncio
import re
import zipfile
from pathlib import Path

from test_editor_trial_fixes import _FN_CT, _FN_REL, _note, _notes, _paras, _ref, _rewrite, tracked_doc


def _doc_xml(path: Path) -> str:
    return zipfile.ZipFile(path).read("word/document.xml").decode()


def _study(path: Path) -> Path:
    """'A védő önálló[1] résztvevő, akit jogok illetnek meg.[2]' + a heading with a note on it [3]."""
    from docx import Document
    d = Document()
    d.add_heading("1. Bevezetés", 1)
    p = d.add_paragraph()
    p.add_run("A védő önálló")
    p.add_run(" résztvevő, akit jogok illetnek meg.")
    d.save(path)

    def doc(x: str) -> str:
        x = x.replace("<w:t>1. Bevezetés</w:t></w:r>", "<w:t>1. Bevezetés</w:t></w:r>" + _ref(3))
        x = x.replace("<w:t>A védő önálló</w:t></w:r>", "<w:t>A védő önálló</w:t></w:r>" + _ref(1))
        return x.replace("<w:t xml:space=\"preserve\"> résztvevő, akit jogok illetnek meg.</w:t></w:r>",
                         "<w:t xml:space=\"preserve\"> résztvevő, akit jogok illetnek meg.</w:t></w:r>" + _ref(2))
    notes = _notes(_note(1, '<w:r><w:t xml:space="preserve"> Be. 39. §.</w:t></w:r>'),
                   _note(2, '<w:r><w:t xml:space="preserve"> Be. 42. §.</w:t></w:r>'),
                   _note(3, '<w:r><w:t xml:space="preserve"> Bevezető jegyzet.</w:t></w:r>'))
    return _rewrite(path, doc, {"word/footnotes.xml": notes}, _FN_CT, _FN_REL)


def _text_with_marks(path: Path, i: int) -> str:
    p = re.findall(r"<w:p[ >].*?</w:p>", _doc_xml(path), re.S)[i]
    return "".join(f"[^{m.group(2)}]" if m.group(2) else m.group(1)
                   for m in re.finditer(r'<w:t[^>]*>([^<]*)</w:t>|footnoteReference w:id="(\d+)"', p))


def test_find_replace_keeps_the_footnote_marks_where_they_stand(tmp_path):
    from app.tools.edits import _docx_find_replace
    src = _study(tmp_path / "s.docx")
    assert _text_with_marks(src, 1) == "A védő önálló[^1] résztvevő, akit jogok illetnek meg.[^2]"
    n = _docx_find_replace(src, tmp_path / "a.docx", "jogok", "a törvényben írt jogok", False)
    assert n == 1
    assert _text_with_marks(tmp_path / "a.docx", 1) == "A védő önálló[^1] résztvevő, akit a törvényben írt jogok illetnek meg.[^2]"
    # a hit across a mark: the new text goes where the hit starts, the mark stays
    _docx_find_replace(src, tmp_path / "b.docx", r"önálló\s+résztvevő", "önálló jogállású résztvevő", True)
    assert _text_with_marks(tmp_path / "b.docx", 1) == "A védő önálló jogállású résztvevő[^1], akit jogok illetnek meg.[^2]"


def test_replace_heading_keeps_its_note(tmp_path):
    from app.tools.edits import _docx_replace_heading
    src = _study(tmp_path / "s.docx")
    _docx_replace_heading(src, tmp_path / "h.docx", "1. Bevezetés", "1. Előszó")
    assert _text_with_marks(tmp_path / "h.docx", 0) == "1. Előszó[^3]"


def test_a_tracked_replacement_keeps_the_mark_and_accept_all_covers_the_notes(tmp_path, put_file):
    from app.storage import output_path
    from app.tools import docx_revisions as R
    fid = put_file(tracked_doc(tmp_path / "t.docx"))
    out = asyncio.run(R.docx_track_replace_paragraph(fid, "Az IFR szerint a sűrűség nőtt.", "Az IFR szerint a robotsűrűség nőtt."))
    revised = output_path(out["file_id"])
    [(_, _, refs)] = [x for x in _paras(revised) if "robotsűrűség" in x[1]]
    assert refs == ["1"], "the note's mark is not in the deletion"
    assert re.search(r'<w:del [^>]*>(?:(?!</w:del>).)*footnoteReference w:id="1"', _doc_xml(revised), re.S) is None

    acc = asyncio.run(R.docx_accept_all_revisions(out["file_id"]))
    final = output_path(acc["file_id"])
    x = _doc_xml(final)
    assert "<w:ins " not in x and "<w:del " not in x and 'footnoteReference w:id="1"' in x and 'footnoteReference w:id="2"' not in x
    notes = zipfile.ZipFile(final).read("word/footnotes.xml").decode()
    assert "2025" in notes and "2023" not in notes and "<w:del " not in notes, "the change inside the note is accepted too"
    assert 'w:id="2"' not in notes, "the note of the deleted paragraph goes"
    assert acc["insertions_accepted"] >= 2 and acc["deletions_accepted"] >= 2


def test_reject_all_covers_the_notes(tmp_path, put_file):
    from app.storage import output_path
    from app.tools import docx_revisions as R
    fid = put_file(tracked_doc(tmp_path / "t.docx"))
    rej = asyncio.run(R.docx_reject_all_revisions(fid))
    final = output_path(rej["file_id"])
    notes = zipfile.ZipFile(final).read("word/footnotes.xml").decode()
    assert "2023" in notes and "2025" not in notes and "<w:ins " not in notes
    assert "Ez a régi MI-rendeletes mondat törlendő." in "".join(p[1] for p in _paras(final))
    assert rej["deletions_rejected"] >= 1


def test_nothing_to_accept_is_a_new_version_not_an_error(tmp_path, put_file):
    from app.tools import docx_revisions as R
    fid = put_file(_study(tmp_path / "s.docx"))
    out = asyncio.run(R.docx_accept_all_revisions(fid))
    assert out["changes"] == 0 and out["insertions_accepted"] == 0 and out["file_id"] != fid
