-- Migration 017: hard constraints and soft preferences (ADR 0012, P1.9).
-- Two tables on purpose: nothing can turn a hard constraint into a
-- preference by changing a column. A hard constraint is deactivated, never
-- deleted. Plan lines gain a field of their own for hard constraints. The
-- Python hook copies the free-text dietary preference into one household
-- dietary rule. Timestamps are ISO 8601 UTC text written by the application.

CREATE TABLE IF NOT EXISTS hard_constraints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    item_id INTEGER REFERENCES household_items(id),
    note TEXT NOT NULL DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deactivated_by TEXT,
    deactivated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_hard_constraints_item
    ON hard_constraints(item_id);

CREATE TABLE IF NOT EXISTS soft_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    item_id INTEGER REFERENCES household_items(id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_soft_preferences_item
    ON soft_preferences(item_id);

ALTER TABLE shopping_plan_lines ADD COLUMN hard_constraints TEXT NOT NULL DEFAULT '[]';
