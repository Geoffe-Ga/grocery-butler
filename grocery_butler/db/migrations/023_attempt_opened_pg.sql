-- Migration 023: when a handoff link was first opened (ADR 0020, #149) (PostgreSQL).
-- Set once, when a member follows the link through Grocery Butler's own
-- redirect. It records that the link was opened and nothing about what the
-- member did afterwards.

ALTER TABLE fulfillment_attempts ADD COLUMN IF NOT EXISTS opened_at TEXT;
