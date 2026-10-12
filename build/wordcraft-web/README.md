# WordCraft webes szerkesztő — AICE-build

A dashboard ✏️ szerkesztője a [storytold/wordcraft](https://github.com/storytold/wordcraft)
(MIT OR Apache-2.0) WASM-buildje. A gyári `wordcraft-web-<verzió>.zip` helyett egy minimálisan
javított buildet használunk; ez a mappa és a `.github/workflows/wordcraft-web.yml` állítja elő.

## Mit javít a patch (`0001-aice-fonts-and-branding.patch`)

Mindkét változás csak a webes buildre (`target_arch = "wasm32"`) hat; a natív build változatlan.

1. **Fontok.** A webes buildnek nincs rendszerfontja, csak a binárisba fordítottak (Source
   Sans/Serif, Inter, JetBrains Mono). A Cambriát/Calibrit kérő Word-dokumentumok így más
   metrikával tördelődtek (a Jókai-regény: böngészőben 406 oldal, szerveren 344). A patch a
   `crates/fonts/src/fontdb.rs` `BUNDLED` listáját bővíti a Caladea (Cambria) és Carlito (Calibri)
   metrika-kompatibilis fontokkal (Regular, Bold, Italic, BoldItalic). A WordCraft saját
   helyettesítési listája (`crates/fonts/src/word.rs`) már eddig is ezeket választotta elsőnek,
   ha jelen vannak — most a böngészőben is jelen vannak.
2. **Arculat.** A címsor „Discord" közösségi gombja (`UiState.show_discord`,
   `crates/ui-egui/src/lib.rs`) a webes buildben alapból ki van kapcsolva. (A webes build nem
   ment beállításokat, így régi mentett érték nem írja felül.)

A fontfájlok NINCSENEK a repóban és a patchben: a `fetch-fonts.sh` tölti le őket build előtt,
az archívumot és minden TTF-et SHA256-tal ellenőrizve.

| Font | Licenc | Forrás |
|---|---|---|
| Caladea 1.002 | Apache-2.0 (a font name táblája, ID 13/14) | `crosextrafonts-20130214.tar.gz` |
| Carlito 1.103 | OFL-1.1 (az archívum `LICENSE`-a) | `crosextrafonts-carlito-20130920.tar.gz` |

Mindkettő innen: `https://commondatastorage.googleapis.com/chromeos-localmirror/distfiles/` (a
Debian/Ubuntu `fonts-crosextra-caladea` / `fonts-crosextra-carlito` csomag upstreamje). A TTF-ek
bitre azonosak az Ubuntu jammy csomagokéval (20130214-2.1 / 20130920-1.1), vagyis a szerver
Docker-képében lévőkkel — ezért egyezik a böngészős és a szerveres tördelés. A SHA256-ok a
`fetch-fonts.sh`-ban vannak; a zipben a `NOTICE-AICE.txt` is felsorolja őket.

## A csomag

`wordcraft-web-<verzió>-aiceN.zip`, benne `wordcraft-web-<verzió>-aiceN/`: `index.html`, a
hash-elt `.js` és `_bg.wasm`, `_headers`, `.htaccess`, `HOSTING.md`, `README.md`, `LICENSE-MIT`,
`LICENSE-APACHE`, a craft-fonts `OFL-*.txt`-i (mint a gyári zipben), `LICENSE-caladea.txt`,
`OFL-carlito.txt`, `NOTICE-AICE.txt` és maga a patch. Ugyanaz a szerkezet, mint a gyári zipé,
így a Dockerfile-ban csak az URL-t és a SHA256-ot kell cserélni.

## Build

**GitHub Actions** (a workflow csak az alapértelmezett ágon indítható): Actions →
`wordcraft-web` → Run workflow, `tag` = `wordcraft-web-0.5.0-aice1`, `upstream_ref` = `v0.5.0`.
A job pinelt eszközökkel (Rust 1.95.0, trunk 0.21.14 SHA256-tal, az upstreammel azonos
craft-fonts commit) futtatja a `build.sh`-t, kiírja a SHA256-ot, és a zipet + `SHA256SUMS.txt`-t
feltölti a `tag` nevű GitHub release-be (`GITHUB_TOKEN`, `contents: write`). Létező taget nem ír
felül.

**Helyben** (ugyanaz a script):

```sh
rustup target add wasm32-unknown-unknown        # + trunk 0.21.14 a PATH-on
git clone --depth 1 --branch v0.5.0 https://github.com/storytold/wordcraft.git wc
git clone https://github.com/storytold/craft-fonts.git cf   # opcionális, ugyanaz a commit, mint a workflow-ban
CRAFT_FONTS_DIR=$PWD/cf build/wordcraft-web/build.sh wc wordcraft-web-0.5.0-aice1 out
```

Hideg build 16 magon ~4 perc (cargo 2 perc 17 mp + wasm-bindgen/wasm-opt). Ellenőrzés: a
Jókai-regény a javított buildben 344 oldal (gyári: 406), a címsorban nincs Discord-gomb.

## Verzióváltás (pl. WordCraft 0.6.0)

1. `git clone --branch v0.6.0 …`, majd `git -C wc apply --check build/wordcraft-web/0001-aice-fonts-and-branding.patch`.
   Ha nem megy rá: a két hunkot kézzel átvinni (`BUNDLED` a `fontdb.rs`-ben, `show_discord`
   alapértéke a `lib.rs`-ben), és a patchet `git -C wc diff`-fel újragenerálni (a fejléc-szöveg
   maradjon).
2. A workflow-ban igazítani: `CRAFT_FONTS_REF` (az új upstream `release.yml`-jéből), szükség
   szerint `RUST_TOOLCHAIN` (a `Cargo.toml` `rust-version`-je legalább) és `TRUNK_VERSION` +
   `TRUNK_SHA256` (az upstream `release.yml` web-jobjából).
3. Workflow indítása `tag` = `wordcraft-web-0.6.0-aice1`, `upstream_ref` = `v0.6.0`.
4. A Dockerfile-ban a webes zip URL-je és SHA256-a (a release `SHA256SUMS.txt`-jéből).

Ugyanazon WordCraft-verzió új patch-változata: `-aice2`, `-aice3` … (a release-tag egyben a zip
és a benne lévő mappa neve).


## GitHub Actions
A workflow: `.github/workflows/wordcraft-web.yml` (10-12 óta a repóban; a gh-token `workflow` jogot kapott).
Csak kézzel indul (`workflow_dispatch`): Actions → `wordcraft-web` → Run workflow, vagy
`gh workflow run wordcraft-web -f tag=wordcraft-web-0.5.0-aice2 -f upstream_ref=v0.5.0`.
