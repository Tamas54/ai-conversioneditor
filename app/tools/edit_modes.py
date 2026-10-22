"""Szerkesztési módok határokkal (10-22) — a szerkesztői intelligencia első köre.

Kommandant: „a szerkesztői intelligenciát mindenképpen »korlátok« között tartanám. Rá kell kérdeznie a rendszernek,
»mit akar a költő« — az ember vállalja a felelősséget a rendszer termékéért, tehát TELJESEN AUTOMATIKUS rendszer nem
képzelhető el.” Ezért két lépés:

  1. brief (AI nélkül, ingyen): a dokumentum adatai + a kérdések a módhoz, mindegyikhez javasolt alapértékkel — cél és
     közönség, határ (legfeljebb hány %-kal rövidebb / hosszabb), mi marad érintetlen (táblázatok, számok, az
     álláspont, a megnevezett szakszavak), szabad-e új gondolat, melyik rész. Semmi nem változik.
  2. propose (a válaszokkal): bekezdésenként az AI javaslata (kötegenként egy Studio AI-lépés) → GÉPI ŐRÖK (a számok és
     a megnevezett szavak megmaradnak, nyelvi javításnál csak kis változás, a határ tartása, az új gondolat csak ha
     szabad) → KÖVETETT VÁLTOZÁS a szerző nevével + megjegyzés, miért; az új gondolat külön megjegyzéssel („vállalod?”).
     Új változat — SEMMI nincs elfogadva: az ember dönt változásonként (Word, a szerkesztő, wordcraft address accept /
     reject). A megőrzési jelentés megmondja, mi hogyan változott és mit tartottunk vissza.

Amihez nem nyúlunk: címsor, táblázat (ha meg kell maradnia), lábjegyzet- / végjegyzet-hivatkozást, mezőt, képet vagy
függő követett változást tartalmazó bekezdés — a jelentés felsorolja, hogy az ember tudja.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.config import settings

log = logging.getLogger("aice.edit_modes")

VER = "edit-modes-v1"
MODES = {
    "proofread": "language only — spelling, grammar, punctuation, typos; no rewording, no change of content",
    "edit": "substantive editing — clearer sentences, less repetition, better flow inside each paragraph; the content, "
            "the facts and the author's position stay",
    "expand": "expansion — develop what is thin with explanation or examples from the text's own line of thought",
    "shorten": "shortening — cut repetition, filler and side remarks, tighten sentences",
    "rewrite": "full rework — tighter, clearer prose, sentences rebuilt where needed; the content and position stay",
}
_ALIASES = {"nyelvi": "proofread", "proofreading": "proofread", "copyedit": "proofread", "lektor": "proofread",
            "érdemi": "edit", "substantive": "edit", "bővítés": "expand", "rövidítés": "shorten", "shorter": "shorten",
            "átdolgozás": "rewrite", "rework": "rewrite"}
BATCH_CHARS = 6000
PROOF_MAX_CHANGE = 0.25          # nyelvi javításnál a bekezdés szavainak legfeljebb ennyi része változhat


def mode_of(raw: Any) -> str:
    m = str(raw or "").strip().lower()
    m = _ALIASES.get(m, m)
    if m not in MODES:
        raise ValueError(f"mode: {' | '.join(MODES)} — " + "; ".join(f"{k}: {v.split(' — ')[0]}" for k, v in MODES.items()))
    return m


# ---------- a dokumentum ----------

def _style_names(pkg) -> dict[str, tuple[str, Optional[int]]]:
    from app.tools.review_loop import q
    st = pkg.xml("word/styles.xml")
    out: dict[str, tuple[str, Optional[int]]] = {}
    if st is None:
        return out
    for s in st.iter(q("style")):
        name = s.find(q("name"))
        ol = s.find(f"{q('pPr')}/{q('outlineLvl')}")
        out[s.get(q("styleId")) or ""] = ((name.get(q("val")) if name is not None else "").lower(),
                                          int(ol.get(q("val"))) if ol is not None and (ol.get(q("val")) or "").isdigit() else None)
    return out


def _paragraphs(pkg) -> list[dict[str, Any]]:
    """A bekezdések a review_loop számozásában (= edit.goto paragraph): szöveg, címsor-e, táblában van-e, miért nem
    nyúlhatunk hozzá, melyik fejezetben van."""
    from app.tools import review_loop as L
    q = L.q
    styles = _style_names(pkg)
    scan = L._scan(L._body(pkg))
    out, section = [], ""
    for i, (p, text) in enumerate(zip(scan["paras"], scan["texts"])):
        ppr = p.find(q("pPr"))
        sid = ppr.find(q("pStyle")).get(q("val")) if ppr is not None and ppr.find(q("pStyle")) is not None else ""
        name, level = styles.get(sid, (sid.lower(), None))
        own_ol = ppr.find(q("outlineLvl")) if ppr is not None else None
        heading = bool(re.match(r"(heading|címsor|cimsor|title|cím)\s*\d*$", name)) or level is not None or own_ol is not None
        in_table = any(a.tag == q("tc") for a in p.iterancestors())
        busy = None
        if any(True for _ in p.iter(q("footnoteReference"), q("endnoteReference"))):
            busy = "it has a footnote / endnote mark"
        elif any(True for _ in p.iter(q("fldChar"), q("fldSimple"), q("instrText"))):
            busy = "it has a field (page number, reference…)"
        elif any(True for _ in p.iter(q("drawing"), q("object"), q("pict"))):
            busy = "it has a picture or object"
        elif any(True for _ in p.iter(*L._TRACKED)):
            busy = "it has tracked changes waiting for a decision"
        if heading and text.strip():
            section = text.strip()
        out.append({"n": i + 1, "text": text, "heading": heading, "table": in_table, "busy": busy, "section": section})
    return out


def _words(t: str) -> list[str]:
    return re.findall(r"[^\W_][\w'’-]*", t or "")


def _terms(text: str, n: int = 8) -> list[str]:
    """Megőrzendő szavak jelöltjei: rövidítések (KSH, EU) és a többször előforduló nagybetűs nevek / kifejezések."""
    from collections import Counter
    cand: Counter = Counter()
    for m in re.finditer(r"\b[A-ZÁÉÍÓÖŐÚÜŰ]{2,}[0-9]*\b", text):
        cand[m.group()] += 2
    for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b[A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+(?:\s+[A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+)*", text):
        cand[m.group()] += 1
    return [t for t, c in cand.most_common(n * 2) if c >= 2][:n]


# ---------- 1. brief: a kérdések ----------

async def brief(file_id: str, mode: str, instruction: str = "") -> dict[str, Any]:
    from app.tools import narration
    from app.tools.review_loop import _load
    m = mode_of(mode)
    pkg = _load(file_id)
    paras = await asyncio.to_thread(_paragraphs, pkg)
    body = " ".join(p["text"] for p in paras)
    words = len(_words(body))
    heads = [p["text"].strip() for p in paras if p["heading"] and p["text"].strip()]
    n_tables = _count_tables(pkg)
    lang = narration.detect(body[:4000]) or "en"
    q: list[dict[str, Any]] = [
        {"id": "goal", "question": "What is the text for, and who will read it?", "default": "as it is now"},
    ]
    if m == "shorten":
        q.append({"id": "limit", "question": "At most how much shorter (percent of the words)?", "default": 10,
                  "options": [5, 10, 20, 30]})
    elif m == "expand":
        q.append({"id": "limit", "question": "At most how much longer (percent)?", "default": 20, "options": [10, 20, 30, 50]})
    elif m == "rewrite":
        q.append({"id": "limit", "question": "How much may the length change (percent, either way)?", "default": 15,
                  "options": [5, 15, 30]})
    q += [
        {"id": "keep_tables", "question": "Must every table stay exactly as it is?", "default": True},
        {"id": "keep_numbers", "question": "Must every number, date and quotation stay exactly?", "default": True},
        {"id": "keep_stance", "question": "Must the author's position and conclusions stay (nothing new concluded)?",
         "default": True},
        {"id": "keep_terms", "question": "Words and names that must not change:", "default": _terms(body)},
        {"id": "new_ideas", "question": "May new thoughts be added? Each one is marked for the author to accept or drop.",
         "default": m in ("expand", "rewrite"), "options": [False, True]},
        {"id": "scope", "question": "Which part? (\"all\", or headings)", "default": "all", "options": ["all"] + heads[:30]},
        {"id": "author", "question": "Name on the tracked changes and comments:", "default": f"Studio ({m})"},
    ]
    return {"mode": m, "what": MODES[m], "instruction": instruction or None,
            "document": {"words": words, "paragraphs": sum(1 for p in paras if p["text"].strip()), "headings": len(heads),
                         "tables": n_tables, "language": lang,
                         "untouchable": sum(1 for p in paras if p["busy"] and p["text"].strip())},
            "questions": q,
            "how": ("NOTHING CHANGED. Ask the person these questions (the defaults are suggestions), then call again with "
                    "the same mode and brief={id: answer, …}. The result will be tracked changes with a comment on each — "
                    "nothing accepted until the person decides.")}


def _count_tables(pkg) -> int:
    from app.tools.review_loop import _body, q
    return sum(1 for _ in _body(pkg).iter(q("tbl")))


def _brief_of(m: str, raw: Any) -> dict[str, Any]:
    """A válaszok tisztítva, az alapértékekkel kiegészítve (a kérdőívben is ezek az alapok)."""
    b = dict(raw or {}) if isinstance(raw, dict) else {}
    yes = lambda v, d: d if v is None else (str(v).strip().lower() not in ("false", "no", "0", "nem", "off", "")  # noqa: E731
                                           if not isinstance(v, bool) else v)
    limit = b.get("limit")
    try:
        limit = float(str(limit).rstrip("% ")) if limit not in (None, "") else {"shorten": 10, "expand": 20, "rewrite": 15}.get(m)
    except ValueError:
        raise ValueError("brief.limit: a percentage, e.g. 10") from None
    terms = b.get("keep_terms")
    terms = [str(t).strip() for t in (terms if isinstance(terms, list) else re.split(r"[;,\n]", str(terms or ""))) if str(t).strip()]
    scope = b.get("scope") or "all"
    scope = [str(s).strip() for s in (scope if isinstance(scope, list) else [scope]) if str(s).strip()]
    return {"goal": " ".join(str(b.get("goal") or "").split())[:400], "limit": limit,
            "keep_tables": yes(b.get("keep_tables"), True), "keep_numbers": yes(b.get("keep_numbers"), True),
            "keep_stance": yes(b.get("keep_stance"), True), "keep_terms": terms[:30],
            "new_ideas": yes(b.get("new_ideas"), m in ("expand", "rewrite")),
            "scope": [] if [s.lower() for s in scope] == ["all"] else scope,
            "author": " ".join(str(b.get("author") or f"Studio ({m})").split())[:60]}


# ---------- 2. propose: az AI javaslata, gépi őrök, követett változás ----------

_SYSTEM = """You are a careful human editor working on one document, paragraph by paragraph, in its own language.
MODE: {mode}.
The author's rules — they MUST hold:
{rules}
Never invent facts, numbers, sources or quotations. Keep the author's voice. Paragraphs are numbered [n]; change only
what the mode asks for and leave every other paragraph out of your answer.
Reply with ONE JSON object: {{"changes": [{{"n": paragraph number, "new": "the WHOLE new paragraph", "why": "why, in at
most 15 words, in {lang}", "kind": "language | clarity | cut | addition | new_idea"}}]}}. Use kind "new_idea" for a
paragraph where you add a thought that is not in the text."""


def _rules(m: str, b: dict[str, Any], batch_words: int) -> str:
    r = []
    if b["goal"] and b["goal"].lower() not in ("as it is now", "as it is"):
        r.append(f"- The text is for: {b['goal']}")
    if m == "shorten":
        r.append(f"- Cut about {max(1, round(b['limit'] * 0.8))}% of the words of these paragraphs (≈ "
                 f"{max(1, round(batch_words * b['limit'] / 100 * 0.8))} words), never more than {b['limit']:g}%.")
    elif m == "expand":
        r.append(f"- Add at most {b['limit']:g}% words (≈ {max(5, round(batch_words * b['limit'] / 100))} words in these paragraphs).")
    elif m == "rewrite":
        r.append(f"- Keep the length within ±{b['limit']:g}%.")
    elif m == "proofread":
        r.append("- Fix only spelling, grammar, punctuation and typos. Do not reword, reorder or shorten anything.")
    if b["keep_numbers"]:
        r.append("- Every number, date, percentage and quotation stays exactly as written.")
    if b["keep_stance"]:
        r.append("- The author's position and conclusions stay; do not add or remove a conclusion.")
    if b["keep_terms"]:
        r.append("- These words and names stay exactly: " + "; ".join(b["keep_terms"]))
    r.append("- New thoughts are " + ("allowed, but mark each such paragraph kind \"new_idea\"." if b["new_ideas"]
                                      else "NOT allowed: work only with what the text already says."))
    return "\n".join(r)


def _nums(text: str) -> set[str]:
    from app.tools.social import _nums as n
    return n(text)


def _change_ratio(old: str, new: str) -> float:
    import difflib
    a, b = _words(old.lower()), _words(new.lower())
    if not a:
        return 1.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    same = sum(bl.size for bl in sm.get_matching_blocks())
    return 1 - same / max(len(a), len(b))


def _new_content(old: str, new: str) -> float:
    """Az új szöveg tartalmas szavainak (≥ 5 betű) mekkora része nincs a régiben (tőre vágva) — új gondolat jele."""
    stem = lambda w: w.lower()[:6]      # noqa: E731
    have = {stem(w) for w in _words(old) if len(w) >= 5}
    cand = [stem(w) for w in _words(new) if len(w) >= 5]
    return sum(1 for w in cand if w not in have) / len(cand) if cand else 0.0


def _guard(m: str, b: dict[str, Any], old: str, ch: dict[str, Any]) -> Optional[str]:
    """→ miért NEM mehet a javaslat (None: mehet)."""
    new = ch["new"]
    if b["keep_numbers"]:
        lost = sorted(_nums(old) - _nums(new))
        added = sorted(_nums(new) - _nums(old))
        if lost:
            return f"it would drop the number(s) {', '.join(lost[:4])}"
        if added:
            return f"it would add the number(s) {', '.join(added[:4])} that aren't in the text"
    for t in b["keep_terms"]:
        if t.lower() in old.lower() and t.lower() not in new.lower():
            return f"it would change “{t}”, which must stay"
    if m == "proofread" and _change_ratio(old, new) > PROOF_MAX_CHANGE:
        return "it rewords more than a proofreading should"
    if not b["new_ideas"] and m != "expand" and (ch.get("kind") == "new_idea" or _new_content(old, new) > 0.45):
        return "it adds a thought that isn't in the text (new thoughts weren't allowed)"
    return None


async def _ask(batch: list[dict[str, Any]], m: str, b: dict[str, Any], lang: str, instruction: str,
               force: bool) -> list[dict[str, Any]]:
    """Egy köteg bekezdés → az AI változtatásai (egy Studio AI-lépés; ugyanarra a kérdésre a content_checks-ből)."""
    from app import db
    from app.llm.siliconflow import _call_model, _strip_json_fences
    words = sum(len(_words(p["text"])) for p in batch)
    system = _SYSTEM.format(mode=f"{m} — {MODES[m]}", rules=_rules(m, b, words), lang=lang)
    user = ((f"The person's request: {instruction}\n\n" if instruction else "")
            + "\n\n".join(f"[{p['n']}] {p['text']}" for p in batch))
    key = hashlib.sha256(json.dumps([VER, system, user], ensure_ascii=False).encode()).hexdigest()
    if not force:
        try:
            hit = await db.check_get("edit_mode", key, VER)
        except Exception:
            hit = None
        if isinstance(hit, dict):
            return list(hit.get("changes") or [])
    last: Exception = RuntimeError("no model")
    for model in (settings.LLM_MODEL_PRIMARY, settings.LLM_MODEL_FALLBACK):
        try:
            raw = await _call_model(model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                    thinking=False, max_tokens=12_000)
            data = json.loads(_strip_json_fences(raw))
            if isinstance(data, dict):
                break
        except Exception as e:
            from app import billing
            if isinstance(e, billing.AIStepsExhausted):
                raise
            last = e
            log.warning("szerkesztési mód (%s) %s modellel nem sikerült: %s", m, model, e)
    else:
        raise last
    changes = [c for c in data.get("changes") or [] if isinstance(c, dict)]
    try:
        await db.check_put("edit_mode", key, VER, {"changes": changes})
    except Exception:
        log.warning("szerkesztési mód gyorsítótár írása nem sikerült", exc_info=True)
    return changes


_COMMENT = {"hu": {"new_idea": "Új gondolat — vállalod? ", "kinds": {"language": "Nyelvi javítás", "clarity":
            "Világosabb", "cut": "Rövidítés", "addition": "Kiegészítés", "new_idea": "Új gondolat"}},
            "en": {"new_idea": "New thought — do you stand behind it? ", "kinds": {"language": "Language",
            "clarity": "Clearer", "cut": "Cut", "addition": "Addition", "new_idea": "New thought"}}}


async def propose(file_id: str, mode: str, brief_raw: Any, instruction: str = "", force: bool = False,
                  progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    from app.storage import new_file_id, output_path, public_url, write_meta
    from app.storage.naming import result_stem
    from app.tools import narration, review_ops
    from app.tools import review_loop as L
    step = progress or (lambda *a, **k: None)
    m = mode_of(mode)
    b = _brief_of(m, brief_raw)
    pkg = L._load(file_id)
    body = L._body(pkg)
    paras = await asyncio.to_thread(_paragraphs, pkg)
    lang = narration.detect(" ".join(p["text"] for p in paras)[:4000]) or "en"
    words_before = sum(len(_words(p["text"])) for p in paras)
    skipped: list[dict[str, Any]] = []
    work = []
    for p in paras:
        if not p["text"].strip() or len(_words(p["text"])) < 3:
            continue
        if b["scope"] and not any(s.lower() in (p["section"] or "").lower() for s in b["scope"]):
            continue
        if p["heading"]:
            continue
        if p["table"] and b["keep_tables"]:
            continue
        if p["busy"]:
            skipped.append({"paragraph": p["n"], "why": p["busy"], "text": p["text"][:80]})
            continue
        work.append(p)
    if not work:
        raise ValueError("There is nothing to work on in that part (headings, tables kept, and paragraphs with notes or "
                         "fields are left alone).")
    batches, cur, size = [], [], 0
    for p in work:
        if cur and size + len(p["text"]) > BATCH_CHARS:
            batches.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += len(p["text"])
    batches.append(cur)
    by_n = {p["n"]: p for p in work}
    accepted: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    for k, batch in enumerate(batches, 1):
        step(k - 1, len(batches) + 1, f"part {k} of {len(batches)}: the AI reads and proposes")
        for ch in await _ask(batch, m, b, lang, instruction, force):
            try:
                n = int(ch.get("n"))
            except (TypeError, ValueError):
                continue
            p = by_n.get(n)
            new = unicodedata.normalize("NFC", " ".join(str(ch.get("new") or "").split()))
            if p is None or not new or new == p["text"].strip():
                continue
            ch = {**ch, "n": n, "new": new, "why": " ".join(str(ch.get("why") or "").split())[:160],
                  "kind": str(ch.get("kind") or "clarity").lower()}
            why_not = _guard(m, b, p["text"], ch)
            if why_not:
                held.append({"paragraph": n, "why": why_not, "proposed": new[:200]})
            else:
                accepted.append(ch)
    # a hossz-határ az egészre: a legnagyobb vágás / bővítés vissza, amíg belefér
    delta = lambda chs: sum(len(_words(c["new"])) - len(_words(by_n[c["n"]]["text"])) for c in chs)  # noqa: E731
    if m in ("shorten", "expand", "rewrite") and b["limit"] is not None and words_before:
        cap = words_before * b["limit"] / 100
        def over(chs) -> bool:
            d = delta(chs)
            return (-d > cap) if m == "shorten" else (d > cap) if m == "expand" else abs(d) > cap
        while accepted and over(accepted):
            worst = max(accepted, key=lambda c: abs(len(_words(c["new"])) - len(_words(by_n[c["n"]]["text"]))))
            accepted.remove(worst)
            held.append({"paragraph": worst["n"], "why": f"it would take the whole text past the agreed {b['limit']:g}%",
                         "proposed": worst["new"][:200]})
    if not accepted:
        return {"changed": False, "file_id": file_id, "mode": m, "brief": b, "changes": 0, "held_back": held,
                "skipped": skipped, "note": "No change passed the agreed limits — nothing changed."}
    step(len(batches), len(batches) + 1, "writing the tracked changes")
    out = await asyncio.to_thread(_apply_all, pkg, body, accepted, by_n, b, lang)
    fid = new_file_id("docx")
    pkg.save(output_path(fid))
    label = f"{result_stem(file_id, {'proofread': 'proofread', 'edit': 'edited', 'expand': 'expanded', 'shorten': 'shortened', 'rewrite': 'reworked'}[m] + ' (proposed)')}.docx"
    write_meta(fid, label=label, source_file_id=file_id, operation=f"edit mode {m}", extra={"edit_brief": b})
    words_after = words_before + delta([c for c in accepted if c["n"] in out["done"]])
    kinds: dict[str, int] = {}
    for c in accepted:
        if c["n"] in out["done"]:
            kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    pct = (words_after - words_before) / words_before * 100 if words_before else 0
    report = {"words": {"before": words_before, "after_if_all_accepted": words_after, "change_pct": round(pct, 1)},
              "tables": "untouched" if b["keep_tables"] else "may be edited",
              "numbers": "every number kept" if b["keep_numbers"] else "not guarded",
              "kept_terms": b["keep_terms"], "by_kind": kinds,
              "new_ideas": kinds.get("new_idea", 0), "held_back": held, "not_touched": skipped,
              "failed": out["failed"]}
    findings = [f"{len(held)} proposal(s) held back (outside the agreed limits) — see held_back."] if held else []
    findings += [f"{len(skipped)} paragraph(s) not touched (notes, fields, pictures or pending changes) — see not_touched."] if skipped else []
    return {"changed": True, "file_id": fid, "url": public_url(fid), "label": label, "mode": m, "brief": b,
            "changes": len(out["done"]), "author": b["author"], "preservation": report,
            "how": (f"NOTHING IS ACCEPTED: {len(out['done'])} paragraph(s) carry tracked changes by “{b['author']}” with a "
                    "comment saying why (new thoughts: “do you stand behind it?”). The person accepts or rejects each in "
                    "Word or the editor, or with wordcraft(file_id, address=[{\"accept\": …} | {\"reject\": …}])."),
            "check": {"status": "needs_check", "findings": findings + ["The author decides on every change — nothing is "
                                                                       "accepted yet."],
                      "verified": [f"{len(out['done'])} paragraph(s) proposed as tracked changes ({', '.join(f'{v} {k}' for k, v in kinds.items())}); "
                                   f"if all were accepted: {words_before} → {words_after} words ({pct:+.1f}%)."]}}


def _apply_all(pkg, body, accepted: list[dict[str, Any]], by_n: dict[int, dict[str, Any]], b: dict[str, Any],
               lang: str) -> dict[str, Any]:
    from app.tools import review_loop as L
    from app.tools import review_ops
    doc = pkg.xml("word/document.xml")
    ctx = L._Ctx(b["author"], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), L._max_id(doc, pkg.xml(L._COMMENTS)))
    taken = {v for root in (doc, pkg.xml(L._COMMENTS)) if root is not None for el in root.iter()
             for v in [el.get(L.q("paraId", L.W14))] if v}
    scan = L._scan(body)
    done, failed = [], []
    for ch in sorted(accepted, key=lambda c: c["n"]):
        i = ch["n"] - 1
        p = scan["paras"][i]
        old = scan["texts"][i]
        snap = deepcopy(p)
        for s, e, t in sorted(L._word_ops(old, ch["new"]), reverse=True):
            L._apply(p, s, e, t, ctx)
        if L._ptext(p) != ch["new"]:                      # biztonsági háló: a redline pontosan a javasolt szöveget adja
            p.getparent().replace(p, snap)
            scan["paras"][i] = snap
            failed.append({"paragraph": ch["n"], "why": "the tracked change didn't give the proposed text — left out"})
            continue
        done.append(ch["n"])
    pkg.put("word/document.xml", doc)
    comments = L._comments(pkg)
    words = _COMMENT.get(lang, _COMMENT["en"])
    for ch in sorted(accepted, key=lambda c: c["n"]):
        if ch["n"] not in done or not ch.get("why"):
            continue
        label = words["kinds"].get(ch["kind"], ch["kind"])
        text = (words["new_idea"] if ch["kind"] == "new_idea" else f"{label}: ") + ch["why"]
        try:
            review_ops.new_comment(pkg, L._body(pkg), comments, {"new_comment": text, "paragraph": ch["n"]}, ctx, taken)
            comments = L._comments(pkg)
        except Exception as e:
            log.info("megjegyzés kimaradt (%s): %s", ch["n"], e)
    pkg.put("word/document.xml", pkg.xml("word/document.xml"))
    return {"done": done, "failed": failed}
