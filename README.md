# grocery-butler

> A grocery ERP for the household — built to protect invisible labor.

## Mission

Households run on a quiet, unrecognized layer of work: someone notices the
peanut butter is low, mentally cross-references it against tomorrow's lunches,
remembers that one kid won't eat the crunchy kind, checks last week's receipt
to avoid double-buying, and threads it all into a coherent shopping trip. That
labor is largely **invisible** — it doesn't show up on a calendar, doesn't get
billed, and doesn't get thanked. When the person doing it is unavailable, sick,
or finally tired of doing it, the household feels the absence immediately.

**Grocery Butler** treats household provisioning as a first-class business
process. Inventory, recipes, brand preferences, pantry staples, and ordering
are stored in a real database, exposed through real interfaces (web, chat, and
CLI), and orchestrated like an ERP system would orchestrate a supply chain.

The goal is not to "add an app" to someone's life. The goal is to make the
invisible work **visible, sharable, and partially automatable**, so that:

- The mental load can be **handed off** without losing context.
- Multiple household members can **contribute** ("we're out of milk") via the
  channels they already use (Discord, web, terminal).
- Decisions made once (preferred brands, default servings, dietary
  restrictions) **persist** instead of being rediscovered every week.
- The boring parts (consolidating ingredients across meals, mapping a recipe
  to a Safeway cart, picking sane substitutions) are handled by **the
  software**, not by a human's evening.

This is a small system with a serious intention: give back the hours that
"just keeping the house running" silently consumes.

## Table of Contents

- [What It Does](#what-it-does)
- [Architecture](#architecture)
- [Code Choices](#code-choices)
- [Installation](#installation)
- [Running the System](#running-the-system)
- [API Documentation](#api-documentation)
  - [HTTP / Web API](#http--web-api)
  - [RubotPaul Integration (/api/v1)](#rubotpaul-integration-apiv1)
  - [Discord Bot Commands](#discord-bot-commands)
  - [Command-Line Interface](#command-line-interface)
  - [Python Module API](#python-module-api)
- [Data Model](#data-model)
- [Development](#development)
- [Deploying to Railway](#deploying-to-railway)
- [License](#license)

## What It Does

Grocery Butler implements the full provisioning loop for a household:

1. **Capture recipes** — describe a meal in natural language; Claude parses it
   into structured ingredients with quantities, units, and categories.
2. **Track real inventory** — the household marks items as `on_hand`, `low`,
   or `out` from any channel. Items go onto the **restock queue** automatically.
3. **Plan a week** — given a list of meal names, the system pulls saved recipes
   (or parses new ones), scales them to your default servings, and emits a
   consolidated shopping list grouped by store aisle.
4. **Respect what's already true** — pantry staples are excluded by default,
   restock items are added unconditionally, and brand preferences and dietary
   rules are applied at consolidation time.
5. **Order through Safeway** — the optional Safeway pipeline maps shopping
   list items to real products, suggests substitutions for out-of-stock items
   via Claude, builds a cart, and submits the order for pickup or delivery.

## Architecture

Grocery Butler is intentionally a **small monolith with three front doors**.
A single shared database, a single domain layer, and three thin adapters that
serve the channels people actually use.

```
                        +-----------------------+
                        |     Anthropic API     |
                        |   (Claude — parsing,  |
                        |    consolidation,     |
                        |    substitutions)     |
                        +-----------+-----------+
                                    |
                                    v
+---------+     +---------+     +---------+     +-----------------+
| Flask   |     | Discord |     |   CLI   |     |  Safeway HTTP   |
|  web    |     |   bot   |     |  argp.  |     |  client (httpx) |
| (app.py)|     | (bot.py)|     |(cli.py) |     |(safeway_client) |
+----+----+     +----+----+     +----+----+     +--------+--------+
     |               |               |                   ^
     +-------+-------+---------------+                   |
             |                                           |
             v                                           |
     +-------------------+   +----------------+          |
     |  Domain services  |-->|  SafewayPipeline|---------+
     |                   |   +----------------+
     |  - MealParser     |
     |  - Consolidator   |
     |  - PantryManager  |
     |  - RecipeStore    |
     +---------+---------+
               |
               v
        +-------------+
        |  db/adapter |   SQLite (dev) / Postgres (prod)
        +-------------+
```

### Layers

**Adapters (channels)** — `app.py` (Flask), `bot.py` (Discord), `cli.py`
(argparse). These are intentionally thin: they parse channel-specific input,
call into the domain services, and format output for the channel. No business
rules live here.

**Domain services** — the heart of the system:

| Module | Responsibility |
|--------|---------------|
| `meal_parser.py` | Turn a meal name (or saved recipe) into a `ParsedMeal` of structured ingredients via Claude. |
| `recipe_store.py` | Persistence for recipes, pantry staples, brand preferences, and user preferences. |
| `pantry_manager.py` | Household inventory CRUD, restock queue, and natural-language inventory updates. |
| `consolidator.py` | Merge ingredients across multiple meals into a single deduplicated shopping list, applying pantry staples and restock overrides. |

**Safeway pipeline** — `safeway_pipeline.py` wires together
`safeway_client.py`, `product_search.py`, `product_selector.py`,
`substitution_service.py`, `cart_builder.py`, and `order_service.py` into a
two-step `build_cart` -> `submit_order` flow. The pipeline is optional; the
core meal-planning system runs without any Safeway credentials.

**Persistence** — `db/adapter.py` provides a thin abstraction over `sqlite3`
and `psycopg2` so the same code runs locally on SQLite (with WAL mode for
concurrent reader/writer between Flask and the Discord bot) and in production
on Railway Postgres. Schema is defined declaratively in `db/schema.sql` and
`db/schema_pg.sql`, with one-shot data migrations in `db/migrate.py`.

### Process Topology

In production (Railway), the system builds and runs from the `Dockerfile` —
there's no separate build/release configuration to keep in sync. The image
boots into `docker-entrypoint.sh`, which applies any pending schema
migrations (`python -m grocery_butler.db.migrate`, reading `DATABASE_URL`)
before anything else starts. If a migration fails, `set -eu` aborts the boot
immediately, so gunicorn never starts and no request is ever served against
an unmigrated schema. Once migrations succeed, the entrypoint `exec`s
straight into gunicorn (`grocery_butler.app:create_app()`), which becomes
the container's only long-lived process, serving both the kitchen-phone HTML
UI and the HMAC-authenticated `/api/v1` blueprint.

There is no separate worker process in production. The Discord bot
(`python -m grocery_butler.bot`) is retired from deployment — RubotPaul is
the household's Discord interface now, and it drives grocery-butler through
`/api/v1` instead. The bot module stays in the codebase and can still be run
locally (see [Discord bot (local only)](#discord-bot-local-only--retired-from-production)).
The CLI is invoked ad-hoc as a single-shot process (no daemon).

See [Deploying to Railway](#deploying-to-railway) for the environment
variables and setup steps that go with this topology.

### Key Design Rules

1. **Inventory status overrides pantry staples.** Salt is normally excluded as
   a pantry staple, but if the household marks salt as `out`, it goes onto
   the order. This rule is enforced inside `Consolidator`, not at the call site.
2. **Claude is a parser, not a planner.** The LLM extracts structure from
   natural language (recipes, inventory updates, substitution choices). It
   does not own state. State lives in the database.
3. **Channels are interchangeable.** Anything you can do in the web UI you can
   do from the CLI or from Discord. No channel is privileged.
4. **The Safeway integration is optional.** Removing it leaves a fully usable
   meal-planning + inventory tracking system.

### Safeway Order Submission Status (v1.0)

The Safeway integration's **checkout API surface is unverified** against the
live Safeway/Albertsons API (Issue #60). Concretely: the Okta auth flow uses
an `aus`-prefixed `OKTA_CLIENT_ID`, which looks like an authorization-server
id rather than a client id, and `/abs/pub/web/orders` has no modeled payment
method or delivery/pickup slot reservation. No real order has ever been
confirmed to go through this client.

For v1.0, real order submission is **disabled by default** and fails safe:

- **Building and reviewing a cart still works** end-to-end —
  `SafewayPipeline.build_cart_only` (Python API), CLI
  `order review`/`order submit --dry-run`, `/order review` (Discord), and
  the RubotPaul `/order/preview` endpoint all function normally.
- **Submitting an order does not.** `OrderService.submit_order`,
  `SafewayPipeline.submit_cart`/`run()`, CLI `order submit`, `/order submit`
  (Discord), and the RubotPaul `/order/submit` → `/actions/confirm` flow all
  return a failed result, raise, or respond `501`, with an actionable
  message pointing at manual checkout — instead of silently pretending to
  place an order.
- **Checkout for v1.0 is manual**: build and review the cart in-app, then
  complete the purchase yourself on safeway.com or in the Safeway app.

Set `SAFEWAY_ORDER_SUBMISSION_ENABLED=true` (see `.env.example`) to lift the
guard once the checklist below is complete. **Do not enable it in
production before that.**

**Human verification checklist** (gates enabling the flag — see Issue #60):

- [ ] Verify the Okta `authn`/`authorize` flow against real credentials. The
      `aus`-prefixed `OKTA_CLIENT_ID` is likely an authorization-server id,
      not a `0oa` client id — capture the real value from web-app network
      traffic.
- [ ] Verify each endpoint against the live API —
      `/api/v2/grocerystore/search`,
      `/abs/pub/web/stores/{id}/fulfillment`, `/abs/pub/web/orders` — and
      check in recorded request/response fixtures for tests.
- [ ] Model the checkout requirements this client currently omits (payment
      method selection, delivery/pickup slot reservation, address/contact),
      or make an explicit decision to keep the descope.
- [ ] Record one successful end-to-end order against a real account (see
      the manual verification checklist in #32 and the automated test plan
      in #31).

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

- **Flask 3** — small, explicit, and fits in a single Railway container
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
  by feature (`/stock`, `/pantry`, `/brands`, `/recipes`, `/preferences`,
  `/order`). Permission is delegated to Discord's native `manage_guild` so
  server admins control access through the Integrations UI.

### LLM

- **Anthropic Claude (Sonnet)** via the official `anthropic` SDK.
  - Recipe parsing: structured extraction with JSON schemas.
  - Inventory natural language: "we ran out of milk and the bread is going
    stale" -> a list of `InventoryUpdate` records.
  - Substitution selection: rank Safeway alternatives for an out-of-stock item.
  - Prompt templates live in `grocery_butler/prompts/` and are loaded by
    `prompt_loader.py` so they can be edited without code changes.

### Data

- **Pydantic v2** — all domain objects (`Ingredient`, `ParsedMeal`,
  `ShoppingListItem`, `InventoryItem`, `BrandPreference`, `SafewayProduct`,
  `CartSummary`, ...) are Pydantic models. Field validators normalize messy
  inputs (e.g. `"lbs"` -> `Unit.LB`) at the edge so the rest of the system
  works with clean enums.
- **`StrEnum` for vocabularies** — `IngredientCategory`, `InventoryStatus`,
  `Unit`, `BrandPreferenceType`, `BrandMatchType`, `FulfillmentType`,
  `PriceSensitivity`, `OrganicPreference`, `SubstitutionSuitability`. The
  string value matches the database value, so enum members can be compared
  directly with stored strings.
- **SQLite (dev) + Postgres (prod)** — same schema, same code, swapped via
  the `db/adapter.py` shim. SQLite uses WAL mode so the Flask process and the
  Discord worker can read/write concurrently.

### HTTP

- **httpx** for the Safeway client — async-capable and gives precise control
  over headers, timeouts, and bearer-token auth. Safeway's web API is not
  officially documented; the client authenticates via an Okta-issued bearer
  token (no cookie session). The full auth flow and every endpoint are
  **unverified** against the live Safeway/Albertsons API — see
  [Safeway Order Submission Status (v1.0)](#safeway-order-submission-status-v10)
  and Issue #60.

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
| `RUBOTPAUL_SHARED_SECRET` | RubotPaul API | HMAC secret for `/api/v1` bearer tokens (shared with RubotPaul). Required at boot when `APP_ENV=production`; otherwise only enforced per-request (401). |
| `DISCORD_BOT_TOKEN` | Local bot runs only | Authenticates the (retired-from-production) Discord bot. |
| `FLASK_SECRET_KEY` | Web (production) | Stable session signing key. Required at boot when `APP_ENV=production`; in dev, an unset key falls back to a random per-process key (see [Fail-fast startup checks](#fail-fast-startup-checks)). |
| `DATABASE_PATH` | SQLite (dev) | Path to the local SQLite file. Defaults to `mealbot.db`. |
| `DATABASE_URL` | Postgres (prod) | Connection URL injected by Railway Postgres. |
| `SAFEWAY_USERNAME` | Ordering only | Safeway account email. |
| `SAFEWAY_PASSWORD` | Ordering only | Safeway account password. |
| `SAFEWAY_STORE_ID` | Ordering only | Safeway store ID for product searches. |
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
[RubotPaul integration API](#rubotpaul-integration-apiv1).

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
| `POST` | `/recipes/add` | Form: `name`, `servings`, repeated `ing_name_N`, `ing_qty_N`, `ing_unit_N`, `ing_category_N`, `ing_pantry_N`. |
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

### RubotPaul Integration (/api/v1)

RubotPaul (the household's OpenClaw assistant) drives grocery-butler through a
JSON API served by the same web process as the HTML UI — the web UI is
unchanged. The blueprint lives in `grocery_butler/api.py` and is registered by
`create_app()`.

- **Base URL:** `https://grocery-butler.tailnet.ts.net/api/v1` — reached over
  the Tailscale tailnet. The tailnet-only boundary is enforced in code, not
  just by network configuration: see
  [Network boundary guard](#network-boundary-guard-tailnet-only) for how
  requests from outside the allow-list are rejected.
- **Auth:** every `/api/v1` route requires a bearer token in the
  `<caller_id>.<timestamp>.<hmac_hex>` format, signed with
  `RUBOTPAUL_SHARED_SECRET` (HMAC-SHA256, 5-minute TTL). Mint with
  `grocery_butler.auth_middleware.mint_token`. An unauthenticated
  `GET /healthz` liveness probe is exempt.
- **Surface:** read endpoints (`/inventory`, `/pantry`, `/recipes`, `/brands`,
  `/preferences`, `/restock`), compute previews (`/meals/parse`,
  `/shopping-list/preview`, `/order/preview`), low-stakes writes
  (`/stock/update`, `/stock/add`, `/restock/clear`, `/recipes/save`,
  `DELETE /recipes/<id>`), and staged destructive actions (`/order/submit`,
  `/brands/set`, `/preferences/set` → `/actions/confirm` / `/actions/deny`
  against the `pending_actions` audit table).
- **Safety:** destructive actions follow draft → confirm → execute. Staging
  returns a `pending_confirmation` action id with a 5-minute expiry; nothing
  executes until RubotPaul confirms after Geoff replies "confirm" in chat.

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

#### `/order`

| Command | Description |
|---------|-------------|
| `/order review <meals>` | Build a Safeway cart from the given meals and show the summary without submitting. |
| `/order submit <meals>` | Build and submit a Safeway order. |

In addition, sending free-text DMs to the bot triggers natural-language
inventory parsing (e.g. "we ran out of milk and eggs" -> two `InventoryUpdate`
records applied to `household_inventory`).

### Command-Line Interface

Invoked via `python -m grocery_butler <subcommand>`. All subcommands share the
same database path and config (`load_config()` reads `.env`).

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

#### `order`

```
python -m grocery_butler order review --meals "tacos,pasta"
python -m grocery_butler order review --items "milk:1:gal,eggs:12:each"
python -m grocery_butler order submit --meals "..."
```

Builds (and optionally submits) a Safeway cart. Requires
`SAFEWAY_USERNAME`/`SAFEWAY_PASSWORD`/`SAFEWAY_STORE_ID` in the environment.

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
from grocery_butler.safeway_pipeline import SafewayPipeline

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

# Optional: build and submit a Safeway order
pipeline = SafewayPipeline(cfg, cfg.database_path)
cart = pipeline.build_cart(shopping_list)
result = pipeline.submit_order(cart)
```

#### Key classes

| Class | Module | Purpose |
|-------|--------|---------|
| `RecipeStore` | `recipe_store` | CRUD for recipes, pantry staples, brand preferences, user preferences. |
| `PantryManager` | `pantry_manager` | Inventory CRUD, restock queue, NL inventory updates via Claude. |
| `MealParser` | `meal_parser` | Resolve meal names to `ParsedMeal` (saved recipe or LLM-parsed). |
| `Consolidator` | `consolidator` | Merge meal ingredients into a deduplicated `ShoppingListItem` list. |
| `SafewayPipeline` | `safeway_pipeline` | End-to-end Safeway ordering: cart build + order submit. |
| `SafewayClient` | `safeway_client` | Low-level httpx client for Safeway's API. |
| `ProductSearchService` | `product_search` | Search Safeway products for a shopping list item. |
| `ProductSelector` | `product_selector` | Pick the best Safeway product given brand prefs and price sensitivity. |
| `SubstitutionService` | `substitution_service` | Claude-driven substitution suggestions for out-of-stock items. |
| `CartBuilder` | `cart_builder` | Assemble a `CartSummary` from selected products. |
| `OrderService` | `order_service` | Submit an assembled cart to Safeway. |

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
| `SafewayProduct` | A real Safeway catalog product. |
| `SubstitutionOption` / `SubstitutionResult` | Substitution candidates and outcomes. |
| `CartItem` / `CartSummary` | A populated Safeway cart ready for submission. |
| `FulfillmentOption` | Pickup or delivery window with fee. |

Vocabulary enums (`StrEnum`): `IngredientCategory`, `InventoryStatus`, `Unit`,
`BrandPreferenceType`, `BrandMatchType`, `PriceSensitivity`,
`OrganicPreference`, `FulfillmentType`, `SubstitutionSuitability`.

The full schema is in `grocery_butler/db/schema.sql` (SQLite) and
`grocery_butler/db/schema_pg.sql` (Postgres). The most important rule is
encoded in the schema's comments:

> Inventory status OVERRIDES pantry staple exclusion.
> If salt is a pantry staple AND inventory says `out`, it gets INCLUDED in
> the order. If an item is a pantry staple AND NOT tracked in inventory,
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
│   ├── safeway_client.py       # Safeway HTTP client (httpx)
│   ├── product_search.py       # Search Safeway products
│   ├── product_selector.py     # Brand/price-aware product picking
│   ├── substitution_service.py # Claude-driven substitutions
│   ├── cart_builder.py         # Assemble CartSummary
│   ├── order_service.py        # Submit Safeway orders
│   ├── safeway_pipeline.py     # End-to-end ordering pipeline
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
├── pyproject.toml              # Tool configuration
├── Dockerfile                  # Railway build + runtime image
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

## Deploying to Railway

Railway builds and runs the project's `Dockerfile` directly. See
[Process Topology](#process-topology) for how the container boots — in
short, `docker-entrypoint.sh` migrates the database before gunicorn ever
starts, and a migration failure aborts the deploy rather than serving
requests against a stale schema.

RubotPaul reaches the `/api/v1` blueprint over Tailscale at
`https://grocery-butler.tailnet.ts.net/api/v1`; there's no standalone worker
process to deploy alongside the web service.

### Required environment variables

Set these in your Railway project settings. `APP_ENV=production` is baked
into the `Dockerfile` and doesn't need to be set separately — it's what
arms the fail-fast checks below.

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection URL (auto-injected by Railway Postgres plugin). |
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for Claude-powered features. |
| `RUBOTPAUL_SHARED_SECRET` | Yes (RubotPaul API) | HMAC secret for `/api/v1` bearer tokens; must match RubotPaul's copy. |
| `FLASK_SECRET_KEY` | Yes (web) | Stable secret key for Flask sessions (generate once, reuse). |
| `SAFEWAY_USERNAME` | Yes (ordering) | Safeway account username. |
| `SAFEWAY_PASSWORD` | Yes (ordering) | Safeway account password. |
| `SAFEWAY_STORE_ID` | Yes (ordering) | Safeway store ID for product searches. |
| `PORT` | Auto | Injected by Railway for the web process. |
| `TAILNET_GUARD_ENABLED` | No (defaults secure) | Kill switch for the network boundary guard (below); the guard is active unless explicitly set to `false`/`0`/`no` (case-insensitive). |
| `TAILNET_GUARD_ALLOWED_CIDRS` | No (defaults secure) | Comma-separated CIDR allow-list for the network boundary guard (below); replaces (does not extend) the default `127.0.0.0/8,::1/128,100.64.0.0/10`. An explicitly-empty value trusts nothing (full lockout, logged at startup) — delete the variable to restore defaults. |

### Network boundary guard (tailnet-only)

Grocery Butler is meant to be reached only over the Tailscale tailnet (or
loopback, locally). A fail-closed `app.before_request` hook, registered by
`create_app()` in `grocery_butler/network_guard.py`, rejects any request
whose **socket peer address** (`request.remote_addr`) falls outside an
allow-list of CIDR ranges.

- **No forwarded-header trust.** Admission is keyed only on
  `request.remote_addr`; the guard never trusts `X-Forwarded-For` (or any
  other client-supplied header), and Werkzeug's `ProxyFix` must never be
  added. Railway's edge is a shared, multi-tenant proxy, so any header it
  forwards from the original client is attacker-controlled input — trusting
  it would let a public request simply set `X-Forwarded-For: 127.0.0.1` and
  walk back through the hole this guard exists to close.
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
    (e.g. clearing the value in the Railway UI instead of deleting the
    variable) is a legitimate "trust nothing" configuration: every
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
  (matched by Flask endpoint name, not path) so Railway's own health
  checks — which arrive from Railway's infrastructure, not the tailnet —
  keep working. This is intentional: both endpoints expose only
  status/DB-connectivity information.
- **Rejection behavior.** Requests from outside the allow-list receive a
  `403`: a JSON `{"error": "forbidden"}` body for `/api/*` paths, an HTML
  error page otherwise.
- **Observability.** At startup the guard logs the resolved allow-list
  (`network_guard_enabled allowed_cidrs=...`), or
  `network_guard_disabled` if the kill switch is set, or
  `network_guard_empty_allowlist` (warning) if the resolved list is
  empty. Every rejection logs
  `network_guard_rejected path=... remote_addr=...` at warning level —
  path and peer address only, never headers or bodies.

#### Verifying the boundary on the live deployment

The guard's correctness rests on one assumption unit tests cannot prove:
that `request.remote_addr`, as seen by gunicorn on Railway's specific
proxy/sidecar topology, actually distinguishes tailnet peers from
public-edge traffic. **Verify this once against the real deployment
before relying on the guard:**

1. Deploy and check the deploy logs for the startup line
   `network_guard_enabled allowed_cidrs=127.0.0.0/8,::1/128,100.64.0.0/10`
   (confirms which allow-list the running process resolved).
2. From a device on the tailnet, load the dashboard and hit
   `/api/v1/inventory` with a valid bearer token — both must succeed. If
   they 403 instead, check the logs for `network_guard_rejected` lines:
   the logged `remote_addr` tells you what address the Tailscale
   sidecar/tailnet hop actually presents (e.g. loopback if the sidecar
   proxies via localhost, or a `100.64.0.0/10` address) so you can
   confirm or correct the allow-list.
3. While the Railway public domain is still attached, request the
   dashboard from a non-tailnet network — it must return 403, and the
   logs must show `network_guard_rejected` with the public-edge
   `remote_addr`. If public traffic is *not* rejected, the logged
   address reveals which allowed range it incidentally lands in.
4. Then remove the public domain (Quick start step 6 below) so the
   guard is defense-in-depth rather than the only line.

**Rate limiting:** flask-limiter is deliberately deferred. With the
fail-closed tailnet boundary above, public HMAC brute-forcing of `/api/v1`
and abuse of paid Claude endpoints (e.g. `/api/v1/meals/parse`) already
require tailnet access, so a limiter would add a dependency without closing
a live gap — and a naive in-memory limiter is ineffective across
per-gunicorn-worker processes without a shared store (no Redis is
provisioned). Revisit if multi-tenant tailnet access is ever introduced.

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

1. Connect your Railway project to this GitHub repo (Railway detects and
   builds the `Dockerfile` automatically).
2. Add a PostgreSQL plugin (provides `DATABASE_URL` automatically).
3. Set the required environment variables above.
4. Railway auto-deploys on push to `main`; each deploy migrates the
   schema before serving traffic (see above).
5. Attach the Railway service to your Tailscale tailnet so RubotPaul can reach
   `/api/v1` privately.
6. In the Railway service's Settings -> Networking, remove the default
   public domain so the app is reachable only through the Tailscale
   sidecar/tailnet. The in-code network boundary guard above is
   defense-in-depth, not a substitute for removing the public domain.

## License

MIT License
