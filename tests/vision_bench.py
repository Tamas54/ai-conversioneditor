"""
Vision benchmark — Kimi K2.6 vs Qwen3-VL-30B-A3B vs Gemma-4-31B.

Three models × three task profiles on the same skewed photo of a Hungarian
book page (text + two bar charts). Measures wall time, output length, JSON
parse success on the structured task, and dumps full outputs for manual
quality review.

Usage:
    .venv/bin/python tests/vision_bench.py [path_to_image]

Default image: /home/tamas1/Downloads/IMG_7389.jpeg

Output:
    /tmp/vision_bench/<model>__<task>.txt   — raw output per cell
    /tmp/vision_bench/summary.md             — comparison table
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

# Make the app package importable so we reuse the production downscaler.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load .env so SILICONFLOW_API_KEY is set before importing config.
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

OUT_DIR = Path("/tmp/vision_bench")
OUT_DIR.mkdir(parents=True, exist_ok=True)

MODELS = [
    "moonshotai/Kimi-K2.6",
    "Qwen/Qwen3-VL-30B-A3B-Instruct",
    "google/gemma-4-31B-it",
]

TASKS = {
    "ocr_full": (
        "Extract ALL visible Hungarian text from this photo of a book page. "
        "Preserve paragraph breaks and reading order as best you can. The page "
        "is skewed and there is a hand on the right edge — ignore the hand and "
        "the table-of-contents text bleeding from the facing page on the left. "
        "Output ONLY the body text of the right-hand page, no commentary."
    ),
    "describe": (
        "Briefly describe (5-7 short bullets): what is the main heading, how "
        "many bar charts are visible, what is each chart's title, what is the "
        "approximate value range of the bars, what is the dominant color, what "
        "page number is shown, what is the source citation."
    ),
    "chart_extract": (
        "Extract the data from BOTH bar charts as STRICT JSON with this shape:\n"
        '{"charts": [{"title": "...", "items": [{"label": "...", "value_pct": 12}, ...]}, ...]}\n\n'
        "Rules:\n"
        "- One object per chart (there are 2).\n"
        "- title = the exact Hungarian header above the chart.\n"
        "- items = each row in the chart, with the bar label and the percent value.\n"
        "- value_pct as a number (no % sign, no quotes).\n"
        "- Output ONLY the JSON, no prose, no markdown fences."
    ),
}

# Yesterday's ground truth from devlog20260501.md §4 (manually verified).
# We compare numerically on the chart_extract task.
GROUND_TRUTH = {
    # "leginkább kiválthatónak vélt" / most replaceable:
    "Pénztáros": 63, "Sofőr": 51, "Fordító": 42, "Árufeltöltő": 26,
    "Ügyfélszolgálatos": 24, "Könyvelő": 23, "Takarító": 21,
    "Pultos": 19, "Pincér": 17,
    # "legkevésbé kiválthatónak vélt" / least replaceable:
    "Művész": 39, "Zenész": 36, "Terapeuta": 35, "Rendőr": 31,
    "Orvos": 30, "Ügyvéd": 26, "Ápoló": 21, "Villanyszerelő": 19,
    "Autószerelő": 16,
}


async def call_one(client: httpx.AsyncClient, model: str, task: str,
                   prompt: str, data_url: str) -> dict:
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        "max_tokens": 4096,
        "temperature": 0.1,
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
                err = r.json().get("error", {}).get("message") or r.text[:300]
            except Exception:
                err = r.text[:300]
            return {"ok": False, "elapsed_s": elapsed, "status": r.status_code,
                    "error": err, "content": ""}
        data = r.json()
        content = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage", {})
        return {"ok": True, "elapsed_s": elapsed, "content": content,
                "completion_tokens": usage.get("completion_tokens", 0),
                "prompt_tokens": usage.get("prompt_tokens", 0)}
    except Exception as e:
        return {"ok": False, "elapsed_s": time.monotonic() - t0,
                "error": f"{type(e).__name__}: {e}", "content": ""}


def score_chart_extraction(content: str) -> dict:
    """Return: {parsed: bool, n_items: int, n_correct: int, off_by: list}."""
    s = content.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    try:
        data = json.loads(s.strip())
    except json.JSONDecodeError:
        return {"parsed": False, "n_items": 0, "n_correct": 0, "off_by": []}
    items = []
    for chart in data.get("charts", []):
        for it in chart.get("items", []):
            items.append((it.get("label", "").strip(), it.get("value_pct")))
    n_correct = 0
    off = []
    for label, val in items:
        truth = None
        for tlabel, tval in GROUND_TRUTH.items():
            if tlabel.lower() == label.lower() or tlabel.lower() in label.lower():
                truth = tval
                break
        if truth is None:
            off.append(f"{label}={val} (label not in truth)")
            continue
        try:
            v = int(val)
        except (TypeError, ValueError):
            off.append(f"{label}={val} (non-int)")
            continue
        if v == truth:
            n_correct += 1
        else:
            off.append(f"{label}: got {v}, truth {truth}")
    return {"parsed": True, "n_items": len(items), "n_correct": n_correct, "off_by": off}


async def main(image_path: Path):
    print(f"[bench] image: {image_path} ({image_path.stat().st_size:,} bytes)")
    t = time.monotonic()
    data_url = image_to_data_url(image_path)
    print(f"[bench] downscaled+encoded: {len(data_url):,} chars, "
          f"{time.monotonic() - t:.2f}s")

    rows = []
    async with httpx.AsyncClient() as client:
        for model in MODELS:
            for task, prompt in TASKS.items():
                print(f"[bench] {model:42s}  {task:14s} ...", end=" ", flush=True)
                res = await call_one(client, model, task, prompt, data_url)
                tag = "OK " if res["ok"] else "ERR"
                print(f"{tag} {res['elapsed_s']:5.1f}s "
                      f"{len(res.get('content', '')):5d}ch "
                      f"{res.get('completion_tokens', 0):4d}tok")
                # dump output
                slug = model.replace("/", "_")
                (OUT_DIR / f"{slug}__{task}.txt").write_text(
                    f"# {model} / {task}\n# elapsed: {res['elapsed_s']:.2f}s "
                    f"ok={res['ok']}\n\n"
                    + (res.get("content") or res.get("error", ""))
                )
                row = {"model": model, "task": task, **res}
                if task == "chart_extract" and res["ok"]:
                    row["score"] = score_chart_extraction(res["content"])
                rows.append(row)

    # Build summary
    lines = ["# Vision Benchmark Summary\n",
             f"Image: `{image_path}` ({image_path.stat().st_size:,} bytes)\n"]
    lines.append("\n## Latency / size\n")
    lines.append("| Model | Task | OK | Time (s) | Out chars | Out tok |")
    lines.append("|-------|------|----|---------:|----------:|--------:|")
    for r in rows:
        ok = "✓" if r["ok"] else "✗"
        lines.append(f"| `{r['model']}` | {r['task']} | {ok} | "
                     f"{r['elapsed_s']:.1f} | {len(r.get('content', ''))} | "
                     f"{r.get('completion_tokens', 0)} |")
    lines.append("\n## Chart accuracy (ground truth: 18 values)\n")
    lines.append("| Model | Parsed JSON | Items found | Correct | Wrong/missing |")
    lines.append("|-------|:-----------:|------------:|--------:|---------------|")
    for r in rows:
        if r["task"] != "chart_extract":
            continue
        s = r.get("score") or {}
        if not r["ok"]:
            lines.append(f"| `{r['model']}` | — | — | — | ERROR: {r.get('error', '')[:80]} |")
            continue
        off_summary = ("; ".join(s.get("off_by", []))[:200]) or "—"
        parsed = "✓" if s.get("parsed") else "✗"
        lines.append(f"| `{r['model']}` | {parsed} | {s.get('n_items', 0)} | "
                     f"{s.get('n_correct', 0)} / 18 | {off_summary} |")
    lines.append("\n## Errors\n")
    for r in rows:
        if not r["ok"]:
            lines.append(f"- `{r['model']}` / {r['task']} (status="
                         f"{r.get('status', '?')}): {r.get('error', '')[:300]}")
    summary = "\n".join(lines) + "\n"
    (OUT_DIR / "summary.md").write_text(summary)
    print("\n" + summary)
    print(f"\nFull per-cell outputs in: {OUT_DIR}/")


if __name__ == "__main__":
    img = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/home/tamas1/Downloads/IMG_7389.jpeg")
    if not img.exists():
        sys.exit(f"image not found: {img}")
    if not settings.SILICONFLOW_API_KEY:
        sys.exit("SILICONFLOW_API_KEY not set")
    asyncio.run(main(img))
