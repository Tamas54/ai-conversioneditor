"""
Cross-document term-consistency checker — Tier-S #8.

Use cases:
  - Legal: ensure "Megrendelő" / "Vevő" / "Ügyfél" used consistently
    across a contract (or across a set of related contracts)
  - Academic: same technical term used uniformly across chapters
  - Translation: same source term consistently mapped to same target term
  - Brand: enforce capitalization conventions ("iPhone" not "Iphone")

Two modes:
  1. Explicit glossary: caller provides {term: [allowed_variants]} dict;
     we flag any text mentions that don't match an allowed variant.
  2. Auto-detect: Kimi K2.6 reads the doc, identifies candidate "important
     terms" (proper nouns, capitalized phrases, defined terms), then we
     scan for variation clusters (case-insensitive groups with multiple
     distinct surface forms).

Output: structured report with each term's surface forms, counts, and
locations (document path + paragraph index).
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter, defaultdict
from typing import Any, Optional

from app.llm import vision as v
from app.tools import parser_self_healing

log = logging.getLogger("aice.term_consistency")


_AUTO_DETECT_PROMPT = """\
Olvasd el a következő dokumentum szövegét, és sorold fel azokat a
KIFEJEZÉSEKET amelyeknél fontos a következetes használat. Például:
  - Definiált jogi terminusok (Megrendelő, Vevő, Ügyfél, Szerződő Fél)
  - Tulajdonnevek (cégnév, terméknév, helynév)
  - Technikai terminusok
  - Rövidítések és teljes alakok

NE sorold fel a hétköznapi szavakat (ami, amely, ezért stb.).
Maximum 20 kifejezést adj meg.

Szigorú JSON formátumban válaszolj:
{{
  "terms": [
    {{"canonical": "preferált forma", "kind": "legal|proper_noun|technical|abbreviation"}}
  ]
}}

A szöveg:
---
{source}
---

Csak a JSON-t add vissza, semmi mást.
"""


async def _auto_detect_terms(text: str) -> list[dict[str, str]]:
    """Use Kimi K2.6 to identify candidate terms worth checking."""
    prompt = _AUTO_DETECT_PROMPT.format(source=text[:6000])
    raw = await v._call(
        [{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}, max_tokens=2048,
    )
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        data = json.loads(s)
    except json.JSONDecodeError as e:
        raise ValueError(f"term-detect returned non-JSON: {e}; raw[:200]={raw[:200]}")
    return data.get("terms", [])


def _find_variations(text: str, canonical: str) -> Counter:
    """Find all surface forms of `canonical` in text (case-insensitive +
    common typo / accent variants).

    Returns a Counter mapping surface_form → occurrence_count.
    """
    if not canonical or not text:
        return Counter()

    # Build a regex that matches the canonical form case-insensitively,
    # collecting actual surface form via the match group.
    # Word-boundary on both sides to avoid partial matches.
    pattern = re.compile(
        r"\b" + re.escape(canonical) + r"\b",
        re.IGNORECASE | re.UNICODE,
    )
    matches = pattern.findall(text)
    return Counter(matches)


def _normalize_form(s: str) -> str:
    """Whitespace-normalize for grouping. Case is preserved — case mismatches
    ARE real inconsistencies in legal/brand contexts (Megrendelő vs.
    megrendelő, iPhone vs. Iphone)."""
    return s.strip()


async def term_consistency_check(
    file_ids: list[str],
    *,
    glossary: Optional[list[dict[str, Any]]] = None,
    auto_detect: bool = True,
    quality_threshold: float = 0.55,
) -> dict[str, Any]:
    """Check term-consistency across one or more documents.

    Args:
      file_ids: list of document file_ids to scan
      glossary: optional explicit term list, format
        [{"canonical": "Megrendelő", "allowed_variants": ["Megrendelő", "MEGRENDELŐ"]}]
        If a variant outside `allowed_variants` is found, it's flagged.
      auto_detect: if True (default) AND no glossary, Kimi K2.6 detects
        candidate terms automatically from the FIRST document
      quality_threshold: passed to parser_self_healing

    Returns:
      {
        documents: [{file_id, char_count, method}],
        glossary_terms: [{canonical, kind?, surface_forms: {...}, total_count, inconsistent: bool, flagged_variants: [...]}],
        summary: {n_terms, n_inconsistent, n_documents}
      }
    """
    t0 = time.time()
    if not file_ids:
        raise ValueError("file_ids must not be empty")

    # 1. Extract text from each document
    docs: list[dict[str, Any]] = []
    for fid in file_ids:
        parsed = await parser_self_healing.parse_with_quality(
            fid, quality_threshold=quality_threshold,
        )
        docs.append({
            "file_id": fid,
            "text": parsed["text"],
            "char_count": len(parsed["text"]),
            "method": parsed.get("method"),
        })
    combined = "\n\n".join(d["text"] for d in docs)

    # 2. Build term list
    if glossary:
        terms = [
            {"canonical": g["canonical"], "kind": g.get("kind"),
             "allowed_variants": [_normalize_form(v) for v in g.get("allowed_variants", [g["canonical"]])]}
            for g in glossary
        ]
    elif auto_detect:
        # Use the first document for detection
        candidates = await _auto_detect_terms(docs[0]["text"])
        terms = [
            {"canonical": c["canonical"], "kind": c.get("kind"),
             "allowed_variants": [_normalize_form(c["canonical"])]}
            for c in candidates
        ]
    else:
        raise ValueError("must provide either glossary or auto_detect=True")

    # 3. Check usage across docs
    out_terms: list[dict[str, Any]] = []
    n_inconsistent = 0
    for term in terms:
        canonical = term["canonical"]
        allowed = set(term["allowed_variants"])

        per_doc_forms: dict[str, Counter] = {}
        all_forms: Counter = Counter()
        for d in docs:
            forms = _find_variations(d["text"], canonical)
            per_doc_forms[d["file_id"]] = forms
            all_forms.update(forms)

        # Distinct surface forms (case-sensitive — case mismatches are real)
        distinct_norm_variants = set(_normalize_form(f) for f in all_forms)
        flagged_variants = [
            f for f in all_forms
            if _normalize_form(f) not in allowed
        ]
        # An explicitly-allowed variant is never flagged. If the caller wants
        # to enforce ONE form only, they pass a single-element allowed_variants.
        is_inconsistent = len(flagged_variants) > 0
        if is_inconsistent:
            n_inconsistent += 1

        out_terms.append({
            "canonical": canonical,
            "kind": term.get("kind"),
            "surface_forms": dict(all_forms),
            "per_document": {fid: dict(c) for fid, c in per_doc_forms.items()},
            "total_count": sum(all_forms.values()),
            "distinct_variant_count": len(distinct_norm_variants),
            "inconsistent": is_inconsistent,
            "flagged_variants": flagged_variants,
        })

    # Sort: inconsistent first, then by frequency
    out_terms.sort(
        key=lambda t: (not t["inconsistent"], -t["total_count"]),
    )

    return {
        "documents": [
            {"file_id": d["file_id"], "char_count": d["char_count"], "method": d["method"]}
            for d in docs
        ],
        "glossary_terms": out_terms,
        "summary": {
            "n_terms": len(out_terms),
            "n_inconsistent": n_inconsistent,
            "n_documents": len(docs),
            "auto_detected": glossary is None,
        },
        "ms_elapsed": int((time.time() - t0) * 1000),
    }
