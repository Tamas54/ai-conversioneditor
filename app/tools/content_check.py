"""Tartalmi ellenőrzés egy Word-dokumentumon (10-14, Kommandant: „mind mehet”).

    consistency(path)  — belső egység: ellentmondás (két hely, ami nem lehet egyszerre igaz), hangnemváltás,
                         szóhasználat (ugyanaz a dolog kétféle néven/írásmóddal). Egy modellhívás, külső adat nélkül.
    facts(path)        — tényállítások forrással: a statisztikai állítás a StatData legfrissebb (vagy a kért időszakú)
                         adatával, az árfolyam az MNB-vel, a többi webes kereséssel (Echolot ENGINE) vetve össze.
                         Ítélet: supported | contradicted | outdated (régebben igaz volt, a legfrissebb adat más) |
                         unverified. A „nem találtam” = unverified, SOSEM „hamis” (a negatív lelet csak a mért útra igaz).

Minden lelet idézettel jön, és csak az marad meg, amelyiknek az idézete tényleg megvan a dokumentumban (a kitalált
idézet kiesik) — így a jelölés (megjegyzés, követett csere) pontosan oda kerül. A javasolt cserében számjegy csak a
forrásból vagy az eredeti idézetből jöhet (a modell számot nem talál ki).

Gyorsítótár (Postgres content_checks): ugyanarra a szövegre nincs új modellhívás; az állítás-ítélet 3 napig él.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Optional

from app.config import settings
from app.llm.siliconflow import _call_model, _strip_json_fences
from app.tools import datasources as ds
from app.tools import textlang
from app.tools.docx_marks import Docx

log = logging.getLogger("aice.content_check")

VER_CONSISTENCY = "c4"
VER_CLAIMS = "f3"          # f3: az éves („YYYY”) és napi időszak is
VER_VERDICT = "v4"         # v4: időszakos statisztika csak a saját időszakával (a régi ítéletek a legfrissebbel vetettek össze)
VERDICT_MAX_AGE_S = 3 * 86400
MAX_CHARS = 110_000
MAX_CLAIMS = 30


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def numbered(texts: list[str], limit: int = MAX_CHARS) -> tuple[str, bool]:
    """„[n] szöveg” sorok (az üres bekezdés kimarad, a sorszám a docx_marks sorrendje) + le kellett-e vágni."""
    out, size = [], 0
    for i, t in enumerate(texts):
        t = " ".join(t.split())
        if not t:
            continue
        line = f"[{i}] {t}"
        if size + len(line) > limit:
            return "\n".join(out), True
        out.append(line)
        size += len(line) + 1
    return "\n".join(out), False


async def _ask(prompt: str, thinking: bool = False) -> dict[str, Any]:
    last: Exception = RuntimeError("no answer")
    for model in (settings.AGENT_MODEL, settings.AGENT_MODEL_FALLBACK):
        try:
            raw = _strip_json_fences(await _call_model(model, [{"role": "user", "content": prompt}], thinking=thinking))
            return json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        except Exception as e:
            last = e
            log.warning("tartalmi ellenőrzés (%s): %s", model, e)
    raise RuntimeError(f"The AI check didn't answer: {last}")


async def _cached(kind: str, key: str, ver: str, make: Callable, max_age_s: Optional[int] = None) -> Any:
    from app import db
    try:
        hit = await db.check_get(kind, key, ver, max_age_s)
    except Exception as e:
        log.warning("ellenőrzés-gyorsítótár olvasása: %s", e)
        hit = None
    if hit is not None:
        return hit
    res = await make()
    try:
        await db.check_put(kind, key, ver, res)
    except Exception as e:
        log.warning("ellenőrzés-gyorsítótár írása: %s", e)
    return res


def _place(d: Docx, quote: str, para: Any) -> Optional[dict[str, Any]]:
    """Az idézet tényleges helye (a modell bekezdés-sorszáma csak tipp) — nincs meg: None (a lelet kiesik)."""
    try:
        hint = int(para)
    except (TypeError, ValueError):
        hint = None
    hit = d.find(quote or "", hint)
    if hit is None:
        return None
    i, a, b = hit
    return {"paragraph": i, "quote": d.texts()[i][a:b], "start": a, "end": b}


# ---------- belső egység ----------

_CONSISTENCY_PROMPT = """You proofread a document for INTERNAL consistency. The paragraphs are numbered [n].
Report only clear problems of these kinds:
- "contradiction": two places that cannot both be true — the same quantity, date, name, count or claim stated
  differently ("founded in 2012" … "since its founding in 2014"; "three members" next to a list of four). Different
  years, different quantities, or a figure and its later update are NOT contradictions.
- "tone": a passage whose register clearly breaks from the rest of the document (slang or sales talk in a formal report,
  a sudden switch between formal and informal address). Ordinary variation is not a finding.
- "term": the same thing named or spelled inconsistently (two spellings of one name, a defined term swapped for a
  synonym, an abbreviation written two ways).
Report ONLY real problems you are sure of. Never list something to say it is consistent or fine, and never list two
places that are written the same way as a "term" finding. If unsure, leave it out.
Quote EXACTLY: copy each quote character for character from its paragraph, 3 to 15 words, enough to find it again.
A contradiction has both places. Suggest a fix only when the document itself shows which version is right, otherwise
"fix" is null. Write "note" in the document's own language, one or two plain sentences.
At most 12 findings, the most important first. No findings is a perfectly good answer.
Reply with JSON only:
{{"findings": [{{"kind": "contradiction|tone|term", "places": [{{"paragraph": n, "quote": "…"}}], "note": "…",
  "fix": {{"paragraph": n, "quote": "exact text to replace", "replacement": "…"}} or null}}]}}

Document:
{doc}"""


_VERIFY_PROMPT = """A careful editor double-checks candidate problems found in a document. Each candidate comes with
the full paragraphs it points to. Keep a candidate ONLY if it is definitely a real problem the author would want to fix:
- contradiction: the places really state incompatible things about the same thing for the same time;
- tone: the passage really breaks the register of the whole document (a vivid or evaluative word inside an argument
  is not a break);
- term: the same thing really is named or spelled two different ways, and it is not normal usage (a short form after
  the full name, "the authority" for a named authority, a person's surname after the full name, an abbreviation
  explained once — all normal).
Drop it when the places are about different things, events or periods, when the variation is normal (a standard
abbreviation in a citation is normal), or when its own note says it may not be a problem.
For each candidate first give a short reason, then the decision.
Reply with JSON only: {{"decisions": [{{"id": "C1", "reason": "…", "keep": true or false}}, …]}}

Candidates:
{block}"""


# kitöltetlen helyőrző: [a könyvtár neve], {{name}}, TBD/TODO/XXX, Lorem ipsum — determinisztikus, modell nélkül
# (UI-próba 10-18: a briefből írt meghívóban három [..] maradt, a Check „nothing found”-ot mondott). A „[1]”-féle
# hivatkozás, a „[…]” kihagyás és a „[sic]” nem az.
_PLACEHOLDER = re.compile(r"\[(?!sic\])[^\[\]\n]*[^\W\d_][^\[\]\n]*[^\W\d_][^\[\]\n]*\]|\{\{[^{}\n]{1,60}\}\}"
                          r"|\b(?:TBD|TODO|XXX+)\b|\b[Ll]orem ipsum\b")


def placeholders(d: Docx) -> Optional[dict[str, Any]]:
    places = [{"paragraph": i, "quote": m.group(), "start": m.start(), "end": m.end()}
              for i, t in enumerate(d.texts()) for m in _PLACEHOLDER.finditer(t) if len(m.group()) <= 82][:20]
    if not places:
        return None
    n = len(places)
    return {"kind": "placeholder", "places": places, "fix": None,
            "note": f"{n} placeholder{'s' if n > 1 else ''} not filled in yet — replace with the real text, or remove."}


async def consistency(path: Path) -> dict[str, Any]:
    d = Docx(path)
    doc, cut = numbered(d.texts())
    if not doc.strip():
        return {"findings": [], "checked": "nothing to check — the document has no text", "truncated": False}
    res = await _consistency_ai(d, doc, cut)
    ph = placeholders(d)
    if ph:                                    # biztos lelet: elöl, a modell átnézése nélkül
        res["findings"] = [{"id": "C1", **ph}] + [{**f, "id": f"C{k}"} for k, f in enumerate(res["findings"], 2)]
    res["checked"] = "unfilled placeholders, " + res["checked"]
    return res


async def _consistency_ai(d: Docx, doc: str, cut: bool) -> dict[str, Any]:

    async def make():
        return await _ask(_CONSISTENCY_PROMPT.format(doc=doc))
    raw = await _cached("consistency", _sha(doc), VER_CONSISTENCY, make)
    out = []
    for f in (raw.get("findings") or [])[:12]:
        if not isinstance(f, dict) or f.get("kind") not in ("contradiction", "tone", "term"):
            continue
        places = [p for p in (_place(d, x.get("quote"), x.get("paragraph")) for x in (f.get("places") or [])
                              if isinstance(x, dict)) if p]
        if not places or (f["kind"] == "contradiction" and len(places) < 2):
            continue
        if f["kind"] == "term" and len({" ".join(p["quote"].lower().split()) for p in places}) < 2 and len(places) > 1:
            continue                        # két egyformán írt hely nem eltérés (a modell néha „rendben van”-t is jelent)
        fix = f.get("fix") if isinstance(f.get("fix"), dict) else None
        if fix:
            at = _place(d, fix.get("quote"), fix.get("paragraph"))
            rep = tidy_replacement(at["quote"], str(fix.get("replacement") or "")) if at and fix.get("replacement") is not None else None
            fix = {**at, "replacement": rep} if at and rep is not None and rep != at["quote"] else None
        out.append({"id": f"C{len(out) + 1}", "kind": f["kind"], "places": places, "note": str(f.get("note") or "")[:600],
                    "fix": fix})
    if out:
        texts = d.texts()
        block = "\n\n".join(f"{f['id']} ({f['kind']}): {f['note']}\n" + "\n".join(
            f"  [{p['paragraph']}] {' '.join(texts[p['paragraph']].split())[:1500]}" for p in f["places"]) for f in out)

        async def verify():
            # indoklás a döntés előtt, gondolkodó mód nélkül (az 99 s volt, és egyszer üres választ adott — 10-14)
            return await _ask(_VERIFY_PROMPT.format(block=block))
        try:
            got = await _cached("consistency-verify", _sha(block), VER_CONSISTENCY, verify)
            keep = {str(x.get("id")) for x in (got.get("decisions") or []) if isinstance(x, dict) and x.get("keep") is True}
        except Exception as e:                    # az átnézés kiesése: inkább semmi, mint zaj
            log.warning("egység-átnézés: %s", e)
            keep = set()
        out = [f for f in out if f["id"] in keep]
        for k, f in enumerate(out, 1):
            f["id"] = f"C{k}"
    return {"findings": out, "truncated": cut,
            "checked": "contradictions between places, sudden changes of tone, the same thing named two ways"}


# ---------- tények ----------

_CLAIMS_PROMPT = """List the checkable FACTUAL CLAIMS in this document: numbers and statistics about the real world,
dates of real events, who did, said or decided what, counts, legal facts, records. SKIP opinions, proposals,
recommendations, the document's own plans and next steps, definitions, hypotheticals, anything about the document
itself, and facts about the author's own organisation, people or work (the writer knows those better than the web). At most {max} claims, the most important and most checkable first. The paragraphs are numbered [n].
For each claim:
  "paragraph": n,
  "quote": the exact words of the paragraph that state it (copy character for character, 3 to 20 words),
  "claim": the claim as one self-contained sentence (who or what, when, how much), in the document's language,
  "query": a web search query that would verify it (the document's language; English for international facts),
  "stat": ONLY when it is one of these indicators for one country — {indicators} —
          {{"country": "ISO-2 code", "indicator": "<one of the names>", "period": the period the claim is ABOUT —
          "YYYY" for a whole year ("in 2024", "2024-ben": the annual figure), "YYYY-MM" for a month or the end of a
          year ("at the end of 2025", "2025 végén" → "2025-12"), "YYYY-Qn" for a quarter, "YYYY-MM-DD" for a day —
          or null when it states the current / latest value, "value": the number stated}}; otherwise null,
  "fx": ONLY for an exchange rate against the Hungarian forint: {{"currency": "EUR", "value": number}}; otherwise null.
Reply with JSON only: {{"claims": [ … ]}}

Document:
{doc}"""

_VERDICT_PROMPT = """You check factual claims against the evidence listed under each claim — ONLY against that evidence,
never your own knowledge. For each claim decide:
- "supported": the evidence states the same thing (numbers equal at the claim's precision).
- "contradicted": the evidence clearly states something different about the same thing for the same time — and the
  evidence is clearly about the SAME person, organisation or place (a namesake or an unclear match is "unverified").
- "outdated": the claim describes the present ("now", "currently", "the latest", or no time given) and the newest figure
  in the evidence is different — the claim may have been true earlier.
- "unverified": the evidence does not settle it (missing, about something else, a different period, or unclear).
  A claim about another period than the evidence is "unverified", not "contradicted".
For each claim reply with:
  "id": the claim id,
  "verdict": one of the four,
  "explanation": one plain sentence in the document's language that names the source and the figure it gives,
  "source": the evidence id you rely on (e.g. "S2") or null,
  "claimed": for contradicted or outdated — the value or wording the claim states (e.g. "4,3%"), otherwise null,
  "correct": the right value or wording, copied from the evidence, or null,
  "replacement": for contradicted or outdated only — the quote with ONLY the wrong part corrected (same length and form
                 as the quote — a table cell stays a cell, a phrase stays a phrase; adjust the words right around the
                 number if the grammar needs it); otherwise null.
If "claimed" and "correct" say the same thing, the verdict is "supported", not "contradicted".
Reply with JSON only: {{"verdicts": [ … ]}}

{claims}"""


def _evidence_text(ev: list[dict[str, Any]]) -> str:
    lines = []
    for e in ev:
        if e["type"] == "data":
            lines.append(f"  {e['id']} [{e['source_name']}] {e['what']}: {e['value']:g} {e['unit']} — period {e['period']}"
                         + (f" — {e['source_url']}" if e.get("source_url") else ""))
        else:
            lines.append(f"  {e['id']} [web] {e['title']} — {e['snippet']}" + (f" ({e['date']})" if e.get("date") else "")
                         + f" — {e['url']}")
    return "\n".join(lines) or "  (no evidence found)"


def stat_period(c: dict[str, Any]) -> Optional[str]:
    """Az állítás saját időszaka (a statisztikai lekérés EZZEL megy) — None: a jelenről / legfrissebbről szól."""
    stat = c.get("stat")
    return str(stat["period"]).strip() if isinstance(stat, dict) and stat.get("period") else None


async def _evidence(c: dict[str, Any]) -> list[dict[str, Any]]:
    """Az állítás forrásai. Múltbeli (időszakos) statisztikánál CSAK a kért időszak értéke — a legfrissebbel nem vetünk
    össze; ha arra az időszakra nincs adat, c["no_period_data"] mondja meg, és webes keresés sem fut helyette."""
    ev: list[dict[str, Any]] = []
    stat, fxc = c.get("stat"), c.get("fx")
    try:
        if isinstance(stat, dict) and stat.get("indicator") in ds.INDICATORS and stat.get("country"):
            spec = {"kind": "indicator", "country": stat["country"], "indicator": stat["indicator"]}
            per = stat_period(c)
            if per:
                spec = {**spec, "period": per}
                try:
                    g = await ds.fetch(spec)
                except ds.NoPeriodData as e:
                    c["no_period_data"] = {"period": per, "why": str(e)}
                    return []
            else:
                g = await ds.fetch(spec)
            ev.append({"type": "data", "what": ds.describe(spec).replace(", latest", ""), "spec": spec, **g})
        elif isinstance(fxc, dict) and str(fxc.get("currency", "")).upper() in ds.CURRENCIES:
            spec = {"kind": "fx", "currency": str(fxc["currency"]).upper()}
            ev.append({"type": "data", "what": ds.describe(spec), "spec": spec, **await ds.fetch(spec)})
    except ds.SourceError as e:
        log.info("adatforrás (%s): %s", c.get("claim", "")[:60], e)
    if ds.search_enabled() and c.get("query") and not any(e["type"] == "data" for e in ev):
        try:
            for r in await ds.web_search(str(c["query"]), limit=5):
                ev.append({"type": "web", **r})
        except ds.SourceError as e:
            log.info("webes keresés (%s): %s", c.get("query", "")[:60], e)
    for k, e in enumerate(ev, 1):
        e["id"] = f"S{k}"
    return ev


_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")


def _same(a: Any, b: Any) -> bool:
    """Ugyanazt mondja-e a két érték (számok és szavak, írásmódtól függetlenül: „okt. 5.” = „október 5-én”)."""
    if not a or not b:
        return False
    na, nb = _DIGITS.findall(str(a).replace(",", ".")), _DIGITS.findall(str(b).replace(",", "."))
    if na and nb:
        return [float(x) if x.replace(".", "", 1).isdigit() else x for x in na] == \
               [float(x) if x.replace(".", "", 1).isdigit() else x for x in nb]
    return " ".join(str(a).lower().split()) == " ".join(str(b).lower().split())


def tidy_replacement(quote: str, rep: str) -> str:
    """A csere az idézet HELYÉRE megy: az idézetben nem szereplő záró írásjel nem kerülhet a mondat közepére."""
    rep = (rep or "").strip()
    while rep and rep[-1] in ".!?;:," and not quote.rstrip().endswith(rep[-1]):
        rep = rep[:-1].rstrip()
    return rep


def minimal_change(quote: str, rep: str) -> tuple[int, int, str]:
    """A tényleges eltérés: (kezdet, vég az idézetben, új szöveg) — közös eleje/vége nélkül, szóhatárra igazítva
    (a követett változás csak a „4,3” → „1,6”-ot jelöli, nem az egész mondatot)."""
    a = 0
    while a < min(len(quote), len(rep)) and quote[a] == rep[a]:
        a += 1
    b = 0
    while b < min(len(quote), len(rep)) - a and quote[-1 - b] == rep[-1 - b]:
        b += 1
    while a > 0 and quote[a - 1].isalnum():               # szó közepén ne kezdődjön (olvashatóbb jelölés)
        a -= 1
    while b > 0 and quote[len(quote) - b].isalnum():
        b -= 1
    return a, len(quote) - b, rep[a:len(rep) - b]


def _numbers_ok(replacement: str, quote: str, ev: list[dict[str, Any]]) -> bool:
    """A cserében minden szám a forrásból vagy az eredeti idézetből jön (más írásmód: 1.6 = 1,6)."""
    def norm(x: str) -> str:
        return x.replace(",", ".").rstrip("0").rstrip(".") if "." in x.replace(",", ".") else x
    pool = set()
    for s in [quote] + [json.dumps(e, ensure_ascii=False) for e in ev]:
        pool |= {norm(x) for x in _DIGITS.findall(s)}
    for e in ev:                       # a mért érték a dokumentum pontosságára kerekítve is elfogadható
        if e["type"] == "data":
            for nd in range(0, 4):
                pool.add(norm(f"{round(e['value'], nd):.{nd}f}"))
    return all(norm(x) in pool for x in _DIGITS.findall(replacement or ""))


def _decimals(quote: str, value: float) -> int:
    """Az állítás pontossága: a idézetben álló szám tizedesjegyei (3,7 → 1); ha nem található, az értékéből."""
    for m in _DIGITS.finditer(quote or ""):
        tok = m.group(0)
        num = tok.replace(" ", "")
        dec = num.rsplit(",", 1) if "," in num else num.rsplit(".", 1) if num.count(".") == 1 else [num]
        try:
            if abs(float(".".join(dec)) - value) < 1e-9:
                return len(dec[1]) if len(dec) == 2 else 0
        except ValueError:
            continue
    t = f"{value:g}"
    return len(t.split(".")[1]) if "." in t else 0


def _fmt(v: float, dec: int, comma: bool) -> str:
    from decimal import ROUND_HALF_UP, Decimal
    q = Decimal(str(v)).quantize(Decimal(1).scaleb(-dec), rounding=ROUND_HALF_UP)
    s = f"{q:.{dec}f}"
    return s.replace(".", ",") if comma else s


def period_verdict(c: dict[str, Any], ev: list[dict[str, Any]], hu: bool) -> dict[str, Any]:
    """Időszakos statisztika: a forrás UGYANARRA az időszakra adott értéke az állítás pontosságán → supported vagy
    contradicted (a helyes érték az állítás saját számformájában, a csere csak a számot cseréli)."""
    e = ev[0]
    claimed = (c.get("stat") or {}).get("value")
    src = {k: e.get(k) for k in ("type", "title", "url", "source_name", "source_url", "value", "unit", "period", "spec")}
    base = {"source": src, "evidence": [{k: e.get(k) for k in ("id", "type", "title", "url", "source_name", "source_url",
                                                                "value", "unit", "period")}]}
    try:
        claimed = float(str(claimed).replace(",", "."))
    except (TypeError, ValueError):
        return {**base, "verdict": "unverified", "correct": None, "replacement": None,
                "explanation": ("Az állításban nincs összevethető szám." if hu else "The claim states no comparable number.")}
    q = c.get("quote") or ""
    dec = _decimals(q, claimed)
    comma = "," in next((m.group(0) for m in _DIGITS.finditer(q) if "," in m.group(0) or "." in m.group(0)), "," if hu else ".")
    mine, theirs = _fmt(claimed, dec, comma), _fmt(e["value"], dec, comma)
    per, name = e.get("period") or stat_period(c), e.get("source_name") or "StatData"
    if mine == theirs:
        return {**base, "verdict": "supported", "correct": None, "replacement": None,
                "explanation": (f"{name}: {theirs} ({per})." if hu else f"{name} gives {theirs} for {per}.")}
    rep = None
    for m in _DIGITS.finditer(q):
        if m.group(0).replace(".", ",") == mine.replace(".", ",") or m.group(0) == mine:
            rep = q[:m.start()] + theirs + q[m.end():]
            break
    return {**base, "verdict": "contradicted", "correct": theirs, "replacement": rep,
            "explanation": (f"{name} szerint {per}: {theirs}, nem {mine}." if hu else f"{name} gives {theirs} for {per}, not {mine}.")}


def no_period_verdict(c: dict[str, Any], hu: bool) -> dict[str, Any]:
    per = c["no_period_data"]["period"]
    return {"verdict": "unverified", "source": None, "correct": None, "replacement": None, "evidence": [],
            "no_data_for_period": per,
            "explanation": (f"Nincs adat erre az időszakra ({per}) a statisztikai forrásokban — más időszak értékével nem "
                            "vetettük össze." if hu else
                            f"No data for this period ({per}) in the statistics sources — it isn't compared with another "
                            "period's figure.")}


async def facts(path: Path, *, charge: Optional[Callable[[int], Any]] = None,
                progress: Optional[Callable[[int, int, str], None]] = None) -> dict[str, Any]:
    d = Docx(path)
    doc, cut = numbered(d.texts())
    if not doc.strip():
        return {"claims": [], "checked": "nothing to check — the document has no text", "truncated": False}
    say = progress or (lambda *_: None)
    say(0, 1, "finding the claims")

    async def make_claims():
        return await _ask(_CLAIMS_PROMPT.format(doc=doc, max=MAX_CLAIMS,
                                                indicators=", ".join(f"{k} ({v})" for k, v in ds.INDICATORS.items())))
    raw = await _cached("claims", _sha(doc), VER_CLAIMS, make_claims)
    claims = []
    for c in (raw.get("claims") or [])[:MAX_CLAIMS]:
        if not isinstance(c, dict) or not c.get("claim"):
            continue
        at = _place(d, c.get("quote"), c.get("paragraph"))
        if at:
            claims.append({**c, **at, "id": f"F{len(claims) + 1}", "key": _sha(json.dumps(
                [at["quote"], c.get("claim"), c.get("stat"), c.get("fx")], ensure_ascii=False, sort_keys=True))})
    from app import db
    done: dict[str, dict[str, Any]] = {}
    for c in claims:
        try:
            hit = await db.check_get("verdict", c["key"], VER_VERDICT, VERDICT_MAX_AGE_S)
        except Exception:
            hit = None
        if hit:
            done[c["id"]] = hit
    todo = [c for c in claims if c["id"] not in done]
    if todo and charge:
        await charge(len(todo))
    say(0, len(todo), "looking up the sources")
    sem = asyncio.Semaphore(4)

    async def gather_ev(c):
        async with sem:
            return await _evidence(c)
    evs = await asyncio.gather(*(gather_ev(c) for c in todo))
    hu = (textlang.detect(doc) or "").startswith("hu")
    for k in range(0, len(todo), 8):                      # 8 állítás egy ítélet-hívásban
        batch = list(zip(todo[k:k + 8], evs[k:k + 8]))
        say(k, len(todo), "weighing the evidence")
        need = []
        for c, ev in batch:
            if c.get("no_period_data"):
                done[c["id"]] = no_period_verdict(c, hu)
            elif stat_period(c) and ev and ev[0]["type"] == "data":
                done[c["id"]] = period_verdict(c, ev, hu)     # adat → determinisztikus összevetés, nem a modell
            elif ev:
                need.append((c, ev))
            else:
                done[c["id"]] = {"verdict": "unverified", "evidence": [], "source": None, "correct": None, "replacement": None,
                                 "explanation": "No source found to check this against." if ds.search_enabled()
                                 else "Only statistics can be checked on this server — web search isn't set up."}
            if c["id"] in done and (c.get("no_period_data") or stat_period(c)):
                try:
                    await db.check_put("verdict", c["key"], VER_VERDICT, done[c["id"]])
                except Exception as e:
                    log.warning("ítélet mentése: %s", e)
        if need:
            block = "\n\n".join(f"Claim {c['id']}: {c['claim']}\n  Quote: “{c['quote']}”\n  Evidence:\n{_evidence_text(ev)}"
                                for c, ev in need)
            try:
                got = {str(v.get("id")): v for v in (await _ask(_VERDICT_PROMPT.format(claims=block))).get("verdicts", [])
                       if isinstance(v, dict)}
            except Exception as e:
                log.warning("ítélet: %s", e)
                got = {}
            for c, ev in need:
                v = got.get(c["id"]) or {}
                verdict = v.get("verdict") if v.get("verdict") in ("supported", "contradicted", "outdated", "unverified") else "unverified"
                if verdict in ("contradicted", "outdated") and _same(v.get("claimed"), v.get("correct")):
                    verdict = "supported"             # a modell maga mondta, hogy a forrás ugyanazt állítja
                if verdict in ("contradicted", "outdated") and not v.get("correct"):
                    verdict = "unverified"            # nincs megnevezett helyes érték → nem állítjuk, hogy hibás
                src = next((e for e in ev if e["id"] == v.get("source")), None)
                rep = tidy_replacement(c["quote"], str(v.get("replacement") or "")) if verdict in ("contradicted", "outdated") else None
                if rep == c["quote"] or (rep and len(rep) > 2 * len(c["quote"]) + 20):
                    rep = None                        # nem csere, hanem új mondat lenne (pl. egy idővonal-cellából)
                if rep and not _numbers_ok(str(rep), c["quote"], ev):
                    log.info("csere elvetve (forrásban nem szereplő szám): %s → %s", c["quote"], rep)
                    rep = None
                done[c["id"]] = {"verdict": verdict, "explanation": str(v.get("explanation") or "")[:600],
                                 "source": src and {k2: src.get(k2) for k2 in ("type", "title", "url", "source_name", "source_url",
                                                                               "value", "unit", "period", "spec")},
                                 "correct": v.get("correct"), "replacement": rep,
                                 "evidence": [{k2: e.get(k2) for k2 in ("id", "type", "title", "url", "source_name", "source_url",
                                                                        "value", "unit", "period")} for e in ev]}
                if got:
                    try:
                        await db.check_put("verdict", c["key"], VER_VERDICT, done[c["id"]])
                    except Exception as e:
                        log.warning("ítélet mentése: %s", e)
    out = []
    for c in claims:
        v = done.get(c["id"]) or {"verdict": "unverified", "explanation": ""}
        out.append({"id": c["id"], "paragraph": c["paragraph"], "quote": c["quote"], "start": c["start"], "end": c["end"],
                    "claim": c["claim"], "stat": c.get("stat"), "fx": c.get("fx"), **v})
    order = {"contradicted": 0, "outdated": 1, "unverified": 2, "supported": 3}
    out.sort(key=lambda x: (order.get(x["verdict"], 9), x["paragraph"]))
    counts = {k: sum(1 for x in out if x["verdict"] == k) for k in order}
    return {"claims": out, "counts": counts, "new_checks": len(todo), "truncated": cut, "web": ds.search_enabled(),
            "checked": "statistics against StatData (Eurostat, KSH, ECB, FRED…), forint rates against the MNB"
                       + (", other facts against a web search" if ds.search_enabled() else " (web search isn't set up)")}


async def consistency_is_cached(path: Path) -> bool:
    """Van-e már eredmény erre a szövegre (akkor nem fogy a havi keretből — a figyelő mód mentésenként kérdez)."""
    from app import db
    doc, _ = numbered(Docx(path).texts())
    try:
        return await db.check_get("consistency", _sha(doc), VER_CONSISTENCY) is not None
    except Exception:
        return False


# ---------- a leletek a dokumentumba ----------

def author() -> str:
    return f"{settings.PRODUCT_NAME} check"


async def apply(file_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Leletek a dokumentumba, új változatként: megjegyzés az idézetre, és ha van javítás, követett cserével
    (a szerkesztőben elfogadható/elutasítható). items: [{id?, quote, paragraph?, comment?, replacement?}].
    Ami nem tehető be (az idézet már nincs meg ebben a változatban), az `skipped` — a többi bemegy."""
    import asyncio as _a
    from app.storage import display_label, output_path, public_url, write_meta
    from app.storage.files import new_file_id
    from app.tools import crafts
    from app.tools.docx_marks import RevisionError
    from app.tools.revise import history
    src = crafts.existing(file_id)
    if src.suffix.lower() != ".docx":
        raise ValueError("Checks can be put into Word documents (.docx).")

    def work() -> tuple[str, list, list]:
        d = Docx(src, author=author())
        applied, skipped = [], []
        for it in items[:200]:
            try:
                hit = d.find(str(it.get("quote") or ""), it.get("paragraph"))
                if hit is None:
                    raise RevisionError("that text isn't in this version any more")
                i, a, b = hit
                p = d.paragraphs()[i]
                if it.get("comment"):
                    d.comment(p, a, b, str(it["comment"])[:2000])
                rep = it.get("replacement")
                if rep is not None and str(rep) != d.texts()[i][a:b]:
                    qa, qb, new = minimal_change(d.texts()[i][a:b], str(rep))
                    d.replace(p, a + qa, a + qb, new)
                if not it.get("comment") and rep is None:
                    raise RevisionError("nothing to put in")
                applied.append(it.get("id"))
            except RevisionError as e:
                skipped.append({"id": it.get("id"), "why": str(e)})
        if not applied:
            raise ValueError("Nothing could be put into the document: " + "; ".join(s["why"] for s in skipped[:3]))
        out = new_file_id("docx")
        d.save(output_path(out))
        return out, applied, skipped
    out_id, applied, skipped = await _a.to_thread(work)
    stem = Path(display_label(file_id)).stem.split(" (")[0]
    label = f"{stem} (v{len(history(file_id)) + 1}).docx"
    n_fix = sum(1 for it in items if it.get("id") in applied and it.get("replacement") is not None)
    summary = (f"{len(applied)} check result{'s' if len(applied) != 1 else ''} put in as comments"
               + (f", {n_fix} with a suggested fix (tracked change)" if n_fix else "") + ".")
    write_meta(out_id, source_file_id=file_id, operation="check", label=label,
               extra={"instruction": "Put the check results into the document", "summary": summary})
    return {"file_id": out_id, "url": public_url(out_id), "label": label, "summary": summary,
            "applied": applied, "skipped": skipped}
