# ADR 0008: Explicit replenishment needs, one per item, with first-class reasons

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (design delegated in #135; review this before building on it)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) P0.6; issue #135; builds on ADR 0004, ADR 0006 and ADR 0007

## Context

What to buy is derived on the fly: any inventory row marked low or out is on
the "restock queue", and recipe ingredients are added at list time. Nothing
records that the household looked at a suggestion and approved, snoozed or
dismissed it, or that it was already ordered. An item wanted for two reasons
used to appear twice (#141). The backlog asks for explicit needs with a
state machine, reasons as data, and one need per canonical item.

## Decision

1. **A replenishment need is a row with a status.** `replenishment_needs`
   holds one need for one household item (ADR 0006). Statuses: `suggested`,
   `approved`, `deferred`, `dismissed`, `planned`, `ordered`, `received`,
   `cancelled`. The last three plus `dismissed` are closed; the rest are
   open.
2. **At most one open need per item.** A new reason for an item that already
   has an open need is added to that need. A partial unique index enforces
   it. This is what makes "one item, two reasons" one line.
3. **Reasons are rows.** `replenishment_need_reasons` records each reason's
   kind (`low_stock`, `out`, `recurring_staple`, `recipe_demand`,
   `manual_request`), a source key that says which recipe or which source it
   came from, and a JSON detail. A reason can be withdrawn; it is marked
   inactive, not deleted.
4. **Allowed moves.**

   | From | To |
   |------|----|
   | `suggested` | `approved`, `deferred`, `dismissed`, `cancelled` |
   | `approved` | `planned`, `deferred`, `dismissed`, `cancelled` |
   | `deferred` | `suggested`, `approved`, `dismissed`, `cancelled` |
   | `planned` | `ordered`, `approved`, `cancelled` |
   | `ordered` | `received`, `approved`, `cancelled` |

   `planned` to `approved` is a plan being dropped; `ordered` to `approved`
   is an order falling through. Closed statuses have no moves out. Every
   move is recorded in `replenishment_need_events` with its actor.
5. **Who starts where.** A need whose first reason is a `manual_request`
   starts `approved`: a person asked for it. Every other need starts
   `suggested` and waits for a person.
6. **Inventory drives suggestions, not decisions.** After each observation
   the item's need is synced with its snapshot (ADR 0007): `low` adds a
   `low_stock` reason, `out` adds an `out` reason, `on_hand` withdraws both,
   and `unknown` changes nothing. A `suggested` need left with no active
   reason is cancelled automatically. A need a person already approved,
   planned or ordered is never cancelled by a sync.
7. **A dismissal holds until new evidence.** After a need is dismissed, the
   observation that prompted it does not raise a new suggestion. A later
   observation does.
8. **A snooze ends on its date.** A `deferred` need with a `deferred_until`
   in the past returns to `suggested` at the next sync. New reasons
   arriving meanwhile are recorded on it without waking it.
9. **Received is a statement, not an inference.** Nothing moves a need to
   `received` automatically, including an `on_hand` observation. That
   belongs to reconciliation (#150).

## Consequences

### Positive

- An item wanted for several reasons is one need that lists them all.
- The household's decisions (approve, snooze, dismiss) are remembered.
- Plans (#143) and handoffs (#137, #148) have a stable thing to reference
  and to advance through `planned` and `ordered`.

### Negative

- Needs are a second derived structure to keep in step with the ledger. The
  sync runs after every observation written through `PantryManager`; an
  observation written straight to the ledger must call it too, until API v2
  (#136) gives both one entry point.
- If two items that each have an open need are merged (ADR 0006), the
  survivor briefly has two. `fold_merged_needs` moves the reasons onto the
  older need and cancels the other; the merge capability in API v2 calls it.
- The shopping list still reads the old restock queue. It moves to needs
  with the shopping-plan builder (#143).

### Neutral

- `recurring_staple` is a reason kind now and gains a producer with staple
  policies (#168). `recipe_demand` gains its producer with #143.
- Priority is not modelled yet; the reason kinds (`out` before `low_stock`)
  are enough to order a short list.

## Alternatives considered

- **Keep deriving the queue from inventory status.** No new tables, but no
  memory of approvals, snoozes or orders, and no place for reasons.
- **One need per reason.** Simple writes, and exactly the duplication the
  backlog's acceptance test forbids.
- **Auto-approve inventory-driven needs.** Fewer taps, but it removes the
  person from the decision the product exists to keep with them.
