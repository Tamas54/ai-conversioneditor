"""Tördelési javítás az ember döntésével (10-22). Kommandant: „TELJESEN AUTOMATIKUS rendszer nem képzelhető el” és
„néha van »főnöki kérés«, amikor az ember a logikus tördelés HELYETT utasításra cselekszik — a rendszernek ezt
intelligensen el kell fogadnia”. Javaslat (semmi nem változik) → jóváhagyott javaslatok végrehajtása, újratördelve
→ az erősebb lépés újra csak javaslat; „így marad” döntés, amit a későbbi változatok is tudnak; a fájlban kifejezetten
kikapcsolt beállítás szándék."""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest


def _doc(path: Path, heading_keep_off: bool = False) -> Path:
    from docx import Document
    d = Document()
    d.styles["Heading 1"].paragraph_format.keep_with_next = False   # egyedi sablon: a címstílus nem tart a szöveggel
    h = d.add_heading("1. Bevezetés", 1)                      # 0
    if heading_keep_off:
        h.paragraph_format.keep_with_next = False             # kifejezetten kikapcsolva (w:keepNext w:val="0")
    d.add_paragraph("Szöveg.")                               # 1
    d.add_paragraph("")                                      # 2 (üres)
    d.add_paragraph("1. ábra: a kép aláírása")                # 3
    t = d.add_table(rows=3, cols=2)                          # 4
    for r in range(3):
        for c in range(2):
            t.cell(r, c).text = f"{r}{c}"
    d.save(path)
    return path


def _blocks(path: Path) -> dict:
    from app.tools import validate as v
    return {b.pos: b for b in v.read_docx(path)[0]}


def _ppr(path: Path, pos: int) -> set[str]:
    from docx import Document
    from app.tools.validate import _W, _body
    el = list(_body(Document(str(path)).element.body))[pos]
    ppr = el.find(_W + "pPr")
    return {c.tag.rsplit("}", 1)[1] for c in ppr} if ppr is not None else set()


ISSUES = [{"id": "E1", "kind": "orphan_heading", "_pos": 0, "message": "Heading … is the last line", "page": 1, "object": "heading"},
          {"id": "E1", "kind": "orphan_heading", "_pos": 0, "message": "same in the other view", "view": "word"},
          {"id": "W1", "kind": "caption_split", "_pos": 3, "message": "The picture is on page 1, its caption on page 2."},
          {"id": "E2", "kind": "table_row_split", "_pos": 4, "row": 2, "message": "Row 2 splits."},
          {"id": "W2", "kind": "table_header_not_repeated", "_pos": 4, "row": 1, "message": "no header"},
          {"id": "W3", "kind": "blank_page", "_pos": None, "message": "Page 3 is empty."}]


def test_proposals_change_nothing_and_only_approved_ones_are_applied(tmp_path):
    from docx import Document
    from app.tools import layout_fix
    from app.tools.validate import _W, _body
    src = _doc(tmp_path / "a.docx")
    plan = layout_fix._plan(ISSUES, _blocks(src))
    assert [(p["id"], p["issue"], p["_how"]) for p in plan] == [("F1", "E1", "keep"), ("F2", "W1", "keep"),
                                                                  ("F3", "E2", "row"), ("F4", "W2", "header")]
    assert plan[1]["_target"] == 1                            # a kép UTÁNI aláírás: az előtte álló tartalmat tartjuk vele
    assert set(layout_fix.public(plan[0])) == {"id", "issue", "kind", "object", "page", "change"}
    out = tmp_path / "b.docx"
    done = layout_fix._apply(src, out, [p for p in plan if p["id"] in ("F1", "F4")])     # csak a jóváhagyottak
    assert [d["id"] for d in done] == ["F1", "F4"] and {"keepNext", "keepLines"} <= _ppr(out, 0)
    assert "keepNext" not in _ppr(out, 1)
    tbl = list(_body(Document(str(out)).element.body))[4]
    rows = tbl.findall(_W + "tr")
    assert rows[0].find(_W + "trPr").find(_W + "tblHeader") is not None
    assert rows[1].find(_W + "trPr") is None or rows[1].find(_W + "trPr").find(_W + "cantSplit") is None
    # ha már rajta van az „együtt a következővel”, és mégis szétesik: az erősebb lépés — de az is javaslat
    again = layout_fix._plan(ISSUES[:1], _blocks(out))
    assert again[0]["_how"] == "break" and "page break" in again[0]["change"]
    tall = [{"id": "E3", "kind": "table_row_split", "_pos": 4, "row": 2, "message": "x"}]
    out2 = tmp_path / "c.docx"
    layout_fix._apply(out, out2, [p for p in layout_fix._plan(tall, _blocks(out))])
    assert layout_fix._plan(tall, _blocks(out2)) == []        # a sor már nem törhet — a lapnál magasabb: az emberé


def test_what_is_set_on_purpose_and_what_was_asked_is_accepted(tmp_path):
    from app.tools import validate as v
    src = _doc(tmp_path / "off.docx", heading_keep_off=True)
    b = _blocks(src)
    assert b[0].keep_off and not _blocks(_doc(tmp_path / "on.docx"))[0].keep_off
    it = {"id": "W9", "kind": "orphan_heading", "_pos": 0, "on_purpose": "keep with next switched off on this heading"}
    active, kept = v.split_kept([it, ISSUES[3]], list(b.values()), [])
    assert active == [ISSUES[3]] and kept[0]["kept"].startswith("keep with next switched off")
    asked = v.asked_kinds({"commands": [{"command": "edit.goto", "params": {"paragraph": 3}},
                                        {"command": "para.set", "params": {"props": {"keepNext": False}}}]})
    assert asked == {"orphan_heading", "caption_split"}
    assert v.asked_kinds({"commands": ['table.properties={"allowBreak": true}']}) == {"table_row_split"}


def test_a_kept_decision_is_remembered_across_versions(tmp_path, put_file):
    from app.storage import write_meta
    from app.tools import layout_fix
    from app.tools import validate as v
    src = _doc(tmp_path / "k.docx")
    parent = put_file(src)
    child = put_file(src)
    write_meta(child, source_file_id=parent)
    blocks = _blocks(src)
    sig = v.issue_sig(ISSUES[0], blocks)
    write_meta(parent, extra={"layout_keep": [{"sig": sig, "kind": "orphan_heading", "reason": "a főnök így kérte"}]})
    assert layout_fix.keeps_for(child)[0]["reason"] == "a főnök így kérte"                 # a későbbi változat is tudja
    active, kept = v.split_kept(ISSUES[:3], list(blocks.values()), layout_fix.keeps_for(child))
    assert [i["kind"] for i in active] == ["caption_split"] and {k["kept"] for k in kept} == {"a főnök így kérte"}


def _engines_ok() -> bool:
    from app.tools import wordcraft
    return wordcraft.available() and bool(shutil.which("soffice"))


def _orphan_doc(path: Path) -> Path:
    """Mért elrendezés (10-22): 50 rövid sor után a cím a Word-nézet (LibreOffice) 2. oldalának alján marad; a 45 soros
    táblázat fejléce nem ismétlődik."""
    from docx import Document
    d = Document()
    d.styles["Heading 1"].paragraph_format.keep_with_next = False
    for i in range(50):
        d.add_paragraph(f"Sor {i + 1}: rövid bekezdés.")
    d.add_heading("2. Következtetések", 1)
    for _ in range(5):
        d.add_paragraph("A fejezet szövege. " * 8)
    t = d.add_table(rows=45, cols=2)
    for r in range(45):
        t.cell(r, 0).text, t.cell(r, 1).text = ("Év", "Érték") if r == 0 else (str(1980 + r), f"{r * 1.5:.1f}")
    d.save(path)
    return path


@pytest.mark.engines
@pytest.mark.skipif(not _engines_ok(), reason="WordCraft and LibreOffice needed")
async def test_propose_ask_apply_keep(tmp_path, put_file):
    from app.mcp_server import validate_document
    from app.storage import read_meta
    fid = put_file(_orphan_doc(tmp_path / "tanulmany.docx"))
    first = (await validate_document(file_id=fid, fix=True)).structuredContent
    props = {p["kind"]: p for p in first["proposals"]}
    assert {"orphan_heading", "table_header_not_repeated"} <= set(props) and first["file_id"] == fid
    assert "Nothing changed yet" in first["fix_note"] and "ASK the person" in first["fix_note"]
    head = props["table_header_not_repeated"]["id"]
    second = (await validate_document(file_id=fid, fix=[head])).structuredContent      # csak a fejlécet hagyta jóvá
    new = second["file_id"]
    assert new != fid and second["source_file_id"] == fid and [f["kind"] for f in second["fixed"]] == ["table_header_not_repeated"]
    orphan = next(i for i in second["issues"] if i["kind"] == "orphan_heading")              # a cím-javaslatot nem kérte
    third = (await validate_document(file_id=new, keep=[orphan["id"]], reason="a főnök így kérte")).structuredContent
    assert not [i for i in third["issues"] if i["kind"] == "orphan_heading"]
    assert third["kept_as_is"][0]["kept"] == "a főnök így kérte" and read_meta(new)["layout_keep"][0]["kind"] == "orphan_heading"
