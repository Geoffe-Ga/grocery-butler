-- Migration 011: append-only inventory observations (ADR 0007, backlog P0.5) (PostgreSQL).
-- Rows are inserted and never updated or deleted. A correction or a
-- retraction is a new row whose supersedes_id names the row it replaces.
-- Timestamps are ISO 8601 UTC text written by the application, so both
-- dialects sort and compare them the same way. The Python hook imports
-- each existing household_inventory row as one 'system' observation.

CREATE TABLE IF NOT EXISTS inventory_observations (
    id SERIAL PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES household_items(id),
    kind TEXT NOT NULL DEFAULT 'observation',
    state TEXT,
    quantity DOUBLE PRECISION,
    unit TEXT,
    source TEXT NOT NULL,
    confidence DOUBLE PRECISION,
    actor TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    evidence_ref TEXT,
    notes TEXT NOT NULL DEFAULT '',
    supersedes_id INTEGER UNIQUE REFERENCES inventory_observations(id)
);

CREATE INDEX IF NOT EXISTS idx_inventory_observations_item
    ON inventory_observations(item_id, observed_at);
