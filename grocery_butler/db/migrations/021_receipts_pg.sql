-- Migration 021: receipts and reconciliation (ADR 0018, backlog P2.3) (PostgreSQL).
-- A receipt is what the household says actually happened after a shop: each
-- line was ordered, received, substituted or refunded. Nothing in it changes
-- the inventory or a need until a member confirms it. A receipt may point at
-- the handoff attempt it reconciles. Timestamps are ISO 8601 UTC text
-- written by the application.

CREATE TABLE IF NOT EXISTS receipts (
    id SERIAL PRIMARY KEY,
    status TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    attempt_id INTEGER REFERENCES fulfillment_attempts(id),
    plan_version INTEGER,
    evidence_ref TEXT,
    notes TEXT NOT NULL DEFAULT '',
    purchased_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_receipts_status ON receipts(status);
CREATE INDEX IF NOT EXISTS idx_receipts_attempt ON receipts(attempt_id);

CREATE TABLE IF NOT EXISTS receipt_lines (
    id SERIAL PRIMARY KEY,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id),
    name TEXT NOT NULL,
    outcome TEXT NOT NULL,
    quantity DOUBLE PRECISION,
    unit TEXT,
    item_id INTEGER REFERENCES household_items(id),
    substitute_name TEXT,
    substitute_item_id INTEGER REFERENCES household_items(id),
    notes TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT 'pending',
    fulfils_need BOOLEAN,
    observation_id INTEGER REFERENCES inventory_observations(id)
);

CREATE INDEX IF NOT EXISTS idx_receipt_lines_receipt ON receipt_lines(receipt_id);
