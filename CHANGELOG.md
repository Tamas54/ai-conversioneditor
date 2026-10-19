# Changelog

## 2026-10-19 — OPERATION OMLETT

Studio development package from the 2026-10-19 live MCP test (the "omlett" news video) and the owner's requests.
Five lanes, merged on `feat/omlett`. Architecture notes: `docs/ARCHITECTURE-studio.md`.

**Off by default / needs env:** sharing (`SHARING_ENABLED=1`), AI video via kie.ai (`KIE_API_KEY` + `AI_VIDEO=admin|1`).
**Behaviour changes:** merge-files fills a clip of another shape with its blurred copy (was black bars) and keeps a common
fps; tool outputs use CRF 17 / AAC 192k (bigger files on grainy footage); the final render goes through a temporary ProRes
master + x264 CRF 19; downloads/exports drop GPS/location metadata (`STRIP_LOCATION=0` keeps it).
**Deferred:** AI Act / C2PA marking of AI clips (legal, out of scope for now); re-rendering only the changed section.

### Phase 0 recon + 3.5 Hungarian transcript

#### 0 — Recon
- `docs/ARCHITECTURE-studio.md`: roles, credits and metering per path, MCP registration and auth → uid, storage /
  versions / signed links / retention, video pipeline and its check, transcript, sharing — each with `file:function`.

#### 3.5 — Hungarian transcript
- New transcript parameters, the same on MCP `video(action="transcript")`, REST `POST /api/video/transcript` and the
  web subtitle maker ("Better recognition"):
  - `prompt` → Whisper `initial_prompt` (what the recording is about);
  - `vocabulary` (list or comma-separated) → Whisper `hotwords`, then the vocabulary fixes;
  - `model`: `large-v3-turbo` (default, `ASR_MODEL`) | `large-v3` (one model in memory at a time);
  - `force`: listen again, bypassing the cache; the new result replaces the cached one;
  - `correct`: left out = vocabulary fixes only (no AI) · `true` = the Studio AI proofreads what is still misheard ·
    `false` = no corrections.
- Cache key (Postgres `ocr_cache`) = audio sha256 + language + a hash of prompt/vocabulary + model; calls without the
  new parameters keep the old key, so earlier transcripts stay hits.
- Corrections, in the owner's order (data → deterministic → AI): (1) vocabulary fixes without AI — a misheard word
  with the same first letter, ≥ 5 letters and a similar sound is replaced, but never another form of the same word
  ("fakanál" stays next to "fakanállal") or a different word ("Kanállal"); (2) with `correct=true` the Studio AI gets
  the remaining text (one AI step per ~6000 characters through `_call_model` → `billing.ai_step`: on MCP the monthly
  allowance, then credits; on the web fair use as everywhere). AI fixes are applied only where the misheard words
  stand verbatim in that sentence and nothing is rephrased; AI decisions are kept in `content_checks` (same text +
  vocabulary → no second charge). Out of AI steps: the transcript still comes back, with the reason in the check.
- Results carry `corrections [{at, heard, fixed, by: vocabulary|ai}]`, `model` and, when the AI worked,
  `studio_ai {steps, credits}`; subtitles and the `.srt` are built from the corrected words.
- Fixture (10-19 mishearings, correct → heard): sonkával → sokkával, fakanállal → fokonállal, Jöhet a tálalás →
  Jó natállalás are fixed from the vocabulary; szép → kép, azt ajánlom → azt jön, Voilà → Ala by the (mocked) AI.
- `transcript.SCHEMA_REF["transcript"]`: the parameter reference for `video(action="schema")` (lane "video" wires it).
- Measured on real large-v3-turbo (CPU, synthetic Piper speech of the fixture sentences, ~12 s): without parameters
  it heard "ometet", "somkában", "Vojra"; with `vocabulary` + `prompt` "omlettet" and "sonkával" came out right
  ("Voilà" is left to `correct=true`). That run also caught a false vocabulary fix ("azt ajánlom" heard right →
  "azt azt ajánlom"); exact hits of a vocabulary phrase are now left alone (regression test).
- Tests: `tests/test_transcript_hu.py` (10 tests fail on main and pass here; + an opt-in real-Whisper test,
  `ASR_SLOW_TEST=1`, verified with the tiny model).

### Video tool — 1.3, 1.4, 3.4 end card, 3.6, 3.7, phase 5

#### 1.3 — the video check sees text collisions
- A graphic's `text` without `w` no longer wraps at a hidden 0.6 of the frame: it stays on one line while it fits in
  0.92 of the frame width (measured with Inter, the renderer's font — ő/ű included; short texts keep their 0.6 box, so
  older videos look the same; left/right-aligned text keeps its old anchor edge).
- The builder reports every graphic text's drawn (ink) box from the engine (`sourceRectAtTime`); the check turns them into
  findings: two texts overlapping (same layer or two layers on screen together), a text running out of its enclosing
  `rect` or off the frame, an unexpected line break (no `w`). Each finding names the layer/scene, the element and a
  suggested `w` / `size` (or `y`). Status becomes `needs_check`.
- `render=false` (overlay / make / edit) still returns this layout check.
- Measured on the reconstructed 10-19 layers (`rendkivuli`, `tudta`, `hir3`, main's 0.6 box): main 0 findings / `ok`,
  now 3 named findings / `needs_check`. Without `w` the new default keeps "RENDKÍVÜLI KIADÁS" (66 px, ~663 px wide) and
  the Tudta line on one line; only the over-long ticker text is still reported (with a size that fits).

#### 1.4 — short `video` tool description + `action="schema"`
- The `video` MCP description went from 21 902 to 1 945 characters (cap 2 000, tested; sent without indentation). It
  lists the main actions and points to `video(action="schema", topic=…)`.
- New `action="schema"`: topics make | overlay | layers | graphic | chart | subtitles | edit (no topic = the list). The
  registry is a plain dict, `app.tools.video_schema.TOPICS` — another lane adds a topic with
  `TOPICS["adjust"] = video_fx.SCHEMA` (no other wiring needed).
- A layer with a `chart` object and no `type` is a chart layer (a list of `elements` without `type` is a graphic); the
  type / `at` / chart errors carry a minimal example and point to the schema.
- New MCP params: `topic`, `render` (`render=false` for make / overlay / edit: save the project only).
- Other tool descriptions over 2 000 characters (not changed here): wordcraft 4 829, design 4 785, pptx_build 4 246,
  forms 3 698, file_tools 3 046, office 2 901, validate_document 2 531, files 2 297, convert 2 255, facts 2 080.

#### 3.6 — `move_to` timing
- The glide starts once the element has appeared (after the ≤ 0.5 s fade-in for `appear`, after `dur` for pop / grow /
  slide, 0.3 s for pulse) and lasts `move_dur` (new; default `dur`), eased. It starts from the element's own place
  (a slide-in no longer makes it start from the slide offset).
- Regression (rendered frames, marker at x 0.2, `at` 1.5, `dur` 2.2, `move_to` [0.88, y]): at 4.3 s main had it at
  x ≈ 0.2 (the move started at 3.9 s and lasted 4.4 s); now x = 0.88 (arrives at 4.2 s).

#### 3.7 — Lottie search
- `{"search": …}` now loads up to five candidates and orders them: transparent background first (a solid layer or a
  filled rect covering the canvas counts as opaque), then the shape that fits the element's box, then the vision
  ranking. `"pick": n` takes the n-th of that order.
- The result has `lottie_picks`: where it went, name, author, `thumb_url` (preview), `gif_url`, page, its pick / of,
  transparent, size, and the alternatives with their previews. The chosen URL, name and preview are kept in the spec.
- The check samples every Lottie element (its comp rendered transparent at 25/50/75 % of its loop): an animation that
  fills its whole box (opaque background) or sits on a measured text is a named finding; a clean one is in `verified`.
- Tests mock LottieFiles (no network); the sampling test renders synthetic Lottie JSONs with the engine.

#### 3.4 (overlay side) — end card that extends the footage
- `video(action="overlay", …, end_card={"title", "subtitle"?, "kicker"?, "dur"?})` (or `end_title` / `end_subtitle`):
  the footage is extended by `dur` seconds (default 4) — last frame frozen with `tpad=stop_mode=clone`, sound padded
  with `apad=pad_dur` (one x264 CRF 17 pass, a hidden work file) — and a full-frame card (80 % veil over the frozen
  frame, kicker / title / accent bar / subtitle, the picture and animation credits) sits on the added seconds.
  An overlay may have only an end card.
- Edits: `changes.end_card` changes only the given fields (a new length re-extends from the plain footage; the same
  length reuses the extension); `"none"` removes it and the video is back to the footage's length.
- Measured: 3.0 s clip + `dur: 2` → 5.0 s video, audio 5.0 s; frames at 3.6 s and 4.6 s are identical under the card.
- The general extend-video / image-to-video operations belong to lane "media".

#### Cross-lane hooks (schema registry, transcript hints)
- `video_schema.register({topic: text})` adds any lane's reference; `app.tools.transcript.SCHEMA_REF` (lane
  transcript) is picked up automatically when present, so `video(action="schema", topic="transcript")` works after the
  merge without wiring.
- Overlay `transcript={"prompt", "vocabulary", "model"}` (MCP param `transcript`) is passed to the speech recognition
  behind `subtitles="auto"` and `at: {"say": …}` (also kept for edits) — only the keys `transcript.transcribe` accepts,
  so it is a no-op until lane transcript's new parameters are merged.

#### Phase 5 — video tool details
- `make`: new `scrim` (0–1, also per scene; `0` = no darkening) controls the veil over title card, scenes, chart / number
  scenes and end card — `backdrop: 0` alone left a 70 % shade before. The title card's kicker gets a dark pill sized to
  its rendered text (engine `sourceRectAtTime`) over a photo, so the red accent is readable.
- Fixed length compensates the crossfades: when every scene has its own `duration` and they come out short, they are
  stretched to the asked length (main: 10 s asked, 3 × 2.5 s scenes → 8.9 s; now 10.0 s). Scene lengths stay minimums
  (too long is still reported, never cut).
- `opacity` is 0–1 everywhere and stored that way (0.94 was stored as 94.0); 0–100 is read as percent; old saved specs
  keep working. Diagram graphics now emit 0–1 too. Documented in the `graphic` schema topic.
- Layer listings / comp names: a graphic is labelled by its title or first text (`graphic “RENDKÍVÜLI KIADÁS”`), else
  `graphic (3 elements)` — no more `graphic “”`.
- `video(action="status", job_id, wait=30)` waits up to 30 s (≤ 55) while the job runs. The `jobs` MCP tool now finds
  Studio jobs (video, render, transcript) too, with a note, instead of "No such job.".
- Faster turns: `render=false` (save only, layout check still returned), `draft=true` on make / overlay / edit (half size,
  2.5 Mbps draft; previews use it until a full render; `info.last_render.draft`). Re-rendering only the changed section
  into the existing MP4 is NOT done: the engine renders spans, but splicing separately encoded H.264 pieces without
  re-encoding needs keyframe-aligned cuts and identical encoder parameters, which the CLI doesn't expose — the existing
  `action="render", scene="L2"` clip covers checking one part.
- Credits: "Pictures and animations:" whenever a Lottie is in the video (main only looked for a credit starting with
  "Animation ", so a Lottie with its own credit got "Pictures:").
- A graphic image / Lottie / cut-in that couldn't be loaded is now a named finding (it was only logged; the check said
  `ok`).

### Media ops — 1.1, 1.2, 3.2, 3.3, 3.4 ops, phase 5 info/metadata

#### 1.1 merge-files keeps portrait clips portrait
- The canvas comes from the first clip's DISPLAY size (after rotation: displaymatrix side data and the legacy `rotate`
  tag are both read) — 2 portrait iPhone-style clips (coded 1920×1080, rotation 90) + 1 landscape → 1080×1920
  (it was 1920×1080 with the portrait clips in a ~608 px strip).
- New merge-files options: `canvas` = auto | vertical | landscape | square | WxH, `fit` = blur (default: a clip of
  another shape sits on its own blurred copy) | contain (black bars) | cover (fill, edges cut).
- resize-video: `aspect_mode = blur` (same helper).
- Shared modules: `app/tools/mediaprobe.py` (duration, display size, rotation, fps, codecs, audio, HDR, bitrate) and
  `app/tools/videonorm.py` (canvas size, fit chains).

#### 1.2 One encoding profile (generation loss)
- `app/tools/encoding.py`: intermediate x264 CRF 17 (tool ops: resize, merge, trim, crop, rotate, watermark,
  gif-to-video, footage set upright, cut-ins), final x264 CRF 19, preset medium, AAC 192k, +faststart. Env:
  `VIDEO_CRF_INTERMEDIATE`, `VIDEO_CRF_FINAL`, `VIDEO_X264_PRESET`, `VIDEO_AUDIO_BITRATE`, `VIDEO_FINAL_ENCODER`.
- Final render: the engine's own H.264 only does a fixed bitrate (8 Mbps; its "quality" mode is HEVC/AV1 only), so
  the engine now writes a ProRes LT master (~100 Mbps, temp, deleted) and x264 CRF 19 makes the MP4 (tagged BT.709).
  `VIDEO_FINAL_ENCODER=engine` restores the old path; draft renders stay on the engine's fast H.264.
- merge-files was already one pass (scale + concat in one filtergraph); it stays one pass.
- Omlett chain, measured (synthetic 1080×1920 30 fps testsrc2 + blur + temporal grain, ~12.6 Mbps x264 source,
  4 s; resize-video → merge-files → video overlay; SSIM/PSNR vs the source over 114 frames, ffmpeg ssim/psnr):

  | stage | before (main a9abf31) | after |
  |---|---|---|
  | resize-video | 6.58 Mbps · SSIM 0.98638 · PSNR 46.56 dB | 13.09 Mbps · SSIM 0.98979 · PSNR 48.56 dB |
  | merge-files | 6.54 Mbps · SSIM 0.98451 · PSNR 45.44 dB | 12.41 Mbps · SSIM 0.98723 · PSNR 47.21 dB |
  | final render | 8.36 Mbps · SSIM 0.97666 · PSNR 35.32 dB (Y: 0.98020 / 37.88) | 5.52 Mbps · SSIM 0.97781 · PSNR 35.29 dB (Y: 0.98104 / 37.73) |

  The final PSNR has a ~35 dB ceiling that no bitrate lifts: the engine resamples the footage (4:2:0 → RGB →
  4:2:0, different chroma interpolation at sharp colour edges) — even its ProRes HQ output or its H.264 at 20 Mbps
  stay at 34.4 dB on this content. The final step's gain is SSIM at a smaller file; removing the ceiling needs the
  graphics composited onto the original footage in ffmpeg (alpha render + overlay), a renderer change.
  Grainier source (testsrc2 + strong noise): final 8.31 → 11.34 Mbps, SSIM 0.97284 → 0.97486.

#### 3.3 HDR input (HLG / PQ)
- `color_transfer` arib-std-b67 (HLG, iPhone) or smpte2084 (PQ) → zscale to linear (npl 100) → BT.709 primaries →
  `tonemap` → BT.709 transfer, 8-bit 4:2:0, in the shared normalisation (`videonorm.head_filters`): merge-files,
  resize-video, the overlay's base footage (set upright / mapped once before the engine) and cut-in clips.
- Algorithm `VIDEO_TONEMAP` (default mobius; hable, reinhard, clip… allowed), `VIDEO_HDR_TONEMAP=0` turns it off.
  Measured on a synthetic 10-bit HEVC HLG clip (SDR testsrc2 → BT.2020/HLG, reference white 75%), signalstats vs the
  SDR original (YAVG 125 / SATAVG 112): untouched (main) 126 / 44 — saturation 39%, washed out, still tagged HLG;
  mobius 121.5 / 95.3 (97% / 85%); hable 101.3 / 77.8 (81% / 69%, darker) — hence mobius as the default.
- zimg/zscale is in the image's ffmpeg 4.4.2 (checked); the test skips where it isn't.

#### 3.4 Extend a clip, image → video, allowlist (ops part)
- ffmpeg-advanced allows `tpad`, `loop`, `apad` — bounded (≤ 3600 s padding, loop repeats × size ≤ 18 000 frames,
  no bare `apad`), so no request can produce an endless output. A refused filter or flag now lists what is allowed.
- New tool `extend-video` (options `duration`, `at`): holds the last frame for `duration` s (room for an end card),
  or freezes the frame at `at` for `duration` s and plays on; silence during the hold, sound stays in sync.
- New tool `image-to-video` (options `duration` default 5, `motion` still | zoom_in | zoom_out | pan_left | pan_right |
  pan_up | pan_down, `canvas`, `fit`, `fps`): a still (EXIF-rotated) → MP4 clip with an optional Ken Burns move
  (zoompan on a 2× upscale, no jitter) and a silent stereo track (so merge-files keeps the other clips' sound).
- Both are catalog tools: web page + REST (`tool/extend-video`, `tool/image-to-video`) + MCP `file_tools` + agent.

#### 3.2 Video effects (CPU, ffmpeg)
- `app/tools/video_fx.py`: stabilise (vidstabdetect + vidstabtransform, two passes; deshake where vidstab is
  missing — the image has vidstab), denoise (hqdn3d fast / nlmeans quality), sharpen (cas / unsharp), colour and light
  (eq brightness/contrast/gamma/saturation; curves for levels, shadows, highlights and highlight roll-off;
  colortemperature with preserved lightness; vibrance), LUT looks via lut3d from generated 17³ .cube tables
  ("Meleg konyha" warm_kitchen, "Hírstúdió" news_studio, "Mozi" cinema, "Fekete-fehér" black_white).
- Auto: signalstats on the keyframes (YAVG, YMIN/YMAX, YLOW/YHIGH, SATAVG) → black/white point, gamma, saturation and
  roll-off for hot highlights — bounded, only for visible faults; the user's own values win.
- Preview: 4 frames, left half before / right half after, as one PNG (before the full render).
- New tool `adjust-video` (web, REST, MCP file_tools; flat options auto, brightness, contrast, gamma, saturation,
  shadows, highlights, highlight_rolloff, temperature, vibrance, lut, denoise, sharpen, sharpen_method, stabilize,
  preview). Audio is copied (AAC), video at the intermediate profile.
- `video` edit: `changes.adjust` (same keys) corrects an overlay video's footage — always from the clean
  (upright / tone-mapped) original, `{}` resets; layers stay. A made (scene) video says to use adjust-video instead.
- Wording everywhere: clipped highlights can't be recovered, only toned down.
- Reference text for the video tool's `action="schema"`: `app.tools.video_fx.REFERENCE`.
- Measured on synthetic clips: shaky testsrc2 (±22 px jitter) frame-to-frame difference 43.5 → 14.9 with vidstab;
  a dim, flat clip gets levels 0.07–0.82 → 0–1 and gamma 1.2 from Auto.

#### Phase 5 (media items)
- `video info` on a raw video (not a project): duration, display width/height (after rotation), rotation, fps,
  video/audio codec, audio, HDR (hlg | pq), bit depth, bitrate, and the stored size when it is rotated. Info results
  (raw video and project) are read-only, so the automatic check no longer adds "It opens: 3 pages" — that line came
  from the generic check counting a video's 3 preview frames as pages; a video/audio result now says
  "It plays: 12.3 s, 1080×1920 at 30 fps, with sound." instead.
- `read-metadata` takes up to 100 files in one call: one JSON with `{count, items: [{file_id, label, …}]}`; one file
  answers as before. Media files also get a `video` summary (display size, rotation, HDR…) from mediaprobe.

### Credits + kie.ai — 1.5, 2.1–2.5

#### 2.3 Credit ledger (Postgres)
- `credit_ledger` extended in place (it already existed): `kind` (grant | purchase | hold | capture | release | refund |
  adjust | admin_usage), `workspace_id`, `operation`, `provider`, `provider_task_id`, `provider_cost`, `idempotency_key`
  (UNIQUE), `meta` (jsonb). Every balance change is one ledger row + `users.credits` (kept as the cached balance) in ONE
  transaction under `SELECT … FOR UPDATE` on the user row — `SUM(delta) = users.credits`.
- Migration keeps existing balances: old rows get a `kind` once; for every account whose balance doesn't add up to its
  ledger (credits granted by hand in SQL, the old 0-delta "signup" row) one `adjust` row "opening balance" with the
  difference, key `opening:<uid>` — idempotent on every restart; `users.credits` itself is never touched.
- `pricing_rules` (operation, model, tier, unit second|job|image, provider_cost, multiplier, min_charge): price =
  ceil(provider_cost × units × multiplier), at least min_charge; local CPU work has no rule → 0. Editable in the admin
  dashboard (💳 pill → modal).
- Paid external work: estimate → `hold` (max) → provider job → `release` + `capture` (actual, ≤ hold) in one
  transaction, or only `release` on failure. Keys `hold:/release:/capture:<op>` make webhook + polling races harmless.
- Daily reconciliation (lifespan loop, advisory lock): provider balance vs. previous snapshot − ledger provider costs →
  drift notice; balance under `KIE_LOW_BALANCE` (default 2000 kie credits ≈ $10) → low-balance notice; accounts whose
  balance ≠ ledger sum → notice. Notices = log warning + admin dashboard (no e-mail).
- UI: credit history on the Plans page (`GET /api/account/ledger`), `GET /api/estimate`, header balance (existing chip).

#### 1.5 MCP uses the same credit path as the web UI
- Every MCP tool call runs inside `credits.mcp_call` (in the `linked` wrapper): the token's user pays; an optional price
  per call comes from `pricing_rules` (`mcp:<tool>` or `mcp:<tool>.<action>`, default 0 — local CPU work stays free),
  held before the tool runs, captured on success, released on error.
- Not enough credits → the tool never starts; error text `insufficient_credits: this needs N credits, you have M …`.
- Admin is exempt but logged: one `admin_usage` row (delta 0) with `would_charge` / AI steps per paid call; paid
  provider jobs log their provider cost the same way.
- `studio_ai` result field extended (back-compatible: `steps`, `credits` unchanged) with `credits_used`,
  `credits_held` (provider job reservations), `balance`; admin: `admin: true`, `would_charge`.
- Code truth vs. the spec: MCP already charged credits for Studio AI steps over the monthly allowance and for paid
  work routed through the web handlers (digitize, restore, fact checks); what was missing was a per-call price hook,
  admin logging and the balance in the result.

#### 2.1 kie.ai adapter (swappable provider layer)
- `app/providers/base.py` (interface: `create(model, input) → task`, `status(task)`, `fetch_result(task) → stored
  file_ids`, `cancel(task)`, `balance()`, `upload(path)`), `app/providers/kie.py` (kie.ai), `app/providers/__init__.py`
  (registry + flag). Provider data (task ids, result URLs, field names) stays in the provider layer and
  `provider_tasks`; the video layer only sees files already in our own storage.
- Per-model field-name mapping table (`MODELS`): unknown fields are refused instead of sent; Seedance first/last-frame
  and reference modes can't be mixed; Topaz `upscale_factor` is a string.
- 429 → exponential backoff 1-2-4-8-16 s plus a client-side bucket (18 new tasks / 10 s, under kie's 20/10 s).
  kie returns errors as HTTP 200 with `code` in the body — always checked.
- Results are downloaded immediately (kie keeps generated files 14 days; uploads 24 h) through the shared SSRF-safe
  `net.download`.
- `POST /webhooks/kie?token=KIE_WEBHOOK_SECRET` (+ optional kie HMAC `KIE_WEBHOOK_HMAC_KEY`): the callback is only a
  signal — the result is re-read from `recordInfo` and matched to our `provider_tasks` row; idempotent (row lock +
  `settled` + ledger keys). No secret → no callback URL is sent, polling only (backoff 3 s → 30 s), and a resume loop
  finishes open tasks after a restart (a task stuck without a provider id for 10 min, or running > 6 h, is released).
- Feature flag: nothing runs without `KIE_API_KEY` AND `AI_VIDEO` = `admin` (admin only) | `1` (everyone).
- Live (2026-10-19): a request kie rejects at create time costs nothing; an accepted task that fails reports
  `creditsConsumed: 0` and the balance didn't move (4 failed Topaz tasks, 0 credits) → failed tasks are not billed.
  kie has no cancel endpoint: cancel is local (hold released; a late provider charge shows up in reconciliation).

#### 2.2 Models and prices (verified 2026-10-19 on kie.ai/pricing's own data source; 1 kie credit = $0.005)
| Operation | Model | Unit | kie credits | Studio credits @ ×0.15 |
|---|---|---|---|---|
| Seedance 2.5 480p / 720p / 1080p, no video input | `bytedance/seedance-2-5` | s | 28 / 63 / 158 | 4.2 / 9.45 / 23.7 per s |
| … with reference video ((ref + output) s) | same | s | 17 / 38 / 95 | |
| Seedance 2.5 draft (480p only) | same, `draft: true` | s | 28 (live: 4 s = 112) | 4 s → 17 |
| Draft → final 1080p | `bytedance/seedance-2-5-draft` | s | not published (held at 158) | |
| Topaz video upscale 1x–2x / 4x | `topaz/video-upscale` | s | 8 / 14 | |
| Topaz image upscale 2K / 4K (not wired) | `topaz/image-upscale` | image | 10 / 20 | |
- Seeded into `pricing_rules` only where missing (admin edits survive restarts). Default multiplier `0.15` Studio
  credits per kie credit (`KIE_CREDIT_MULTIPLIER`), `min_charge` 1 — owner decision, see the lane report.
- Estimates: `ai_video.estimate_clip` (resolution, 4–30 s, reference video seconds) and `estimate_enhance` (probed
  length rounded up × factor tier). Seedance modes (text | first frame | first+last | reference) can't be mixed.

#### 2.5 "Enhance" (Topaz video upscale)
- `video(action="enhance", file_id, factor=1|2|4)` over MCP, `POST /api/ai/enhance` + an "Enhance… AI" quick tool
  for videos and video projects in the app (only when AI video is on), with the estimate on the button
  ("Enhance — 4 credits"); `video(action="estimate", …)` / `POST /api/ai/estimate` give the price only.
- The result is a NEW version of the file (`source_file_id` = the source, label "… (enhanced 2x).mp4"), usable as the
  base of overlay / merge-files / edit; a project is enhanced from its last render.
- kie's API input limit is 50 MB (mp4/mov/mkv, same as the web form): bigger inputs are cut in time with stream copy
  (keyframe cuts, no re-encode; an oversized part is re-encoded to a target bitrate), upscaled part by part (one user
  operation, one hold, one capture) and joined back with stream copy; the original sound (first decodable track) is
  muxed back in.
- Credit pre-check before anything is uploaded: not enough → `insufficient_credits`, nothing starts.
- Live: kie's Topaz video upscale answered "internal error" (failCode 500) for every input on 2026-10-19 — also for
  kie's own documentation example — and billed 0; the flow is covered by tests with a fake provider that really
  upscales with ffmpeg.

#### 2.4 AI clip (Seedance 2.5)
- `video(action="ai_clip", ai={prompt, resolution, duration 4–30, audio, aspect, draft, image | first_frame (+ last_frame)
  | reference_images/videos/audios, at | after_scene}, file_id=<project>)`, `POST /api/ai/clip`, and an "AI clip… AI"
  quick tool on video projects and videos (estimate on the button; "Make final (1080p)" on a draft clip).
- Text→video; image→video from an uploaded picture or `{"frame": seconds}` of the current video/project (its last
  render) — the frame is prepared to kie's rules (JPEG, 300–6000 px, aspect 0.4–2.5); first/last-frame mode vs.
  reference mode are refused when mixed.
- Draft (480p, cheap) → `ai={"finalize": task_id}` → `bytedance/seedance-2-5-draft` with the draft's kie task id at
  1080p (kie keeps drafts 7 days); the final clip is a new version of the draft file.
- On the timeline (the existing edit path, a new project version): `at` → a full-screen cut-in layer (its sound mixed
  when the clip has sound); `after_scene` → a scene of its own (a graphic with the clip as a full-frame video element).
  If placing fails, the paid clip is still returned with the exact `changes` to place it by hand.
- The estimate is reserved inside the request (MCP `studio_ai.credits_held`), so `insufficient_credits` comes back
  before anything is uploaded or started.
- Live, through the full app path (ledger + kie, polling): 480p 4 s draft → kie charged 112 credits
  (`creditsConsumed`), the Studio captured 17, clip 854×480 24 fps H.264, downloaded from kie's CDN into our storage.
- Deferred: AI Act art. 50 / C2PA / IPTC `DigitalSourceType` marking (legal is out of scope for now); the AI origin is
  recorded in the file's metadata (`extra.ai`: provider, model, prompt).
- Live test spend (budget 250): 224 kie credits (two 480p 4 s drafts — the first one's download failed in the test
  harness, not the app); 5 failed / refused tasks cost 0.

#### Tests
- Full suite in the prod-like image on a throwaway Postgres: 618 passed, 9 skipped (main: 572 / 9) — 46 new tests in
  `test_credits.py`, `test_mcp_credits.py`, `test_kie_provider.py`, `test_ai_models.py`, `test_ai_enhance.py`,
  `test_ai_clip.py`; kie is always mocked (no network), the timeline tests render with the real engine.

### Shareable workspace — phase 4, 3.1 Save to Photos, location stripping

#### Phase 4 — shareable workspace (lane "share")

- **Sharing (owner / editor / viewer), behind `SHARING_ENABLED=1`** (off by default — off, everything behaves as before:
  workspaces stay physically isolated). Share a file or a video project by user or e-mail; an e-mail without an account
  becomes a pending invite that resolves on sign-up / sign-in (no e-mail is sent — the app has no mail path). Role links
  (`/share/<token>`) work only for signed-in users; signed download links stay read-only.
  A share covers the file's *family* in the owner's workspace: its versions, renders, the project's media assets (from
  every version's `assets` map) and — read-only — the footage it was made from.
- **One ACL gate for every request**: the auth middleware reads every file id in the URL, query, JSON body and MCP
  tool arguments, checks the actor's role and runs the request in the owner's workspace; the acting user stays the
  operation's owner (credits, quotas, jobs, audit). Web: 403 `{"detail", "not_shared": true}`; MCP: a tool error
  (`isError`), no job is started. New Postgres tables: `shares`, `share_links`, `share_audit`.
- **Shared with me**: `/api/files` → `shared` (owner name, role), MCP `files(action="list")` → `shared_with_me`;
  `files(action="share" | "share_link")`. Edits by invitees are new versions in the owner's workspace with
  `edited_by`; the audit log (`share_audit`) records who shared / changed what and when.
- **Retention**: shared projects and their whole family (assets included) are exempt from the 30-day cleanup while
  the share (or a pending invite) is active.
- **Versions with optimistic locking**: every edit stays a new version; if someone else saved a newer version of the
  same base meanwhile (or of the `if_version` the client started from — any MCP tool and the editor save accept it),
  the new one becomes a fork labelled "<Name>'s version", the result carries `conflict {fork, other, message}`, and
  the user picks the current one (`POST /api/share/choose`, MCP `files(action="choose")`); nothing is lost. Same
  person branching an older version is not a conflict. Advisory edit lock with a 30 s heartbeat (stale after 90 s,
  `edit_locks` table) only tells who is editing; it never blocks.
- **Web UI** (only when sharing is on): file view "More → Share…" dialog (invite by e-mail with Can edit / Can view,
  change role, remove, pending invites, link for signed-in people, "keep location" opt-out, recent activity);
  Files → "Shared with me" (owner's name on each card, "By <name>" on versions others made in your workspace); the file
  header tag "<Owner>'s · you can edit"; a "Two versions" bar that lets the user choose the current version after a
  parallel save; a "<Name> is editing this right now" bar from the edit-lock heartbeat.
- **Privacy — GPS / location stripped on share and on export** (`app/storage/privacy.py`, `STRIP_LOCATION=1` default):
  video via `ffmpeg -c copy` with the location keys blanked (`location`, `location-eng`, `©xyz`,
  `com.apple.quicktime.location.ISO6709`; data tracks dropped) — rotation (display matrix), creation time and the other
  metadata are kept, the mtime too; photos via ExifTool (EXIF GPS IFD, XMP/IPTC location fields), Pillow fallback for
  JPEG/PNG without touching pixels. On share the shared family's media is cleaned in place (opt-out `keep_location`);
  on export every outgoing copy is cleaned and cached (`/files/…` downloads incl. Range, image `/preview`, cloud export,
  Save to Photos). If location is present but can't be removed, the file isn't sent (422).
  **Measured: the spec's "pipeline outputs are already clean" is false for the ffmpeg tools** — resize-video,
  trim-video, compress-video, rotate-video and merge-files keep `location`/`location-eng` from an iPhone-like input
  (ffmpeg 4.4 copies global metadata by default; `ios_safe` uses `-map_metadata 0`). The export guard covers them.

#### 3.1 — Save to Photos (iOS)

- File view **"Save to Photos"** button for videos and pictures, shown where the browser can share files (phones): on
  tap it fetches the file with a progress readout, wraps it as a `File` and calls `navigator.share({files})` → the
  share sheet's "Save Video" / "Save Image" puts it in Photos. If the tap's user activation ran out during the
  download (Safari), the button turns into "Ready — tap to save to Photos"; if this file can't be shared, it downloads.
- `GET /api/photos/<file_id>` → `{url, name, type, size, converted}`: MP4/MOV with H.264/HEVC + AAC and
  JPEG/PNG/HEIC/HEIF/GIF go as they are; WebM/MKV/other codecs are transcoded to MP4 (H.264 CRF 20 + AAC 192k,
  faststart) and WebP/TIFF/BMP/PSD to JPEG (PNG with alpha) first, as a job; a video project uses its last render.
  `GET /api/photos/<file_id>/file` serves it with the right Content-Type and file name, location-free. Guests too.
- Native app variant (Capacitor/Expo, PHPhotoLibrary "add only") is left for later.

#### Sharing — ACL hardening after the independent review

- **Fail closed per file**: inside a shared request the owner's workspace resolves only for the shared family (plus
  files the request creates and new versions whose source chain leads into the family); any other owner file is
  403 even if its id slipped past the request scan. Signed links are never minted for such a file.
- **Request scan everywhere**: path, query keys/values, JSON (keys, values, ids inside URLs / comma lists /
  JSON-in-a-string, `\u`-escaped), form-urlencoded (decoded), multipart and text bodies, MCP arguments — up to 64 MB.
  In a shared request a body that can't be checked is refused (413 too large, 415 unknown type); raw-file bodies
  (editor save, video asset) carry their ids in the query. Uploads never enter a shared context.
- **Workspace-wide code stays in the actor's workspace**: `tenancy.user_root()` without an argument is always the
  signed-in user's (listing, temp, delete); the owner's tree only explicitly — `workspace_root()` for new-file naming,
  `visible_root()` (family only) for the version tree and the digitize sibling lookup; pdfcraft `--root` is the
  narrowest root covering the script's (ACL-resolved) paths, owner-side paths must be family members.
- Review inbox / resolve are the actor's own; lineage, live-data bindings, the share activity log (for non-owners)
  and conflict info are filtered to the shared family.
- **Retention on a DB error**: every successful round saves the shared-file list in the workspace root
  (`.shared-keep.json`); if Postgres doesn't answer, the last list (≤ 7 days old) keeps the shared files and the rest
  of the workspace is cleaned as usual; with no list that round skips the workspace — logged as an error.
