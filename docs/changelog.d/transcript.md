# Lane "transcript" (OPERATION OMLETT)

## 0 — Recon
- `docs/ARCHITECTURE-studio.md`: roles, credits and metering per path, MCP registration and auth → uid, storage /
  versions / signed links / retention, video pipeline and its check, transcript, sharing — each with `file:function`.

## 3.5 — Hungarian transcript
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
