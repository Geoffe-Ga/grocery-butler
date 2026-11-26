# ADR 0007: Append-only inventory observations and a snapshot derived on read

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (design delegated in #134; review this before building on it)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) P0.5; issue #134; builds on ADR 0003 and ADR 0006

## Context

`household_inventory.status` is overwritten in place. Nothing records who
said an item was low, when, or how sure they were, and a state set months
ago still reads as current. The backlog asks for an append-only ledger, a
current state derived from the latest valid evidence, corrections that keep
history, and freshness: "stale state becomes `unknown`, not silently
`on_hand`."

## Decision

1. **`inventory_observations` is append-only.** Rows are inserted and never
   updated or deleted. Each row names a household item (ADR 0006) and
   records a state, an optional quantity and unit, a source (`human`,
   `photo`, `receipt`, `order`, `system`), a confidence, an actor, when it
   was observed, when it was recorded, an optional evidence reference and
   notes.
2. **A correction is a new row that supersedes an old one.** A correction
   carries its own state and `supersedes_id`. A retraction supersedes a row
   and carries no state ("that was wrong, and I don't know better"). A
   superseded row stays in the ledger and stops counting.
3. **Current state is derived on read, not stored.** An item's snapshot is
   its latest observation, by observed time, that is neither superseded nor
   a retraction. Observations made against an item later merged into
   another count for the surviving item.
4. **State is qualitative (ADR 0003).** An observation states `on_hand`,
   `low` or `out`. `unknown` is never stated; it is what the snapshot
   reports when there is no valid observation or the latest one is stale.
   A quantity is accepted only from `human`, `receipt` or `order` sources,
   and needs a unit.
5. **Freshness is a window per category.** A snapshot older than its item's
   window reports `unknown`, and still says what was last stated and when.
   Starting windows, in days: produce, meat, bakery and deli 7; dairy 10;
   beverages and other 30; frozen and dry pantry goods 60. An item with no
   category gets 30. These are first guesses for dogfood to tune.
6. **Confidence is a number from 0 to 1.** A human statement defaults to 1.
   A photo observation must state one. The snapshot reports it; turning low
   confidence into a review step belongs to the proposal flow (#136, #163).
7. **Existing inventory writes also write the ledger.** While
   `household_inventory` remains what the dashboard and the consolidator
   read, `PantryManager` appends an observation for each status change, and
   the migration imports each existing row as one `system` observation.
   Readers move to the snapshot with replenishment needs (#135).

## Consequences

### Positive

- Every state can say why: who or what said it, when, and how sure.
- A wrong observation is corrected without losing the record of it.
- Old state stops looking current.

### Negative

- Until #135, state lives in two places. The ledger is the complete record;
  `household_inventory.status` is the copy older readers use. The ledger
  can report `unknown` for something the old table still shows as on hand.
- Deriving on read costs a query per request. For one household this is
  small; a materialized snapshot can be added later without changing the
  ledger.
- The freshness windows are guesses. Too short and the household is asked
  to recheck constantly; too long and stale state misleads plans.

### Neutral

- The backlog's `InventorySnapshot` is described as materialized. Here it is
  a computed view with the same content.
- Actors on these writes are `system` until API v2 (#136) passes the
  household member through (ADR 0002).

## Alternatives considered

- **Keep overwriting, add an audit table.** Cheaper, but the audit trail and
  the state could disagree, and freshness would still need the ledger's
  timestamps.
- **Materialize the snapshot in a table.** Faster reads, with a second copy
  to keep consistent on every insert, correction and merge.
- **Let stale state decay to `low` instead of `unknown`.** It would nudge
  replenishment, but it invents a state nobody observed.
