"""
MTMT (Magyar Tudományos Művek Tára) integration — Tier-S #9.

Two distinct sub-features:

  1. CITATION EXTRACTION (works today, no external API):
     Use Kimi K2.6 to parse academic citations out of any document
     (footnote-style, parenthetical, in-text bibliography). Returns
     structured records [{authors, title, year, journal, doi, kind}].

  2. MTMT LOOKUP (PLACEHOLDER — MTMT REST API not publicly documented):
     Verify a citation against MTMT's catalog. The skeleton is here so
     the agent can call it; full impl pending working API access via
     m2.mtmt.hu OAI-PMH or REST endpoints.

Practical value of (1) alone: bibliography deduplication, citation-style
normalization, missing-field detection — all without MTMT.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.llm import vision as v
from app.tools import parser_self_healing

log = logging.getLogger("aice.mtmt")


_CITATION_EXTRACT_PROMPT = """\
Olvasd el a következő szöveget, és sorolj fel MINDEN benne található
tudományos hivatkozást (idézet, lábjegyzet, irodalomjegyzék-tétel).

Szigorú JSON formátumban válaszolj:
{{
  "citations": [
    {{
      "authors": ["Vezetéknév Keresztnév", ...],
      "title": "string or null",
      "year": "string or null (4-digit if known)",
      "journal_or_publisher": "string or null",
      "volume": "string or null",
      "issue": "string or null",
      "pages": "string or null (pl. '120-145')",
      "doi": "string or null",
      "isbn": "string or null",
      "kind": "journal | book | chapter | thesis | conference | other",
      "raw": "the original text snippet that became this citation"
    }}
  ]
}}

Szabályok:
  - Ha egy hivatkozás többször előfordul, csak EGYSZER szerepeljen
  - "et al." / "és tsai." → csak az első szerző + üres marad a többi név
  - Évszám pontos 4 jegyű forma (ha a forrás azt adja meg)
  - DOI: csak a 10.xxxx/yyyy alakot, "doi:" prefix nélkül
  - NE találj ki adatokat — ha nincs benne, írj null-t
  - Üres "citations": [] ha nincs hivatkozás

A szöveg:
---
{source}
---

Csak a JSON-t add vissza, semmi mást.
"""


async def extract_citations(
    file_id: str, *, max_chars: int = 12000,
) -> dict[str, Any]:
    """Extract structured academic citations from a document via Kimi K2.6.

    Works on any format (PDF/DOCX/HTML/MD) — uses parse_with_quality first.
    Returns: {citations: [...], n_citations, source_method}
    """
    t0 = time.time()
    parsed = await parser_self_healing.parse_with_quality(file_id)
    text = parsed["text"]
    if not text.strip():
        return {"citations": [], "n_citations": 0,
                "source_method": parsed.get("method"),
                "ms_elapsed": int((time.time() - t0) * 1000)}

    # Split into chunks if doc is huge — citations are usually localized to
    # a bibliography section + footnotes, so we process the WHOLE doc but
    # may need multiple Kimi calls for very long files.
    chunks = []
    for i in range(0, min(len(text), max_chars * 3), max_chars):
        chunks.append(text[i:i + max_chars])

    all_citations: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for chunk in chunks:
        prompt = _CITATION_EXTRACT_PROMPT.format(source=chunk)
        raw = await v._call(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}, max_tokens=4096,
        )
        s = raw.strip()
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*", "", s)
            s = re.sub(r"\s*```$", "", s)
        try:
            data = json.loads(s)
        except json.JSONDecodeError as e:
            log.warning("citation-extract returned non-JSON: %s", e)
            continue

        for c in data.get("citations", []):
            # Dedup key: first author + year + title-prefix
            authors = c.get("authors") or []
            first_author = (authors[0] if authors else "").strip().lower()
            year = (c.get("year") or "").strip()
            title_prefix = (c.get("title") or "").strip().lower()[:40]
            key = f"{first_author}|{year}|{title_prefix}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_citations.append(c)

    return {
        "citations": all_citations,
        "n_citations": len(all_citations),
        "source_method": parsed.get("method"),
        "n_chunks_processed": len(chunks),
        "ms_elapsed": int((time.time() - t0) * 1000),
    }


# ============================================================
# MTMT lookup — PLACEHOLDER (REST API endpoints unverified)
# ============================================================


_MTMT_BASE_URL = "https://m2.mtmt.hu/api"


async def mtmt_search_author(name: str, *, limit: int = 10) -> dict[str, Any]:
    """PLACEHOLDER: search MTMT for authors by name. The exact REST endpoint
    + response shape needs to be verified against the live MTMT v2 API.
    Returns a stub indicating this is not yet wired."""
    return {
        "status": "not_implemented",
        "tool": "mtmt_search_author",
        "query": name,
        "note": (
            "MTMT REST API endpoints are not publicly documented. "
            "Skeleton exists; integration pending working API access "
            "(possibly via m2.mtmt.hu OAI-PMH). Citation extraction "
            "(extract_citations) works standalone without MTMT."
        ),
    }


async def mtmt_verify_citation(
    citation: dict[str, Any], *, author_id: Optional[str] = None,
) -> dict[str, Any]:
    """PLACEHOLDER: verify a citation matches an MTMT record. See
    mtmt_search_author note about API access."""
    return {
        "status": "not_implemented",
        "tool": "mtmt_verify_citation",
        "citation_preview": {
            k: citation.get(k) for k in ("authors", "title", "year", "doi")
        },
        "note": (
            "MTMT REST API integration pending. Until then, callers can "
            "use extract_citations to get structured citations + manually "
            "cross-check with m2.mtmt.hu web search."
        ),
    }
