-- Migration 008: pending_actions resolver column (SQLite).
-- Issue #75 (W1): confirm/deny routes must record which caller resolved
-- a staged action, distinct from the requester who staged it. Nullable
-- because system-initiated resolutions (TTL expiry) have no resolving
-- caller.

ALTER TABLE pending_actions ADD COLUMN resolver TEXT;
