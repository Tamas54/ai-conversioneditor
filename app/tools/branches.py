"""Ágak és összefésülés (10-14): ugyanabból a változatból több irány („A: sales fókusz”, „B: szakmai fókusz”), és a
kettő összefésülése GONDOLATONKÉNT — nem soronként.

    tree(file_id)          — a változatfa (a gyökértől minden leszármazott változat), ág-címkékkel és ág-fejekkel
    set_branch(file_id, n) — egy változat ág-neve (a felületen és az MCP-n)
    plan(a, b)             — a két változat eltérő részei egységekre bontva: ahol a bekezdések párosíthatók (difflib), ott
                             bekezdésenként; ahol egy egész szakasz más (átírt ág), ott a modell párosítja a gondolatokat
                             (melyik A-bekezdés melyik B-bekezdésnek felel meg) → egységek {topic, a[], b[]}
    merge(a, b, choices)   — az A változatra épül: egységenként „a” (marad), „b” (B szövege), „both” (A, utána B),
                             „merge” (a modell a kettőből egy szöveget ír, az utasítás szerint) — követett változásként,
                             új változatban; a szerkesztőben elfogadható/elutasítható. Utasítás egyedül is elég: akkor a
                             modell választ egységenként („tartsd meg az A felütését, de fűzd hozzá a B adatos érvelését”).
A képek, táblázatok szerkezete az A-ból marad; a B-ből a bekezdések szövege (a stílusával) jön át.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
from pathlib import Path
from typing import Any, Optional

from app.config import settings
from app.tools.docx_marks import Docx, w

log = logging.getLogger("aice.branches")


def _items() -> list[dict[str, Any]]:
    from app import tenancy
    from app.routes.dashboard import _list_dir
    root = tenancy.user_root()
    return _list_dir(root / "inputs", "input") + _list_dir(root / "outputs", "output")


def tree(file_id: str) -> dict[str, Any]:
    from app.storage.metadata import read_meta
    from app.tools.revise import _version_step
    items = _items()
    by_id = {it["file_id"]: it for it in items}
    if file_id not in by_id:
        raise FileNotFoundError(file_id)
    kids: dict[str, list[str]] = {}
    for it in items:
        if it.get("source_file_id") and _version_step(it.get("operation") or ""):
            kids.setdefault(it["source_file_id"], []).append(it["file_id"])
    cur = by_id[file_id]
    seen = set()
    while cur["file_id"] not in seen and _version_step(cur.get("operation") or "") and cur.get("source_file_id") in by_id:
        seen.add(cur["file_id"])
        cur = by_id[cur["source_file_id"]]
    root = cur["file_id"]
    nodes, stack = [], [(root, None, 0)]
    while stack:
        fid, parent, depth = stack.pop()
        it = by_id[fid]
        m = read_meta(fid) or {}
        ch = sorted(kids.get(fid, []), key=lambda x: by_id[x]["mtime"])
        nodes.append({"file_id": fid, "parent": parent, "depth": depth, "label": it["label"], "mtime": it["mtime"],
                      "step": _version_step(it.get("operation") or "") if parent else None,
                      "instruction": m.get("instruction"), "branch": m.get("branch"), "merged_from": m.get("merged_from"),
                      "children": ch})
        for c in reversed(ch):
            stack.append((c, fid, depth + 1))
    heads = [n for n in nodes if not n["children"]]
    # ág-név: a kimondott (meta.branch), különben a fej felé vezető úton a legközelebbi kimondott; végső esetben betű
    by_node = {n["file_id"]: n for n in nodes}
    letters = iter("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    for h in sorted(heads, key=lambda n: n["mtime"]):
        x, name = h, None
        while x and not name:
            name = x.get("branch")
            x = by_node.get(x["parent"]) if x["parent"] else None
        h["branch_name"] = name or (f"Branch {next(letters)}" if len(heads) > 1 else "Main")
    return {"root": root, "nodes": nodes, "heads": [{"file_id": h["file_id"], "branch": h["branch_name"], "label": h["label"],
                                                     "mtime": h["mtime"]} for h in heads]}


def set_branch(file_id: str, name: str) -> dict[str, Any]:
    from app.storage import write_meta
    from app.storage.metadata import read_meta
    m = read_meta(file_id)
    if m is None:
        raise FileNotFoundError(file_id)
    write_meta(file_id, extra={"branch": (name or "").strip()[:60] or None})
    return {"file_id": file_id, "branch": (name or "").strip()[:60] or None}


# ---------- összevetés: egységek ----------

def _norm(t: str) -> str:
    return " ".join(t.lower().split())


_GROUP_PROMPT = """Two versions of the same part of a document differ. Pair up their paragraphs by IDEA: which paragraph(s)
of version A say the same thing as which paragraph(s) of version B (reworded, shortened, extended, in another tone).
Every paragraph number appears in exactly one group. A group may have paragraphs only from A or only from B when the
other version has nothing on that idea. Keep version A's order; put B-only ideas where they fit best.
Give each group a short topic (2-6 words, in the document's language).
Version A:
{a}
Version B:
{b}
Reply with JSON only: {{"groups": [{{"topic": "…", "a": [numbers], "b": [numbers]}}]}}"""


async def _ask(prompt: str) -> dict[str, Any]:
    from app.tools.content_check import _ask as ask
    return await ask(prompt)


async def _group(a_idx: list[int], b_idx: list[int], ta: list[str], tb: list[str]) -> list[dict[str, Any]]:
    """Egy eltérő szakasz gondolat-párosítása (kis szakasz: egy egység, modell nélkül)."""
    if len(a_idx) <= 1 and len(b_idx) <= 1 or not a_idx or not b_idx:
        return [{"a": a_idx, "b": b_idx, "topic": None}]
    try:
        got = await _ask(_GROUP_PROMPT.format(a="\n".join(f"[{k}] {' '.join(ta[i].split())[:1200]}" for k, i in enumerate(a_idx)),
                                              b="\n".join(f"[{k}] {' '.join(tb[i].split())[:1200]}" for k, i in enumerate(b_idx))))
    except Exception as e:
        log.warning("gondolat-párosítás: %s", e)
        return [{"a": a_idx, "b": b_idx, "topic": None}]
    out, used_a, used_b = [], set(), set()
    for g in got.get("groups") or []:
        ga = [a_idx[k] for k in g.get("a") or [] if isinstance(k, int) and 0 <= k < len(a_idx) and a_idx[k] not in used_a]
        gb = [b_idx[k] for k in g.get("b") or [] if isinstance(k, int) and 0 <= k < len(b_idx) and b_idx[k] not in used_b]
        if not ga and not gb:
            continue
        used_a.update(ga), used_b.update(gb)
        out.append({"a": sorted(ga), "b": sorted(gb), "topic": str(g.get("topic") or "")[:60] or None})
    left_a, left_b = [i for i in a_idx if i not in used_a], [i for i in b_idx if i not in used_b]
    if left_a or left_b:                                  # amit a modell kihagyott, nem vész el
        out.append({"a": left_a, "b": left_b, "topic": None})
    # A sorrendje; a csak-B csoport a modell által elé tett A-csoport után marad
    key, last = [], -1.0
    for g in out:
        last = float(min(g["a"])) if g["a"] else last + 0.001
        key.append(last)
    return [g for _, g in sorted(zip(key, out), key=lambda x: x[0])]


async def plan(a_id: str, b_id: str) -> dict[str, Any]:
    from app.tools import crafts
    pa, pb = crafts.existing(a_id), crafts.existing(b_id)
    if pa.suffix.lower() != ".docx" or pb.suffix.lower() != ".docx":
        raise ValueError("Branches can be merged for Word documents (.docx).")
    da, db_ = await asyncio.to_thread(Docx, pa), await asyncio.to_thread(Docx, pb)
    ta, tb = da.texts(), db_.texts()
    ia = [i for i, t in enumerate(ta) if t.strip()]
    ib = [i for i, t in enumerate(tb) if t.strip()]
    sm = difflib.SequenceMatcher(a=[_norm(ta[i]) for i in ia], b=[_norm(tb[i]) for i in ib], autojunk=False)
    blocks, same = [], 0
    prev_a: Optional[int] = None                      # az eltérés előtti utolsó közös A-bekezdés (a csak-B beszúrás helye)
    for op, a1, a2, b1, b2 in sm.get_opcodes():
        if op == "equal":
            same += a2 - a1
            prev_a = ia[a2 - 1]
            continue
        blocks.append((ia[a1:a2], ib[b1:b2], prev_a))
        if a2 > a1:
            prev_a = ia[a2 - 1]
    groups = await asyncio.gather(*(_group(a, b, ta, tb) for a, b, _ in blocks))
    units, k = [], 0
    for (a_all, b_all, anchor), gs in zip(blocks, groups):
        last_a = anchor
        for g in gs:
            k += 1
            units.append({"id": f"U{k}", "topic": g["topic"], "after": last_a,
                          "a": [{"i": i, "text": ta[i]} for i in g["a"]], "b": [{"i": i, "text": tb[i]} for i in g["b"]],
                          "kind": "changed" if g["a"] and g["b"] else "only_a" if g["a"] else "only_b"})
            if g["a"]:
                last_a = g["a"][-1]
    return {"a": a_id, "b": b_id, "units": units, "same": same,
            "summary": f"{len(units)} place{'s' if len(units) != 1 else ''} where the two differ, {same} paragraph{'s' if same != 1 else ''} the same."}


# ---------- összefésülés ----------

_CHOOSE_PROMPT = """Two branches of a document are being merged. The user said how: "{instruction}".
For each place where they differ, choose: "a" (keep A's text), "b" (take B's), "both" (A, then B's added after it), or
"merge" (write one text from both). For "merge", write the merged text yourself — in the document's language, keeping
the facts and numbers of the sources exactly (never invent any), as many paragraphs as needed (a list of strings).
{units}
Reply with JSON only: {{"choices": [{{"id": "U1", "choice": "a|b|both|merge", "text": ["…"] or null}}]}}"""

_MERGE_PROMPT = """Merge two versions of the same part of a document into one text{how}. Keep every fact and number exactly as
the versions state them (never invent any), in the document's language and style. Write as many paragraphs as needed.
Version A:
{a}
Version B:
{b}
Reply with JSON only: {{"paragraphs": ["…"]}}"""


def _unit_block(u: dict[str, Any]) -> str:
    a = "\n".join(f"    A: {' '.join(x['text'].split())[:1500]}" for x in u["a"]) or "    A: (nothing here)"
    b = "\n".join(f"    B: {' '.join(x['text'].split())[:1500]}" for x in u["b"]) or "    B: (nothing here)"
    return f"{u['id']}{' — ' + u['topic'] if u.get('topic') else ''}\n{a}\n{b}"


async def merge(a_id: str, b_id: str, choices: Optional[dict[str, str]] = None, instruction: str = "",
                progress=None) -> dict[str, Any]:
    from app.storage import display_label, output_path, public_url, write_meta
    from app.storage.files import new_file_id
    from app.tools import crafts
    from app.tools.revise import history
    say = progress or (lambda *_: None)
    say(0, 3, "comparing the branches")
    pl = await plan(a_id, b_id)
    units = pl["units"]
    if not units:
        raise ValueError("The two versions have the same text — there's nothing to merge.")
    choices = {k: v for k, v in (choices or {}).items() if v in ("a", "b", "both", "merge")}
    texts: dict[str, list[str]] = {}
    instruction = (instruction or "").strip()
    if instruction and len(choices) < len(units):            # amit a felhasználó nem választott ki, azt az utasítás dönti el
        say(1, 3, "deciding place by place")
        todo = [u for u in units if u["id"] not in choices]
        got = await _ask(_CHOOSE_PROMPT.format(instruction=instruction[:1500], units="\n\n".join(_unit_block(u) for u in todo)))
        for c in got.get("choices") or []:
            if isinstance(c, dict) and c.get("id") in {u["id"] for u in todo} and c.get("choice") in ("a", "b", "both", "merge"):
                choices[c["id"]] = c["choice"]
                if c["choice"] == "merge" and isinstance(c.get("text"), list) and c["text"]:
                    texts[c["id"]] = [str(x) for x in c["text"] if str(x).strip()]
    for u in units:
        choices.setdefault(u["id"], "a")
    need = [u for u in units if choices[u["id"]] == "merge" and u["id"] not in texts]
    if need:
        say(2, 3, "writing the merged parts")
        how = f', following this: "{instruction[:600]}"' if instruction else ""

        async def one(u):
            try:
                r = await _ask(_MERGE_PROMPT.format(how=how, a="\n".join(x["text"] for x in u["a"]) or "(nothing)",
                                                    b="\n".join(x["text"] for x in u["b"]) or "(nothing)"))
                return [str(x) for x in r.get("paragraphs") or [] if str(x).strip()]
            except Exception as e:
                log.warning("összefésülés (%s): %s", u["id"], e)
                return []
        for u, t in zip(need, await asyncio.gather(*(one(u) for u in need))):
            if t:
                texts[u["id"]] = t
            else:
                choices[u["id"]] = "both"                     # a modell nem írt: mindkettő marad, a felhasználó dönt

    def work() -> str:
        da = Docx(crafts.existing(a_id), author=f"{settings.PRODUCT_NAME} merge")
        db_ = Docx(crafts.existing(b_id))
        pa, pb = da.paragraphs(), db_.paragraphs()
        a_el = {i: pa[i] for i in range(len(pa))}            # az eredeti A-bekezdések (a beszúrások nem tolják el)
        tail: dict[int, Any] = {}                             # horgony → az utána utoljára beszúrt bekezdés (sorrend!)
        for u in units:
            ch = choices[u["id"]]
            if ch == "a":
                continue
            new_paras = ([(x["text"], pb[x["i"]]) for x in u["b"]] if ch in ("b", "both")
                         else [(t, (pb[u["b"][0]["i"]] if u["b"] else a_el[u["a"][0]["i"]])) for t in texts.get(u["id"], [])])
            if u["a"]:
                key = u["a"][-1]["i"]
            elif u.get("after") is not None:
                key = u["after"]
            else:
                key = -1                                    # a dokumentum legeleje
            anchor = tail.get(key, a_el.get(key))
            if ch in ("b", "merge"):
                for x in u["a"]:
                    da.delete_paragraph(a_el[x["i"]])
            for text, like in new_paras:
                if anchor is None:                          # a legelejére: az első bekezdés elé
                    first = a_el[0]
                    anchor = da.insert_paragraph_after(first, text, like=like)
                    first.addprevious(anchor)
                else:
                    anchor = da.insert_paragraph_after(anchor, text, like=like)
                tail[key] = anchor
        out = new_file_id("docx")
        da.save(output_path(out))
        return out
    out_id = await asyncio.to_thread(work)
    n = {k: sum(1 for v in choices.values() if v == k) for k in ("a", "b", "both", "merge")}
    summary = ("Merged as tracked changes: " + ", ".join(x for x in (
        f"{n['b']} place{'s' if n['b'] != 1 else ''} from {display_label(b_id).rsplit('.', 1)[0]}" if n["b"] else "",
        f"{n['both']} with both" if n["both"] else "", f"{n['merge']} written from both" if n["merge"] else "",
        f"{n['a']} kept as they were" if n["a"] else "") if x) + ".")
    stem = Path(display_label(a_id)).stem.split(" (")[0]
    label = f"{stem} (v{len(history(a_id)) + 1}).docx"
    write_meta(out_id, source_file_id=a_id, operation="merge", label=label,
               extra={"instruction": instruction or "Merge the branches", "summary": summary, "merged_from": b_id})
    return {"file_id": out_id, "url": public_url(out_id), "label": label, "summary": summary, "choices": choices}
