-- Migration 019: fulfillment attempts (ADR 0016, backlog P1.11 and epic #142).
-- One row per try at handing an approved plan to an adapter. A row says a
-- handoff was created or failed. It never says anything was bought.
-- idempotency_key is unique, so a repeated request finds its own attempt
-- instead of sending again. Timestamps are ISO 8601 UTC text written by the
-- application.

CREATE TABLE IF NOT EXISTS fulfillment_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    envelope_id INTEGER NOT NULL REFERENCES approval_envelopes(id),
    adapter TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    plan_content_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    external_url TEXT,
    link_expires_at TEXT,
    error_code TEXT,
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    human_action TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fulfillment_attempts_plan
    ON fulfillment_attempts(plan_version);
CREATE INDEX IF NOT EXISTS idx_fulfillment_attempts_envelope
    ON fulfillment_attempts(envelope_id);
