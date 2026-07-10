-- Migration 004: pending_actions staging table (PostgreSQL).
-- Server-side staging + audit log for destructive actions
-- (e.g. Safeway order submissions) awaiting confirmation.

CREATE TABLE IF NOT EXISTS pending_actions (
    action_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | denied | expired
    requester TEXT NOT NULL DEFAULT 'rubotpaul',
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_pending_actions_status
    ON pending_actions(status, expires_at);
