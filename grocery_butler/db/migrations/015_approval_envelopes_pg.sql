-- Migration 015: approval envelopes (ADR 0010, backlog P0.9) (PostgreSQL).
-- An envelope stores exactly what a household member is asked to approve.
-- Its payload, hash, subject and fingerprint are written once and never
-- updated; only the status and the approval fields change. Timestamps are
-- ISO 8601 UTC text written by the application.

CREATE TABLE IF NOT EXISTS approval_envelopes (
    id SERIAL PRIMARY KEY,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id INTEGER NOT NULL,
    subject_version INTEGER NOT NULL,
    subject_fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    approval_binding TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution_note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_approval_envelopes_subject
    ON approval_envelopes(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_approval_envelopes_status
    ON approval_envelopes(status, expires_at);
