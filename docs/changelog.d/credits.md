# Lane "credits" — credit ledger, MCP credit path, kie.ai (Seedance 2.5, Topaz)

## 2.3 Credit ledger (Postgres)
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

## 1.5 MCP uses the same credit path as the web UI
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

## 2.1 kie.ai adapter (swappable provider layer)
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

## 2.2 Models and prices (verified 2026-10-19 on kie.ai/pricing's own data source; 1 kie credit = $0.005)
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

## 2.5 "Enhance" (Topaz video upscale)
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

## 2.4 AI clip (Seedance 2.5)
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

## Tests
- Full suite in the prod-like image on a throwaway Postgres: 618 passed, 9 skipped (main: 572 / 9) — 46 new tests in
  `test_credits.py`, `test_mcp_credits.py`, `test_kie_provider.py`, `test_ai_models.py`, `test_ai_enhance.py`,
  `test_ai_clip.py`; kie is always mocked (no network), the timeline tests render with the real engine.
