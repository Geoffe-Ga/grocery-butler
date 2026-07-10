-- Migration 004: pending_actions staging table (SQLite).
-- Server-side staging + audit log for destructive actions
-- (e.g. Safeway order submissions) awaiting confirmation.
-- Payload is stored as JSON-encoded TEXT on SQLite.

CREATE TABLE IF NOT EXISTS pending_actions (
    action_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,                    -- JSON-encoded action payload
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | denied | expired
    requester TEXT NOT NULL DEFAULT 'rubotpaul',
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pending_actions_status
    ON pending_actions(status, expires_at);
