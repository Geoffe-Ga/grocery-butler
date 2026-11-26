-- Migration 016: versioned shopping plans (ADR 0011, backlog P1.8).
-- A plan version and its lines are written once and never updated. The
-- household has one sequence of versions. Timestamps are ISO 8601 UTC text
-- written by the application; meals, amounts, reasons and constraints are
-- canonical JSON text.

CREATE TABLE IF NOT EXISTS shopping_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER UNIQUE NOT NULL,
    content_hash TEXT NOT NULL,
    meals TEXT NOT NULL DEFAULT '[]',
    unplanned TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shopping_plan_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES shopping_plans(id),
    position INTEGER NOT NULL,
    item_id INTEGER NOT NULL REFERENCES household_items(id),
    display_name TEXT NOT NULL,
    category TEXT,
    amounts TEXT NOT NULL DEFAULT '[]',
    reasons TEXT NOT NULL DEFAULT '[]',
    constraints TEXT NOT NULL DEFAULT '{}',
    need_id INTEGER REFERENCES replenishment_needs(id),
    UNIQUE (plan_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_shopping_plan_lines_plan
    ON shopping_plan_lines(plan_id, position);
