#!/usr/bin/env python3
"""Minden mintaforrásból minden (a gépen elérhető) célformátumba átalakít, és jelenti a hibákat.

    python scripts/sweep_conversions.py [--only docx,png] [--jobs 4] [--out sweep.json]

Karbantartóknak: új motor, új él vagy motorfrissítés után ezzel derül ki, melyik irány tört el. A mintafájlok
a tests/samples.py-ból jönnek (ékezetes szöveggel), a kimenetek ideiglenes mappába kerülnek.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="sweep-data-"))

SOURCES = {"docx": "docx", "md": "md", "html": "html", "txt": "txt", "pdf": "pdf", "png": "png", "jpg": "jpg", "svg": "svg",
           "wav": "wav", "mp4": "mp4", "zip": "zip", "ttf": "ttf", "dxf": "dxf", "xlsx": "xlsx", "csv": "csv_rows", "pptx": "pptx"}


async def main() -> int:
    from samples import Samples
    from app.convert import graph
    from app.convert.service import convert_path
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    only = {x for x in a.only.split(",") if x}
    work = Path(tempfile.mkdtemp(prefix="sweep-"))
    s = Samples(work)
    pairs = []
    for src, meth in SOURCES.items():
        if only and src not in only:
            continue
        path = getattr(s, meth)()
        for dst in graph.targets(src):
            pairs.append((src, dst, path))
    sem = asyncio.Semaphore(a.jobs)
    results = []

    async def one(src, dst, path):
        async with sem:
            t0 = time.time()
            try:
                outs, used = await convert_path(path, dst, {}, work / f"o-{src}-{dst.replace('.', '_')}")
                ok = bool(outs) and all(o.stat().st_size > 0 for o in outs)
                results.append({"src": src, "dst": dst, "ok": ok, "engines": used, "s": round(time.time() - t0, 2)})
            except Exception as e:
                results.append({"src": src, "dst": dst, "ok": False, "error": str(e)[:300], "s": round(time.time() - t0, 2)})
    await asyncio.gather(*(one(*p) for p in pairs))
    bad = [r for r in results if not r["ok"]]
    for r in sorted(bad, key=lambda r: (r["src"], r["dst"])):
        print(f"FAIL {r['src']}→{r['dst']}: {r.get('error', 'empty output')}")
    print(f"{len(results) - len(bad)}/{len(results)} ok")
    if a.out:
        Path(a.out).write_text(json.dumps(results, indent=1, ensure_ascii=False))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
