-- Migration 018: operational counters (ADR 0015, backlog P1.16).
-- One row per counter per UTC day. Counts of things that happened and left
-- no other row behind, such as a model reply that could not be parsed.
-- Holds no household content: a name, a day and a number.

CREATE TABLE IF NOT EXISTS operational_counters (
    name TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (name, day)
);
