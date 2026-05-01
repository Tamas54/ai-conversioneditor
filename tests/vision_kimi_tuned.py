"""
Kimi K2.6 vision tuning — find the fastest payload variant that keeps
the OCR accuracy on the Hungarian book page.

Tests multiple "disable thinking" parameter names since SiliconFlow's
docs don't make explicit which one Kimi honors.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from difflib import SequenceMatcher

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ENV_PATH = ROOT / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

from app.llm.vision import image_to_data_url  # noqa: E402
from app.config import settings  # noqa: E402

IMG = Path("/home/tamas1/Downloads/IMG_7389.jpeg")
MODEL = "moonshotai/Kimi-K2.6"

BASE_PROMPT = (
    "Extract ALL visible Hungarian text from this photo of a book page. "
    "Preserve paragraph breaks and reading order as best you can. The page "
    "is skewed and there is a hand on the right edge — ignore the hand and "
    "the table-of-contents text bleeding from the facing page on the left. "
    "Output ONLY the body text of the right-hand page, no commentary."
)
TIGHT_PROMPT = (
    "Transcribe the Hungarian text from this book page photo. "
    "Output ONLY the verbatim text, paragraph breaks preserved. "
    "Skip: the hand on the right, the bleed from the facing page on the left. "
    "No commentary, no quotes, no markdown, no confidence notes."
)

# Each variant adds extra payload keys on top of the standard request.
VARIANTS = [
    ("baseline",                {}, BASE_PROMPT, 4096),
    ("enable_thinking_false",   {"enable_thinking": False}, BASE_PROMPT, 4096),
    ("thinking_budget_0",       {"thinking_budget": 0}, BASE_PROMPT, 4096),
    ("reasoning_effort_low",    {"reasoning_effort": "low"}, BASE_PROMPT, 4096),
    ("tight_prompt_only",       {}, TIGHT_PROMPT, 2500),
    ("combo_thinkfalse_tight",  {"enable_thinking": False}, TIGHT_PROMPT, 2500),
]


async def call(client, extra_payload: dict, prompt: str, max_tokens: int,
               data_url: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
        "max_tokens": max_tokens,
        "temperature": 0.1,
        **extra_payload,
    }
    t0 = time.monotonic()
    try:
        r = await client.post(
            f"{settings.SILICONFLOW_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {settings.SILICONFLOW_API_KEY}"},
            json=payload,
            timeout=300,
        )
        elapsed = time.monotonic() - t0
        if r.status_code != 200:
            try:
                err = r.json().get("error", {}).get("message") or r.text[:200]
            except Exception:
                err = r.text[:200]
            return {"ok": False, "elapsed": elapsed, "status": r.status_code,
                    "error": err, "content": ""}
        data = r.json()
        msg = data["choices"][0]["message"]
        usage = data.get("usage", {})
        return {
            "ok": True,
            "elapsed": elapsed,
            "content": msg.get("content") or "",
            "reasoning_present": bool(msg.get("reasoning_content")),
            "completion_tokens": usage.get("completion_tokens", 0),
            "prompt_tokens": usage.get("prompt_tokens", 0),
        }
    except Exception as e:
        return {"ok": False, "elapsed": time.monotonic() - t0,
                "error": f"{type(e).__name__}: {e}", "content": ""}


async def main():
    print(f"image: {IMG}")
    data_url = image_to_data_url(IMG)
    print(f"encoded: {len(data_url):,} chars\n")

    # Run baseline first to set up the similarity reference
    results = []
    async with httpx.AsyncClient() as client:
        for name, extra, prompt, max_tok in VARIANTS:
            print(f"  {name:30s} ...", end=" ", flush=True)
            res = await call(client, extra, prompt, max_tok, data_url)
            tag = "OK " if res["ok"] else "ERR"
            t = res["elapsed"]
            ct = res.get("completion_tokens", 0)
            chars = len(res.get("content", ""))
            reason_flag = "T" if res.get("reasoning_present") else "-"
            print(f"{tag} {t:6.1f}s  {chars:5d}ch  {ct:5d}tok  reasoning={reason_flag}")
            if not res["ok"]:
                print(f"     ERROR: {res.get('error', '')[:160]}")
            results.append((name, res))

    # Similarity to baseline
    baseline = next((r for n, r in results if n == "baseline" and r["ok"]), None)
    if baseline:
        print(f"\n--- vs baseline (similarity, time delta) ---")
        for name, r in results:
            if not r["ok"] or name == "baseline":
                continue
            sim = SequenceMatcher(None, baseline["content"], r["content"]).ratio()
            delta = r["elapsed"] - baseline["elapsed"]
            speedup = baseline["elapsed"] / r["elapsed"] if r["elapsed"] else 0
            print(f"  {name:30s} sim={sim*100:5.1f}%  Δt={delta:+6.1f}s  ({speedup:.2f}x)")

    OUT = Path("/tmp/vision_bench_kimi_tuned")
    OUT.mkdir(exist_ok=True)
    for name, r in results:
        (OUT / f"{name}.txt").write_text(
            f"# variant: {name}\n# ok={r['ok']} elapsed={r['elapsed']:.2f}s "
            f"tokens={r.get('completion_tokens', 0)} "
            f"reasoning_returned={r.get('reasoning_present', False)}\n\n"
            + (r.get("content") or r.get("error", ""))
        )
    print(f"\noutputs: {OUT}/")


if __name__ == "__main__":
    asyncio.run(main())
