"""
SiliconFlow LLM client (OpenAI-compatible chat completions) + the shared _call_model helper.

Used by edit_with_instruction to compile natural-language edit instructions
into structured edit-operation lists.
"""
from __future__ import annotations

import json
import logging
import re

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings

log = logging.getLogger("aice.llm")


SYSTEM_PROMPT = """\
You are a precise document-editing planner. Given:
  - a document's textual content (DOCX/MD/HTML/PPTX, simplified to plain structure)
  - the document's format (so you pick format-appropriate operations)
  - a natural-language editing instruction

Your task: return a STRICT JSON plan of deterministic edit operations. The
host system will execute each operation in order.

Allowed operations (use exactly these `type` values):
  {"type": "find_replace",   "pattern": str, "replacement": str, "regex": bool}
  {"type": "delete_section", "heading": str, "level": int|null}
  {"type": "insert_after",   "anchor": str,  "content": str}
  {"type": "replace_heading","old_title": str, "new_title": str}
  {"type": "delete_pages",   "page_range": str}    // PDF or PPTX, e.g. "3,5-7"

Output schema (return ONLY this JSON, no prose, no fences):
{
  "operations": [ <ops...> ],
  "warnings":   [ <strings> ],
  "confidence": <float 0..1>
}

Rules:
- If the instruction is ambiguous or risky, set confidence < 0.7 and add
  warnings. The host will surface the warnings before executing.
- Prefer `find_replace` with regex=false and exact strings when possible.
- For "rewrite this section to be more formal" style requests, generate the
  rewritten text yourself and use a single find_replace with the EXACT old
  text (extracted from the document content) and the new text.
- Do NOT invent headings or anchors that don't appear in the document.
- Output strictly valid JSON.

Format-specific notes:
- PPTX: the document text is shown grouped by slides with "### Slide N: TITLE"
  markers — these markers are NOT part of the actual file content; do NOT use
  them in `find_replace` patterns. Use them only to understand structure.
    * To rename a slide title:    use `replace_heading` with old_title = the
      slide's title (WITHOUT the "### Slide N:" prefix), new_title = the new title.
    * To delete an entire slide:  use `delete_pages` with page_range = the
      slide's 1-based index (e.g. "3" or "2,4-5").
    * To delete a slide by title: prefer `delete_section` with heading = the
      slide's title (level is ignored for PPTX).
    * To change body text:        use `find_replace` with the exact body text
      that appears INSIDE the slide (never include the "### Slide N:" marker).
- PDF: only `delete_pages` is supported as a deterministic op; for textual
  rewrites the host round-trips PDF→DOCX→edits→PDF, so choose ops as for DOCX.
- DOCX/MD/HTML: all ops apply naturally; `delete_pages` is not supported.
"""


def _strip_json_fences(s: str) -> str:
    """Some models still wrap JSON in fences despite instructions."""
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


class LLMServiceError(RuntimeError):
    """A SiliconFlow HTTP-hibája felhasználónak mondható szöveggel; a státusz és a válasz eleje a naplóban."""

    def __init__(self, status: int, model: str):
        self.status = status
        msg = ("the AI service is out of credit right now — try again later" if status == 402
               else "the AI service is busy right now — try again in a minute" if status == 429 or status >= 500
               else f"the AI service refused the request (HTTP {status})")
        super().__init__(msg)


def _retryable(e: BaseException) -> bool:
    # 4xx (a 429 kivételével) újrapróbálva is ugyanaz — ne várakoztasson
    return not (isinstance(e, LLMServiceError) and 400 <= e.status < 500 and e.status != 429)


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4), retry=retry_if_exception(_retryable), reraise=True)
async def _call_model(model: str, messages: list[dict], thinking: bool = True, json_mode: bool = True) -> str:
    if not settings.SILICONFLOW_API_KEY:
        raise RuntimeError("SILICONFLOW_API_KEY not set")
    from app import billing
    await billing.ai_step()          # MCP-ről: a szerver saját modellje dolgozik — AI-lépés (havi keret, felette kredit)

    async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT_S) as client:
        r = await client.post(
            f"{settings.SILICONFLOW_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.SILICONFLOW_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                **({"response_format": {"type": "json_object"}} if json_mode else {}),   # szabad szöveghez (levél) ki
                "temperature": 0.2,
                "max_tokens": 4096,
                # gondolkodás nélkül ~2,5× gyorsabb (pakli-vázlat GLM-5.3-Flash: 30,8 s → 12,1 s, 10-11)
                **({} if thinking else {"enable_thinking": False}),
            },
        )
        if r.status_code >= 400:   # a RetryError eddig elnyelte a státuszt — most a naplóban is látszik
            log.warning("SiliconFlow %s → HTTP %s: %s", model, r.status_code, r.text[:300])
            raise LLMServiceError(r.status_code, model)
        data = r.json()
        return data["choices"][0]["message"]["content"]


async def plan_edits(
    document_text: str, instruction: str, format_hint: str = ""
) -> tuple[dict, str]:
    """
    Returns (plan_dict, model_used).
    `format_hint` is the source format (e.g. "pptx", "docx") so the planner
    can pick format-appropriate operations.
    Tries Kimi K2.6 first, falls back to DeepSeek V3.2 on failure or if
    the JSON parse fails.
    """
    fmt_line = f"DOCUMENT FORMAT: {format_hint}\n\n" if format_hint else ""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{fmt_line}"
                f"DOCUMENT CONTENT:\n```\n{document_text[:30000]}\n```\n\n"
                f"INSTRUCTION:\n{instruction}\n\n"
                f"Return the JSON plan now."
            ),
        },
    ]

    # Primary: Kimi K2.6
    try:
        raw = await _call_model(settings.LLM_MODEL_PRIMARY, messages)
        plan = json.loads(_strip_json_fences(raw))
        return plan, settings.LLM_MODEL_PRIMARY
    except (json.JSONDecodeError, httpx.HTTPError, RuntimeError) as e:
        log.warning("Primary model failed: %s — falling back", e)

    # Fallback: DeepSeek V3.2
    raw = await _call_model(settings.LLM_MODEL_FALLBACK, messages)
    plan = json.loads(_strip_json_fences(raw))
    return plan, f"{settings.LLM_MODEL_FALLBACK}_fallback"
