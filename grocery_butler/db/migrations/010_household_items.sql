-- Migration 010: canonical household items (ADR 0006, backlog P0.4).
-- household_items is the identity table; household_item_aliases holds every
-- name an item answers to (its own name included); household_item_events is
-- the append-only audit trail of identity changes. Existing tables gain a
-- nullable item_id, backfilled by this migration's Python hook.

CREATE TABLE IF NOT EXISTS household_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    category TEXT,
    preferred_unit TEXT,
    staple_policy TEXT,
    preferred_brand TEXT,
    preferred_size TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    merged_into_id INTEGER REFERENCES household_items(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS household_item_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES household_items(id),
    alias TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_household_item_aliases_item
    ON household_item_aliases(item_id);

CREATE TABLE IF NOT EXISTS household_item_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES household_items(id),
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_household_item_events_item
    ON household_item_events(item_id);

ALTER TABLE recipe_ingredients ADD COLUMN item_id INTEGER REFERENCES household_items(id);
ALTER TABLE pantry_staples ADD COLUMN item_id INTEGER REFERENCES household_items(id);
ALTER TABLE household_inventory ADD COLUMN item_id INTEGER REFERENCES household_items(id);
