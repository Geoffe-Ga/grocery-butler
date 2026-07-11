# Manual End-to-End Checklist — Pre-Ship Validation (Issue #32)

This is a **manual** run-through of every user-facing surface (CLI, web,
`/api/v1`) before shipping a build for real family use. It is not a
substitute for the automated suite (`./scripts/test.sh`) — it exists to
catch the things automated tests can't: does the dashboard actually look
right on a phone, does a real Safeway order actually place, does RubotPaul's
staged-confirmation flow actually protect against double-ordering.

Every command below is checked against the real source of truth:
`grocery_butler/cli.py` (`_build_parser`) for CLI subcommands/flags and
`grocery_butler/api.py` (`api_v1` blueprint) for `/api/v1` routes — not the
README, which has drifted from the implementation before (`stock show`,
`order review`, `recipes list`, `pantry list` are not real invocations; see
`tests/test_docs_checklist.py`, the guard that keeps this file honest).

## Legend

- **PASS** / **FAIL** — the runner records one or the other for every step.
  Never silently skip a step because it's inconvenient or slow.
- **BLOCKED-BY #NN** — this step cannot fully pass until GitHub issue #NN is
  closed. Run the step anyway, observe the actual (broken) behavior, and
  record **FAIL (BLOCKED-BY #NN)** — never mark it PASS, and never skip it.
  The point of running a blocked step is to confirm the failure mode hasn't
  gotten worse, not to pretend it passed.
- Anything not explicitly tagged BLOCKED-BY is expected to fully pass today.

---

## 0. Pre-flight

1. Python 3.11+ (`python3 --version`).
2. `pip install -r requirements.txt` (runtime only) or
   `pip install -r requirements-dev.txt` (contributors running the full
   quality suite).
3. `cp .env.example .env` and fill in real values. Environment variables
   actually read by the code (verified against `grocery_butler/config.py`,
   `grocery_butler/app.py`, and `grocery_butler/auth_middleware.py`):

   | Variable | Read by | Notes |
   |---|---|---|
   | `ANTHROPIC_API_KEY` | `config.load_config` | Required for real (non-stub) meal parsing. CLI: `load_config()` fails fast — `plan` / `order --meals` refuse to run. API: does **not** fail fast — `/api/v1/meals/parse` and `/api/v1/shopping-list/preview` silently fall back to stub parsing / simple consolidation (`meal_parser.py:296`, `consolidator.py:242`), so a missing key in prod degrades RubotPaul output quality with no visible failure. (Related to the silent-degradation concerns tracked in #64.) |
   | `RUBOTPAUL_SHARED_SECRET` | `auth_middleware._shared_secret` | HMAC secret for every `/api/v1` bearer token. Missing -> the process raises `RuntimeError` at first authenticated request, not at boot. |
   | `FLASK_SECRET_KEY` | `app.py:55` | **BLOCKED-BY #64**: `create_app` falls back to `os.urandom(32).hex()` per worker when unset, so sessions silently break across multi-worker/gunicorn restarts instead of failing fast. Always set this explicitly in prod. |
   | `DATABASE_PATH` | `config.load_config` | Dev SQLite file path (default `mealbot.db`). |
   | `DATABASE_URL` | `config.load_config` | Prod Postgres URL. **BLOCKED-BY #57**: `create_app(db_path=...)` never reads `DATABASE_URL` — see step 0.5. |
   | `SAFEWAY_USERNAME` / `SAFEWAY_PASSWORD` / `SAFEWAY_STORE_ID` | `config.load_config` | Required only for the Safeway ordering pipeline. |
   | `DEFAULT_SERVINGS` | `config.load_config` | Meal-planning default (falls back to `4`). |
   | `DEFAULT_UNITS` | `config.load_config` | `imperial` or `metric`. |
   | `FLASK_PORT` | `config.load_config` | Local dev server port. |
   | `FLASK_DEBUG` | `config.load_config` | Never `true` in production. |

   **BLOCKED-BY #64**: none of the above are fail-fast validated at process
   start for the web/prod path — a misconfigured deploy can boot green and
   fail on the first real request. Treat "the process started" as
   insufficient evidence of readiness.

4. Initialize and inspect the local database:

   ```bash
   python -m grocery_butler stock
   sqlite3 mealbot.db ".tables"
   sqlite3 mealbot.db "SELECT ingredient, display_name, category FROM pantry_staples;"
   ```

   A fresh database has an *empty* `pantry_staples` table — that's expected,
   not a bug. Before shipping to the family, populate real staples (see
   section 6) and confirm this query returns them.

5. **Production persistence — KNOWN-BLOCKED FAIL until #57/#58 land.**
   Do not test "data persists across a Railway redeploy" and mark it PASS.
   - **BLOCKED-BY #57**: `create_app()` takes a `db_path` argument and never
     reads `DATABASE_URL`, so on Railway the app writes to an ephemeral
     local SQLite file rather than the provisioned Postgres database.
   - **BLOCKED-BY #58**: even once #57 is fixed, migrations
     (`python -m grocery_butler.db.migrate`) may not run against the
     Postgres target on every Railway deploy.
   - Record this row as **FAIL (BLOCKED-BY #57 #58)** on every pre-ship run
     until both issues close — do not skip it, do not soften it to PASS.

---

## 1. Meal Planning

### CLI

```bash
python -m grocery_butler plan "chicken stir fry, tacos"
python -m grocery_butler plan "chicken stir fry, tacos" --servings 8
python -m grocery_butler plan "chicken stir fry, tacos" --save
```

- `meals` is a single positional argument — a comma-separated string, not
  repeated positional args. Confirm the printed shopping list is grouped by
  aisle category and (with `--save`) that new recipes are persisted
  (`python -m grocery_butler recipes` should list them afterward).
- Empty-input error case:

  ```bash
  python -m grocery_butler plan ""
  ```

  Expect `Error: No meals specified.` on stderr and a non-zero exit code.

### Web

- Visit `/shopping-list`. Submit meal names (newline-delimited textarea).
  Confirm the generated list is grouped by category and persists across a
  page reload within the same session (stored server-side in the Flask
  session, not the database).

### API (`/api/v1`)

```bash
export TOKEN=$(python -c "from grocery_butler.auth_middleware import mint_token; print(mint_token('rubotpaul'))")

curl -s -X POST "$BASE_URL/api/v1/meals/parse" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "chicken stir fry, tacos"}'

curl -s -X POST "$BASE_URL/api/v1/shopping-list/preview" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"meals": [], "include_restock": true}'
```

Confirm both are read-only (no rows written) and that `shopping-list/preview`
folds in the current restock queue when `include_restock` is true.

---

## 2. Inventory

### CLI

```bash
python -m grocery_butler stock
python -m grocery_butler stock add "milk" dairy
python -m grocery_butler stock out "milk"
python -m grocery_butler stock low "eggs" --quantity 3 --unit count
python -m grocery_butler stock good "milk"
python -m grocery_butler restock
```

- Bare `stock` (no action) lists inventory — it is **not** `stock show`.
- `stock` action is a positional choice of `out|low|good|add`; `add` takes
  `item_name` then `category` as positionals (`_add_stock_parser` in
  `cli.py`). `--quantity`/`--unit` are optional flags valid on any
  status-change action.
- Bare `restock` (no action) lists the restock queue; `restock clear` clears
  it. There is no `restock show`.

### Web

- `/inventory`: toggle status inline (AJAX to `/inventory/update`), add a
  new item via the form (`/inventory/add`).
- `/` (dashboard): confirm the restock-queue count reflects the CLI changes
  above without restarting the process (same SQLite file, WAL mode).

### API

```bash
curl -s "$BASE_URL/api/v1/inventory" -H "Authorization: Bearer $TOKEN"

curl -s -X POST "$BASE_URL/api/v1/stock/update" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"item": "milk", "status": "out"}'

curl -s -X POST "$BASE_URL/api/v1/stock/add" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"item": "yogurt", "category": "dairy"}'

curl -s "$BASE_URL/api/v1/restock" -H "Authorization: Bearer $TOKEN"

curl -s -X POST "$BASE_URL/api/v1/restock/clear" \
  -H "Authorization: Bearer $TOKEN"
```

`stock/update` and `stock/add` write **immediately** — no staging, no
`pending_actions` row. Confirm a write made via the API shows up in
`python -m grocery_butler stock` and on `/inventory` right after.

---

## 3. RubotPaul Chat-Side Ordering via `/api/v1`

This section replaces the old Discord `/order` slash commands, which are
retired from production (see section 8). RubotPaul is the household's chat
interface and drives ordering entirely through `/api/v1`.

Mint a bearer token exactly as the real function signature requires
(`mint_token(caller_id: str, *, now: float | None = None) -> str` in
`grocery_butler/auth_middleware.py`):

```bash
export TOKEN=$(python -c "from grocery_butler.auth_middleware import mint_token; print(mint_token('rubotpaul'))")
```

Never hardcode `$RUBOTPAUL_SHARED_SECRET` or a literal token in a script or
chat log — always interpolate from the environment.

**(a) Preview — server computes the cart and total:**

```bash
curl -s -X POST "$BASE_URL/api/v1/order/preview" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "shopping_list": [
      {"ingredient": "milk", "quantity": 1, "unit": "gal",
       "category": "dairy", "search_term": "milk", "from_meals": ["manual"]}
    ]
  }' | tee /tmp/preview.json
```

Confirm the response has `cart` (a full `CartSummary`) and a server-computed
`total`. Capture both — the next step reuses the exact `cart` object.

**(b) Submit — stage, don't execute:**

```bash
CART=$(python -c "import json; print(json.dumps(json.load(open('/tmp/preview.json'))['cart']))")

curl -s -X POST "$BASE_URL/api/v1/order/submit" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"cart\": $CART}" | tee /tmp/submit.json
```

Confirm the response is `status: pending_confirmation` with a fresh
`action_id` and a human-readable `message` ("Reply 'confirm' to submit.").
Nothing has been ordered yet.

**(c) Total integrity check — BLOCKED-BY #73:**

Compare the `total` reported in step (a)'s preview response against the
dollar amount embedded in step (b)'s submit `message`. They should be
identical, because the submit endpoint is documented to recompute the total
server-side from the cart. **BLOCKED-BY #73**: today, `post_order_submit`
accepts an optional client-supplied `total` in the request body and only
falls back to server computation if the client omits it — a modified client
could currently submit a cart with a falsified `total`. Record this row as
**FAIL (BLOCKED-BY #73)** and re-test once the endpoint is changed to always
recompute (ignoring any client-supplied `total`).

**(d) Deny — nothing gets ordered:**

```bash
ACTION_ID=$(python -c "import json; print(json.load(open('/tmp/submit.json'))['action_id'])")

curl -s -X POST "$BASE_URL/api/v1/actions/deny" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"action_id\": \"$ACTION_ID\"}"
```

Confirm `status: denied` and that no Safeway order was placed.

**(e) Confirm — real submission. BLOCKED-BY #59 #60:**

Stage a *fresh* order (repeat (a)/(b) — a denied `action_id` cannot be
re-confirmed) and only then:

```bash
curl -s -X POST "$BASE_URL/api/v1/actions/confirm" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"action_id\": \"$ACTION_ID\"}"
```

**BLOCKED-BY #59** (unit-blind quantities can silently over-order — inspect
every line item's quantity before allowing this to run against a real
account) and **BLOCKED-BY #60** (the live Safeway submission path is
unverified end-to-end and this step spends real money). Do not run this
step against a real Safeway account until both issues close; if run
anyway for testing, immediately cancel the resulting order in the Safeway
app and record **FAIL (BLOCKED-BY #59 #60)** regardless of outcome.

**(f) Audit trail:**

```bash
sqlite3 mealbot.db "SELECT action_id, kind, status, requester, created_at, resolved_at FROM pending_actions ORDER BY created_at DESC LIMIT 10;"
```

Confirm one row per staged action above, `kind = 'safeway_order_submit'`,
and status transitions `pending -> approved` (confirm) or
`pending -> denied` (deny) — never both from the same row (the store's
guarded `UPDATE ... WHERE status = 'pending'` makes a double-resolve
impossible; a raced second confirm/deny attempt should get HTTP 409).

**(g) Expiry:**

Staged actions expire after `CONFIRMATION_TTL` (5 minutes). Stage an order,
wait 5+ minutes, then call `/api/v1/actions/confirm` with that `action_id`.
Expect HTTP 410 and the `pending_actions` row's `status` to read `expired`
(verify with the same `sqlite3` query as (f)).

---

## 4. Safeway Ordering — CLI (Dry-Run First)

**Always dry-run before a real submission.** The `--items` format is a
plain comma-separated list of ingredient names — *not* the
`name:qty:unit` triples the older README described (verified against
`claude_utils.items_from_string`, which defaults every parsed item to
`quantity=1.0`, `unit=each`, `category=other`):

```bash
python -m grocery_butler order --items "milk, eggs, bread" --dry-run
python -m grocery_butler order --meals "tacos" --dry-run
```

Review the printed cart summary for:

- **Product-selection quality**: are the matched Safeway products actually
  the right products (not a wildly different size/brand)?
- **Quantity sanity — BLOCKED-BY #59**: `--items` quantities default to `1
  each` regardless of what a sane real-world quantity would be (e.g. "milk"
  defaults to 1 *each*, not 1 gallon). Combined with meal-based scaling,
  unit-blind quantity math can over-order by 100x. Manually inspect every
  line item's quantity and unit before ever running the real-submission
  form below. Record this row as **FAIL (BLOCKED-BY #59)** until quantity
  validation lands, even if the individual dry-run cart happens to look
  correct.

### Error cases

```bash
python -m grocery_butler order --items "this-item-does-not-exist-anywhere" --dry-run
python -m grocery_butler order --meals "tacos"          # (no --dry-run, no SAFEWAY_* env set)
python -m grocery_butler order                           # neither --items nor --meals
```

Expect, respectively: a failed-item entry in the cart summary rather than a
crash; a clear configuration error surfaced via `SafewayPipelineError`
when `SAFEWAY_USERNAME`/`SAFEWAY_PASSWORD`/`SAFEWAY_STORE_ID` are unset; and
`Error: Provide --items or --meals.` on stderr with a non-zero exit code.

### REAL SUBMISSION — BLOCKED-BY #59 #60 — REAL MONEY

> **Do not run this against a real Safeway account outside of a deliberate,
> supervised test.** This places an actual order and charges an actual
> payment method on file.

```bash
python -m grocery_butler order --items "milk, eggs, bread"
```

(no `--dry-run`). **BLOCKED-BY #59** (quantity sanity — see above) and
**BLOCKED-BY #60** (the live submission path has not been verified
end-to-end; treat any real order placed this way as needing manual
confirmation in the Safeway app). If you do run this for supervised
testing, immediately open the Safeway app and **cancel the order** rather
than letting it fulfill, and record **FAIL (BLOCKED-BY #59 #60)**.

The `/api/v1/actions/confirm` path (section 3(e)) is the production
real-submission path RubotPaul actually uses; this CLI form exists for local
testing of the same underlying `SafewayPipeline.run`.

---

## 5. Recipes

### CLI

```bash
python -m grocery_butler recipes
python -m grocery_butler recipes show "name"
python -m grocery_butler recipes forget "name"
```

Bare `recipes` (no action) lists saved recipes — there is no `recipes
list`. `show`/`forget` are positional action choices taking a
`recipe_name` positional.

### Web

- `/recipes`: list with ingredient counts; `/recipes/add` create form;
  `/recipes/<id>` detail page; delete via the detail page's delete button.

### API

```bash
curl -s "$BASE_URL/api/v1/recipes" -H "Authorization: Bearer $TOKEN"

curl -s -X POST "$BASE_URL/api/v1/recipes/save" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "Taco Night",
    "servings": 4,
    "ingredients": [
      {"ingredient": "ground beef", "quantity": 1, "unit": "lb",
       "category": "meat", "is_pantry_item": false}
    ]
  }'

curl -s -X DELETE "$BASE_URL/api/v1/recipes/<id>" -H "Authorization: Bearer $TOKEN"
```

`recipes/save` writes immediately (409 if the name already exists) and
`DELETE /recipes/<id>` writes immediately — neither is staged through
`pending_actions`; recipe management is a low-stakes write, unlike Safeway
orders and brand/preference changes.

---

## 6. Pantry Staples

### CLI

```bash
python -m grocery_butler pantry
python -m grocery_butler pantry add "cumin" pantry_dry
python -m grocery_butler pantry remove "cumin"
```

Bare `pantry` (no action) lists staples — there is no `pantry list`. `add`
takes `ingredient_name` then `category` as positionals; the category is a
plain string validated against `IngredientCategory` (`produce`, `meat`,
`dairy`, `bakery`, `pantry_dry`, `frozen`, `beverages`, `deli`, `other`) —
`pantry_dry` (not `pantry`/`dry_goods`) is the pantry-and-dry-goods value.

### Web

- `/pantry`: list, add (`/pantry/add`), remove (`/pantry/<id>/remove`).

### API

```bash
curl -s "$BASE_URL/api/v1/pantry" -H "Authorization: Bearer $TOKEN"
```

Read-only; there is no staged or immediate pantry-staple write endpoint
under `/api/v1` today — pantry staples are managed from the CLI or web only.

---

## 7. Brand Preferences via `/api/v1`

This replaces the retired Discord `/brands` slash commands.

```bash
curl -s "$BASE_URL/api/v1/brands" -H "Authorization: Bearer $TOKEN"

curl -s -X POST "$BASE_URL/api/v1/brands/set" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "match_target": "milk", "match_type": "ingredient",
    "brand": "Organic Valley", "preference_type": "preferred"
  }'
```

`brands/set` **stages** the rule (`status: pending_confirmation`, an
`action_id`) rather than writing it immediately. Confirm it:

```bash
curl -s -X POST "$BASE_URL/api/v1/actions/confirm" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"action_id\": \"$ACTION_ID\"}"
```

Then verify the `pending_actions` row shows `kind = 'brands_set'` and
`status = 'approved'` (same query as section 3(f)), and that
`GET /api/v1/brands` now includes the new rule.

Also exercise app-level preferences, which follow the identical
staged-confirmation shape:

```bash
curl -s -X POST "$BASE_URL/api/v1/preferences/set" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"preferences": {"default_servings": "6"}}'

curl -s "$BASE_URL/api/v1/preferences" -H "Authorization: Bearer $TOKEN"
```

### Web

- `/brands`: list grouped by preferred/avoid, add/remove forms. Note the
  web UI writes brand preferences **immediately** (no staging) — the
  staged-confirmation requirement is specific to the `/api/v1` surface
  RubotPaul uses, not to every channel.

---

## 8. RubotPaul Integration End-to-End

Confirm the full staged-action lifecycle at least once per family-facing
category (Safeway order, brand rule, preference change), each ending a
different way:

1. **draft -> confirm**: stage, confirm, verify `pending_actions.status =
   'approved'` and the underlying data changed (inventory/brand/preference/
   order as appropriate).
2. **draft -> deny**: stage, deny, verify `pending_actions.status =
   'denied'` and nothing changed.
3. **draft -> expire**: stage, wait out `CONFIRMATION_TTL` (5 minutes),
   attempt confirm, verify HTTP 410 and `pending_actions.status =
   'expired'`.

The standalone Discord bot (`python -m grocery_butler bot`) is legacy and
local-only — it still runs if you start it manually with
`DISCORD_BOT_TOKEN` set, but it is **not** part of the production ship
gate. RubotPaul, not the bot, is the household's production Discord
interface, and it talks exclusively through `/api/v1`.

---

## 9. Cross-Component Integration & Edge Cases

- **CLI write visible in web + API read**: `python -m grocery_butler stock
  out "milk"`, then confirm `/inventory` (web) and
  `GET /api/v1/inventory` (API) both reflect `out` without restarting any
  process (shared SQLite file, WAL mode for concurrent access).
- **Large meal list**: `python -m grocery_butler plan "<15+ comma-separated
  meals>"` — confirm it completes without timing out and the shopping list
  is still correctly grouped/deduplicated.
- **Duplicate-ingredient consolidation**: plan two meals that both use
  "garlic" — confirm the consolidated shopping list has one `garlic` line
  with a summed quantity, not two lines.
- **Unicode meal names**: `python -m grocery_butler plan "pâté, jalapeño
  poppers, 麻婆豆腐"` — confirm no crash and reasonable parsing.
- **Concurrent web + API access**: hit `/inventory` (web) and
  `GET /api/v1/inventory` (API) at the same time from two terminals/tabs
  while a third makes a write — confirm no SQLite "database is locked"
  errors (WAL mode should prevent this) and no lost writes.

---

## 10. Ship-Readiness

### Open blockers gating ship

The runner **cannot** declare ship-ready while any row below is open. Check
each issue's state before signing off; treat "the checklist step passed
today" as informational, not sufficient, if the issue is still open.

| Issue | Title | Must be CLOSED before ship |
|---|---|---|
| #57 | `create_app()` ignores `DATABASE_URL` — Railway writes ephemeral SQLite instead of Postgres | Yes |
| #58 | Migrations may not run on Railway on every deploy | Yes |
| #59 | Unit-blind order quantities can over-order 100x with no sanity check | Yes |
| #60 | Live Safeway order submission is unverified end-to-end (real money) | Yes |
| #64 | No fail-fast validation of required prod config (e.g. `FLASK_SECRET_KEY`) | Yes |
| #73 | `/api/v1/order/submit` trusts an optional client-supplied `total` instead of always recomputing server-side | Yes |

### Family-facing readiness (beyond the blocker table)

- [ ] Real credentials are in `.env`/Railway env vars — no placeholder
      values (`ANTHROPIC_API_KEY`, `RUBOTPAUL_SHARED_SECRET`,
      `SAFEWAY_USERNAME`/`SAFEWAY_PASSWORD`/`SAFEWAY_STORE_ID`,
      `FLASK_SECRET_KEY`).
- [ ] Pantry staples reflect what this household actually always has on
      hand (section 6) — not an empty or placeholder list.
- [ ] Brand preferences reflect real household preferences (section 7).
- [ ] A real payment method is on file with the Safeway account used by
      `SAFEWAY_USERNAME`.
- [ ] `SAFEWAY_STORE_ID` is the correct physical store for this household.
- [ ] Every household member who can trigger RubotPaul ordering understands
      that replying "confirm" to a staged order (section 3) places a real
      order and spends real money — this is not a simulation once #59/#60
      close and the confirm path is unblocked.
