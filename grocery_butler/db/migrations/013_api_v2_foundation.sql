-- Migration 013: capability API v2 foundation (ADR 0009, ADR 0002).
-- household_members: the named people every v2 mutation is attributed to.
-- api_idempotency_keys: one row per accepted mutation, so a retried request
-- returns the stored response and changes nothing. Timestamps are ISO 8601
-- UTC text written by the application.

CREATE TABLE IF NOT EXISTS household_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_key TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    status_code INTEGER,
    response_body TEXT,
    created_at TEXT NOT NULL
);
