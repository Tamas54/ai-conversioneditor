# Craft-szerkesztők webes buildje — AICE-változat

A ✏️ szerkesztők közül a WordCraft saját buildje a `build/wordcraft-web/` alatt él; ez a mappa a
másik hat app ugyanilyen buildje: **PhotoCraft, VectorCraft, DeckCraft, PdfCraft, DesignCraft,
GridCraft** (storytold/<app>, mind MIT OR Apache-2.0 — a repók `LICENSE-MIT`/`LICENSE-APACHE`/
`NOTICE` fájlja szerint ellenőrizve, 10-12). A gyári `<app>-web-<verzió>.zip` helyett egy
minimálisan javított buildet használunk: a beágyazott szerkesztőben ne legyen idegen közösségi
gomb/link (Discord, getartcraft.com, GitHub, „Report an issue”, „Check for updates”).

Minden patch CSAK a webes buildre (`target_arch = "wasm32"`) hat; a natív build változatlan. A
patchek fejléce (`AICE-CHANGE:` sorok) a csomag `NOTICE-AICE.txt`-jébe is bekerül.

## Mit javítanak a patchek (`<app>/0001-aice-branding.patch`)

| App (verzió) | Kivett elemek | Fontok |
|---|---|---|
| PhotoCraft 0.7.0 | címsor Discord-gombja; kezdőképernyő és About: Discord-gomb + website/GitHub/ArtCraft linksor; Help menü: Discord, website, ArtCraft website, GitHub, Report an Issue | — |
| VectorCraft 0.9.0 | alkalmazássáv Discord-gombja; kezdőképernyő „Community” blokkja; About linkjei; Help (és App) menü: Discord, website, app page, GitHub | — (a craft-fonts pin az upstreamé) |
| DeckCraft 0.5.0 | About: Discord/app page/GitHub linkek; kezdőképernyő Discord-linkje; Record fül „Learn More” (app page) gombja | **Carlito + Caladea** a webes buildbe is (a pinelt craft-fontsból; az upstream web-build csak Intert ágyaz be) — a Calibri/Cambria-paklik így ugyanúgy tördelnek, mint a szerveren |
| PdfCraft 0.6.0 | fülsor Discord-gombja; kezdőképernyő „Join the ArtCraft community” kártyája; About linkjei; Help-parancsok (menü + keresés): Discord, web page, GitHub, ArtCraft website, Check for updates | — |
| DesignCraft 0.6.0 | címsor Discord-gombja; kezdőképernyő közösségi kártyája; About: Discord-gomb, linksor, „More ArtCraft apps”; Help menü és parancskeresés link-elemei | — (IDML-ben ritka a Calibri) |
| GridCraft 0.5.0 | About: app page + Discord link | **Carlito** a webes buildbe (a webnek nincs rendszerfontja, eddig az egui alapfontjával rajzolta a Calibri-cellákat). A fájlokat a `fetch-fonts.sh` tölti le (ugyanazok és ugyanolyan SHA256-ellenőrzéssel, mint a WordCraftnál; bitre azonosak a szerver Docker-képének `fonts-crosextra-carlito` csomagjával) |
| EffectCraft 0.8.0 (videó, 10-12) | fejléc Discord/GitHub/getartcraft-gombjai; kezdőképernyő Community-blokkja és „More ArtCraft apps”; About linkjei; Help menü (új `[!web]` menücímke), parancskeresés és ügynök-katalógus: Discord, website, app page, GitHub-doksik és -issue, online tutorials, testvérappok | — (az upstream web-build sem használ craft-fontsot) |
| FilmCraft 0.6.0 (videó, 10-12) | fejléc Discord-gombja (a cím kapja a helyét); Import-képernyő Community-blokkja; About linkjei; Help menü és parancskeresés link-elemei; a webes build semmilyen külső linket nem nyit | — (`craft-fonts.ref`: az upstream web-job pinje) |

Az EffectCraft és a FilmCraft nem trunk-os: a `build.sh` az upstream `packaging/web/package.sh`-t hívja, az
`cargo xtask web`-bel épít — ehhez a **wasm-bindgen CLI 0.2.129** kell (a workflow telepíti; helyben:
`cargo install wasm-bindgen-cli --version 0.2.129 --locked`). A patchek `cargo check --target wasm32-unknown-unknown`-nal
ellenőrizve (10-12).

Megmarad (szándékosan): az appok neve, az ArtCraft-jel, az About licenc/szerzői szövege és a
közreműködők listája (attribúció). A webes appok nem küldenek telemetriát, csak a saját
originről töltenek (fájl, fontok) — ezt a patchelés előtt ellenőriztük.

## Fájlok

- `build.sh <app> <forrásfa> <csomagnév> <kimenet>` — patch (git-checkouton `git apply`,
  tag-archívumon `patch -p1`) → fontok, ha az appnak van `fonts` fájlja → az upstream
  `packaging/web/package.sh` (trunk build, licencek) → `<kimenet>/<csomagnév>.zip` (benne
  `<csomagnév>/`, ugyanaz a fájlkészlet, mint a gyári zipé + `NOTICE-AICE.txt` + a patch) és
  `SHA256SUMS.txt`.
- `fetch-fonts.sh <forrásfa> <licenc-mappa> <Caladea|Carlito>…` — a WordCraft-féle fontletöltő
  általánosítása.
- `<app>/0001-aice-branding.patch` — a patch; `<app>/craft-fonts.ref` — az upstream release
  web-jobjában pinelt storytold/craft-fonts commit (a PhotoCraft web-buildje nem használ ilyet);
  `<app>/fonts` — a befordítandó fontcsaládok (csak GridCraft).
- `.github/workflows/craft-web.yml` — ugyanez CI-ban (csak kézzel indul).

## Build

**GitHub Actions:** Actions → `craft-web` → Run workflow: `app`, `version` (pl. 0.7.0), `suffix`
(aice1). A release-tag `<app>-web-<verzió>-<suffix>`; létező taget nem ír felül. Parancssorból:
`gh workflow run craft-web -f app=photocraft -f version=0.7.0 -f suffix=aice2`.

**Helyben** (ugyanaz a script; rustup + `wasm32-unknown-unknown`, trunk 0.21.14):

```sh
git clone --depth 1 --branch v0.7.0 https://github.com/storytold/photocraft.git src
# craft-fontsot igénylő appnál (lásd <app>/craft-fonts.ref):
git clone https://github.com/storytold/craft-fonts.git cf && git -C cf checkout "$(cat build/craft-web/vectorcraft/craft-fonts.ref)"
CRAFT_FONTS_DIR=$PWD/cf build/craft-web/build.sh vectorcraft src vectorcraft-web-0.9.0-aice1 out
```

Hideg build 16 magon appenként ~5–8 perc (cargo + wasm-bindgen/wasm-opt).

## Release (helyi buildből)

A Dockerfile a release-eket SHA256-tal húzza le; a release feltöltése nyilvános közzététel, ezért
csak jóváhagyással. Appenként (`$OUT` = a build kimeneti mappája):

```sh
gh release create photocraft-web-0.7.0-aice1  "$OUT/photocraft-web-0.7.0-aice1.zip"  "$OUT/SHA256SUMS.txt" --repo Tamas54/ai-conversioneditor --title photocraft-web-0.7.0-aice1  --notes-file NOTES.md
gh release create vectorcraft-web-0.9.0-aice1 "$OUT/vectorcraft-web-0.9.0-aice1.zip" "$OUT/SHA256SUMS.txt" --repo Tamas54/ai-conversioneditor --title vectorcraft-web-0.9.0-aice1 --notes-file NOTES.md
gh release create deckcraft-web-0.5.0-aice1   "$OUT/deckcraft-web-0.5.0-aice1.zip"   "$OUT/SHA256SUMS.txt" --repo Tamas54/ai-conversioneditor --title deckcraft-web-0.5.0-aice1   --notes-file NOTES.md
gh release create pdfcraft-web-0.6.0-aice1    "$OUT/pdfcraft-web-0.6.0-aice1.zip"    "$OUT/SHA256SUMS.txt" --repo Tamas54/ai-conversioneditor --title pdfcraft-web-0.6.0-aice1    --notes-file NOTES.md
gh release create designcraft-web-0.6.0-aice1 "$OUT/designcraft-web-0.6.0-aice1.zip" "$OUT/SHA256SUMS.txt" --repo Tamas54/ai-conversioneditor --title designcraft-web-0.6.0-aice1 --notes-file NOTES.md
gh release create gridcraft-web-0.5.0-aice1   "$OUT/gridcraft-web-0.5.0-aice1.zip"   "$OUT/SHA256SUMS.txt" --repo Tamas54/ai-conversioneditor --title gridcraft-web-0.5.0-aice1   --notes-file NOTES.md
```

(A `NOTES.md` lehet a csomag `NOTICE-AICE.txt`-je.) A Dockerfile `<APP>_WEB_TAG` /
`<APP>_WEB_SHA256` ARG-jai a release `SHA256SUMS.txt`-jéből jönnek.

## Verzióváltás (pl. PhotoCraft 0.8.0)

1. Új forrás: `git clone --branch v0.8.0 …`, majd `git -C src apply --check build/craft-web/photocraft/0001-aice-branding.patch`.
   Ha nem megy rá: a hunkokat kézzel átvinni (mindegyik egy `cfg!(target_arch = "wasm32")`-feltétel
   vagy `#[cfg(not(target_arch = "wasm32"))]` egy listaelemen — keress rá a `discord`, `links::`,
   `help.` szavakra az `crates/ui-egui`-ban), és a patchet újragenerálni (`diff -ruN` vagy
   `git -C src diff`), a fejléc `AICE-CHANGE:` sorait megtartva/igazítva.
2. Az upstream `.github/workflows/release.yml` web-jobjából: a craft-fonts pin → `<app>/craft-fonts.ref`
   (ha a web-job nem használ craft-fontsot, a fájlt törölni), a `TRUNK_VERSION`, és szükség szerint a
   `RUST_TOOLCHAIN` a `craft-web.yml`-ben (legalább a `Cargo.toml` `rust-version`-je).
3. Workflow (`version=0.8.0`, `suffix=aice1`) vagy helyi build + release.
4. Dockerfile: `<APP>_VERSION` (a CLI-é), `<APP>_WEB_TAG`, `<APP>_WEB_SHA256`.
5. Ellenőrzés: a szerkesztő megnyit egy fájlt `?src=`-vel (az `aice_bridge.js` szintetikus drop-ja),
   és sem a címsorban, sem a kezdőképernyőn, sem az About-ban, sem a Help menüben nincs közösségi link.

Ugyanazon verzió új patch-változata: `-aice2`, `-aice3` … (a release-tag egyben a zip és a benne
lévő mappa neve).
