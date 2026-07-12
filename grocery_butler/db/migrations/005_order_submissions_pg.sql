-- Migration 005: order_submissions ledger (PostgreSQL).
-- Issue #61: duplicate-order guard for Safeway order submissions.
-- Records every submission attempt (keyed by idempotency_key and a
-- content-only cart fingerprint) so a timeout-then-retry or a re-staged
-- identical cart cannot double-charge the user within the duplicate
-- window. created_at is inserted explicitly (UTC ISO 8601) by
-- OrderSubmissionStore for deterministic cross-backend comparison; the
-- DEFAULT below is a safety net only.

CREATE TABLE IF NOT EXISTS order_submissions (
    id SERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    cart_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,                     -- submitted | confirmed | unknown | failed
    order_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_order_submissions_fingerprint
    ON order_submissions(cart_fingerprint, created_at);
