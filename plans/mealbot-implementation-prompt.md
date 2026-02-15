# MealBot Implementation Prompt

You are building **MealBot**, a meal planning and household inventory system. This is a personal project that will eventually automate grocery ordering from Safeway, but for now we are building the core intelligence: meal decomposition, recipe memory, pantry inventory, ingredient consolidation, and a web dashboard — with a Discord bot as the conversational interface.

## What to Build Now (Phases 1, 2, and 2.5)

Build a fully working system with these components:

1. **Meal Parser** — Claude API integration that decomposes meal names into structured ingredient lists
2. **Recipe Memory** — SQLite-backed persistent store for learned recipes with fuzzy matching
3. **Pantry Inventory** — Household inventory tracker with status lifecycle (on_hand / low / out) and restock queue
4. **Ingredient Consolidator** — Merges ingredients across meals, applies pantry exclusions, appends restock queue
5. **Discord Bot** — Conversational interface with slash commands and natural language inventory updates
6. **Web Dashboard** — Flask app with Jinja2 templates served on localhost, providing a visual interface for recipes, inventory, shopping lists, and brand preferences

## What NOT to Build Yet (Context Only)

The Safeway integration (Phase 3+) is **not being built yet**, but you must **design all data models and interfaces to support it later**. This means:

- Include the `product_mapping` table in the schema (even though nothing writes to it yet)
- Include `search_term` in the consolidated shopping list output (even though nothing searches yet)
- Include `SafewayProduct`, `CartItem`, `CartSummary`, `SubstitutionResult` Pydantic models (even though they're unused)
- The consolidator should produce output that a future Safeway client can consume directly
- The web dashboard should have a "Shopping List" view that shows the consolidated list with a placeholder "Order from Safeway" button that's disabled with a "Coming Soon" tooltip

Here's the Safeway context for when that phase arrives:

<safeway_context>
Safeway uses Okta for authentication. The flow is: POST to Okta's `/api/v1/authn` with username/password to get a session token, exchange for an access token via Okta's `/oauth2` authorize flow, then use the bearer token against Safeway's `nimbus.safeway.com` API.

Known endpoints (from 2019, may have changed):
- Okta base: `https://albertsons.okta.com`
- Okta client ID for Safeway: `ausp6soxrIyPrm8rS2p6`
- Nimbus API base: `https://nimbus.safeway.com`
- Product search: `{NIMBUS_BASE}/api/v2/grocerystore/search?q={query}&storeId={id}&rows=10`
- Coupon/gallery API: `https://www.safeway.com/abs/pub/web/j4u/api/`
- Store ID is required for most product queries

The Safeway client will need to: authenticate, search products, select best match (Claude-assisted with brand preference hierarchy), build cart, handle out-of-stock with substitution flow (never silent — always user approval), compare pickup vs delivery, and submit order.

Product selection uses Claude with this priority: (1) filter out avoided brands, (2) prefer specified brands for ingredient or category, (3) best option by price/quality given user's price sensitivity. Substitutions are ranked by suitability (same cut/form most important, then brand prefs, then price).

Rate limit all Safeway calls to ~2 req/sec. Cache product mappings in `product_mapping` table with `is_pinned` flag for user overrides.
</safeway_context>

---

## Technical Specification

### Project Structure

```
mealbot/
├── app.py                  # Flask web app entry point
├── bot.py                  # Discord bot entry point
├── meal_parser.py          # Claude API integration for meal decomposition
├── recipe_store.py         # SQLite DAL for recipes, pantry, preferences
├── pantry_manager.py       # Household inventory tracking and restock queue
├── consolidator.py         # Ingredient merging and shopping list optimization
├── config.py               # Configuration loading and validation
├── models.py               # Pydantic models for ALL data structures (including future Safeway ones)
├── prompts/                # All Claude prompt templates as .txt files
│   ├── meal_decomposition.txt
│   ├── ingredient_consolidation.txt
│   ├── recipe_matching.txt
│   ├── inventory_intent.txt
│   ├── product_selection.txt       # Future use
│   ├── substitution_ranking.txt    # Future use
│   └── brand_selection.txt         # Future use
├── templates/              # Jinja2 templates
│   ├── base.html           # Layout with nav, shared CSS/JS
│   ├── dashboard.html      # Home: recent meals, restock alerts, quick actions
│   ├── recipes.html        # Recipe list with search/filter
│   ├── recipe_detail.html  # Single recipe view/edit
│   ├── recipe_add.html     # Add new recipe (form or natural language)
│   ├── inventory.html      # Household inventory with status toggles
│   ├── pantry.html         # Pantry staples management
│   ├── shopping_list.html  # Current consolidated shopping list
│   ├── brands.html         # Brand preference management
│   └── preferences.html    # User preferences
├── static/
│   ├── css/
│   │   └── style.css
│   └── js/
│       └── app.js          # Minimal vanilla JS for interactive elements
├── db/
│   └── schema.sql
├── tests/
│   ├── test_meal_parser.py
│   ├── test_consolidator.py
│   ├── test_pantry_manager.py
│   └── test_recipe_store.py
├── .env.example
├── requirements.txt
└── README.md
```

### Database Schema (SQLite)

```sql
CREATE TABLE recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT,
    default_servings INTEGER DEFAULT 4,
    source TEXT,                         -- 'user_described', 'url', 'auto_generated'
    source_url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    times_ordered INTEGER DEFAULT 0
);

CREATE TABLE recipe_ingredients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    ingredient TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit TEXT NOT NULL,
    category TEXT NOT NULL,
    is_pantry_item BOOLEAN DEFAULT FALSE,
    notes TEXT,
    quantity_per_serving REAL NOT NULL
);

-- Pantry staples: static exclusion list.
-- "I always have salt, so don't buy it for me."
CREATE TABLE pantry_staples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingredient TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    category TEXT
);

-- Household inventory: tracks real status of items you care about.
-- Items marked "low" or "out" are the restock queue.
-- KEY RULE: inventory status OVERRIDES pantry staple exclusion.
-- If salt is a pantry staple AND inventory says "out", it gets INCLUDED in the order.
CREATE TABLE household_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingredient TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    category TEXT,
    status TEXT NOT NULL DEFAULT 'on_hand',  -- 'on_hand', 'low', 'out'
    default_quantity REAL,
    default_unit TEXT,
    default_search_term TEXT,
    last_restocked TIMESTAMP,
    last_status_change TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);

CREATE INDEX idx_household_inventory_status ON household_inventory(status);

-- Brand preferences: per-category or per-ingredient.
-- ingredient-level overrides category-level.
CREATE TABLE brand_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_target TEXT NOT NULL,
    match_type TEXT NOT NULL,              -- 'category' or 'ingredient'
    brand TEXT NOT NULL,
    preference_type TEXT NOT NULL,         -- 'preferred' or 'avoid'
    notes TEXT,
    UNIQUE(match_target, brand)
);

CREATE TABLE preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Future use: Safeway product mapping cache
CREATE TABLE product_mapping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingredient_description TEXT NOT NULL,
    safeway_product_id TEXT NOT NULL,
    safeway_product_name TEXT NOT NULL,
    safeway_price REAL,
    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    times_selected INTEGER DEFAULT 1,
    is_pinned BOOLEAN DEFAULT FALSE
);

CREATE INDEX idx_product_mapping_ingredient ON product_mapping(ingredient_description);
```

Pre-populate pantry staples:

```python
DEFAULT_PANTRY = [
    ("salt", "pantry_dry"), ("black pepper", "pantry_dry"),
    ("olive oil", "pantry_dry"), ("vegetable oil", "pantry_dry"),
    ("butter", "dairy"), ("garlic", "produce"),
    ("onion", "produce"), ("sugar", "pantry_dry"),
    ("flour", "pantry_dry"), ("soy sauce", "pantry_dry"),
]
```

### Pydantic Models (models.py)

Define ALL of the following, even the ones not used yet. This is the shared type system.

```python
from pydantic import BaseModel
from enum import Enum
from datetime import datetime

class IngredientCategory(str, Enum):
    PRODUCE = "produce"
    MEAT = "meat"
    DAIRY = "dairy"
    BAKERY = "bakery"
    PANTRY_DRY = "pantry_dry"
    FROZEN = "frozen"
    BEVERAGES = "beverages"
    DELI = "deli"
    OTHER = "other"

class PriceSensitivity(str, Enum):
    BUDGET = "budget"
    MODERATE = "moderate"
    PREMIUM = "premium"

class OrganicPreference(str, Enum):
    YES = "yes"
    NO = "no"
    WHEN_REASONABLE = "when_reasonable"

class FulfillmentType(str, Enum):
    PICKUP = "pickup"
    DELIVERY = "delivery"

class InventoryStatus(str, Enum):
    ON_HAND = "on_hand"
    LOW = "low"
    OUT = "out"

class BrandPreferenceType(str, Enum):
    PREFERRED = "preferred"
    AVOID = "avoid"

class BrandMatchType(str, Enum):
    CATEGORY = "category"
    INGREDIENT = "ingredient"

class SubstitutionSuitability(str, Enum):
    EXCELLENT = "excellent"
    GOOD = "good"
    ACCEPTABLE = "acceptable"
    POOR = "poor"

class InventoryItem(BaseModel):
    ingredient: str
    display_name: str
    category: IngredientCategory | None = None
    status: InventoryStatus = InventoryStatus.ON_HAND
    default_quantity: float | None = None
    default_unit: str | None = None
    default_search_term: str | None = None
    notes: str = ""

class InventoryUpdate(BaseModel):
    ingredient: str
    new_status: InventoryStatus
    confidence: float

class BrandPreference(BaseModel):
    match_target: str
    match_type: BrandMatchType
    brand: str
    preference_type: BrandPreferenceType
    notes: str = ""

class Ingredient(BaseModel):
    ingredient: str
    quantity: float
    unit: str
    category: IngredientCategory
    notes: str = ""
    is_pantry_item: bool = False

class ParsedMeal(BaseModel):
    name: str
    servings: int
    known_recipe: bool
    needs_confirmation: bool
    purchase_items: list[Ingredient]
    pantry_items: list[Ingredient]

class ShoppingListItem(BaseModel):
    ingredient: str
    quantity: float
    unit: str
    category: IngredientCategory
    search_term: str
    from_meals: list[str]
    estimated_price: float | None = None

# --- Future Safeway models (define now, use later) ---

class SafewayProduct(BaseModel):
    product_id: str
    name: str
    price: float
    unit_price: float | None = None
    size: str
    in_stock: bool = True

class SubstitutionOption(BaseModel):
    product: SafewayProduct
    suitability: SubstitutionSuitability
    form_warning: str | None = None
    reasoning: str

class SubstitutionResult(BaseModel):
    status: str
    original_item: ShoppingListItem
    alternatives: list[SubstitutionOption] = []
    selected: SubstitutionOption | None = None
    message: str = ""

class CartItem(BaseModel):
    shopping_list_item: ShoppingListItem
    safeway_product: SafewayProduct
    quantity_to_order: int
    estimated_cost: float

class FulfillmentOption(BaseModel):
    type: FulfillmentType
    available: bool
    fee: float
    windows: list[dict]
    next_window: str | None = None

class CartSummary(BaseModel):
    items: list[CartItem]
    failed_items: list[ShoppingListItem]
    substituted_items: list[SubstitutionResult]
    skipped_items: list[ShoppingListItem]
    restock_items: list[CartItem]
    subtotal: float
    fulfillment_options: list[FulfillmentOption]
    recommended_fulfillment: FulfillmentType
    estimated_total: float
```

### Claude Prompt Templates

Store these as .txt files in `prompts/`. Load them at runtime and format with context.

**prompts/meal_decomposition.txt:**

```
You are a meal planning assistant. Your job is to decompose meals into structured grocery lists.

CONTEXT:
- Default serving size: {default_servings}
- Dietary restrictions: {dietary_restrictions}
- Known pantry staples (DO NOT include these): {pantry_staples}
- Measurement preference: {units} (imperial/metric)

KNOWN RECIPES (use these exactly, adjusting only for serving size):
{known_recipes}

RULES:
1. Output ONLY valid JSON. No preamble, no markdown fences.
2. Use standard grocery quantities (don't say "200g chicken" — say "1 lb chicken thighs"). Think about what the store actually sells.
3. Round up to reasonable purchase quantities. Nobody buys 3 tablespoons of olive oil.
4. If a recipe is in the KNOWN RECIPES list, use those ingredients exactly, adjusting quantities proportionally for serving size.
5. For unknown recipes, generate your best version and flag as "needs_confirmation": true.
6. Distinguish between "purchase_items" (things to buy) and "pantry_items" (things assumed on hand from the pantry staples list).

OUTPUT FORMAT:
{{
  "meals": [
    {{
      "name": "chicken tikka masala",
      "servings": 4,
      "known_recipe": true,
      "needs_confirmation": false,
      "purchase_items": [
        {{
          "ingredient": "chicken thighs, boneless skinless",
          "quantity": 2,
          "unit": "lbs",
          "category": "meat",
          "notes": ""
        }}
      ],
      "pantry_items": [
        {{
          "ingredient": "olive oil",
          "quantity": 2,
          "unit": "tbsp"
        }}
      ]
    }}
  ]
}}
```

**prompts/ingredient_consolidation.txt:**

```
You are a grocery list optimizer. Given ingredient lists from multiple meals, produce a single consolidated shopping list.

RULES:
1. Merge identical ingredients (same item, same form). Sum their quantities.
2. DO NOT merge different cuts/forms of the same protein (chicken thighs ≠ chicken breast).
3. Round up to store-friendly quantities:
   - Produce: whole units (don't buy 0.5 onion)
   - Meat: round to nearest 0.5 lb
   - Dairy: standard container sizes (1 pint, 1 quart, 1 gallon)
   - Spices: only include if not in pantry staples
4. If two meals both need "1 cup heavy cream," buy 1 pint (2 cups).
5. Add a "from_meals" field showing which meals need this ingredient.

Remove pantry staples UNLESS their inventory status overrides:
Pantry staples (exclude by default): {pantry_staples}
Inventory overrides (INCLUDE these even if they're staples): {restock_queue}

ADDITIONAL RESTOCK ITEMS (not from any recipe, but needed):
{restock_items}
Append these to the shopping list as separate line items with from_meals: ["restock"].

OUTPUT FORMAT (JSON only, no markdown):
{{
  "shopping_list": [
    {{
      "ingredient": "chicken thighs, boneless skinless",
      "quantity": 2.5,
      "unit": "lbs",
      "category": "meat",
      "estimated_price": null,
      "from_meals": ["chicken tikka masala", "chicken caesar salad"],
      "search_term": "boneless skinless chicken thighs"
    }}
  ],
  "total_estimated_items": 15,
  "notes": ["Large onion need — consider buying a bag vs individual"]
}}
```

**prompts/recipe_matching.txt:**

```
Is "{query}" the same recipe as any of these?
{recipe_list}

Reply with ONLY the matching name, or "none".
```

**prompts/inventory_intent.txt:**

```
Given this message from the user: "{user_message}"

And the current household inventory:
{current_inventory}

Does this message contain an inventory update? Messages like "we're out of X", "running low on Y", "need more Z", "we have plenty of X" are inventory updates.

Reply with ONLY JSON:
{{
  "is_inventory_update": true,
  "updates": [
    {{
      "ingredient": "parsley",
      "new_status": "out",
      "confidence": 0.95
    }}
  ]
}}

If the message is NOT an inventory update, reply:
{{"is_inventory_update": false, "updates": []}}
```

### Module Specifications

#### meal_parser.py

- Accept a list of meal names (strings)
- For each meal, check recipe memory first using fuzzy matching
- For unknown meals, call Claude API with the meal_decomposition prompt
- For known meals, return stored recipe (pass through Claude for serving size adjustments if needed)
- Recipe name normalization: lowercase, strip articles, normalize possessives, remove punctuation, collapse whitespace
- For near-matches, use Claude with recipe_matching prompt as fallback

#### recipe_store.py

- Full CRUD for recipes table and recipe_ingredients
- CRUD for pantry_staples
- CRUD for preferences
- CRUD for brand_preferences
- Fuzzy recipe lookup using normalized names
- Method to export recipe as JSON (for passing to Claude as context)

#### pantry_manager.py

- CRUD on household_inventory table
- `get_restock_queue()` — returns all items with status "low" or "out"
- `update_status(ingredient, new_status)` — with timestamp tracking
- `mark_restocked(ordered_ingredients)` — after order, move items back to "on_hand" with fuzzy matching
- `parse_inventory_intent(message, current_inventory)` — Claude API call using inventory_intent prompt, returns list of InventoryUpdate
- For low-confidence matches (< 0.8), return them flagged for user confirmation
- Pantry staples vs inventory interaction rule: **pantry staples are excluded from shopping lists UNLESS the inventory table says the item is "low" or "out".** If an item is a staple but not tracked in inventory at all, assume on_hand.

#### consolidator.py

- Takes parsed meals (list of ParsedMeal) + restock queue + pantry staples + inventory state
- Calls Claude API with ingredient_consolidation prompt
- Produces list of ShoppingListItem with `search_term` field (grocery-store-friendly phrasing)
- Does NOT need to call Safeway — just produces the list

#### bot.py (Discord)

Use `discord.py` with async. Single-server, single-authorized-user via `DISCORD_USER_ID` env var.

Slash commands:

| Command | Behavior |
|---|---|
| `/meals <meal1>, <meal2>, <meal3>` | Run meal-to-shopping-list pipeline, post result |
| `/pantry add/remove/list` | Manage pantry staples |
| `/stock` | Show inventory with status indicators |
| `/stock out/low/good <item>` | Update item status |
| `/stock add <item> [category]` | Track a new item |
| `/restock` | Show restock queue |
| `/restock clear` | Clear restock queue |
| `/brands` | Show brand preferences |
| `/brands set <target> <brand>` | Set preferred brand |
| `/brands avoid <brand>` | Add to avoid list |
| `/brands clear <target>` | Remove preference |
| `/recipes list/show/forget` | Recipe management |
| `/preferences` | Show/edit preferences |

Natural language handling: Any non-command message from the authorized user should be passed through Claude's inventory intent parser. If it's an inventory update, act on it and confirm. If confidence < 0.8, ask for clarification. If it's not an inventory update, ignore it.

Clarification dialog: When the meal parser encounters an unknown recipe, enter a multi-turn dialog. Store dialog state per-user in memory with a 5-minute timeout. Accept: rough ingredient lists, URLs, or natural language descriptions.

#### app.py (Flask Web Dashboard)

Serve on `localhost:5000`. Use Jinja2 templates with minimal vanilla JS (no React, no npm build step). Share the same SQLite database as the Discord bot (use proper connection handling — no concurrent writes from both processes without WAL mode).

**Enable WAL mode on the database** so the Flask app and Discord bot can read concurrently:

```python
import sqlite3
conn = sqlite3.connect('mealbot.db')
conn.execute('PRAGMA journal_mode=WAL')
```

**Routes:**

| Route | Template | Description |
|---|---|---|
| `GET /` | dashboard.html | Recent meal plans, restock alerts, quick stats |
| `GET /recipes` | recipes.html | All recipes, searchable/filterable by category |
| `GET /recipes/<id>` | recipe_detail.html | Single recipe with ingredients, edit capability |
| `GET /recipes/add` | recipe_add.html | Form to add recipe (structured form OR natural language box that sends to Claude) |
| `POST /recipes/add` | — | Handle recipe creation |
| `POST /recipes/<id>/edit` | — | Handle recipe edit |
| `POST /recipes/<id>/delete` | — | Delete recipe |
| `GET /inventory` | inventory.html | Household inventory with status toggle buttons (on_hand/low/out) |
| `POST /inventory/update` | — | AJAX endpoint: update item status |
| `POST /inventory/add` | — | Add new tracked item |
| `GET /pantry` | pantry.html | Pantry staples list with add/remove |
| `POST /pantry/add` | — | Add pantry staple |
| `POST /pantry/<id>/remove` | — | Remove pantry staple |
| `GET /shopping-list` | shopping_list.html | Most recent consolidated shopping list |
| `POST /shopping-list/generate` | — | Generate a new shopping list from meal names (form input) |
| `GET /brands` | brands.html | Brand preferences with add/edit/remove |
| `POST /brands/add` | — | Add brand preference |
| `POST /brands/<id>/remove` | — | Remove brand preference |
| `GET /preferences` | preferences.html | User preferences form |
| `POST /preferences` | — | Save preferences |

**Dashboard (dashboard.html) should show:**
- Restock queue alert ("3 items need restocking") with the items listed
- Last meal plan (what meals, what date)
- Quick-add form: "Plan meals" text input that triggers the pipeline
- Recipe count, inventory item count

**Inventory page (inventory.html) should be the star:**
- Each item shown as a card or row with its current status
- Status toggle: clickable buttons or a dropdown to switch between on_hand / low / out
- Status changes should submit via fetch() to `/inventory/update` and update the UI without a full page reload
- Color coding: green for on_hand, yellow for low, red for out
- "Add item" form at the top
- Filter/search bar

**Shopping list page (shopping_list.html):**
- Grouped by category (produce, meat, dairy, etc.)
- Each item shows: ingredient name, quantity, unit, which meals need it
- Restock items visually distinguished (different background or badge)
- Disabled "Order from Safeway" button with "Coming Soon" tooltip
- "Copy to clipboard" button that formats the list as plain text
- "Export as JSON" button

**Design notes for templates:**
- Use a single CSS file. Clean, functional design. Not fancy, but not ugly.
- Mobile-friendly (this will be checked on a phone). Use responsive basics: max-width container, stack on small screens.
- Use system fonts. No Google Fonts dependency.
- Color palette: Keep it simple. Use status colors (green/yellow/red) for inventory. Neutral grays for everything else. One accent color for interactive elements.
- The templates should feel like a lightweight admin dashboard, not a consumer app. Think: Django admin but with better taste.
- Future expansion note: This is designed for localhost now. The templates should use relative URLs and avoid hardcoded localhost references so the app could be deployed to GitHub Pages (static export) or a cloud host later. The Flask routes should have a clean REST-ish structure that could back a static frontend if needed.

### Configuration

`.env.example`:

```
ANTHROPIC_API_KEY=sk-ant-...
DISCORD_BOT_TOKEN=...
DISCORD_USER_ID=...
DATABASE_PATH=mealbot.db
FLASK_PORT=5000
FLASK_DEBUG=true
DEFAULT_SERVINGS=4
DEFAULT_UNITS=imperial
```

Load via `python-dotenv`. Validate required vars at startup. `config.py` should expose a typed config object, not raw env vars.

### Error Handling

- All Claude API calls wrapped in try/except with retry (1 retry, exponential backoff)
- If Claude returns invalid JSON, retry once with a "Your previous response was not valid JSON. Please try again, outputting ONLY valid JSON." appended
- SQLite operations wrapped in transactions where appropriate
- Flask routes return proper HTTP status codes (400 for bad input, 404 for missing recipes, 500 for internal errors)
- Discord commands should always respond to the user, even on failure ("Something went wrong parsing that meal — try again?")

### Testing

Write tests for:
- `test_meal_parser.py` — Mock Claude API responses, verify structured output parsing, test unknown recipe flagging
- `test_consolidator.py` — Test ingredient merging (same item sums, different cuts don't merge), pantry exclusion logic, restock queue appending
- `test_pantry_manager.py` — Test status transitions, restock queue retrieval, inventory override of pantry exclusions
- `test_recipe_store.py` — Test CRUD, fuzzy matching, name normalization

Use `pytest`. Mock the Anthropic client — don't make real API calls in tests.

### Dependencies

```
# requirements.txt
discord.py>=2.3.0
anthropic>=0.40.0
flask>=3.0.0
aiosqlite>=0.19.0
pydantic>=2.5.0
python-dotenv>=1.0.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

### Implementation Order

Build in this exact order. Each step should be fully working before moving to the next.

1. **Schema + models** — Create `db/schema.sql`, `models.py`, and `config.py`. Initialize the database with default pantry staples and default preferences.

2. **recipe_store.py** — Full CRUD. Include fuzzy matching. Write `test_recipe_store.py`.

3. **pantry_manager.py** — Full CRUD, status lifecycle, restock queue. Include the Claude inventory intent parsing (mock it in tests). Write `test_pantry_manager.py`.

4. **meal_parser.py** — Claude integration with prompt templates. Test with hardcoded meals first. Write `test_meal_parser.py`.

5. **consolidator.py** — Wire up meal parser output + pantry/inventory state. Write `test_consolidator.py`.

6. **CLI smoke test** — Before building the Discord bot or web app, create a `cli.py` that lets you run the full pipeline from the terminal:
   ```
   python cli.py plan "chicken tikka masala, caesar salad, carbonara"
   python cli.py stock out "soy sauce"
   python cli.py restock
   ```
   This proves the core works without any UI dependency.

7. **app.py + templates** — Flask web dashboard. Start with the inventory page (most interactive), then recipes, then shopping list, then dashboard.

8. **bot.py** — Discord bot with all slash commands and natural language inventory parsing.

### Quality Standards

- Type hints on all function signatures
- Docstrings on all public methods
- No hardcoded values — everything configurable
- Prompt templates loaded from files, not inline strings
- Database access only through recipe_store.py and pantry_manager.py — no raw SQL in bot.py or app.py
- Pydantic models used for all data boundaries (Claude responses parsed into models, Flask routes accept/return models)
- All async code uses `async/await` properly (no blocking calls in async functions)
