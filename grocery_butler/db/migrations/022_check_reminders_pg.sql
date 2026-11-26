-- Migration 022: check reminders (ADR 0019, backlog P1.10) (PostgreSQL).
-- check_reminder_state holds, per item, what a household member asked for:
-- stop asking, snooze until a time, or dismiss until new evidence arrives.
-- check_reminder_events is the append-only record of every reminder issued
-- and every such choice, with its actor. Timestamps are ISO 8601 UTC text
-- written by the application.

CREATE TABLE IF NOT EXISTS check_reminder_state (
    item_id INTEGER PRIMARY KEY REFERENCES household_items(id),
    stop_asking BOOLEAN NOT NULL DEFAULT FALSE,
    snoozed_until TEXT,
    dismissed BOOLEAN NOT NULL DEFAULT FALSE,
    dismissed_observation_id INTEGER,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS check_reminder_events (
    id SERIAL PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES household_items(id),
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_check_reminder_events_action
    ON check_reminder_events(action, created_at);
CREATE INDEX IF NOT EXISTS idx_check_reminder_events_item
    ON check_reminder_events(item_id, created_at);
