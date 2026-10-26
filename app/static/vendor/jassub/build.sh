#!/bin/bash
# How the files in this folder were made (JASSUB 2.5.20, libass in WASM, for app/static/live-captions.js).
#   bash build.sh <work dir> [<esbuild binary>]
# Downloads the exact npm tarballs (integrity checked), bundles JASSUB's main module and its worker with esbuild into
# two self-contained ES modules (no bare imports: a module worker cannot use an import map), and copies the WASM and
# JASSUB's own LICENSE here. Nothing is changed in JASSUB's code; three of its npm dependencies are replaced by stubs:
#   rvfc-polyfill (GPL-3.0)  → empty: only for its <video> mode; we draw on a canvas with our own clock, and Chrome,
#                              Safari and Firefox have requestVideoFrameCallback natively
#   lfa-ponyfill (MIT)       → a stub that finds nothing: only for queryFonts 'localandremote' (Google Fonts), which
#                              we never use (the fonts come from our server)
#   throughput (MIT)         → a no-op: only for debug: true
# abslink (Apache-2.0, a Comlink derivative) is bundled as is.
set -euo pipefail
W=${1:?work dir}
ESB=${2:-/home/tamas1/videoforge2/node_modules/.bin/esbuild}
OUT=$(cd "$(dirname "$0")" && pwd)
declare -A SRI=(
  [jassub@2.5.20]="sha512-uUE0YoUnw/pdgFVoefRN0leLu5iFS39hfOikouCqHUffLCAU3wqRU5VkpBbY1piV8WWlFcHCz3HEPzvyxbAJTw=="
  [abslink@1.3.1]="sha512-ukFoU6q/RBMjoC7fu0Zx2+eVH2QFMfV+aJ2cI3Ypl0Shm0ikGjZ0T2VN0xvLl0a8imhWqvfVrqu2n2Ml3eSMyQ=="
)
mkdir -p "$W/pkgs" "$W/node_modules"
for spec in "${!SRI[@]}"; do
  n=${spec%@*}; v=${spec#*@}; f="$W/pkgs/$n-$v.tgz"
  [ -f "$f" ] || curl -sfL "https://registry.npmjs.org/$n/-/$n-$v.tgz" -o "$f"
  got="sha512-$(openssl dgst -sha512 -binary "$f" | base64 -w0)"
  [ "$got" = "${SRI[$spec]}" ] || { echo "integrity mismatch: $spec"; exit 1; }
  rm -rf "$W/pkgs/$n" && mkdir -p "$W/pkgs/$n" && tar xzf "$f" -C "$W/pkgs/$n"
done
ln -sfn "$W/pkgs/abslink/package" "$W/node_modules/abslink"
stub() { mkdir -p "$W/node_modules/$1"; echo '{"name":"'"$1"'","type":"module","main":"index.js"}' > "$W/node_modules/$1/package.json"
         printf '%s\n' "$2" > "$W/node_modules/$1/index.js"; }
stub rvfc-polyfill '// stub: the GPL-3.0 polyfill is left out (native requestVideoFrameCallback; canvas mode only)'
stub lfa-ponyfill  'export async function queryRemoteFonts () { return [] } // stub: no Google Fonts lookups'
stub throughput    'export default function throughput () { return () => 0 } // stub: debug mode only'
J="$W/pkgs/jassub/package"
rm -rf "$W/jassub" && cp -r "$J" "$W/jassub"
HEAD="/*! JASSUB 2.5.20 (MIT, github.com/ThaUnknown/jassub, LICENSE in this folder) bundled with abslink 1.3.1 (Apache-2.0,
 github.com/ThaUnknown/abslink, a Comlink derivative). Made by build.sh in this folder. */"
WHEAD="/*! JASSUB 2.5.20 worker (MIT) + abslink 1.3.1 (Apache-2.0). jassub-worker*.wasm is libass (ISC) with FreeType
 (FTL: portions of this software are copyright The FreeType Project, www.freetype.org), FriBidi (LGPL-2.1-or-later,
 source: github.com/fribidi/fribidi), HarfBuzz (MIT, old), Brotli (MIT) and expat (MIT), compiled with Emscripten by
 JASSUB (source and build: github.com/ThaUnknown/jassub, commit dea3d2d398 = npm 2.5.20). */"
"$ESB" "$W/jassub/dist/jassub.js" --bundle --format=esm --target=es2022 --legal-comments=inline \
  --banner:js="$HEAD" --outfile="$OUT/jassub.js"
"$ESB" "$W/jassub/dist/worker/worker.js" --bundle --format=esm --target=es2022 --legal-comments=inline \
  --banner:js="$WHEAD" --outfile="$OUT/jassub-worker.js"
cp "$J/dist/wasm/jassub-worker.wasm" "$J/dist/wasm/jassub-worker-modern.wasm" "$J/LICENSE" "$OUT/"
sha256sum "$OUT"/jassub.js "$OUT"/jassub-worker.js "$OUT"/*.wasm
