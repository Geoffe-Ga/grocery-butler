-- Migration 005: shopping_lists / shopping_list_items persistence (PostgreSQL).
-- Issue #65 (HIGH): the generated shopping list used to be stored in the
-- Flask session cookie, which silently truncates past ~4KB and is scoped
-- to a single browser (invisible to other household members). These
-- tables persist the list server-side instead, via ShoppingListStore.
-- from_meals is stored as JSON-encoded TEXT via the stdlib json module,
-- not jsonb, to keep the column portable across both backends.

CREATE TABLE IF NOT EXISTS shopping_lists (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shopping_list_items (
    id SERIAL PRIMARY KEY,
    list_id INTEGER NOT NULL REFERENCES shopping_lists(id) ON DELETE CASCADE,
    ingredient TEXT NOT NULL,
    quantity DOUBLE PRECISION,
    unit TEXT NOT NULL,
    category TEXT NOT NULL,
    search_term TEXT,
    from_meals TEXT NOT NULL                  -- JSON-encoded list of strings
);

CREATE INDEX IF NOT EXISTS idx_shopping_list_items_list_id
    ON shopping_list_items(list_id);
