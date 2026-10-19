# Studio — architecture notes (OPERATION OMLETT, phase 0)

Read from the code on main `a9abf31` (2026-10-19). Every claim cites `file:function`. Where the Omlett spec
assumes something the code does differently, it is marked **≠ spec**.

## 1. Users, roles, workspaces

- **Identity per request** — `app/auth.py:auth_middleware` resolves a uid and sets it in a contextvar
  (`app/tenancy.py:set_user`). Every storage path goes through `tenancy.user_root()`: admin → `DATA_DIR`,
  everybody else → `DATA_DIR/u/<uid>`. Isolation is physical (directories), not ACL rows: modules never
  check ownership themselves (`app/tenancy.py` docstring).
- **Admin = the uid `"admin"`** (`tenancy.ADMIN_ID`). Every exemption compares `uid == tenancy.ADMIN_ID`
  (`billing.use / scan_pages / api_task / ai_step / plan_of`, `db.credits_spend`, `routes/make.py:_credits_left`).
  `users.is_admin` exists (`db.py SCHEMA`, seeded true for the `admin` row) but is **not used for any decision** —
  only selected in `db.user_get`. Ways to be admin: `DASHBOARD_PASSWORD` login with empty/`admin` e-mail
  (`auth.py:login`), the legacy `MCP_AUTH_TOKEN` as Bearer (`auth.py:key_uid`), OAuth while signed in as admin, or
  local dev with no passwords set. **A personal e-mail account of the owner is NOT admin.**
- **Guests** — uid `g<hex>` (`tenancy.new_guest_id`), whitelisted routes only (`auth.py:_GUEST_OK`), files kept
  1 day, migrated into the account on sign-up/login (`auth.py:_adopt_guest`, `tenancy.migrate`).

## 2. Credits, plans, metering (`app/billing.py`, `app/db.py`)

- **Balance**: `users.credits integer` (`db.py SCHEMA`). `db.credits_spend` = one atomic
  `UPDATE … SET credits = credits - n WHERE credits >= n` (admin → no-op, returns None; shortfall →
  `db.InsufficientCredits`); `db.credits_add` for grants/refunds. Both append to **`credit_ledger`**
  (`id, user_id, delta, balance_after, reason, ref, created_at`) — **≠ spec 2.3: a `credit_ledger` table already
  exists** with a different shape (no `kind`, `idempotency_key`, `provider_*`, `workspace_id`, `meta`); the 2.3
  migration must extend it additively, not create it. No holds/captures exist today; refunds are explicit
  `credits_add` calls (`tools/vision_ops.py:generate_image`, `tools/restore.py`).
- **Plans** (`billing.PLANS`): free / plus (€9) / pro (€19) — `monthly_credits` 10/150/400, monthly quotas
  (`deck, revise, document, photo_edit, scan_pages, form_pages, check`), `api_tasks` 250/5000/25000,
  `ai_steps` 60/1500/6000, `retention_days` 30/90/365. Sign-up grants `SIGNUP_CREDITS` (30,
  `auth.py:signup`); free accounts are topped up to 10 once a month (`billing.monthly_topup` → `db.free_topup`,
  called from `GET /api/account`). Paid credits arrive by Stripe webhook (`billing._apply`, idempotent via
  `stripe_events`). Admin grant: `POST /api/admin/credits` (`auth.py:api_admin_credits`). 1 credit ≈ €0.10.
- **Monthly counters**: `usage_monthly(user_id, month, kind, n)`; `db.usage_add` is atomic with an optional limit.
- **What charges what** (all skip admin, guests and a DB-less server):

  | Path | Mechanism | Where |
  |---|---|---|
  | AI image (FLUX), web only | `IMAGE_CREDIT_COST` credits, refund on failure | `tools/vision_ops.py:generate_image` |
  | Photo restore / colourise | credits | `tools/restore.py` |
  | Scan pages | quota, then 1 credit / 20 pages | `billing.scan_pages` (`routes/make.py:make_digitize`, `ops._charge`) |
  | Fact check | credits per `FACTCHECK_CLAIMS_PER_CREDIT` claims | `routes/checks.py:_charge_claims` |
  | AI form recognition | credits | `formkit/deep.py` |
  | deck / document / revise / photo_edit / form_pages / check | monthly quota → HTTP 402, no credits | `billing.use` |
  | REST `/v1` tasks (+ MCP `jobs` tool) | `api_tasks` quota, then 1 credit / 20, charged up front | `api_v1/operations.py:_charge` → `billing.api_task`; `sk_test_` sandbox free |
  | Server's own LLM/vision call **on MCP** | `ai_steps` quota, then 1 credit / 20 (block paid up front); `AIStepsExhausted` | `billing.ai_step`, called inside `llm/siliconflow.py:_call_model` and `llm/vision.py` |
  | Render, merge, overlay, video make/edit, transcript, file tools, convert, music | **nothing** (web and MCP) | — |

- **Does MCP charge today? Partly — ≠ spec 1.5 ("MCP doesn't consume credits").** An MCP call pays exactly
  what the web pays when the tool delegates to a web handler (`pptx_build(from_document)` →
  `routes/make.py:make_deck` → `billing.use("deck")`; `digitize` → `make_digitize` → scan pages; `photo_edit` →
  `make_photo_edit`; `restore_photos` → credits; `facts` → `_charge_claims`; `jobs` → `api_task`; blank
  `files(action="new")` documents/decks are free), plus **AI steps** for the server's own model (`billing.ai_step` only counts
  when `tenancy.client_makes_images` is set, i.e. on MCP — on the web the LLM is "fair use" and free). AI pictures
  are refused on MCP (`tenancy.CLIENT_IMAGE_MSG`). What the QA run saw as "no charge" is real for the **video**
  tool: render/merge/overlay/transcript cost 0 credits everywhere, by design (server instructions in
  `mcp_server.py`: "YOUR THINKING, OUR ENGINES … cost the person nothing extra"). There is no per-operation price
  for CPU work at all yet.
- **Reporting**: `mcp_server.py:linked` wraps each tool in `billing.count_steps()` and adds
  `studio_ai = {steps, credits, note}` to the result when steps were used. Caveat: long tools return a job
  (`jobs.start` → `asyncio` task, contextvars copied); steps taken inside the job still charge but are **not**
  reported (the box is read before the job runs). No balance is returned.

## 3. MCP server

- **Registration**: `app/mcp_server.py` — one `FastMCP("ai-conversioneditor", stateless_http=True,
  json_response=True)`; tools are `@mcp.tool()` + `@linked` (35 in `mcp_server.py`); `app/mcp_ext.py` (imported at
  the end of `mcp_server.py`) adds `file_tools` (built from the catalog: `mcp.tool()(linked(_file_tools))`),
  `transfer`, `jobs`, `forms`, `catalog`, `units`, `signature`, `fill_assistant` — 43 tools.
  `_annotate_tools` sets read-only/open-world hints. Mounted at `/mcp` (`mount_mcp`, `main.py`).
- **`linked`** (per call): sets `client_makes_images=True`, counts AI steps, runs `_finalize` (labels, sizes,
  `checks.run` → `check`, `_links` → `edit_url`, `preview_url`, `job_id`), logs `mcp tool(args) ok/FAILED`.
  `catalog`, `units`, `signature`, `fill_assistant` are not wrapped.
- **Auth → uid** (`auth.py:key_uid`, inside `auth_middleware`; key from `Authorization: Bearer` or `?key=`):
  1. `MCP_AUTH_TOKEN` (legacy) → `admin`;
  2. `at_…` OAuth access token → `oauth.py:access_uid`: HMAC-sealed `{k: api_keys.id, u: uid, exp}` (30 days),
     valid while that `api_keys` row is not revoked (cached 60 s); issued by `oauth.py:token` (PKCE S256, dynamic
     client registration, stateless — no new tables; the grant is an `api_keys` row "<client> (connected)");
  3. `sk_…` user key → `db.api_key_user` (sha256 lookup, cached 60 s);
  4. no/invalid key on `/mcp`: 401 + `WWW-Authenticate` pointing at the OAuth metadata when `MCP_AUTH_TOKEN` is
     set; when it is not set, **admin** (local dev).
  The uid is set for the whole request; background jobs inherit it via contextvars.

## 4. Files, versions, signed links, retention (`app/storage/`)

- **Files**: `file_id = <uuid hex>.<ext>` (`storage/files.py:new_file_id`) under the workspace's `inputs/`,
  `outputs/` or `temp/`; `_safe` blocks path tricks. A sidecar `<file_id>.meta.json` (`storage/metadata.py:write_meta`)
  holds label, `source_file_id`, operation and extras; each write is mirrored to Postgres `file_versions`
  (`db.record_version_bg`, `owner_id` = current uid).
- **Versions**: there is no in-place overwrite — every edit writes a **new file_id** whose `source_file_id` points at
  its parent. The version tree is `file_versions` (`db.lineage`, MCP `history(action="lineage")`; branches/merge in
  `tools/branches.py`). Video edits produce a new `.ecproj` + MP4 (`tools/video.py:edit`); project media are hidden
  "asset" files referenced from the project meta (`video.add_asset`, `assets_of`), and a new version re-uses the
  same asset file_ids (`video.carry_assets`).
- **Signed links**: `/files/{file_id}?t=<exp>.<hmac>[&u=<uid>]` and `/files/{file_id}/preview.png`
  (`routes/files.py:get_file / get_preview`). HMAC-SHA256 with `FILE_SIGNING_KEY` over `uid|file_id|exp`
  (admin: `file_id|exp`) (`storage/files.py:sign_file_id / verify_file_token`); TTL `FILE_TTL_HOURS` (24 h).
  Anyone holding the link can **read** that one file; it never grants editing. `public_url` signs a fresh link on
  every result.
- **Retention**: `storage/cleanup.py:cleanup_loop` deletes files by **mtime** older than `FILE_RETENTION_DAYS`
  (30) in every workspace; guests 1 day; `temp/` 1 day; deletions mark `file_versions.deleted_at`. **≠ the plans:
  `PLANS[*].retention_days` (90/365) is advertised (`site/content.py`) but never enforced.** Because assets are
  separate files with their own mtime, an old asset of a recently edited video project is swept on its own schedule
  (relevant to "shared projects don't expire", phase 4).

## 5. Video pipeline

- **File tools** (catalog `app/catalog/tools.py`, run by `app/ops`, same on web `/api/tools/run`, MCP `file_tools`,
  REST `/v1`):
  - `merge-files` → `ops/handlers/files.py:merge` → `_concat_av`: canvas = the **first input's coded**
    `width×height` from `convert/steps.py:probe` (no rotation handling), every clip
    `scale … force_original_aspect_ratio=decrease,pad,setsar=1,fps=30`, concat filter, libx264 CRF 20 medium,
    AAC 160k, `+faststart` (`ops/handlers/media.py:_vcodec_for / _acodec_for / _faststart`). This is the 1.1 bug:
    an iPhone portrait clip (coded 1920×1080, rotation 90) yields a 1920×1080 canvas.
  - `resize-video` → `media.py:resize_video`: `aspect_mode` pad | crop | stretch (default pad).
  - `ffmpeg-advanced` → `ops/handlers/advanced.py:ffmpeg` with `convert/advanced.py:check`: flag allowlist
    `FFMPEG_FLAGS`, `-vf`/`-af` simple comma chains only (no `;`/`[`), filter allowlists `_FV` / `_FA`
    (`tpad`, `loop`, `apad` are absent); the error is just `filter not allowed: <name>`.
- **`video` MCP tool** (`mcp_server.py:video`) → `routes/video.py` → `tools/video.py`. Engine: storytold
  EffectCraft CLI (`video.effect_cli`, Apache-2.0), a JSX builder writes an `.ecproj`, `effectcraft-cli render`
  renders it (one render at a time, `_render_gate`).
  - `make` (`video.make`): article/text → script (server LLM = AI step on MCP) → pictures → `.ecproj` → MP4.
  - `overlay` (`video.overlay`, `overlay_input` pre-checks): the person's footage is the base track (rotation-aware
    `video.probe_movie`, `_upright` re-encodes rotated footage at CRF 17), layers → per-layer compositions.
  - `edit` (`video.edit`): changes → new project + MP4. `render` (`video.render`, `scene=` → `render_scene` draft).
  - Final encode: `video._render_staged` → `render … --format h264 --bitrate 8000` (2500 for draft) — a fixed
    8 Mbps, the ~8.3 Mbps seen in QA; then `ios_safe.finalize` (moov first).
  - **Check**: `tools/checks.py:check_video` (5 decoded sample frames, length vs timeline and vs the requested
    length, expected W×H, black frames) merged with layer notes: `video._overlaps` compares **layer boxes**
    (> 15 % of the smaller box, ≥ 0.3 s together); chart/coverage notes. No text measurement: a `graphic` text
    element defaults to `w = 0.6` of the frame (`video._graphic_elements`, `e.get("w", 0.6)`), box height
    3.6 × size, and `_fit_size` silently shrinks the font (only an internal `_FIT` note) — the 1.3 gap.
  - Two job systems: video jobs live in `app/jobs.py` (in-memory, owner-checked `job_status`); the MCP `jobs`
    tool is the REST `/v1` job engine (`api_v1/jobs.py`) — hence "No such job." for a video job_id there.

## 6. Transcript (`app/tools/transcript.py`)

- Engine: **local faster-whisper 1.1.1** (CTranslate2, CPU int8), model from env `ASR_MODEL` (default
  `large-v3-turbo`), downloaded on first use into `ASR_MODELS_DIR` or `DATA_DIR/models/whisper` (~1.6 GB; not in
  the image). One model instance per process, one transcription at a time (`_lock`). No API, no cost.
- `transcribe(src, language)`: ffmpeg → 16 kHz mono PCM, `WhisperModel.transcribe(word_timestamps=True,
  vad_filter=True)` → `{language, model, duration, sentences, words}`. Cached in Postgres `ocr_cache`
  (`sha256(pcm), "asr-v1[:lang]", model`) — the same audio is never transcribed twice.
- Exposed as MCP `video(action="transcript"|"subtitles")`, REST `POST /api/video/transcript` (job) and the web
  subtitle maker (`templates/app.html:subtitleSubmit`) — all via `video.transcribe_file` (sentences, subtitle-sized
  cues from word times `transcript.cues`, an `.srt`, a check). Used internally by overlay `at: {"say": …}`
  (`video.said_to_times`) and `subtitles="auto"` (`video._auto_subtitles`).
- Omlett 3.5 adds `prompt`, `vocabulary`, `model`, `force`, `correct` — see `docs/changelog.d/transcript.md`.

## 7. Sharing — current state

- **None.** No ACL, no project/workspace membership, no "shared with me": a workspace is a directory per uid
  (§1) and every route resolves file_ids only inside the caller's own root (`crafts.existing`, `storage/files.py`).
- The only way out is a **read-only signed link** (§4, 24 h), the editor deep link `edit_url`
  (`/app/file/<id>/edit`, owner only — it needs the owner's session), `transfer(action="export")` to a cloud store
  or `provider="url"`, and the review inbox (`review_requests`, same owner only).
- MCP server instructions promise files are "never published or shared" (`mcp_server.py` FastMCP `instructions`).
- `file_versions.owner_id` is the only ownership record in Postgres (written on every meta write).

## 8. Repo notes

- `CLAUDE.md`, `app/CLAUDE.md`, `app/{templates,llm,storage,tools,workers,routes}/CLAUDE.md`,
  `app/brands/renderers/CLAUDE.md` contain only auto-generated claude-mem activity tables (dates + titles from
  April–May 2026), no instructions or conventions.
- Postgres schema lives in one string, `db.py:SCHEMA` (`CREATE … IF NOT EXISTS`, guarded `ALTER`s), applied at
  start (`db.start`). Tests: `tests/conftest.py` (`pg_url` / `TEST_DATABASE_URL`, `db_client`).
