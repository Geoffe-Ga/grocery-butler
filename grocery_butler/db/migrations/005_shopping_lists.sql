-- Migration 005: shopping_lists / shopping_list_items persistence (SQLite).
-- Issue #65 (HIGH): the generated shopping list used to be stored in the
-- Flask session cookie, which silently truncates past ~4KB and is scoped
-- to a single browser (invisible to other household members). These
-- tables persist the list server-side instead, via ShoppingListStore.
-- from_meals is stored as JSON-encoded TEXT via the stdlib json module
-- (portable across SQLite and PostgreSQL).

CREATE TABLE IF NOT EXISTS shopping_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shopping_list_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER NOT NULL REFERENCES shopping_lists(id) ON DELETE CASCADE,
    ingredient TEXT NOT NULL,
    quantity REAL,
    unit TEXT NOT NULL,
    category TEXT NOT NULL,
    search_term TEXT,
    from_meals TEXT NOT NULL                  -- JSON-encoded list of strings
);

CREATE INDEX IF NOT EXISTS idx_shopping_list_items_list_id
    ON shopping_list_items(list_id);
