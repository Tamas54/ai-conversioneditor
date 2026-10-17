"""A videók háttérzenéje (CC0) a Docker-képbe: app/tools/music_library.json → MUSIC_DIR (alap /opt/music).

Fájlonként a rögzített forrás-commitról tölt, és SHA256-tal ellenőriz — eltérésnél a build megáll (nem kerül be
ismeretlen fájl). Csak a szabványkönyvtárat használja (a Dockerfile a pip előtt is futtathatja).
    python scripts/fetch_music.py [/opt/music]
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(dst: Path) -> int:
    lib = json.loads((ROOT / "app" / "tools" / "music_library.json").read_text("utf-8"))
    base = lib["source"]["base_url"]
    for t in lib["tracks"]:
        out = dst / t["file"]
        if out.exists() and hashlib.sha256(out.read_bytes()).hexdigest() == t["sha256"]:
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(5):
            try:
                with urllib.request.urlopen(base + t["file"], timeout=60) as r:
                    data = r.read()
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(3)
        if hashlib.sha256(data).hexdigest() != t["sha256"]:
            print(f"SHA256 mismatch: {t['file']}", file=sys.stderr)
            return 1
        out.write_bytes(data)
    print(f"{len(lib['tracks'])} music tracks in {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/music")))
