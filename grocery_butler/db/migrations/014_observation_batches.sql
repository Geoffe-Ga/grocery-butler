-- Migration 014: observation batches (ADR 0009, backlog P0.8 and principle 1,
-- "evidence before mutation"). A batch holds what a model or a person
-- proposed about the inventory. Nothing in it reaches the observation
-- ledger until a household member confirms or corrects each proposal.
-- Timestamps are ISO 8601 UTC text written by the application.

CREATE TABLE IF NOT EXISTS observation_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    evidence_ref TEXT,
    notes TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_observation_batches_status
    ON observation_batches(status, expires_at);

CREATE TABLE IF NOT EXISTS observation_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES observation_batches(id),
    candidate_name TEXT NOT NULL,
    item_id INTEGER REFERENCES household_items(id),
    state TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT 'pending',
    decided_item_id INTEGER REFERENCES household_items(id),
    decided_state TEXT,
    observation_id INTEGER REFERENCES inventory_observations(id)
);

CREATE INDEX IF NOT EXISTS idx_observation_proposals_batch
    ON observation_proposals(batch_id);
