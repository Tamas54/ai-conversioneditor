#!/usr/bin/env bash
# Caladea (Cambria) és/vagy Carlito (Calibri) metrika-kompatibilis fontok egy Craft-forrásfa
# assets/fonts/ mappájába — a build/wordcraft-web/fetch-fonts.sh általánosítása (ugyanazok a
# források és SHA256-ok).
#
# Használat: fetch-fonts.sh <forrásfa> <licenc-mappa> <család…>      (család: Caladea | Carlito)
#   → <forrásfa>/assets/fonts/<Család>-{Regular,Bold,Italic,BoldItalic}.ttf
#   → <licenc-mappa>/OFL-carlito.txt és/vagy LICENSE-caladea.txt
#
# Forrás: a ChromeOS distfiles-tükör eredeti „crosextrafonts" archívumai (a Debian/Ubuntu
# fonts-crosextra-caladea / fonts-crosextra-carlito csomag upstreamje). A TTF-ek bitre azonosak a
# jammy csomagokéval (20130214-2.1 / 20130920-1.1), vagyis a szerver Docker-képében lévőkkel.
# Az archívumot ÉS minden kicsomagolt fontfájlt SHA256-tal ellenőrizzük; eltérésnél leáll.
set -euo pipefail

[ $# -ge 3 ] || { echo "használat: $0 <forrásfa> <licenc-mappa> <Caladea|Carlito>…" >&2; exit 2; }
SRC=$1
LIC=$2
shift 2
BASE=https://commondatastorage.googleapis.com/chromeos-localmirror/distfiles

# család | archívum | archívum-sha256 | mappa az archívumon belül
ARCHIVES="
Caladea crosextrafonts-20130214.tar.gz         c48d1c2fd613c9c06c959c34da7b8388059e2408d2bb19845dc3ed35f76e4d09 crosextrafonts-20130214
Carlito crosextrafonts-carlito-20130920.tar.gz 4bd12b6cbc321c1cf16da76e2c585c925ce956a08067ae6f6c64eff6ccfdaf5a crosextrafonts-carlito-20130920
"
# fájl | sha256  (Caladea 1.002: Apache-2.0; Carlito 1.103: OFL-1.1)
FONTS="
Caladea-Regular.ttf    d2f6cad33f191e65b68bd74e6d4f7708080a41b32db635866109df3090265d91
Caladea-Bold.ttf       74eda4fc5ffba0d8dc4aa76d41499f5ab76168d9ed6141c6417064c6244db9b6
Caladea-Italic.ttf     9c968bf60ba1e851cdfea77c71e3540c57792f77482dd241acc29d1425569c4e
Caladea-BoldItalic.ttf f47a35ad6cd0efa9914d93f9d03c93e30c55da43674bccfabac73b3c0d522c01
Carlito-Regular.ttf    b4ff23ba370cc95a3c349336b73f9c28514a1371210f89832efc85c4b1ea7131
Carlito-Bold.ttf       0f62ab34ad5d079a0a28fac01bcf7c7a724a4db4d6cb99cab9cabff382fbb80f
Carlito-Italic.ttf     718a0663864d37a4868220a19b9668a5fe10a46197f6df367b4c2c30c04c026c
Carlito-BoldItalic.ttf 380764b6898d7b73ceae6384b2958b196d2a0428962ef3adf138d27947228666
"

[ -f "$SRC/Cargo.toml" ] || { echo "hiba: $SRC nem Craft-forrásfa" >&2; exit 1; }
mkdir -p "$SRC/assets/fonts" "$LIC"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

for family in "$@"; do
    line=$(echo "$ARCHIVES" | awk -v f="$family" '$1 == f')
    [ -n "$line" ] || { echo "hiba: ismeretlen család: $family (Caladea | Carlito)" >&2; exit 2; }
    set -- $line
    name=$2 sha=$3 dir=$4
    curl -fsSL --retry 3 -o "$tmp/$name" "$BASE/$name"
    echo "$sha  $tmp/$name" | sha256sum -c -
    tar -xzf "$tmp/$name" -C "$tmp"
    echo "$FONTS" | awk -v f="$family-" 'index($1, f) == 1' | while read -r file fsha; do
        dest="$SRC/assets/fonts/$file"
        install -m 0644 "$tmp/$dir/$file" "$dest"
        echo "$fsha  $dest" | sha256sum -c -
    done
    case "$family" in
        Carlito) install -m 0644 "$tmp/$dir/LICENSE" "$LIC/OFL-carlito.txt" ;;
        Caladea)
            # A Caladea-archívumban nincs licencfájl, a licencet a fontok name táblája adja
            # (ID 13/14: Apache-2.0) — ahhoz a teljes Apache-2.0 szöveget a forrásfa LICENSE-APACHE-jából.
            {
                echo "Caladea (Regular, Bold, Italic, BoldItalic), version 1.002 -- metric-compatible with Cambria."
                echo "Copyright (c) 2012 Huerta Tipografia."
                echo "Licensed under the Apache License, Version 2.0 (font name table, IDs 13/14)."
                echo "Source: $BASE/$name"
                echo "(identical to the Debian/Ubuntu fonts-crosextra-caladea package, 20130214)."
                echo
                cat "$SRC/LICENSE-APACHE"
            } >"$LIC/LICENSE-caladea.txt"
            ;;
    esac
done
echo "fontok: $SRC/assets/fonts, licencek: $LIC"
