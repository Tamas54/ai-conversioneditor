#!/usr/bin/env bash
# A WordCraft webes (WASM) szerkesztő AICE-buildje: patch → fontok → trunk build → zip.
# Ugyanezt futtatja a .github/workflows/wordcraft-web.yml is, így a helyi és a CI-build azonos.
#
# Használat: build.sh <wordcraft-forrásfa> <csomagnév> <kimeneti-mappa>
#   pl.: build.sh ./wordcraft wordcraft-web-0.5.0-aice1 ./out
#   → <kimenet>/<csomagnév>.zip (benne <csomagnév>/: index.html, a hash-elt .js/.wasm, licencek)
#   → <kimenet>/SHA256SUMS.txt
#
# A forrásfa a storytold/wordcraft git-checkoutja a kívánt tagen (most v0.5.0).
# Környezet (opcionális): CRAFT_FONTS_DIR — storytold/craft-fonts checkout, mint az upstream
# release-ben (a web-build abból csak a BIZ UDPGothic Regular UI-fontot ágyazza be).
# Kell: rustup + wasm32-unknown-unknown target, trunk, git, curl, zip, sha256sum.
set -euo pipefail

[ $# -eq 3 ] || { echo "használat: $0 <wordcraft-forrásfa> <csomagnév> <kimeneti-mappa>" >&2; exit 2; }
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC=$(cd "$1" && pwd)
NAME=$2
mkdir -p "$3"
OUT=$(cd "$3" && pwd)
PATCH="$HERE/0001-aice-fonts-and-branding.patch"

# 1) Patch — ha már fent van (ismételt futás), nem rakjuk fel újra.
if git -C "$SRC" apply --reverse --check "$PATCH" 2>/dev/null; then
    echo "patch: már alkalmazva"
else
    git -C "$SRC" apply --check "$PATCH"
    git -C "$SRC" apply "$PATCH"
    echo "patch: alkalmazva"
fi

# 2) Caladea + Carlito → assets/fonts (SHA256-ellenőrzéssel), licencek külön mappába.
LIC="$OUT/.font-licenses"
rm -rf "$LIC"
"$HERE/fetch-fonts.sh" "$SRC" "$LIC"

# 3) Build, az upstream release web-jobjával azonos módon (packaging/web/package.sh).
export WORDCRAFT_BUILD_SHA="${WORDCRAFT_BUILD_SHA:-$(git -C "$SRC" rev-parse HEAD)}"
if [ -n "${CRAFT_FONTS_DIR:-}" ]; then
    export CRAFT_FONTS_DIR CRAFT_FONTS_REQUIRED=1
fi
start=$(date +%s)
(cd "$SRC/apps/wordcraft-web" && trunk build --release)
echo "trunk build: $(($(date +%s) - start)) s"

SITE="$SRC/dist/web"
[ -f "$SITE/index.html" ] || { echo "hiba: $SITE/index.html hiányzik" >&2; exit 1; }
if grep -Eq '(src|href)="/[^/]' "$SITE/index.html"; then
    echo "hiba: $SITE/index.html gyökér-abszolút URL-t tartalmaz" >&2
    exit 1
fi

# 4) Csomag: az upstream wordcraft-web-<verzió>.zip fájlkészlete + a két font licence + a patch.
WORK="$OUT/.pkg"
rm -rf "$WORK"
mkdir -p "$WORK/$NAME"
cp -R "$SITE/." "$WORK/$NAME/"
cp "$SRC/packaging/web/_headers" "$SRC/packaging/web/.htaccess" "$WORK/$NAME/"
cp "$SRC/packaging/web/README.md" "$WORK/$NAME/HOSTING.md"
cp "$SRC/README.md" "$SRC/LICENSE-MIT" "$SRC/LICENSE-APACHE" "$WORK/$NAME/"
if [ -n "${CRAFT_FONTS_DIR:-}" ]; then
    for f in "$CRAFT_FONTS_DIR"/fonts/*/OFL.txt; do
        cp "$f" "$WORK/$NAME/OFL-$(basename "$(dirname "$f")").txt"
    done
fi
cp "$LIC"/* "$WORK/$NAME/"
cp "$PATCH" "$WORK/$NAME/"
{
    echo "$NAME"
    echo
    echo "Modified build of WordCraft (https://github.com/storytold/wordcraft,"
    echo "commit $WORDCRAFT_BUILD_SHA), MIT OR Apache-2.0."
    echo "Changes: $(basename "$PATCH") (included):"
    echo "  - the web build embeds Caladea (Apache-2.0, LICENSE-caladea.txt) and"
    echo "    Carlito (OFL-1.1, OFL-carlito.txt), metric-compatible with Cambria/Calibri;"
    echo "  - the title-bar Discord button is off by default in the web build."
    echo
    echo "Embedded font files (SHA256):"
    (cd "$SRC/assets/fonts" && sha256sum Caladea-*.ttf Carlito-*.ttf | sed 's/^/  /')
} >"$WORK/$NAME/NOTICE-AICE.txt"

rm -f "$OUT/$NAME.zip"
(cd "$WORK" && zip -qr9 -X "$OUT/$NAME.zip" "$NAME")
(cd "$OUT" && sha256sum "$NAME.zip" | tee SHA256SUMS.txt)
ls -l "$OUT/$NAME.zip"
