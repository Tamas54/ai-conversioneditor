"""Házi stílusszabályok — a FELHASZNÁLÓHOZ kötve (Kommandant, 10-22: „A házi stílusszabály legyen userhez kötött”).

A szerző szabálya az övé, nem egy dokumentumé: elmentve minden későbbi validate_document ellenőrzi. A kliens AI a
memóriájából amúgy is tudja; a szerveroldal ezért karcsú — gépi (determinisztikus) ellenőrzés + a memória nélküli
kliensnek (ChatGPT, kézi szerkesztő) ugyanaz a lista. Szerveroldali AI-átírás nincs: a javítás a kliens ügynöké.

Szabályfajták (Postgres house_rules, felhasználónként egy lista):
  {"preset": "hu_modal"}                       a -hat/-het feltételes állítás és a bizonytalankodó szavak
                                               („valószínűleg”, „talán”…); kimondott feltételben (ha / amennyiben)
                                               a -hat/-het rendben van; idézetben („…”) nem keresünk
  {"avoid": ["szerintünk", …], "why": "…"}     ezek a szavak / kifejezések ne legyenek benne
  {"prefer": "korábbi vitairatunk", "instead_of": ["a 2025-ös vitairat"], "why": "…"}
  {"note": "…"}                                gépileg nem ellenőrizhető szabály — a kliens kapja vissza, ő tartja be
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

PRESETS = {"hu_modal": "no -hat/-het hedging and no hedge words — say it as a statement with a number or source; a "
                       "condition goes in an explicit „ha … (akkor)” sentence"}
MAX_RULES, MAX_PHRASES, MAX_PHRASE, MAX_NOTE, MAX_HITS = 30, 50, 80, 500, 25

# ---------- -hat/-het ----------

_BACK = ["", "ok", "sz", "unk", "tok", "nak", "ja", "juk", "játok", "ják", "om", "od", "lak", "tam", "tál", "tunk",
         "tatok", "tak", "ott", "ta", "tuk", "ták", "na", "nék", "nál", "nánk", "nátok", "nának", "nám", "nád", "ná", "nák"]
_FRONT = ["", "ek", "sz", "ünk", "tek", "nek", "i", "jük", "itek", "ik", "em", "ed", "lek", "tem", "tél", "tünk",
          "tetek", "ett", "te", "tük", "ték", "ne", "nék", "nél", "nénk", "nétek", "nének", "ném", "néd", "né"]
_SUBJ_B = ["s", "son", "sunk", "satok", "sanak", "sam", "sad", "sa", "suk", "sák"]
_SUBJ_F = ["s", "sen", "sünk", "setek", "senek", "sem", "sed", "se", "sük", "sék"]
_SUFFIXES = sorted({"hat" + e for e in _BACK} | {"het" + e for e in _FRONT} | {"has" + e for e in _SUBJ_B}
                   | {"hes" + e for e in _SUBJ_F}, key=len, reverse=True)
# igekötő + „hat” (hatni): kihat, áthat, visszahatott — nem feltételes mód
_PREFIX = {"ki", "be", "el", "fel", "föl", "meg", "át", "rá", "vissza", "oda", "össze", "szét", "közre", "ellen",
           "hozzá", "újra", "bele", "alá", "keresztül", "végig", "tovább", "egymásra"}
_NUMBER = re.compile(r"(tizen|huszon|harminc|negyven|ötven|hatvan|hetven|nyolcvan|kilencven|száz|ezer|millió)$")
_HEDGES = {"valószínűleg", "feltehetően", "vélhetően", "várhatóan", "talán", "esetleg", "elképzelhető",
           "feltételezhető", "feltételezhetően", "valószínűsíthető", "valószínűsíthetően", "lehetséges"}
_WORD = re.compile(r"[^\W\d_]+")
_COND = re.compile(r"(?<!\w)(ha|amennyiben|feltéve|hogyha)(?!\w)", re.I)
_QUOTE = re.compile(r"„[^”]*”|“[^”]*”|\"[^\"]*\"|«[^»]*»|»[^«]*«")
_SENT = re.compile(r"(?<=[.!?…])\s+")


def modal(word: str) -> bool:
    """-hat/-het (-hass/-hess) feltételes igealak-e: „okozhat”, „lehet”, „mondhatjuk”, „kérhetnénk”, „tehessen”."""
    w = word.lower()
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 2:
            stem = w[: -len(suf)]
            if suf[1] == "a" and (stem in _PREFIX or _NUMBER.search(stem)):
                return False
            return True
    return False


def _unquoted(text: str) -> str:
    return _QUOTE.sub(lambda m: " " * len(m.group(0)), text)


def _hu_modal_hits(text: str) -> list[str]:
    hits: list[str] = []
    for sent in _SENT.split(_unquoted(text)):
        cond = bool(_COND.search(sent))
        for m in _WORD.finditer(sent):
            w = m.group(0)
            if w.lower() in _HEDGES or (not cond and modal(w)):
                hits.append(w)
    return hits


def _phrase_hits(text: str, phrases: list[str]) -> list[str]:
    t = _unquoted(text)
    out = []
    for ph in phrases:
        for m in re.finditer(r"(?<!\w)" + re.escape(ph) + r"(?!\w)", t, re.I):
            out.append(m.group(0))
    return out


# ---------- szabálylista ----------

def normalize(raw: Any) -> list[dict[str, Any]]:
    """A kliens listája → ellenőrzött szabálylista (azonosító: R1, R2… a sorrendben). Hibás alaknál ValueError."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError('house_rules: a list of rules, e.g. [{"preset": "hu_modal"}, {"avoid": ["szerintünk"]}, '
                             '{"prefer": "korábbi vitairatunk", "instead_of": ["a vitairat"]}, {"note": "…"}]') from None
    if not isinstance(raw, list):
        raise ValueError("house_rules: a list of rules ([] clears them).")
    if len(raw) > MAX_RULES:
        raise ValueError(f"house_rules: at most {MAX_RULES} rules.")

    def phrases(v: Any, key: str) -> list[str]:
        v = [v] if isinstance(v, str) else v
        if not isinstance(v, list) or not v or not all(isinstance(x, str) and x.strip() for x in v):
            raise ValueError(f"house_rules: “{key}” is a word or a list of words / phrases.")
        if len(v) > MAX_PHRASES or any(len(x) > MAX_PHRASE for x in v):
            raise ValueError(f"house_rules: “{key}” — at most {MAX_PHRASES} phrases, each up to {MAX_PHRASE} characters.")
        return [x.strip() for x in v]

    out: list[dict[str, Any]] = []
    for r in raw:
        if not isinstance(r, dict):
            raise ValueError("house_rules: each rule is an object (preset | avoid | prefer + instead_of | note).")
        why = str(r.get("why") or "").strip()[:MAX_NOTE]
        if "preset" in r:
            if r["preset"] not in PRESETS:
                raise ValueError(f"house_rules: preset is one of {', '.join(PRESETS)}.")
            rule: dict[str, Any] = {"preset": r["preset"]}
        elif "avoid" in r:
            rule = {"avoid": phrases(r["avoid"], "avoid")}
        elif "prefer" in r:
            if not isinstance(r["prefer"], str) or not r["prefer"].strip():
                raise ValueError("house_rules: “prefer” is the wording to use.")
            rule = {"prefer": r["prefer"].strip()[:MAX_PHRASE], "instead_of": phrases(r.get("instead_of"), "instead_of")}
        elif "note" in r:
            if not isinstance(r["note"], str) or not r["note"].strip():
                raise ValueError("house_rules: “note” is the rule in words.")
            rule = {"note": r["note"].strip()[:MAX_NOTE]}
        else:
            raise ValueError("house_rules: a rule has preset, avoid, prefer (+ instead_of) or note.")
        if why:
            rule["why"] = why
        out.append(rule)
    for i, rule in enumerate(out, 1):
        rule["id"] = f"R{i}"
    return out


def describe(rule: dict[str, Any]) -> str:
    if "preset" in rule:
        return PRESETS[rule["preset"]]
    if "avoid" in rule:
        return "avoid " + ", ".join(f"“{p}”" for p in rule["avoid"][:6]) + ("…" if len(rule["avoid"]) > 6 else "")
    if "prefer" in rule:
        return f"say “{rule['prefer']}” instead of " + ", ".join(f"“{p}”" for p in rule["instead_of"][:6])
    return rule.get("note", "")


def short(rule: dict[str, Any]) -> str:
    """Rövid jel a bekezdésenkénti tételben (a teljes leírás egyszer, a validate house_rules mezőjében)."""
    if "preset" in rule:
        return {"hu_modal": "-hat/-het"}.get(rule["preset"], rule["preset"])
    if "prefer" in rule:
        return f"say “{rule['prefer']}”"
    return "avoid"


def hits(text: str, rule: dict[str, Any]) -> list[str]:
    if rule.get("preset") == "hu_modal":
        return _hu_modal_hits(text)
    if "avoid" in rule:
        return _phrase_hits(text, rule["avoid"])
    if "prefer" in rule:
        return _phrase_hits(text, rule["instead_of"])
    return []


def check(blocks: list, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """validate-tételek: bekezdésenként EGY (az összes szabály találatával), szabályonként legfeljebb MAX_HITS
    bekezdés, utána egy összegző tétel."""
    from app.tools.validate import WARNING, _goto, _issue
    checked = [r for r in rules if "note" not in r]
    if not checked:
        return []
    shown: dict[str, int] = {r["id"]: 0 for r in checked}
    more: dict[str, int] = {r["id"]: 0 for r in checked}
    out: list[dict[str, Any]] = []
    for b in blocks:
        texts = [b.text] if b.kind == "paragraph" else [c for row in b.rows for c in row]
        parts, words = [], []
        for r in checked:
            found = [h for t in texts for h in hits(t, r)]
            if not found:
                continue
            if shown[r["id"]] >= MAX_HITS:
                more[r["id"]] += 1
                continue
            shown[r["id"]] += 1
            uniq = list(dict.fromkeys(found))
            parts.append(f"{r['id']} ({short(r)}): " + ", ".join(f"“{h}”" for h in uniq[:8]))
            words += uniq[:8]
        if parts:
            out.append(_issue("house_rule", WARNING, "House rules (see house_rules) — " + "; ".join(parts) + ".", None, None, b,
                              fix=_goto(b, "reword " + ", ".join(f"“{w}”" for w in words[:5]) + " as a tracked change")))
    for rid, n in more.items():
        if n:
            out.append(_issue("house_rule", WARNING, f"House rule {rid}: {n} more paragraph(s) break it (only the first "
                              f"{MAX_HITS} are listed)."))
    for it in out:
        it["_structural"] = True
    return out


# ---------- tárolás (Postgres, felhasználónként) ----------

async def load(uid: Optional[str] = None) -> list[dict[str, Any]]:
    from app import db, tenancy
    if not db.enabled():
        return []
    rows = await db._rows("SELECT rules FROM house_rules WHERE owner_id = %s", (uid or tenancy.current_user_id(),))
    rules = rows[0]["rules"] if rows else []
    return rules if isinstance(rules, list) else json.loads(rules or "[]")


async def save(raw: Any, uid: Optional[str] = None) -> list[dict[str, Any]]:
    from app import db, tenancy
    rules = normalize(raw)
    if not db.enabled():
        raise RuntimeError("House rules are saved in the database (DATABASE_URL) — it is not available right now.")
    owner = uid or tenancy.current_user_id()
    if rules:
        await db._rows("INSERT INTO house_rules (owner_id, rules) VALUES (%s, %s) ON CONFLICT (owner_id) "
                       "DO UPDATE SET rules = EXCLUDED.rules, updated_at = now()",
                       (owner, json.dumps(rules, ensure_ascii=False)))
    else:
        await db._rows("DELETE FROM house_rules WHERE owner_id = %s", (owner,))
    return rules
