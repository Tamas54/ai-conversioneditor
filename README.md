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
