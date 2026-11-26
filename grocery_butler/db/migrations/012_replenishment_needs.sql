-- Migration 012: explicit replenishment needs (ADR 0008, backlog P0.6).
-- One open need per household item, with its reasons as rows and every
-- status change recorded. Timestamps are ISO 8601 UTC text written by the
-- application.

CREATE TABLE IF NOT EXISTS replenishment_needs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES household_items(id),
    status TEXT NOT NULL,
    deferred_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_replenishment_needs_one_open
    ON replenishment_needs(item_id)
    WHERE status IN ('suggested', 'approved', 'deferred', 'planned', 'ordered');

CREATE TABLE IF NOT EXISTS replenishment_need_reasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    need_id INTEGER NOT NULL REFERENCES replenishment_needs(id),
    kind TEXT NOT NULL,
    source_key TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TEXT NOT NULL,
    UNIQUE (need_id, kind, source_key)
);

CREATE TABLE IF NOT EXISTS replenishment_need_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    need_id INTEGER NOT NULL REFERENCES replenishment_needs(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_replenishment_need_events_need
    ON replenishment_need_events(need_id);
