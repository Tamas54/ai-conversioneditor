# ai-conversioneditor

> Univerzális dokumentum-konverziós és AI-szerkesztési MCP — *Claus von Zahnrad Werkstatt*

Egy MCP-szerver, ami a dokumentum-pipeline minden lépését lefedi:
**konverzió** (PDF/DOCX/HTML/MD/ODT/PPTX/XLSX cross-format), **deterministic szerkesztés**
(find-replace, szekciótörlés, fejlécmódosítás, oldaltörlés, merge),
és **természetes nyelvű szerkesztés** Kimi K2.6 motorral.

## Filozófia

A célja, hogy Claude (web-claus / cli-claus) MCP-hívással *azonnal* elvégezzen
dokumentum-műveleteket, kódgenerálás és sandbox-bohóckodás nélkül. Egy hívás:
50-100 token + 1-3 másodperc, szemben a Computer Use-os \~1700 token + 10-30 mp-pel.

## Architektúra

```
[ Claude / MCP-kliens ]
        ↓ MCP-over-HTTP
[ ai-conversioneditor — egyetlen Railway-szervíz ]
        ├── /mcp                  — MCP endpoint
        ├── /upload               — multipart fájl-upload → file_id
        ├── /files/{file_id}?t=…  — kimenet (HMAC signed token, 24h TTL)
        ├── /health               — pool-státusz
        │
        ├── Worker Pool
        │     ├── LibreOffice × 2 pre-warmed (max 8)
        │     └── Chromium × 1 pre-warmed (max 4)
        │
        ├── Backendek (in-process Python)
        │     ├── Pandoc (text-flow)
        │     ├── WeasyPrint (HTML→PDF, gyors)
        │     ├── Playwright/Chromium (HTML→PDF JS-rendering)
        │     ├── pypdf / pikepdf / pdfplumber (PDF-ops)
        │     └── python-docx (DOCX-edit)
        │
        ├── /data Railway Volume (~5 GB, 24h TTL)
        │
        └── Kimi K2.6 kliens (SiliconFlow → Moonshot)
              fallback: DeepSeek V3.2
```

## Toolok

### Konverzió
- `convert(target_format, file_id|source_url, source_format?, use_browser?, options?)`
  — Minden cross-format. Routing: lásd `app/tools/formats.py`.

### Determinisztikus szerkesztés (zéró LLM-token)
- `find_replace(file_id, pattern, replacement, regex?)` — DOCX/MD/HTML/TXT
- `delete_section(file_id, heading, level?)` — DOCX/MD szekció törlése a következő azonos/magasabb szintű fejlécig
- `insert_after(file_id, anchor, content)` — beszúrás horgony után
- `replace_heading(file_id, old_title, new_title)` — fejléc-csere a szint megőrzésével
- `delete_pages(file_id, page_range)` — PDF-oldaltörlés (`"3,5-7"`)
- `merge(file_ids[], target_format)` — PDF-összefűzés
- `extract(file_id, mode)` — szöveg/táblázatok kinyerése

### AI-szerkesztés
- `edit_with_instruction(file_id, instruction, require_confidence?, return_format?)`
  — Kimi K2.6 strukturált edit-tervet készít, deterministic toolokkal végrehajtjuk.
  PDF input automatikusan round-trippel DOCX-en át (layout-warning a kimenetben).

### WordCraft — Rust Word-motor (kézi + gépi szerkesztés)
A [WordCraft](https://github.com/storytold/wordcraft) (MIT OR Apache-2.0) kész release-ét a
Dockerfile tölti le (rögzített verzió + SHA256), fordítás nincs.
- `wordcraft(file_id?, commands?, author?, save_format?, template?, read?, query?)` — a Word
  szalagját hajtó ~460 parancs fej nélkül; az eredmény ÚJ file_id. `author` = a változáskövetés
  szerzője. `read`: `text` | `info` | `inspect` | `commands` (szűrés: `query`).
- `convert(..., engine="wordcraft")` — LibreOffice nélkül, pár ms (docx/odt/rtf/html/md/tex/txt → … / pdf / png).
- Dashboard ✏️ **Szerkesztés**: a WASM-szerkesztő a középső panelen; Mentés (Ctrl+S) / Exportálás
  új verzióként kerül a listába, forrás-linkkel (`/editor/`, `POST /api/editor/save`,
  ragasztó: `app/static/editor/aice_bridge.js`).

## Mindent konvertál, mindent tud — a bővítés

Minden funkció egyszerre van a **honlapon** (saját aloldallal), a **REST API-ban** (`/v1`), **MCP-eszközként** és az
app **ügynökénél**. A forrás egy helyen van: `app/catalog` (formátumok, eszközök, mértékegységek), a végrehajtás
`app/convert` (konverziós gráf, motorok) és `app/ops` (eszközök), az űrlap/aláírás `app/formkit`.

| Rész | Hol | Mit tud |
|---|---|---|
| Konverzió | `app/convert` | 264 formátum, ~8700 irány, legfeljebb 3 lépéses útvonal a legolcsóbb motorokkal; opciók kategóriánként (oldaltartomány, motor, OCR-nyelv; kép: méret, minőség, metaadat; videó/hang: kodek, felbontás, képarány, fps, bitráta, vágás, forgatás, tükrözés, felirat, hangerő, néma), mentett presetek, „mindre alkalmaz” |
| Eszközök | `app/ops/handlers` | tömörítés (PDF, kép, JPEG, PNG, GIF, videó, MP3, WAV), PDF-csomag (PDF/A, PDF/X, OCR-réteg, titkosítás jogosultságokkal, feloldás, lapítás, vágás, átméretezés, rendezés, bontás, kivonat, törlés, forgatás, képkinyerés, annotálás, szerkesztés, kitakarás, oldalszám, összevetés), kép, videó, hang, vízjel, weboldal → PDF/kép, összefűzés, bélyegkép, archívum, metaadat, haladó ffmpeg/ImageMagick (engedélyezőlistás) |
| Űrlap, aláírás | `app/formkit` | felismerés bármiből — mindig az ingyenes helyi felismerés először; gyenge vagy összetett eredménynél Plus/Pro csomaggal több lépéses látó modell (`OCR_MODEL`, oldalanként kredit), MCP-n helyette a kliens saját ügynöke javít az oldalképekből; (kitölthető/lapos PDF, DOCX, XLSX, fotó/szken) → egységes séma → kitöltés az eredeti formátumban; aláírás tisztítása átlátszó PNG-vé, felhasználónként titkosítva; ügynöki kitöltés profilból és iratokból, kérdésekkel és validálással (dátum, IBAN, adóazonosító jel, TAJ, adószám, bankszámlaszám) |
| Import/export | `app/net.py`, `app/connectors` | eszköz, URL, Google Drive, Dropbox, OneDrive (web); S3, Azure Blob, GCS, SFTP (API); darabolt feltöltés; ideiglenes aláírt link |
| REST API | `app/api_v1` | jobs/tasks DAG, élő státusz (`?wait`, SSE), webhookok (HMAC), sandbox (`sk_test_`), rate limit, kreditelszámolás, OpenAPI (`/v1/openapi.json`) |
| SDK, integrációk | `sdk/python`, `sdk/node`, `integrations/` | Python és Node SDK, Zapier, Make, n8n |
| MCP | `app/mcp_ext.py` | `convert`, `file_tools`, `transfer`, `jobs`, `catalog`, `units`, `forms`, `signature`, `fill_assistant` (elicitation, MCP Apps / ChatGPT Apps nézet) |
| Honlap | `app/site` | a meglévő `/app` honlap része (ugyanaz a fejléc és stílus, a `/` továbbra is `/app`-ra visz, az `/app` láblécéből linkelve, minden oldalon „← Back”); SSR oldal minden konverzióhoz és eszközhöz, „AI agents” oldal a meglévő ügynöki funkciókkal, kategória-hubok, összevetések, API-oldalak, „Connect your AI”; sitemap-index, hreflang, canonical, schema.org, `llms.txt`, `llms-full.txt`, IndexNow |

**Új nyelv**: másold le az `app/site/i18n/en.json`-t `xx.json` néven, fordítsd le (a `_meta` adja a beszédes
URL-mintákat és a nyelvtani szabályokat) — az oldalak, a sitemap és a hreflang automatikusan bővül.

**Motorok** (a Dockerfile telepíti, mind nyílt forrású): LibreOffice, Pandoc, Calibre (hivatalos, rögzített build), FFmpeg, ImageMagick, libvips,
libheif, LibRaw, Inkscape, librsvg, Potrace, 7-Zip, unar, bsdtar, zstd/lzip/lzop, DjVuLibre, MuPDF, QPDF,
Ghostscript, Tesseract (12 nyelv), OCRmyPDF, ExifTool, FontForge, fontTools, LibreDWG, ezdxf, WeasyPrint,
pdf2docx, Chromium. Ami egy gépen hiányzik, azt a gráf kikerüli (`/v1/catalog` és a weboldal a teljes készletet mutatja).

**Biztonság**: minden felhasználói URL SSRF-ellenőrzésen megy át (privát, loopback, link-local, metaadat-címek
tiltva, átirányításonként újra); a Chromium minden betöltött erőforrást ellenőriz; a felhős hitelesítő adatok csak a
hívás idejéig élnek, a nyilvános feladat-nézetben maszkolva; archívum-kibontás zip-slip és zip-bomba ellen
védve; az ffmpeg/ImageMagick-kapcsolók engedélyezőlistásak, fájlra/URL-re hivatkozó szűrő nem mehet át; az
aláírás és az adatprofil AES-256-GCM-mel, felhasználónként külön kulccsal titkosítva.

**Tesztek**: `pytest` (a mintafájlokat a `tests/samples.py` generálja; ideiglenes Postgres a `scripts/pgtemp.py`-ból),
teljes konverziós söprés: `python scripts/sweep_conversions.py`.

## Konverziós mátrix

|         | pdf      | docx     | html        | md       | odt | pptx | xlsx |
|---------|----------|----------|-------------|----------|-----|------|------|
| **pdf** | —        | pdf2docx | (LO)        | extract+ | LO  | —    | —    |
| **docx**| LO       | —        | pandoc      | pandoc   | LO  | —    | —    |
| **html**| weasy/cr | pandoc   | —           | pandoc   | LO  | —    | —    |
| **md**  | md→pdf   | pandoc   | pandoc      | —        | LO  | —    | —    |
| **odt** | LO       | LO       | pandoc      | pandoc   | —   | —    | —    |
| **pptx**| LO       | —        | —           | —        | —   | —    | —    |
| **xlsx**| LO       | —        | LO          | —        | —   | —    | —    |

`weasy/cr` = WeasyPrint default; `use_browser=True` → Chromium (JS-rendering).

**Word-lektorálás MCP-n (Beata és a Wordben kommentelő főnöke)**: `wordcraft(read="comments")` → a
szálak a kijelölt szöveggel, válaszokkal, lezárással, a követett változások azonosítóval; `address=[…]` egy hívásban:
javítás követett változásként a felhasználó nevén, válasz a szálba, lezárás — és mostantól saját új megjegyzés
(`new_comment` + `on`/`paragraph`), változások elfogadása/elutasítása szelektíven (`accept`/`reject`: id, szerző,
bekezdés, típus), megjegyzés átírása/törlése/újranyitása, tiszta végleges példány (`final`). Minden Word-natív marad
(commentsExtended, commentsIds, commentsExtensible, people) — a WordCraft 460 parancsa után is (`docx_extras.restore`
visszateszi, amit a motor eldob). Tesztelve a Word 2019+ szerkezetű mintán (`samples.word_review`), az Open XML SDK
validátorán 0 hiba.

**Rust-motorok elöl**: a konverziós gráf a saját Rust-motorjainkat választja, ahol a mérés szerint jobbak
(production kép, 2026-10): WordCraft (dokumentumok), DeckCraft (pptx→pdf 0,08 s a LibreOffice 1,6 s-a helyett,
pptx→png diánként, a diasor→videó is ebből), GridCraft (xlsx/csv/tsv ↔ xlsx/csv/tsv/html, 4–6 ms), PhotoCraft
(webp/gif cél, PSD), VectorCraft (svg→png/pdf/eps/dxf… 11–19 ms, Inkscape 300 ms). Ahol nem jobbak, ott nem
(png→jpg: ImageMagick/Pillow; xlsx→json: a GridCraft saját formátumot ír; DXF: az ezdxf skáláz jól), és ha a
felhasználó olyan opciót kér, amit a Rust-motor nem tud (méret, minőség, munkalap…), a gráf a következő legjobbat
választja (`service.craft_avoid`). A söprésben a Rust-utak száma 86-ról 144-re nőtt (524-ből).

**AI-narráció a videókban**: a videókészítő alapból felolvastatja a címet és a jelenetek szövegét
(`app/tools/narration.py`, offline Piper-hangok — 58 nyelv, magyarul Anna/Berta/Imre; a nyelv a szövegből vagy a
`language`-ből; `narration_voice`: female | male | hang-id; `narration: "none"` = hang nélkül). A jelenet a
narrációja hosszáig tart, a zene alatta halkabban szól; szerkesztéskor a módosult szövegből újra készül. A
hangmodell (~63 MB) első használatkor töltődik le a Hugging Face-ről a `DATA_DIR/models/piper` alá
(`PIPER_VOICES_DIR`, `PIPER_BASE_URL`), utána gépen fut, új szolgáltató és költség nélkül.

**Videó a dián**: a „videó diára” és minden python-pptx-szel beszúrt videó a PowerPoint saját időzítését kapja
(`app/tools/pptx_media.py`): a dia megjelenésekor magától indul, koppintásra/kattintásra megáll és folytatódik —
Keynote-ban (iPhone is) és PowerPointban.

**WebMCP**: a `/app` és minden nyilvános oldal regisztrálja az eszközeit a böngészős AI-ügynököknek
(`app/static/webmcp.js`, `document.modelContext` — a régebbi `navigator.modelContext` is): list_formats, list_tools,
list_my_files, import_file, convert_file, run_tool, open_page. A látogató saját munkamenetével hívják a meglévő
végpontokat; a gépi katalógus a `/api/catalog`.

**iPhone (és minden Apple-eszköz)**: minden kimenet átmegy az `app/tools/ios_safe.py` kapun —
a konverziók, az eszközök, a Craft-motorok (DeckCraft, GridCraft, WordCraft), a videógenerátor és a „videó diára”:
- PowerPoint → `keynote_safe.py`: jegyzetmester regisztrálva, jegyzetdia-színleképezés, 16:9 sldSz, nyomtató-csonk ki,
  negatív diagramtengely-id javítva (a python-pptx minden mentése is, horoggal);
- Word/Excel → `ooxml_safe.py`: sémasorrend (pPr, rPr, stílus, beállítások, táblázat, szegély, számozás, betűtípus),
  „#szín” és „16.0” értékek, w:shd típus, w:nsid — a tartalom bájtra ugyanaz (a python-docx/openpyxl mentése is);
- MP4/MOV/M4A → faststart (moov elöl), HEVC `hvc1` címke, H.264 4:2:0 8 bites (a 4:4:4 / 10 bites nem megy iPhone-on),
  AAC hang (Opus/Vorbis helyett) — ami jó, csak átcsomagolva, ami rendben van, ahhoz nem nyúl; a kifejezetten kért
  kodeket nem kódolja újra;
- EPUB → az Apple Books-nak a `mimetype` elöl, tömörítetlenül.

Az Open XML SDK validátorán minden PPTX/DOCX/XLSX-gyártónk 0 hibás. A letöltés a valódi kiterjesztéssel és
MIME-típussal megy (különben a Safari `.txt`-t fűz a `.pptx` után), HEAD-et és byte-range-et is kezel
(az iPhone videólejátszója range-kérésekkel olvas).

## Railway deployment

### 1. Init
```bash
railway login
railway init
railway link  # ha létező projekt
```

### 2. Volume
Railway dashboard → **+ New** → **Volume** → mount path: `/data`, méret: **5 GB**.

### 3. Env vars
A dashboard `Variables` fülén:
```
SILICONFLOW_API_KEY=sk-...
FILE_SIGNING_KEY=$(openssl rand -hex 32)
MCP_AUTH_TOKEN=$(openssl rand -hex 24)
LO_WARM=2
CHROMIUM_WARM=1
```

Bővítés (konverzió, eszközök, űrlapok, API, honlap) — mind opcionális, alapértékkel:
```
PRODUCT_NAME=Studio                 # márkanév a honlapon, az API-ban és az MCP-ben
PUBLIC_BASE_URL=https://…           # domain (canonical, sitemap, hreflang, OpenAPI servers)
INDEXING_OPEN=1                     # keresők és AI-robotok csak a végleges domainen
SITE_DEFAULT_LANG=en                # az alapnyelv
SITE_LANGS=en                       # bekapcsolt nyelvek (nemzetközi termék: alapból csak angol; pl. "en,hu")
DEEP_FORM_CREDITS_PER_PAGE=1  DEEP_FORM_MAX_PAGES=10   # összetett űrlap: fizetős, több lépéses látó modell
INDEXNOW_KEY=…                      # IndexNow (Bing, Yandex…) kulcs
DATA_ENCRYPTION_KEY=$(openssl rand -hex 32)   # aláírások, adatprofil titkosítása (ennek híján FILE_SIGNING_KEY)
API_RATE_PER_MIN=60                 # /v1 kulcsonként (sandbox: fele)
API_MAX_FILE_MB=2048                # /v1 és a darabolt feltöltés felső határa
ARCHIVE_MAX_FILES=20000  ARCHIVE_MAX_EXTRACT_MB=4096  # archívum-kibontás (zip-bomba ellen)
VIDEO_TIMEOUT_S=1800                # egy videós lépés felső ideje (mp)
GOOGLE_PICKER_CLIENT_ID=…  GOOGLE_PICKER_API_KEY=…  GOOGLE_PICKER_APP_ID=…   # „Google Drive” gomb
DROPBOX_APP_KEY=…                   # Dropbox Chooser / Saver
ONEDRIVE_CLIENT_ID=…                # OneDrive-választó
ALLOW_PRIVATE_URLS=                 # csak fejlesztéshez: belső címek engedése (SSRF-védelem ki)
CHROMIUM_EXECUTABLE=                # egyéni Chromium (ha a Playwright-verzió eltér)
AICE_DISABLE_ENGINES=               # motorok kikapcsolása vesszővel (pl. calibre,inkscape)
```

### 4. Deploy
```bash
railway up
```

A `RAILWAY_PUBLIC_DOMAIN` automatikusan beáll. Az MCP-URL:
```
https://<your-domain>/mcp
```

### 5. Connect to Claude
Claude → Settings → Connectors → Add custom MCP:
- URL: `https://<your-domain>/mcp`
- Auth header: `Authorization: Bearer <MCP_AUTH_TOKEN>`

## Tesztelés lokálisan

```bash
docker build -t aice .
docker run --rm -p 8000:8000 \
  -v $(pwd)/data:/data \
  -e SILICONFLOW_API_KEY=$SILICONFLOW_API_KEY \
  -e FILE_SIGNING_KEY=test \
  aice
```

Aztán:
```bash
# Upload
curl -F "file=@example.docx" http://localhost:8000/upload

# Convert (használd a visszakapott file_id-t)
curl -X POST http://localhost:8000/mcp/... # MCP-kliens kell hozzá
```

## Erőforrásigény (24 GB / 24 mag instance)

| Komponens          | RAM (idle) | RAM (peak/job) | CPU |
|--------------------|-----------|----------------|-----|
| LibreOffice × 2    | ~150 MB   | ~1.5 GB / job  | 1-2 mag/job |
| Chromium × 1       | ~400 MB   | ~800 MB / job  | 1 mag/job   |
| Python + FastAPI   | ~150 MB   | —              | —   |
| **Összesen idle**  | **~700 MB** | — | — |
| **Peak (4 párhuzamos LO + 2 Chromium)** | — | **~7-8 GB** | **8-10 mag** |

A 24 GB / 24 mag instance bőséges fejterű — `LO_MAX=8` és `CHROMIUM_MAX=4` mellett
is komfortosan elfér, miközben a Pyramid többi szervize változatlanul fut a saját
instance-ain.

## Iterációs roadmap

- [ ] `add_watermark` — pikepdf
- [ ] `apply_signature` — kép-overlay PDF-en
- [ ] `template_render(template_id, data)` — Jinja2 + HTML sablonokból PDF
- [ ] `diff_documents(a, b)` — strukturált verzió-diff
- [ ] `batch_convert([{src, tgt}, ...])` — egy MCP-hívás, N konverzió
- [ ] PDF-form fillin (pikepdf)
- [ ] OCR scan-PDF-ekhez (tesseract integrálás)

## Megjegyzés a PDF-szerkesztéshez

A PDF *közvetlen* layout-szintű szerkesztése zsákutca. A nyerő stratégia a
**szemantikus szerkesztés köztes formátumon**: PDF → DOCX → edit → PDF.
A round-trip 95%-ban tökéletes szöveg-szempontból, de a layout (margók,
képek, fejlécek) 5%-ban torzulhat. A szerver ezt minden PDF-edit-nél a
`warnings` mezőben jelzi.
