-- MealBot database schema
-- All tables use IF NOT EXISTS for idempotent initialization.

CREATE TABLE IF NOT EXISTS recipes (
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

CREATE TABLE IF NOT EXISTS recipe_ingredients (
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
CREATE TABLE IF NOT EXISTS pantry_staples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingredient TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    category TEXT
);

-- Household inventory: tracks real status of items you care about.
-- Items marked "low" or "out" are the restock queue.
--
-- KEY RULE: inventory status OVERRIDES pantry staple exclusion.
-- If salt is a pantry staple AND inventory says "out", it gets INCLUDED in the order.
-- If an item is a pantry staple AND NOT tracked in inventory, assume on_hand.
CREATE TABLE IF NOT EXISTS household_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingredient TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    category TEXT,
    status TEXT NOT NULL DEFAULT 'on_hand',  -- 'on_hand', 'low', 'out'
    current_quantity REAL,
    current_unit TEXT,
    default_quantity REAL,
    default_unit TEXT,
    default_search_term TEXT,
    last_restocked TIMESTAMP,
    last_status_change TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_household_inventory_status
    ON household_inventory(status);

-- Brand preferences: per-category or per-ingredient.
-- ingredient-level overrides category-level.
CREATE TABLE IF NOT EXISTS brand_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_target TEXT NOT NULL,
    match_type TEXT NOT NULL,              -- 'category' or 'ingredient'
    brand TEXT NOT NULL,
    preference_type TEXT NOT NULL,         -- 'preferred' or 'avoid'
    notes TEXT,
    UNIQUE(match_target, brand)
);

CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Future use: Safeway product mapping cache.
-- Nothing writes to this yet; included so the schema is ready for Phase 3.
CREATE TABLE IF NOT EXISTS product_mapping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingredient_description TEXT NOT NULL,
    safeway_product_id TEXT NOT NULL,
    safeway_product_name TEXT NOT NULL,
    safeway_price REAL,
    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    times_selected INTEGER DEFAULT 1,
    is_pinned BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_product_mapping_ingredient
    ON product_mapping(ingredient_description);
