#!/usr/bin/env bash
# A Craft-szerkesztők (PhotoCraft, VectorCraft, DeckCraft, PdfCraft, DesignCraft, GridCraft) webes
# (WASM) AICE-buildje: patch → (fontok) → az upstream packaging/web/package.sh → saját csomag.
# Ugyanezt futtatja a .github/workflows/craft-web.yml is, így a helyi és a CI-build azonos.
# (A WordCraft külön él: build/wordcraft-web/ — ugyanez a minta.)
#
# Használat: build.sh <app> <forrásfa> <csomagnév> <kimeneti-mappa>
#   pl.: build.sh photocraft ./photocraft photocraft-web-0.7.0-aice1 ./out
#   → <kimenet>/<csomagnév>.zip (benne <csomagnév>/: index.html, a hash-elt .js/.wasm, licencek,
#     NOTICE-AICE.txt, a patch) — ugyanaz a szerkezet, mint az upstream <app>-web-<verzió>.zip-é
#   → <kimenet>/SHA256SUMS.txt
#
# A forrásfa a storytold/<app> checkoutja (vagy kicsomagolt tag-archívuma) a kívánt tagen.
# Környezet (opcionális):
#   CRAFT_FONTS_DIR   storytold/craft-fonts checkout az upstream release-ben pinelt commiton
#                     (<app>/craft-fonts.ref); a PhotoCraft webes buildje nem használja
#   <APP>_BUILD_SHA   a forrás commitja, ha a forrásfa nem git-checkout (pl. tag-archívum)
# Kell: rustup + wasm32-unknown-unknown target, trunk, curl, zip, unzip, sha256sum, patch vagy git.
set -euo pipefail

[ $# -eq 4 ] || { echo "használat: $0 <app> <forrásfa> <csomagnév> <kimeneti-mappa>" >&2; exit 2; }
APP=$1
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC=$(cd "$2" && pwd)
NAME=$3
mkdir -p "$4"
OUT=$(cd "$4" && pwd)
case "$APP" in
    photocraft | vectorcraft | deckcraft | pdfcraft | designcraft | gridcraft) ;;
    *) echo "hiba: ismeretlen app: $APP" >&2; exit 2 ;;
esac
UPPER=$(printf '%s' "$APP" | tr '[:lower:]' '[:upper:]')
PATCH="$HERE/$APP/0001-aice-branding.patch"
[ -f "$PATCH" ] || { echo "hiba: $PATCH hiányzik" >&2; exit 1; }
[ -f "$SRC/apps/$APP-web/Trunk.toml" ] || { echo "hiba: $SRC nem $APP-forrásfa" >&2; exit 1; }

# 1) Patch — ha már fent van (ismételt futás), nem rakjuk fel újra. Git-checkouton git apply,
#    tag-archívumon patch(1); mindkettő ugyanazt a -p1-es egységes diffet olvassa.
if [ -e "$SRC/.git" ]; then
    if git -C "$SRC" apply --reverse --check "$PATCH" 2>/dev/null; then
        echo "patch: már alkalmazva"
    else
        git -C "$SRC" apply --check "$PATCH"
        git -C "$SRC" apply "$PATCH"
        echo "patch: alkalmazva"
    fi
elif patch -d "$SRC" -p1 --dry-run --reverse --force -s <"$PATCH" >/dev/null 2>&1; then
    echo "patch: már alkalmazva"
else
    patch -d "$SRC" -p1 --dry-run -s <"$PATCH"
    patch -d "$SRC" -p1 --no-backup-if-mismatch -s <"$PATCH"
    echo "patch: alkalmazva"
fi

# 2) Fontok: csak ahol a patch befordít valamit (GridCraft: Carlito a Calibri-cellákhoz).
LIC="$OUT/.font-licenses-$APP"
rm -rf "$LIC"
mkdir -p "$LIC"
if [ -f "$HERE/$APP/fonts" ]; then
    "$HERE/fetch-fonts.sh" "$SRC" "$LIC" $(cat "$HERE/$APP/fonts")
fi

# 3) Build + az upstream zip (packaging/web/package.sh: trunk build --release, méretkapu, licencek).
SHA_VAR="${UPPER}_BUILD_SHA"
if [ -z "${!SHA_VAR:-}" ]; then
    export "$SHA_VAR=$(git -C "$SRC" rev-parse HEAD 2>/dev/null || echo unknown)"
fi
if [ -n "${CRAFT_FONTS_DIR:-}" ]; then
    export CRAFT_FONTS_DIR CRAFT_FONTS_REQUIRED=1
fi
export DIST="$OUT/.upstream-$APP"
rm -rf "$DIST"
start=$(date +%s)
"$SRC/packaging/web/package.sh"
echo "upstream package.sh: $(($(date +%s) - start)) s"
UPZIP=$(ls "$DIST"/"$APP"-web-*.zip)
UPNAME=$(basename "$UPZIP" .zip)

# 4) Saját csomag: az upstream zip tartalma <csomagnév>/ alatt + font-licencek + NOTICE + a patch.
WORK="$OUT/.pkg-$APP"
rm -rf "$WORK"
mkdir -p "$WORK"
unzip -q "$UPZIP" -d "$WORK"
mv "$WORK/$UPNAME" "$WORK/$NAME"
cp "$LIC"/* "$WORK/$NAME/" 2>/dev/null || true
cp "$PATCH" "$WORK/$NAME/"
{
    echo "$NAME"
    echo
    echo "Modified build of the $APP web editor (https://github.com/storytold/$APP,"
    echo "commit ${!SHA_VAR}), MIT OR Apache-2.0 (LICENSE-MIT, LICENSE-APACHE)."
    echo "Changes: $(basename "$PATCH") (included) — web build (wasm32) only, the native"
    echo "build is unchanged:"
    sed -n 's/^AICE-CHANGE: /  - /p' "$PATCH"
    if [ -f "$HERE/$APP/fonts" ]; then
        echo
        echo "Embedded font files (SHA256):"
        (cd "$SRC/assets/fonts" && for f in $(cat "$HERE/$APP/fonts"); do sha256sum "$f"-*.ttf; done | sed 's/^/  /')
    fi
} >"$WORK/$NAME/NOTICE-AICE.txt"

rm -f "$OUT/$NAME.zip"
(cd "$WORK" && zip -qr9 -X "$OUT/$NAME.zip" "$NAME")
(cd "$OUT" && sha256sum "$NAME.zip" | tee SHA256SUMS.txt)
ls -l "$OUT/$NAME.zip"
