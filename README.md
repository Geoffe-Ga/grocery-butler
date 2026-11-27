# grocery-butler

> The household's replenishment memory: what we keep, what we probably have,
> what we need, and why. Operated by OpenClaw, controlled by people.

## Mission

Households run on a quiet, unrecognized layer of work: someone notices the
peanut butter is low, mentally cross-references it against tomorrow's lunches,
remembers that one kid won't eat the crunchy kind, checks last week's receipt
to avoid double-buying, and threads it all into a coherent shopping trip. That
labor is largely **invisible**. It doesn't show up on a calendar, doesn't get
billed, and doesn't get thanked. When the person doing it is unavailable, sick,
or finally tired of doing it, the household feels the absence immediately.

**Grocery Butler** is the household's durable system of record for that work.
It helps a household answer four questions reliably:

1. What do we usually keep?
2. What do we probably have right now, and how certain are we?
3. What needs attention or replenishment?
4. How can a human approve and complete that replenishment through an
   authorized retailer experience?

It does **not** promise perfect perpetual inventory or autonomous purchasing.
Household inventory is noisy: packages are partly full, items move, receipts
differ from orders, and people forget to report what they used. The useful
product is a low-friction replenishment memory that makes uncertainty visible
and takes the invisible labor off one person's shoulders, so that:

- The mental load can be **handed off** without losing context.
- Multiple household members can **contribute** ("we're out of milk") through
  OpenClaw, the web dashboard, or the terminal.
- Decisions made once (preferred brands, default servings, dietary
  restrictions) **persist** instead of being rediscovered every week.
- A human still decides **what gets purchased**, every time, in the
  retailer's own checkout.

The product direction, epics, and delivery sequence live in
[`docs/OPENCLAW_ERP_BACKLOG.md`](docs/OPENCLAW_ERP_BACKLOG.md). Architecture
decisions are recorded in [`docs/adr/`](docs/adr/README.md).

The next step on that sequence is a four-week dogfood in one household. How
it runs and what it records are in
[`docs/operations/dogfood-protocol.md`](docs/operations/dogfood-protocol.md).

## Table of Contents

- [What It Does](#what-it-does)
- [Architecture](#architecture)
- [Retailer Integration Status](#retailer-integration-status)
- [Code Choices](#code-choices)
- [Installation](#installation)
- [Running the System](#running-the-system)
- [API Documentation](#api-documentation)
  - [HTTP / Web API](#http--web-api)
  - [OpenClaw Integration (/api/v1)](#openclaw-integration-apiv1)
  - [OpenClaw Capability API (/api/v2)](#openclaw-capability-api-apiv2)
  - [Discord Bot Commands](#discord-bot-commands)
  - [Command-Line Interface](#command-line-interface)
  - [Python Module API](#python-module-api)
- [Data Model](#data-model)
- [Development](#development)
- [Deploying](#deploying)
- [License](#license)

## What It Does

Grocery Butler runs the household side of the provisioning loop:

1. **Capture recipes.** Describe a meal in natural language; Claude parses it
   into structured ingredients with quantities, units, and categories.
2. **Track inventory.** The household marks items as `on_hand`, `low`, or
   `out` from any channel. Items go onto the **restock queue** automatically.
3. **Plan a week.** Given a list of meal names, the system pulls saved recipes
   (or parses new ones), scales them to your default servings, and emits a
   consolidated, retailer-neutral shopping list grouped by store aisle.
4. **Respect what's already true.** Pantry staples are excluded by default,
   restock items are added unconditionally, and brand preferences and dietary
   rules are applied at consolidation time.
5. **Hand off, don't check out.** The shopping list is the end of Grocery
   Butler's job today. You (or OpenClaw, in your own logged-in session) take
   it to any store; the retailer's own surface owns the cart, payment, and
   checkout. See [Retailer Integration Status](#retailer-integration-status).

The target workflow the backlog builds toward adds photo-based inventory
capture through OpenClaw, an append-only observation ledger with confidence
and provenance, explicit replenishment needs, and reconciliation of what
actually arrived. See
[Target workflow](docs/OPENCLAW_ERP_BACKLOG.md#target-workflow).

## Architecture

Grocery Butler is a **small monolith with one source of truth**. A single
shared database and domain layer sit behind thin channel adapters. OpenClaw
(today through RubotPaul, the household's OpenClaw assistant) is the primary
operator; the web dashboard and CLI cover direct use.

```
   +-------------------+        +-----------------------+
   |     OpenClaw      |        |     Anthropic API     |
   |   (RubotPaul):    |        |  (Claude: parsing and |
   |  chat, photos,    |        |    consolidation)     |
   |  approvals        |        +-----------+-----------+
   +---------+---------+                    |
             | /api/v1 (HMAC, tailnet)      |
             v                              v
+-----------------+   +---------+   +-------------------+
|  Flask web +    |   |   CLI   |   |  Domain services  |
|  /api/v1        |-->| (cli.py)|-->|  - MealParser     |
|  (app.py,       |   +---------+   |  - Consolidator   |
|   api.py)       |---------------->|  - PantryManager  |
+-----------------+                 |  - RecipeStore    |
                                    |  - PendingActions |
                                    +---------+---------+
                                              |
                                              v
                                       +-------------+
                                       |  db/adapter |  SQLite (dev) /
                                       +-------------+  Postgres (prod)

   Retailer checkout: outside the system. The household (or OpenClaw in the
   user's own session) completes it on the retailer's authorized surface.
```

### Ownership boundary

| Grocery Butler owns | OpenClaw owns | The retailer owns |
|---------------------|---------------|-------------------|
| Canonical household items and staple policies | Conversation and multimodal input | Catalog, prices, promotions, availability |
| Inventory state, confidence, and provenance | Turning photos, voice, and chat into *proposed* observations | Authentication and account security |
| Preferences, brands, dietary rules, substitutions | Targeted clarification when confidence is low | Cart, fulfillment slots, payment, checkout |
| Replenishment needs and their reasons | Orchestrating review and approval | Final order and fulfillment truth |
| Retailer-neutral shopping plans | Operating an authorized retailer surface when permitted | |
| Drafts, approvals, audit history, reconciliation | Reporting results back for reconciliation | |

### Layers

**Adapters (channels).** `api.py` (the `/api/v1` blueprint OpenClaw calls),
`app.py` (Flask HTML dashboard), and `cli.py` (argparse). `bot.py` (Discord)
is retired from production. Adapters parse channel-specific input, call into
the domain services, and format output. No business rules live here.

**Domain services**, the heart of the system:

| Module | Responsibility |
|--------|---------------|
| `meal_parser.py` | Turn a meal name (or saved recipe) into a `ParsedMeal` of structured ingredients via Claude. |
| `recipe_store.py` | Persistence for recipes, pantry staples, brand preferences, and user preferences. |
| `pantry_manager.py` | Household inventory CRUD, restock queue, and natural-language inventory updates. |
| `consolidator.py` | Merge ingredients across multiple meals into a single deduplicated shopping list, applying pantry staples and restock overrides. |
| `pending_actions.py` | Draft → confirm → execute audit trail for staged, human-confirmed changes. |

**Fulfillment adapters** come next (backlog P1.11, P1.12): a retailer-neutral
interface with per-adapter declared capabilities (`export_list`,
`open_search`, `draft_cart`, `deep_link_cart`, `read_cart`, `read_order`,
`import_receipt`), starting with a plain list export that works with any
store. Core logic never imports a retailer-specific model.

**Persistence.** `db/adapter.py` provides a thin abstraction over `sqlite3`
and `psycopg2` so the same code runs locally on SQLite (with WAL mode for
concurrent readers and writers) and in production on Postgres. Schema
changes are numbered SQL migrations in `db/migrations/`, applied by
`db/migrate.py`.

### Process Topology

In production the system builds and runs from the `Dockerfile`, on the
household's own server beside Postgres
([ADR 0021](docs/adr/0021-self-host-the-dogfood-on-the-openclaw-server.md)).
The image
boots into `docker-entrypoint.sh`, which applies any pending schema
migrations (`python -m grocery_butler.db.migrate`, reading `DATABASE_URL`)
before anything else starts. If a migration fails, `set -eu` aborts the boot
immediately, so gunicorn never starts and no request is ever served against
an unmigrated schema. Once migrations succeed, the entrypoint `exec`s
straight into gunicorn (`grocery_butler.app:create_app()`), which becomes
the container's only long-lived process, serving both the kitchen-phone HTML
UI and the HMAC-authenticated `/api/v1` blueprint.

**Who migrates.** Only process entry points run the migration runner: the
container entrypoint, `create_app()`, each CLI command, and the local Discord
bot. Stores and managers never do; they assume a migrated schema. The runner
serializes itself across processes, with a session advisory lock on Postgres
and a `flock` on `<database>.migratelock` beside a SQLite file, so gunicorn
workers that boot together apply each migration exactly once. A runner that
waited for the lock re-reads what has been applied and finds nothing to do.

**If a migration fails.** The boot stops and the previous deploy keeps
serving. Migrations are forward-only: there is no downgrade. Each one is
recorded in `schema_migrations` only after its SQL and any Python hook
succeed, so a failed migration is retried from its start on the next boot.
To recover, fix the migration and redeploy, or restore the database from a
backup taken before the deploy and redeploy the previous commit. The restore
steps are in the [runbook](docs/operations/data-lifecycle.md). Never edit
`schema_migrations` by hand to skip a version.

There is no separate worker process in production. The Discord bot
(`python -m grocery_butler.bot`) is retired from deployment: RubotPaul is
the household's Discord interface now, and it drives grocery-butler through
`/api/v1` instead. The bot module stays in the codebase and can still be run
locally (see [Discord bot (local only)](#discord-bot-local-only--retired-from-production)).
The CLI is invoked ad-hoc as a single-shot process (no daemon).

Production needs **no retailer configuration**. See
[Deploying](#deploying) for the environment variables and setup steps that
go with this topology.

### Key Design Rules

The full list is in the backlog's
[Safety and product principles](docs/OPENCLAW_ERP_BACKLOG.md#safety-and-product-principles).
The ones the current code already enforces or must never break:

1. **Inventory status overrides pantry staples.** Salt is normally excluded as
   a pantry staple, but if the household marks salt as `out`, it goes onto
   the list. This rule is enforced inside `Consolidator`, not at the call site.
2. **Claude is a parser, not a planner.** The LLM extracts structure from
   natural language (recipes, inventory updates). It does not own state.
   State lives in the database.
3. **Evidence before mutation.** A photo or model inference creates a
   *proposal*; it never silently overwrites inventory.
4. **Draft → confirm → execute.** Money-moving or destructive actions always
   require explicit human confirmation against an immutable draft. Approval
   to hand off a plan is not approval to purchase; purchase approval belongs
   in the retailer's authorized surface.
5. **Retailers are adapters.** Core inventory and replenishment logic must not
   import retailer-specific models.
6. **Never claim a purchase without evidence.** A draft list, a browser click,
   or a retailer acknowledgment is not a completed order.

## Retailer Integration Status

**Grocery Butler has no retailer integration, and does not support one built
on private APIs.** The earlier Safeway integration, which talked to
Safeway's undocumented web API with stored account credentials, was never
verified against a real order and has been retired. See
[ADR 0001](docs/adr/0001-pivot-from-private-safeway-api-to-authorized-fulfillment-adapters.md)
for why.

What that means today:

- There is **no** Safeway (or other retailer) username, password, store id, or
  order-submission flag to configure. Don't add one.
- Grocery Butler **cannot** build, review, or submit a retailer cart. Its
  output is a retailer-neutral shopping plan; checkout happens in the
  retailer's own app or site, by a person.
- New fulfillment paths arrive only as authorized adapters, chosen after the
  terms review in backlog P2.2. No adapter is built from undocumented
  endpoints. The first chosen is Instacart's documented shopping list link
  ([ADR 0014](docs/adr/0014-instacart-developer-platform-as-first-handoff-adapter.md),
  [research](docs/research/fulfillment-options.md)); it is not built yet.

### Fulfillment adapters

A fulfillment adapter moves an approved plan toward a purchase the household
completes somewhere else. The interface is
`grocery_butler/fulfillment/base.py`; adapters live beside it and are found
through `grocery_butler/fulfillment/registry.py`.

- **Each adapter declares itself:** its capabilities (`export_list`,
  `open_search`, `shopping_list_link`, `draft_cart`, `deep_link_cart`,
  `read_cart`, `read_order`, `import_receipt`), how it is authorized, how
  fresh its data is, and that a person must act.
- **A handoff is never an order.** A result says a handoff was created or
  failed. The vocabulary has no word for a purchase.
- **No stored retailer credentials.** The authorization methods are none, a
  provider's developer program, or the user's own session.
- **Core modules never import an adapter.** A test walks the imports of
  every domain module to hold that line.
- **A hard constraint is enforced or reported, never dropped.** An adapter
  declares the kinds it can guarantee; the rest come back on the handoff as
  unenforced, for a person to check.

The one adapter today is the **list export** (`fulfillment/export.py`): any
plan version as a grouped checklist in text, JSON or CSV, for any store. It
contacts nothing. Get it from `GET /api/v2/plans/{version}/export`.

The **Instacart adapter** (`fulfillment/instacart/`) creates a shopping
list link through Instacart's documented developer API
([ADR 0014](docs/adr/0014-instacart-developer-platform-as-first-handoff-adapter.md)).
It is off unless `INSTACART_ENABLED=true`, and by default it runs on a stub
transport that returns a fake link on `example.invalid` and contacts nothing.
It claims one capability, `shopping_list_link`.

| Variable | Meaning |
|----------|---------|
| `INSTACART_ENABLED` | `true` registers the adapter. Unset, only the list export exists. |
| `INSTACART_ENV` | `stub` (default): a fake link, nothing contacted. `development` or `production`: Instacart's real host. Anything else refuses every call. |
| `INSTACART_API_KEY` | The deployment's key from Instacart's developer program, for the two real environments. Never a household member's login. Without it every call fails with `not_configured` and the household is pointed at the list export. |

A production key waits on the conditions in ADR 0014. Until then, use
`stub`, or `development` with a development key.

*Handoffs* (`fulfillment/handoffs.py`,
[ADR 0016](docs/adr/0016-approved-idempotent-handoff-attempts.md)). Sending
a plan outside the household takes three steps. **Draft** stores exactly
what a member will approve: the lines, every hard constraint the adapter
can't enforce, and everything its translation leaves out. **Approve** is the
member echoing that draft's hash. **Execute** claims the approval once,
calls the adapter and records a `fulfillment_attempts` row: `created` with a
link, or `failed` with a stable code. The request's idempotency key is the
attempt's key, so repeating a request never sends twice; only an attempt
that failed with a passing fault is tried again under the same key. If the
plan's needs change, the approval is stale and nothing is sent. A created
attempt is a link, not an order: it changes no need and no inventory.

*What we say about a handoff*
([ADR 0020](docs/adr/0020-handoff-wording-and-the-opened-state.md)). Every
sentence about a handoff is built in `fulfillment/wording.py`, and a test
refuses "ordered", "purchased", "on the way" and similar words in any
handoff sentence, page or API response. The dashboard's **Handoff** page
shows where the latest plan's handoff stands, what to check yourself, a
"Shop on Instacart" button and "Export list instead". The button goes
through `/handoff/attempts/{id}/open`, which records that the link was
opened and redirects to it. `/handoff/export.txt` serves the plan as a
plain list whether or not Instacart is enabled or working.

The API key is held by one module (`instacart/http_transport.py`), sent in
one header to one of two fixed hosts, and appears in no `repr`, error or
log. Tests never call Instacart: a suite-wide fixture fails any test that
would.

*Translation* (`fulfillment/instacart/translate.py`) turns each plan line
into a documented line item, and reports everything it can't carry:

| Plan | Sent to Instacart |
|------|-------------------|
| Item name | `name` (lowercased, the search term) and `display_text` |
| One amount in a supported unit | One entry in `line_item_measurements` |
| Preferred brand | `filters.brand_filters` |
| Organic | `filters.health_filters: ["ORGANIC"]` |
| Preferred size | In brackets in `display_text`; Instacart has no size field |
| A UPC the household verified | `upcs`; Instacart then ignores the filters |
| Any hard constraint | Never sent. Reported, and the handoff waits for a person |

Units sent, from Instacart's published list: teaspoon, tablespoon, cup,
fl oz, ml, liter, gallon, pint, quart, oz, lb, gram, kilogram, each, bunch,
head, can, package. A dozen is sent as 12 each. Clove, slice, bag, box, jar,
bottle, block, stick, pinch and dash have no Instacart unit: the item is
sent with no amount and an issue says so.

| Issue | Meaning |
|-------|---------|
| `unsupported_unit` | The amount's unit isn't on Instacart's list, so no amount was sent. |
| `ambiguous_mapping` | Amounts in more than one unit, a non-positive amount, or a preference with no Instacart equivalent. Nothing was guessed. |
| `hard_constraint_unrepresentable` | Instacart can't enforce this rule. The handoff is refused until a person acknowledges it. |
| `filter_dropped_by_upc` | The line was sent by UPC, so its brand or organic filter is ignored. |
| `invalid_upc` | The stored UPC isn't 12 or 14 digits, or another line already used it; the line was sent by name. |

Issues travel with the handoff result, whether or not a link was created.
The plan builder doesn't put a UPC or an organic preference on a line yet,
so those two paths are exercised only by tests today.

The live-API verification plans that used to live here (#31, #32, #60) are
superseded by the ADR.

## Code Choices

Each technology choice is deliberate. The shared theme: **boring, well-typed,
and easy to operate alone.**

### Language and runtime

- **Python 3.11+** — `StrEnum`, structural pattern matching, exception groups,
  and excellent type-checker support. Tested against 3.11, 3.12, and 3.13.
- **Strict typing everywhere.** `mypy --strict` runs in CI. Function
  signatures are required to have type hints, and `# type: ignore` requires a
  linked issue.

### Web

- **Flask 3** — small, explicit, and fits in a single container
  without ceremony. The app is constructed by a `create_app(db_path=None)`
  factory so tests can spin up isolated instances against a tmpfile
  database.
- **Jinja2 templates + Pico CSS** — server-rendered HTML, no SPA, no build
  step. The dashboard is meant to be operated from a phone in the kitchen,
  not from a workstation.
- **Gunicorn** — production WSGI server, launched by the `Dockerfile`'s
  `CMD` (after `docker-entrypoint.sh` runs migrations).

### Chat

- **discord.py 2.x** — slash commands via `app_commands.CommandTree`, grouped
  by feature (`/stock`, `/pantry`, `/brands`, `/recipes`, `/preferences`).
  Retired from production in favor of OpenClaw (RubotPaul). Permission is
  delegated to Discord's native `manage_guild` so server admins control
  access through the Integrations UI.

### LLM

- **Anthropic Claude (Sonnet)** via the official `anthropic` SDK.
  - Recipe parsing: structured extraction with JSON schemas.
  - Inventory natural language: "we ran out of milk and the bread is going
    stale" -> a list of `InventoryUpdate` records.
  - Prompt templates live in `grocery_butler/prompts/` and are loaded by
    `prompt_loader.py` so they can be edited without code changes.

### Data

- **Pydantic v2** — all domain objects (`Ingredient`, `ParsedMeal`,
  `ShoppingListItem`, `InventoryItem`, `BrandPreference`, `PendingAction`,
  ...) are Pydantic models. Field validators normalize messy
  inputs (e.g. `"lbs"` -> `Unit.LB`) at the edge so the rest of the system
  works with clean enums.
- **`StrEnum` for vocabularies** — `IngredientCategory`, `InventoryStatus`,
  `Unit`, `BrandPreferenceType`, `BrandMatchType`, `PendingActionStatus`. The
  string value matches the database value, so enum members can be compared
  directly with stored strings.
- **SQLite (dev) + Postgres (prod)** — same schema, same code, swapped via
  the `db/adapter.py` shim. SQLite uses WAL mode so concurrent readers and
  writers don't block each other.

### Quality tooling

The project enforces **maximum quality** as a baseline:

| Tool | Purpose | Threshold |
|------|---------|-----------|
| `pytest` + `pytest-cov` | Tests + coverage | >= 90% branch coverage |
| `ruff` | Lint + format (replaces black, isort, flake8) | 0 violations |
| `mypy --strict` | Static typing | 0 errors |
| `bandit` | Security lint | 0 findings |
| `pip-audit` | Dependency CVEs | 0 vulnerabilities |
| `radon` / `xenon` | Cyclomatic complexity | <= 10 per function |
| `interrogate` | Docstring coverage | >= 95% |
| `mutmut` | Mutation testing | High-value-test verification |
| `pre-commit` | Hook runner | All of the above on every commit |

All tools are invoked through `./scripts/*.sh` so local dev and CI run the
exact same commands.

## Installation

```bash
# Clone the repository
git clone <repository-url>
cd grocery-butler

# Install dependencies (runtime + dev)
pip install -r requirements-dev.txt

# Install the Chromium the browser tests drive (tests/browser, ADR 0022)
./scripts/install-browser.sh

# Install pre-commit hooks
pre-commit install

# Copy and fill in environment variables
cp .env.example .env
$EDITOR .env
```

### Required environment variables

| Variable | When required | Purpose |
|----------|---------------|---------|
| `ANTHROPIC_API_KEY` | Always | Claude API access for parsing and consolidation. Missing key logs a startup warning and degrades to stub/saved-recipe paths rather than blocking boot. |
| `APP_ENV` | Set by the Docker image | Set to `production` in the runtime image; arms fail-fast startup checks for `FLASK_SECRET_KEY` and `RUBOTPAUL_SHARED_SECRET` (see [Fail-fast startup checks](#fail-fast-startup-checks), issue #64). Leave unset for local dev. |
| `RUBOTPAUL_SHARED_SECRET` | OpenClaw API | HMAC secret for `/api/v1` bearer tokens (shared with RubotPaul, the household's OpenClaw assistant). Required at boot when `APP_ENV=production`; otherwise only enforced per-request (401). |
| `DISCORD_BOT_TOKEN` | Local bot runs only | Authenticates the (retired-from-production) Discord bot. |
| `FLASK_SECRET_KEY` | Web (production) | Stable session signing key. Required at boot when `APP_ENV=production`; in dev, an unset key falls back to a random per-process key (see [Fail-fast startup checks](#fail-fast-startup-checks)). |
| `DATABASE_PATH` | SQLite (dev) | Path to the local SQLite file. Defaults to `mealbot.db`. |
| `DATABASE_URL` | Postgres (prod) | Connection URL. The compose file sets it in production. |
| `DEFAULT_SERVINGS` | Optional | Default recipe scaling target (default `4`). |
| `DEFAULT_UNITS` | Optional | `imperial` or `metric` (default `imperial`). |

## Running the System

### Web dashboard (local)

```bash
gunicorn 'grocery_butler.app:create_app()' --bind 0.0.0.0:5000
# or for hot reload during development:
FLASK_APP='grocery_butler.app:create_app()' flask run --debug
```

Open <http://localhost:5000>. `APP_ENV` is unset here, so a missing
`FLASK_SECRET_KEY` doesn't block boot: the app falls back to a random
per-process key and logs a warning. That's fine for a single local
`flask run`/gunicorn worker, but sessions won't survive a restart or be
shared across multiple workers — see
[Fail-fast startup checks](#fail-fast-startup-checks) for the production
behavior.

### Discord bot (local only — retired from production)

```bash
python -m grocery_butler.bot
```

The bot connects with `DISCORD_BOT_TOKEN` and registers slash commands on
startup. In production this process no longer runs: RubotPaul owns the
Discord side and talks to grocery-butler through the
[OpenClaw integration API](#openclaw-integration-apiv1).

### CLI

```bash
python -m grocery_butler --help
python -m grocery_butler plan "tacos" "stir fry" "pasta"
python -m grocery_butler stock out milk
python -m grocery_butler restock show
```

## API Documentation

Grocery Butler exposes the same domain through three distinct surface areas.

### HTTP / Web API

Constructed by `grocery_butler.app.create_app(db_path: str | None = None)`.
With no argument, it resolves `DATABASE_URL`, then `DATABASE_PATH`, then
defaults to `mealbot.db` — this is how production gunicorn invokes it. All
routes are server-rendered HTML except where noted.

#### Health

| Method | Path | Returns |
|--------|------|---------|
| `GET` | `/health` | `200 {"status":"healthy","database":"connected"}` or `503` if the DB is unreachable. JSON. |
| `GET` | `/healthz` | `200 {"ok":true}`. A liveness probe with no database check. Exempt from the network guard, like `/health`. |

#### Dashboard

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Stats overview: restock queue, inventory count, recipe count. |

#### Inventory

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/inventory` | List all tracked inventory items with editable status. |
| `POST` | `/inventory/add` | Form: `ingredient`, `display_name`, `category`, `status`. |
| `POST` | `/inventory/update` | **JSON**: `{ "ingredient": "milk", "status": "out" }` -> `{"success":true,"status":"out"}`. |

#### Recipes

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/recipes` | List saved recipes with ingredient counts. |
| `GET` | `/recipes/add` | Render the add-recipe form. |
| `POST` | `/recipes/add` | Form: `name`, `servings`, repeated `ing_name_N`, `ing_qty_N`, `ing_unit_N`, `ing_category_N`, `ing_pantry_N`. A unit the system does not know is refused with the ingredient named, and nothing is saved (same units as `/api/v1/recipes/save`). A refused form comes back with a 400 and everything that was typed still in it. |
| `GET` | `/recipes/<id>` | Recipe detail page. |
| `POST` | `/recipes/<id>/delete` | Delete recipe. |

#### Shopping list

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/shopping-list` | Render the most recently generated shopping list (stored in the Flask session), grouped by category. |
| `POST` | `/shopping-list/generate` | Form: `meals` (newline-delimited meal names). Runs `MealParser` + `Consolidator` and stores the result in the session. |

#### Pantry staples

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/pantry` | List pantry staples (always-have items). |
| `POST` | `/pantry/add` | Form: `ingredient`, `category`. |
| `POST` | `/pantry/<staple_id>/remove` | Delete a staple. |

#### Brand preferences

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/brands` | List preferred and avoided brands grouped by `preference_type`. |
| `POST` | `/brands/add` | Form: `match_target`, `match_type` (`category`/`ingredient`), `brand`, `preference_type` (`preferred`/`avoid`), `notes`. |
| `POST` | `/brands/<pref_id>/remove` | Delete a preference. |

#### User preferences

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/preferences` | Render the preferences form. |
| `POST` | `/preferences` | Form: `default_servings`, `default_units`, `dietary_restrictions`. |

#### Handoff

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/handoff` | The current plan's handoff: what was sent, what to check, and the link if there is one. |
| `GET` | `/handoff/attempts/<id>/open` | Record that the link was followed, then redirect to it. |
| `GET` | `/handoff/export.txt` | Download the current plan as a plain list for any store. |

### OpenClaw Integration (/api/v1)

RubotPaul (the household's OpenClaw assistant) drives grocery-butler through a
JSON API served by the same web process as the HTML UI — the web UI is
unchanged. The blueprint lives in `grocery_butler/api.py` and is registered by
`create_app()`.

- **Base URL:** `http://127.0.0.1:8000/api/v1` — OpenClaw runs on the same
  server and calls the loopback port
  ([ADR 0021](docs/adr/0021-self-host-the-dogfood-on-the-openclaw-server.md)).
  The boundary is enforced in code, not just by network configuration: see
  [Network boundary guard](#network-boundary-guard-tailnet-only) for how
  requests from outside the allow-list are rejected.
- **Auth:** every `/api/v1` route requires a bearer token in the
  `<caller_id>.<timestamp>.<hmac_hex>` format, signed with
  `RUBOTPAUL_SHARED_SECRET` (HMAC-SHA256, 5-minute TTL). Mint with
  `grocery_butler.auth_middleware.mint_token`. An unauthenticated
  `GET /healthz` liveness probe is exempt.
- **Surface:** read endpoints (`/inventory`, `/pantry`, `/recipes`, `/brands`,
  `/preferences`, `/restock`, `/actions`, `/actions/<action_id>`), compute
  previews (`/meals/parse`, `/shopping-list/preview`),
  low-stakes writes (`/stock/update`, `/stock/add`, `/restock/clear`,
  `/recipes/save`, `DELETE /recipes/<id>`), and staged destructive actions
  (`/brands/set`, `/preferences/set` → `/actions/confirm` /
  `/actions/deny` against the `pending_actions` audit table). `GET /actions`
  lists staged/resolved actions (`?limit=`, default 50, cap 200; `?status=`
  filter), sweeping past-due pending rows to `expired` first -- the
  staging routes (`/brands/set`, `/preferences/set`) sweep
  on write too, so a row never sits `pending` past its TTL just because the
  audit log went unread; `GET /actions/<action_id>` returns one action or a
  JSON 404.
- **Recipe units:** `/recipes/save` validates each ingredient's `unit`
  strictly. It accepts a canonical unit (`tsp`, `tbsp`, `cup`, `fl_oz`, `ml`,
  `l`, `gal`, `pint`, `quart`, `oz`, `lb`, `g`, `kg`, `each`, `dozen`,
  `bunch`, `head`, `clove`, `slice`, `can`, `bag`, `box`, `jar`, `bottle`,
  `package`, `block`, `stick`, `pinch`, `dash`, `to_taste`) or a common alias
  such as `cups`, `gallon` or `pounds`, ignoring case; a blank unit means
  `each`. Any other unit is a 400 that names the ingredient by position
  (`invalid ingredient 2: ...`) and saves nothing. An ingredient holds one
  measurement only, a quantity and a unit: send `19 oz` or `1 can`, never a
  compound `1 × 19 oz can`. A package count and a package size cannot both
  be stored today.
- **Safety:** destructive actions follow draft → confirm → execute. Staging
  returns a `pending_confirmation` action id with a 5-minute expiry; nothing
  executes until RubotPaul confirms after Geoff replies "confirm" in chat.
  Resolved actions record a terminal status of `approved`, `denied`,
  `expired`, or `failed` (a claimed action whose execution failed). Rows
  from the retired Safeway order flow remain in the audit table as history.

### OpenClaw skill

`openclaw/grocery-butler/` is what OpenClaw reads to operate Grocery Butler:
`SKILL.md`, a playbook of what to do, in what order and what to say, and
`gb.py`, a standard-library script that signs each request. OpenClaw picks
the skill up from this checkout, so `git pull` on the server updates it.
`tests/test_openclaw_skill.py` holds the playbook to `docs/api/v2.md` (every
capability, no phantom ones), runs the script against the real app, and
holds its tokens to the published vectors.

### OpenClaw Capability API (/api/v2)

The successor to `/api/v1`, served beside it from `grocery_butler/api_v2.py`.
The contract is [`docs/api/v2.md`](docs/api/v2.md); the reasons are in
[ADR 0009](docs/adr/0009-capability-api-v2-contract.md). In short:

- **Same token, same tailnet guard** as `/api/v1`.
- **Every change names a household member** in `X-Household-Member`
  ([ADR 0002](docs/adr/0002-single-household-pilot-with-named-members.md)).
  The audit trail records `member@channel`, for example `geoff@rubotpaul`.
- **Every change carries an `Idempotency-Key`.** A retry returns the first
  response and changes nothing.
- **Every error has a stable code**:
  `{"error": {"code", "message", "details"}}`.
- **A model's inference is a proposal.** What OpenClaw reads off a photo or
  parses with any doubt goes in as an observation batch. Nothing is recorded
  until a member confirms, corrects or rejects each proposal, and a review is
  applied all at once or not at all.
- **Live today:** household members; household items with their aliases,
  merges, policies, current state and history; observations a member states
  directly; observation batches; replenishment needs; shopping plans; handoffs
  to an outside service behind an approval; receipts; check reminders;
  operational metrics;
  and the full household export.

### Discord Bot Commands

> **Retired from production.** RubotPaul replaces the standalone bot as the
> Discord interface; these commands remain available when running the bot
> locally.

All commands are slash commands. Permission defaults to `manage_guild` and can
be re-mapped per role via Server Settings -> Integrations.

#### Top-level

| Command | Description |
|---------|-------------|
| `/meals <names>` | Plan meals (comma- or newline-separated) and post a consolidated shopping list. |

#### `/stock`

| Command | Description |
|---------|-------------|
| `/stock show` | Show the full household inventory with status emoji. |
| `/stock out <item>` | Mark an item as out of stock. |
| `/stock low <item>` | Mark an item as running low. |
| `/stock good <item>` | Mark an item as on hand. |
| `/stock add <item> [category]` | Track a new inventory item. |

#### `/pantry`

| Command | Description |
|---------|-------------|
| `/pantry list` | Show all pantry staples. |
| `/pantry add <ingredient> [category]` | Add a pantry staple. |
| `/pantry remove <ingredient>` | Remove a pantry staple. |

#### `/restock`

| Command | Description |
|---------|-------------|
| `/restock show` | Show the current restock queue (low + out items). |
| `/restock clear` | Mark all queued items as on hand. |

#### `/brands`

| Command | Description |
|---------|-------------|
| `/brands show` | List all brand preferences. |
| `/brands set <target> <brand> [match_type] [notes]` | Add a preferred brand. |
| `/brands avoid <target> <brand> [match_type] [notes]` | Add a brand to the avoid list. |
| `/brands clear <target> <brand>` | Remove a brand preference. |

#### `/recipes`

| Command | Description |
|---------|-------------|
| `/recipes list` | List all saved recipes. |
| `/recipes show <name>` | Show a recipe's details. |
| `/recipes forget <name>` | Delete a saved recipe. |

#### `/preferences`

| Command | Description |
|---------|-------------|
| `/preferences show` | Show current user preferences. |
| `/preferences set <key> <value>` | Update a preference (`default_servings`, `default_units`, `dietary_restrictions`). |

In addition, sending free-text DMs to the bot triggers natural-language
inventory parsing (e.g. "we ran out of milk and eggs" -> two `InventoryUpdate`
records applied to `household_inventory`).

### Command-Line Interface

Invoked via `python -m grocery_butler <subcommand>`. All subcommands share the
same database path and config (`load_config()` reads `.env`).

#### `data`

```
python -m grocery_butler data export [--output FILE]
python -m grocery_butler data delete --confirm "delete all household data"
python -m grocery_butler data restore FILE --confirm "replace all household data"
```

Export every household table as one JSON document, delete every household
row, or rebuild the database from an export. Delete and restore change
nothing without their exact phrase. These need no Anthropic key, and read the
database from `--database`, then `DATABASE_URL`, then `DATABASE_PATH`.
Encrypted backups and the restore runbook:
[`docs/operations/data-lifecycle.md`](docs/operations/data-lifecycle.md).

#### `plan`

```
python -m grocery_butler plan <meal> [<meal> ...] [--servings N]
```

Parse and consolidate a list of meals into a printed shopping list grouped by
store aisle, applying pantry staples and the restock queue.

#### `stock`

```
python -m grocery_butler stock show
python -m grocery_butler stock out <item>
python -m grocery_butler stock low <item>
python -m grocery_butler stock good <item>
python -m grocery_butler stock add <item> [--category <cat>]
```

#### `restock`

```
python -m grocery_butler restock show
python -m grocery_butler restock clear
```

#### `recipes`

```
python -m grocery_butler recipes list
python -m grocery_butler recipes show <name>
python -m grocery_butler recipes forget <name>
```

#### `pantry`

```
python -m grocery_butler pantry list
python -m grocery_butler pantry add <ingredient> [--category <cat>]
python -m grocery_butler pantry remove <ingredient>
```

#### `bot`

```
python -m grocery_butler bot
```

Starts the Discord worker (equivalent to `python -m grocery_butler.bot`).

### Python Module API

For embedding or scripting, the domain services are importable directly.

```python
from grocery_butler.config import load_config
from grocery_butler.recipe_store import RecipeStore
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.meal_parser import MealParser
from grocery_butler.consolidator import Consolidator

cfg = load_config()
store = RecipeStore(cfg.database_path)
pantry = PantryManager(cfg.database_path)
parser = MealParser(store)

meals = parser.parse_meals(["tacos", "stir fry"])
shopping_list = Consolidator().consolidate_simple(
    parsed_meals=meals,
    restock_queue=pantry.get_restock_queue(),
    pantry_staples=store.get_pantry_staple_names(),
)
```

#### Key classes

| Class | Module | Purpose |
|-------|--------|---------|
| `RecipeStore` | `recipe_store` | CRUD for recipes, pantry staples, brand preferences, user preferences. |
| `PantryManager` | `pantry_manager` | Inventory CRUD, restock queue, NL inventory updates via Claude. |
| `MealParser` | `meal_parser` | Resolve meal names to `ParsedMeal` (saved recipe or LLM-parsed). |
| `Consolidator` | `consolidator` | Merge meal ingredients into a deduplicated `ShoppingListItem` list. |

## Data Model

All domain types live in `grocery_butler/models.py` as Pydantic models. The
public ones:

| Model | Description |
|-------|-------------|
| `Ingredient` | One ingredient with `quantity`, `unit`, `category`, `is_pantry_item`. |
| `ParsedMeal` | A meal split into `purchase_items` and `pantry_items`. |
| `ShoppingListItem` | A consolidated item across one or more meals. |
| `InventoryItem` | A tracked household item with `status` (`on_hand`/`low`/`out`). |
| `InventoryUpdate` | NL-parsed status change with confidence. |
| `BrandPreference` | A `preferred`/`avoid` rule scoped to category or ingredient. |
| `HouseholdItem` | One thing the household keeps, with a stable id, a staple policy and a preferred brand and size. |
| `ItemEvent` | One entry in a household item's append-only audit trail. |
| `InventoryObservation` | One row of the append-only observation ledger. |
| `InventorySnapshot` | What we currently believe about an item, and the observation behind it. |
| `ReplenishmentNeed` | An item that should be bought, with its status and reasons. |
| `ShoppingPlan` | One immutable version of what the household intends to buy. |
| `HardConstraint` | Something that must hold: an allergen exclusion, a dietary rule, no substitutes. |
| `SoftPreference` | Something the household would like: a brand, a size, organic, generic. |

Vocabulary enums (`StrEnum`): `IngredientCategory`, `InventoryStatus`, `Unit`,
`BrandPreferenceType`, `BrandMatchType`, `PendingActionStatus`,
`StaplePolicy`, `ItemEventType`, `StockState`, `ObservationSource`,
`ObservationKind`, `NeedStatus`, `NeedReasonKind`.

**Canonical items ([ADR 0006](docs/adr/0006-canonical-household-item-identity.md)).**
`grocery_butler/household_items.py` owns item identity. Every name an item
answers to is an alias, and text resolves to at most one item. Matching only
ignores case and spacing: "tomato" and "tomatoes" stay separate until a
person adds an alias or merges them. A merge sets a pointer and rewrites
nothing, so an unmerge restores the earlier state exactly, and every
identity change is recorded with its actor. Recipe ingredients, pantry
staples and inventory rows each carry an `item_id`.

**Observation ledger ([ADR 0007](docs/adr/0007-append-only-inventory-observations.md)).**
`grocery_butler/inventory_ledger.py` holds what was observed about each
item: a state (`on_hand`, `low`, `out`), an optional stated quantity, a
source, a confidence, an actor and a time. Rows are only ever added. A
correction or a retraction is a new row that supersedes an old one. An
item's current state is derived on read from its latest valid observation,
and becomes `unknown` once that observation is older than its category's
freshness window (7 days for produce, meat, bakery and deli; 10 for dairy;
30 for beverages and other; 60 for frozen and dry goods). The snapshot
always says what was last stated, by whom, and when. For now the
`household_inventory` table is still what the dashboard and the shopping
list read; every status change through `PantryManager` is also written to
the ledger.

**Replenishment needs ([ADR 0008](docs/adr/0008-explicit-replenishment-needs.md)).**
`grocery_butler/replenishment.py` records that an item should be bought.
A need has a status (`suggested`, `approved`, `deferred`, `dismissed`,
`planned`, `ordered`, `received`, `cancelled`), the reasons it is wanted
(low stock, out, recurring staple, recipe demand, manual request), and a
record of every status change with its actor. An item has at most one open
need, so an item wanted for two reasons is one need listing both. Inventory
observations raise and withdraw suggestions; only a person approves, defers
or dismisses, and nothing is marked received automatically. The shopping
list still reads the older restock queue; it moves to needs with shopping
plans.

**Approval envelopes ([ADR 0010](docs/adr/0010-immutable-approval-envelopes.md)).**
`grocery_butler/approvals.py` is how anything gets approved from here on. An
envelope stores exactly what a member is asked to approve, with its hash and
a fingerprint of the state it was built from. Approving means sending back
the hash of what was shown. Executing reads the content from the envelope,
never from the caller, and is refused if the envelope expired, was altered,
or the state under it changed. So a changed plan can't execute under an
earlier approval. The only scope is a handoff; approving a purchase belongs
in the retailer's own checkout. Shopping plans and handoffs start using
envelopes as they are built.

**Household data lifecycle ([ADR 0013](docs/adr/0013-household-data-lifecycle.md)).**
`grocery_butler/data_lifecycle.py` treats everything the household stored as
one unit. An export is one JSON document holding every table, found from the
database's own catalogue so a new table is never missed. The same document is
the backup: `scripts/backup.sh` encrypts it, and a restore rebuilds the
schema the backup was taken at, loads it, and applies the migrations written
since. Delete removes every row and needs a typed phrase. The runbook, what
is retained and who else handles the data are in
[`docs/operations/data-lifecycle.md`](docs/operations/data-lifecycle.md).

**Check reminders ([ADR 0019](docs/adr/0019-check-reminders-are-rate-limited-suggestions.md)).**
`grocery_butler/check_reminders.py` chooses which items are worth asking
the household about: ones it keeps in stock whose state was never checked,
has gone stale, or was read with low confidence. Always-keep items come
first. At most three are issued a day unless the household changes the
`check_reminders_per_day` preference, and an issued item rests for three
days. A member can snooze an item, dismiss it until new evidence arrives, or
say "stop asking", which holds until they turn it back on; each choice is
recorded with who made it. Reminders are suggestions to OpenClaw. Nothing in
Grocery Butler sends a notification.

**Receipts and reconciliation ([ADR 0018](docs/adr/0018-receipts-are-reviewed-evidence.md)).**
`grocery_butler/reconciliation.py` records what actually came home. A
handoff is only a link, and nothing comes back from the retailer, so the
household says what happened: a receipt whose lines are each `ordered`,
`received`, `substituted` or `refunded`. A receipt is a proposal. Nothing
changes until a member reviews it, confirming or skipping every line, and
the review applies whole or not at all. A received item becomes `on_hand`
with source `receipt` and its need is marked received. A refund puts the
item back on the next plan. For a substitution the member says whether it
will do. A line naming an item we don't know waits in review and never
creates one. To reconcile by hand, tell OpenClaw what arrived, or
`POST /api/v2/receipts` and then `POST /api/v2/receipts/{id}/resolve`.

**Staples and policies ([ADR 0017](docs/adr/0017-staple-policies-decide-what-may-put-an-item-on-a-plan.md)).**
Each household item can carry one policy, in plain words:

| Policy | Meaning |
|--------|---------|
| `always_keep` | When it runs low it goes straight onto the next plan. Nobody is asked. |
| `replenish_when_low` | When it runs low it is suggested, and a member approves. Every pantry staple starts here. |
| `meals_only` | Bought only when a planned meal needs it. Running out is not a reason to buy it. |
| `never_suggest` | Nothing automatic proposes it. A recipe that uses it says so on the plan instead of adding it. |

No quantity is needed for any of them. An item may also have a target
amount (say, 2 bags), which becomes the amount on the plan when nothing else
states one, and a cadence hint. A member can always ask for an item by name,
whatever its policy. Set a policy with `PATCH /api/v2/items/{id}`; the
dashboard's pantry page still adds and removes staples, which sets and
clears `replenish_when_low`.

**Shopping plans ([ADR 0011](docs/adr/0011-versioned-deterministic-shopping-plans.md)).**
`grocery_butler/shopping_plans.py` builds the plan: one line per household
item, with every reason it is there. It is built by plain code from the needs
a member approved and, optionally, saved recipes to shop for. No model takes
part, so the same inputs always give the same plan and the same hash. Each
plan is a numbered version that is never changed; a change in the inputs
makes the next version. Amounts come only from recipes; a line that only a
need put there has no amount and means "the usual". The older shopping list
(`/api/v1/shopping-list/preview` and the dashboard page) still works as
before and is retired with v1.

**Hard constraints and soft preferences ([ADR 0012](docs/adr/0012-hard-constraints-and-soft-preferences.md)).**
`grocery_butler/constraints.py` keeps two things apart. A hard constraint
must hold: an allergen exclusion, a dietary rule, or "don't substitute this
item". A soft preference is what the household would like: a brand, a size,
organic, generic. They are different types in different tables, and nothing
converts one into the other. Every plan line carries the hard constraints
that apply to it, in a field of its own, and changing one makes the current
plan stale. A fulfillment adapter either enforces a hard constraint or
reports it as unenforced for a person to check; it never drops one. The old
free-text dietary restrictions were copied, word for word, into one
unverified dietary rule: enter real allergens as allergen exclusions.

The backlog's next domain concepts (`FulfillmentDraft` and others) are
described in
[Domain model changes](docs/OPENCLAW_ERP_BACKLOG.md#domain-model-changes).

The full schema is in `grocery_butler/db/schema.sql` (SQLite) and
`grocery_butler/db/schema_pg.sql` (Postgres). The most important rule is
encoded in the schema's comments:

> Inventory status OVERRIDES pantry staple exclusion.
> If salt is a pantry staple AND inventory says `out`, it gets INCLUDED in
> the shopping list. If an item is a pantry staple AND NOT tracked in inventory,
> assume `on_hand`.

## Development

### Running quality checks

```bash
# Run all quality checks (recommended before commit)
./scripts/check-all.sh

# Or run individual checks:
./scripts/test.sh          # Run tests with coverage
./scripts/lint.sh          # Run linting
./scripts/format.sh --fix  # Auto-format code
./scripts/typecheck.sh     # Run type checking
./scripts/security.sh      # Run bandit + pip-audit
./scripts/complexity.sh    # Run radon/xenon
./scripts/coverage.sh      # Coverage report (--html for browser view)
./scripts/mutation.sh      # Mutation testing via mutmut
./scripts/container-scan.sh  # Build and scan the image (needs Docker; not in check-all)
```

Always invoke through `./scripts/*.sh` rather than the underlying tools — the
scripts pin flags so local and CI behavior match.

### Project structure

```
grocery-butler/
├── grocery_butler/
│   ├── __init__.py
│   ├── __main__.py             # `python -m grocery_butler` entry point
│   ├── app.py                  # Flask web application
│   ├── bot.py                  # Discord bot
│   ├── cli.py                  # argparse CLI
│   ├── config.py               # .env loader + Config dataclass
│   ├── claude_utils.py         # Anthropic SDK wrapper
│   ├── prompt_loader.py        # Prompt template loader
│   ├── prompts/                # Claude prompt templates
│   ├── models.py               # Pydantic models + enums
│   ├── meal_parser.py          # Recipe / meal name -> ParsedMeal
│   ├── recipe_store.py         # Recipes + staples + brand prefs
│   ├── pantry_manager.py       # Inventory CRUD + NL parsing
│   ├── consolidator.py         # Multi-meal -> shopping list
│   ├── pending_actions.py      # Draft -> confirm -> execute audit trail
│   ├── data_lifecycle.py       # Export, delete and restore all household data
│   ├── observability.py        # Audit events and correlation ids
│   ├── metrics.py              # Operational metrics
│   ├── retries.py              # Bounded, logged retries
│   ├── api.py                  # /api/v1 blueprint (OpenClaw)
│   ├── db/
│   │   ├── adapter.py          # SQLite/Postgres adapter
│   │   ├── migrate.py          # Schema migrations
│   │   ├── schema.sql          # SQLite schema
│   │   ├── schema_pg.sql       # Postgres schema
│   │   └── migrations/         # Versioned migration files
│   ├── templates/              # Jinja2 templates
│   └── static/                 # Pico CSS + assets
├── tests/                      # pytest suite (>=90% coverage)
├── scripts/                    # Quality control scripts
├── .github/workflows/          # CI/CD pipelines
├── .claude/                    # AI subagents and skills
├── docs/
│   ├── OPENCLAW_ERP_BACKLOG.md # Product backlog and delivery sequence
│   └── adr/                    # Architecture decision records
├── plans/                      # Historical build plans (superseded)
├── pyproject.toml              # Tool configuration
├── deploy/vps/                 # compose.yaml and env.example for the server
├── Dockerfile                  # Production build + runtime image
├── docker-entrypoint.sh        # Runs migrations, then execs gunicorn
├── requirements.txt            # Runtime dependencies
└── requirements-dev.txt        # Development dependencies
```

### Quality standards

| Standard | Threshold |
|----------|-----------|
| Test coverage (branch) | >= 90% |
| Docstring coverage | >= 95% |
| Cyclomatic complexity | <= 10 per function |
| Lint violations | 0 |
| Type errors (`mypy --strict`) | 0 |
| Security findings (bandit, pip-audit) | 0 |

See `CLAUDE.md` for the full engineering culture document.

## Deploying

Grocery Butler runs on the household's own server, the one OpenClaw already
runs on ([ADR 0021](docs/adr/0021-self-host-the-dogfood-on-the-openclaw-server.md)).
`deploy/vps/compose.yaml` starts two containers: the app, built from this
repository's `Dockerfile`, and Postgres. The step-by-step runbook is
[`docs/operations/vps-deployment.md`](docs/operations/vps-deployment.md).

- **Nothing listens on a public address.** Both containers publish only on
  the server's loopback address.
- **OpenClaw calls `http://127.0.0.1:8000/api/v1`** on the same server.
- **People open the dashboard over the tailnet**, at the HTTPS address
  `tailscale serve` gives it.
- **Backups are nightly and encrypted**, kept on the server by a systemd
  timer (`deploy/vps/systemd/`). Someone copies one off the server each week.
- **A deploy is manual:** `git pull`, then
  `docker compose up --detach --build`. See
  [Process Topology](#process-topology) for how the container boots: it
  migrates the database before gunicorn starts, and a failed migration
  stops the boot.

### Required environment variables

No household retailer login is needed, or supported. Set the secrets in
`deploy/vps/.env` on the server (start from `deploy/vps/env.example`); the
compose file sets the rest. `APP_ENV=production` is baked into the
`Dockerfile` and doesn't need to be set separately — it's what arms the
fail-fast checks below.

| Variable | Required | Description |
|----------|----------|-------------|
| `POSTGRES_PASSWORD` | Yes | Password for the bundled Postgres. The compose file builds `DATABASE_URL` from it. |
| `DATABASE_URL` | Set by compose | PostgreSQL connection URL. |
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for Claude-powered features. |
| `RUBOTPAUL_SHARED_SECRET` | Yes (OpenClaw API) | HMAC secret for `/api/v1` bearer tokens; must match RubotPaul's copy. |
| `FLASK_SECRET_KEY` | Yes (web) | Stable secret key for Flask sessions (generate once, reuse). |
| `PORT` | No | Port gunicorn binds inside the container (default 8000). |
| `TAILNET_GUARD_ENABLED` | No (defaults secure) | Kill switch for the network boundary guard (below); the guard is active unless explicitly set to `false`/`0`/`no` (case-insensitive). |
| `TAILNET_GUARD_ALLOWED_CIDRS` | No (defaults secure) | Comma-separated CIDR allow-list for the network boundary guard (below); replaces (does not extend) the default `127.0.0.0/8,::1/128,100.64.0.0/10`. The compose file sets it to loopback plus its own network's gateway. An explicitly-empty value trusts nothing (full lockout, logged at startup) — delete the variable to restore defaults. |

### Network boundary guard (tailnet-only)

Grocery Butler is meant to be reached only over the Tailscale tailnet (or
loopback, locally). A fail-closed `app.before_request` hook, registered by
`create_app()` in `grocery_butler/network_guard.py`, rejects any request
whose **socket peer address** (`request.remote_addr`) falls outside an
allow-list of CIDR ranges.

- **No forwarded-header trust.** Admission is keyed only on
  `request.remote_addr`; the guard never trusts `X-Forwarded-For` (or any
  other client-supplied header), and Werkzeug's `ProxyFix` must never be
  added. A forwarded header is whatever the client chose to send —
  trusting it would let a request simply set `X-Forwarded-For: 127.0.0.1`
  and walk back through the hole this guard exists to close.
- **`TAILNET_GUARD_ENABLED`** — kill switch, read once at app-creation time.
  The guard is active unless explicitly set to `false`/`0`/`no`
  (case-insensitive); unset means enabled (fail-closed by default).
- **`TAILNET_GUARD_ALLOWED_CIDRS`** — comma-separated CIDR allow-list, read
  once at app-creation time. Defaults to
  `127.0.0.0/8,::1/128,100.64.0.0/10` (loopback + IPv6 loopback + the
  Tailscale CGNAT range). Setting this variable **replaces** the default
  list; it does not extend it. RFC1918 private ranges are deliberately
  excluded from the default so a misconfigured office/home LAN is never
  accidentally trusted.
  - **Blank is not unset.** Setting the variable to an *empty string*
    (e.g. `TAILNET_GUARD_ALLOWED_CIDRS=` with nothing after it) is a legitimate "trust nothing" configuration: every
    non-health request is rejected, including loopback and the tailnet.
    This fails closed, and the app logs a startup warning
    (`network_guard_empty_allowlist`) so the lockout is loud rather than
    silent — but to restore the defaults, *delete* the variable, don't
    blank it.
  - **Entries are parsed strictly.** A CIDR entry with host bits set
    (e.g. `100.64.1.5/24` instead of `100.64.1.0/24`) or any other
    malformed entry raises `ValueError` at startup rather than being
    silently corrected or dropped — misconfiguration fails loud, at
    deploy time.
  - **IPv4-mapped IPv6 peers are normalized.** If the WSGI server ever
    listens dual-stack, an IPv4 peer can be reported as
    `::ffff:100.64.1.2`; the guard also checks the unmapped IPv4 form so
    such peers get IPv4 CIDR semantics (this widens availability only —
    a mapped public address is still rejected).
- **Health check exemption.** `/health` and `/healthz` are always exempt
  (matched by Flask endpoint name, not path) so a platform's or Docker's
  health check keeps working wherever it comes from. This is intentional: both endpoints expose only
  status/DB-connectivity information.
- **Rejection behavior.** Requests from outside the allow-list receive a
  `403`: a JSON `{"error": "forbidden"}` body for `/api/*` paths, an HTML
  error page otherwise.
- **Observability.** At startup the guard logs the resolved allow-list
  (`network_guard_enabled allowed_cidrs=...`, at info level, which gunicorn
  does not print in production; rejections and warnings do show), or
  `network_guard_disabled` if the kill switch is set, or
  `network_guard_empty_allowlist` (warning) if the resolved list is
  empty. Every rejection logs
  `network_guard_rejected path=... remote_addr=...` at warning level —
  path and peer address only, never headers or bodies.

#### Verifying the boundary on the live deployment

The guard's correctness rests on one assumption unit tests cannot prove:
that `request.remote_addr`, as gunicorn sees it on the real server, is the
address the allow-list names. On the self-hosted deployment a request to
the loopback-published port arrives from the compose network's gateway.
**Verify this once on first deploy;** the runbook's
[step 5](docs/operations/vps-deployment.md#first-deploy) is that check, and
step 7 confirms nothing answers on the server's public address.

**Rate limiting:** flask-limiter is deliberately deferred. With the
fail-closed tailnet boundary above, public HMAC brute-forcing of `/api/v1`
and abuse of paid Claude endpoints (e.g. `/api/v1/meals/parse`) already
require tailnet access, so a limiter would add a dependency without closing
a live gap — and a naive in-memory limiter is ineffective across
per-gunicorn-worker processes without a shared store (no Redis is
provisioned). Revisit if multi-tenant tailnet access is ever introduced.

### Operations: audit events and metrics

How to see what the system did and whether it is drifting
([ADR 0015](docs/adr/0015-audit-events-metrics-and-bounded-retries.md)).

**Audit events** are single lines of JSON on standard error, from the
`grocery_butler.audit` logger. Each has `event`, `at`, `correlation_id` and
a few fields. Every response carries `X-Correlation-ID`; search the logs for
it to see everything that request did. No image, raw bytes or secret is ever
written: such fields are replaced with `[redacted]`.

| Event | When | Fields |
|-------|------|--------|
| `http.request` | Every `POST`, `PUT`, `PATCH` or `DELETE`, including rejected ones | `method`, `route`, `status`, `actor` |
| `capture.batch_submitted` | An observation batch is stored | `capture_session`, `batch_id`, `source`, `proposals`, `actor` |
| `capture.batch_resolved` | A member reviews a batch | `capture_session`, `batch_id`, `status`, `confirmed`, `corrected`, `rejected`, `actor` |
| `capture.batch_rejected` | A batch is thrown away | `capture_session`, `batch_id`, `proposals`, `actor` |
| `extraction.failed` | A model reply could not be turned into rows | `stage`, `reason` |
| `call.attempt` | Each try of a model or adapter call | `what`, `attempt`, `of`, `outcome`, `error`, `will_retry` |
| `adapter.handoff_created` | A fulfillment handoff succeeded | `adapter`, `plan_version`, `unenforced_constraints` |
| `adapter.failed` | A fulfillment handoff failed | `adapter`, `plan_version`, `error` |
| `handoff.drafted` | A handoff was drafted for approval | `envelope_id`, `adapter`, `plan_version`, `actor` |
| `handoff.approved`, `handoff.rejected` | A member approved or turned down a handoff | `envelope_id`, `actor` |
| `handoff.attempt` | An approved handoff was carried out | `attempt_id`, `envelope_id`, `adapter`, `plan_version`, `status`, `error`, `retryable` |
| `handoff.opened` | A member followed a handoff link through the redirect | `attempt_id`, `adapter` |
| `receipt.submitted` | A receipt was stored for review | `receipt_id`, `attempt_id`, `lines`, `needs_review`, `actor` |
| `receipt.resolved` | A member reviewed a receipt | `receipt_id`, `attempt_id`, `confirmed`, `skipped`, `actor` |
| `receipt.rejected` | A receipt was thrown away | `receipt_id`, `actor` |
| `reminder.issued` | Check reminders were taken to surface | `count`, `actor` |
| `reminder.snoozed`, `reminder.dismissed`, `reminder.stopped`, `reminder.resumed` | A member chose how an item's reminders behave | `item_id`, `actor` |
| `audit.count_failed` | A counter could not be advanced | `counted`, `error` |

`capture_session` (`batch-<id>`) is the same on every event about one
batch, across the requests that submit and review it.

**Metrics** are read at `GET /api/v2/operations/metrics`:

| Metric | What it tells you |
|--------|-------------------|
| `extraction_failures` | Model replies that couldn't be used. A burst means the model or a prompt changed. |
| `adapter_failures` | Fulfillment handoffs that failed. |
| `correction_rate` | How often a member had to correct a proposal. Rising means proposals are getting worse. |
| `stale_inventory` | Items nobody has checked within their freshness window. |
| `reconciliation_gaps` | Needs marked ordered over 3 days ago and never confirmed received, plus handoffs that old with no confirmed receipt. |
| `handoffs` | How handoffs turn out: created, opened, abandoned, reconciled, failed, and drafts corrected before sending. |

**Retries.** A model or adapter call that times out, loses its connection,
or gets a 408, 429 or 5xx is tried up to three times, with pauses of 0.5 s
then 1 s. Every try is a `call.attempt` event. Any other error fails at
once.

### Container scanning

`./scripts/container-scan.sh` builds the production image and scans it with
a pinned Trivy for HIGH and CRITICAL vulnerabilities that have a fix. It is
**not** part of `./scripts/check-all.sh` or the CI gate: a CVE published
overnight in the base image should be seen within a day, not block every
unrelated pull request. The `Container scan` workflow runs it nightly, on
demand, and on any pull request that changes the `Dockerfile`,
`requirements.txt`, the entrypoint or the scan itself.

When it fails, rebuild first: most findings are fixed by a newer base image.
If a finding remains, upgrade the package that carries it (see the
`cve-remediation` skill). Don't suppress it.

The runtime stage runs `apt-get upgrade` and removes pip, for two reasons the
first scan showed. A Debian fix can be newer than the `python:3.13-slim`
image it applies to. And pip carries private copies of `urllib3`, `msgpack`
and `setuptools` under `pip/_vendor`, which a pin in `requirements.txt`
cannot reach; the app never installs anything at run time, so the installer
goes. A finding in a package that is not in `requirements.txt` is usually one
of these two.

### Compose smoke test

`./scripts/compose-smoke.sh` starts the real deployment (the app image and
Postgres from `deploy/vps/compose.yaml`) under a throwaway project name,
with secrets generated for the run, and checks it over HTTP: both
containers healthy, the dashboard admitted by the network guard and a
request from another container refused by it, a signed
API call written to Postgres and read back, the same call refused without
a token, the data still there after a restart, and both ports bound to
`127.0.0.1`. Every other test runs the app in-process, so this is the only
one that proves the image boots.

Like the container scan it is not part of the CI gate. The
`Compose smoke test` workflow runs it nightly, on demand, and on a pull
request that changes the image, the compose file or the script. When it
fails it prints the containers' logs.

### Browser security baseline

`grocery_butler/web_security.py` protects the HTML dashboard, on top of the
network boundary guard:

- **CSRF tokens.** Every dashboard form and the inventory toggle send a
  token bound to the visitor's session; a state-changing request without it
  gets a 400 and changes nothing. This stops a public web page, open in a
  household member's browser, from posting at the dashboard's tailnet
  address. `/api/v1` is exempt: it authenticates each request with its HMAC
  bearer token and never reads the session cookie. A new form needs
  `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">`;
  a test fails if one is missing.
- **Session cookie.** `HttpOnly` and `SameSite=Lax` everywhere, and
  `Secure` in production (`APP_ENV=production`), where people reach the
  dashboard only over the HTTPS address `tailscale serve` gives it. A
  browser on plain HTTP does not send that cookie back, so its forms are
  refused. Open the dashboard at its tailnet address, not through an SSH
  tunnel to `http://127.0.0.1:8000`.
- **Security headers** on every response: a same-origin-only Content
  Security Policy (no inline script or style, no framing),
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and
  `Referrer-Policy: same-origin`. Templates therefore use no inline event
  handlers or styles.
- **No third-party fetches.** The dashboard loads nothing from outside its
  own origin, so browsing it leaks nothing off the tailnet.
- **Request size limit.** Bodies over 1 MiB get a 413.
- **Secret-safe config.** `Config`'s repr leaves out the API key, bot token
  and database URL.

### Fail-fast startup checks

The Docker image sets `APP_ENV=production` (see the `Dockerfile`), which
arms startup checks in `create_app()` (issue #64):

- Missing or empty `FLASK_SECRET_KEY` or `RUBOTPAUL_SHARED_SECRET` raises
  `RuntimeError` and the process refuses to boot — a misconfigured deploy
  crashes immediately instead of serving requests that later 401/500 or
  silently generate an unstable session key.
- Missing `ANTHROPIC_API_KEY` never blocks boot (in any mode); it logs a
  startup warning since Claude-backed meal parsing degrades to
  stub/saved-recipe paths without it.

See `.env.example` for the full variable list and comments.

### Quick start

Follow [`docs/operations/vps-deployment.md`](docs/operations/vps-deployment.md).
In short: write `deploy/vps/.env`, run `docker compose up --detach --build`
in `deploy/vps`, confirm the guard's allow-list from the logs, then
`sudo tailscale serve --bg 8000`.

## License

MIT License
