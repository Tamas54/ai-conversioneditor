"""
OCR-mérés könyvoldalakon — melyik vision-modell olvas jobban (és jelöli jól az illusztrációkat).

Ugyanazokat az oldalakat (PDF → 200 dpi PNG → a production kicsinyítő) minden modell a `digitize`
book-v2 promptjával kapja. Referencia: a PDF meglévő szövegrétege (pl. ABBYY) — NEM tökéletes, ezért
a pontszám mellé a modellek egymással való egyezését és a szavas eltéréseket is kiírjuk kézi ellenőrzésre.

Usage:
    .venv/bin/python tests/ocr_book_bench.py OUT_DIR PDF:PAGE[,PAGE…] [PDF:PAGE…] [--models a,b,c] [--edge 1280]

Output: OUT_DIR/<model>__<pdf>-p<page>.md, OUT_DIR/results.json, OUT_DIR/summary.md
"""
from __future__ import annotations

import asyncio
import difflib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text().splitlines():
    k, _, v = line.partition("=")
    if k and not k.startswith("#") and v:
        import os
        os.environ.setdefault(k.strip(), v.strip())

from app.config import settings  # noqa: E402
from app.llm import vision  # noqa: E402
from app.tools import digitize  # noqa: E402

MODELS = ["moonshotai/Kimi-K2.6", "Qwen/Qwen3.8-27B", "moonshotai/Kimi-K3"]


def norm(s: str) -> str:
    s = re.sub(r"\[\[FIGURE[^\]]*\]\]", " ", s)
    s = re.sub(r"\[\^[\w-]+\]:?", " ", s)
    s = re.sub(r"^#+\s*", "", s, flags=re.M)
    s = re.sub(r"-\s*\n\s*", "", s)                      # sorvégi elválasztás
    s = s.replace("–", "-").replace("—", "-").replace("„", '"').replace("”", '"').replace("’", "'")
    return re.sub(r"\s+", " ", s).strip()


def words(s: str) -> list[str]:
    return re.findall(r"\w+", norm(s).lower())


def ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, words(a), words(b), autojunk=False).ratio()


def diff_words(ref: str, hyp: str, n: int = 12) -> list[str]:
    a, b = words(ref), words(hyp)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            out.append(f"{' '.join(a[i1:i2])[:40]!r} → {' '.join(b[j1:j2])[:40]!r}")
        if len(out) >= n:
            break
    return out


async def call(model: str, img: Path, edge: int) -> dict:
    vision._MAX_IMAGE_LONG_EDGE = edge
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": digitize._PROMPT.format(hint="")},
        {"type": "image_url", "image_url": {"url": vision.image_to_data_url(img)}},
    ]}]
    t0 = time.time()
    async with httpx.AsyncClient(timeout=400) as c:
        for attempt in range(3):
            r = await c.post(f"{settings.SILICONFLOW_BASE_URL}/chat/completions",
                             headers={"Authorization": f"Bearer {settings.SILICONFLOW_API_KEY}"},
                             json={"model": model, "messages": msgs, "max_tokens": 6000, "temperature": 0.1,
                                   "enable_thinking": False})
            if r.status_code < 500:
                break
            await asyncio.sleep(5)
    d = r.json()
    if r.status_code != 200:
        return {"error": str(d)[:300], "s": time.time() - t0}
    return {"text": digitize._clean(d["choices"][0]["message"].get("content") or ""), "s": round(time.time() - t0, 1),
            "usage": d.get("usage", {})}


async def main() -> None:
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    opts = dict(a[2:].split("=", 1) for a in sys.argv[2:] if a.startswith("--"))
    models = opts.get("models", ",".join(MODELS)).split(",")
    edge = int(opts.get("edge", "1280"))
    dpi = opts.get("dpi", "200")
    pages = []
    for spec in args:
        pdf, _, nums = spec.rpartition(":")
        for p in nums.split(","):
            img = out / f"{Path(pdf).stem[-9:]}-p{p}-{dpi}.png"
            if not img.exists():
                subprocess.run(["pdftoppm", "-r", dpi, "-png", "-f", p, "-l", p, "-singlefile", pdf, str(img.with_suffix(""))], check=True)
            ref = subprocess.run(["pdftotext", "-f", p, "-l", p, pdf, "-"], capture_output=True, text=True).stdout
            pages.append((f"{Path(pdf).stem[-9:]}-p{p}", img, ref))
    gate = asyncio.Semaphore(6)

    async def one(model, key, img):
        async with gate:
            return model, key, await call(model, img, edge)
    res = await asyncio.gather(*(one(m, k, i) for m in models for k, i, _ in pages))
    table: dict = {}
    for model, key, r in res:
        table.setdefault(model, {})[key] = r
        if "text" in r:
            (out / f"{model.split('/')[-1]}__{key}__e{edge}.md").write_text(r["text"], "utf-8")
    refs = {k: ref for k, _, ref in pages}
    lines = [f"# OCR-mérés ({len(pages)} oldal, long edge {edge}px)\n",
             "| modell | átl. egyezés a PDF-réteggel | átl. idő/oldal | kimenő token/oldal | ábra-jelölés |", "|---|---|---|---|---|"]
    for m in models:
        rs = [table[m][k] for k in refs]
        ok = [r for r in rs if "text" in r]
        sim = sum(ratio(refs[k], table[m][k]["text"]) for k in refs if "text" in table[m][k]) / max(1, len(ok))
        figs = sum(len(re.findall(r"\[\[FIGURE", r["text"])) for r in ok)
        lines.append(f"| {m} | {sim:.3f} | {sum(r['s'] for r in rs) / len(rs):.1f} s | "
                     f"{sum(r.get('usage', {}).get('completion_tokens', 0) for r in ok) // max(1, len(ok))} | {figs} "
                     f"{'' if len(ok) == len(rs) else f'⚠ {len(rs) - len(ok)} hiba'} |")
    lines.append("\n## Oldalanként (egyezés a PDF-réteggel)\n")
    lines.append("| oldal | " + " | ".join(m.split('/')[-1] for m in models) + " |")
    lines.append("|---|" + "---|" * len(models))
    for k in refs:
        lines.append(f"| {k} | " + " | ".join(f"{ratio(refs[k], table[m][k]['text']):.3f}" if "text" in table[m][k] else "HIBA"
                                             for m in models) + " |")
    lines.append("\n## Eltérések a PDF-rétegtől (első 12 / oldal)\n")
    for k in refs:
        lines.append(f"### {k}")
        for m in models:
            if "text" in table[m][k]:
                lines.append(f"- **{m.split('/')[-1]}**: " + "; ".join(diff_words(refs[k], table[m][k]["text"])))
    (out / f"summary_e{edge}.md").write_text("\n".join(lines), "utf-8")
    (out / f"results_e{edge}.json").write_text(json.dumps(table, ensure_ascii=False, indent=1), "utf-8")
    print("\n".join(lines[:4 + len(models)]))


if __name__ == "__main__":
    asyncio.run(main())
