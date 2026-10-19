# Lane "credits-fix" — fixes from the independent credits + kie review

- **One cancel rule** (DELETE /api/ai/task, job cancel, MCP jobs cancel): before a part reaches kie, cancel releases the
  whole hold and nothing starts; once kie has it (kie has no cancel API), the hold stays, the user pays what kie really
  charged (0 if it fails), the result is kept in their files and nothing is put on the timeline. A part that fails to
  start and the 6 h wait timeout no longer release while other parts run; the provider cost is booked when they end.
  Closes the "start + cancel = free kie money" hole.
- Shared files: an op started on someone else's file stores the owner's workspace and reopens it
  (`sharing.workspace`) when the webhook or the resume loop finishes it — the result lands next to the source, the
  starter pays.
- Job cancel during prepare/upload releases the hold and never leaves the op "watched" forever.
- The real video length is paid: max(format, stream, packets ÷ fps) — a header patched to "1 s" over 20 s of picture
  is billed 20 s; the Topaz upload and Seedance reference videos are cut with `-t` to the paid seconds.
- Capture is never above the hold (no hold → no charge); a deleted pricing rule is a tombstone (`deleted_at`) and
  isn't re-seeded at boot; a paid AI op with no price (deleted / zero rule) isn't started for non-admins.
  `KIE_CREDIT_MULTIPLIER` only applies to rules seeded for the first time — change existing prices in the admin.
- Users no longer see our cost basis: the credit history leaves out provider cost, kie task id and meta; estimates
  drop `provider_cost` for non-admins.
- The resume loop always runs when the database is on (also with `AI_VIDEO` off).
- Post-processing runs once: its result is stored before settling and reused after a crash; raw kie downloads are
  removed only after settling.
- The `token=` of `/webhooks/kie` is masked in the uvicorn access log.
- Small: a spend on an account without a row is `InsufficientCredits` again; the free monthly top-up is one
  transaction; `/api/ai/estimate` is behind the AI video switch; a finalize estimate needs your own draft.
- Tests: `tests/test_ai_review_fixes.py`, `tests/test_credits_review_fixes.py` (19 tests, all failing on bf29859).
