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
