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
