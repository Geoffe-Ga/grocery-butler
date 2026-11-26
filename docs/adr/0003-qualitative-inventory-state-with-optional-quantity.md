# ADR 0003: Qualitative inventory state with an optional quantity

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) "Decisions still required" 4; issue #140; shapes P0.5 (#134)

## Context

Inventory today is qualitative: `on_hand`, `low` or `out`. The observation
ledger (#134) needs to know what an observation holds. A glance, a chat
message or a photo supports a rough state. A receipt, or a person saying "we
have three cans", supports a number. The backlog lists "claiming exact
quantities when evidence only supports `on_hand`, `low`, or `out`" as a
non-goal, and names inventory drift as a reason to pause the product.

## Decision

1. **Every observation records a qualitative state:** `on_hand`, `low`, `out`
   or `unknown`.
2. **An observation may also record a quantity and unit,** but only when the
   number was stated outright by a person or read from authoritative evidence
   such as a receipt. A model never estimates one.
3. **Only the qualitative state drives replenishment.** Needs, plans and
   reminders must not depend on a stored quantity being present or correct.
4. **A quantity ages like any other evidence.** When an item's state goes
   stale and becomes `unknown`, its quantity is no longer shown as current.

Quantities on a shopping plan are a separate matter. They come from recipe
demand and package-size preferences, not from counting stock.

## Consequences

### Positive

- The household never has to log consumption for the system to work.
- Receipt reconciliation (#150) has a place to record what actually arrived.
- The system doesn't present a guess as a count.

### Negative

- The system can't check whether stock on hand covers a recipe, so a plan may
  include something the household already has enough of. Uncertain items are
  surfaced for a quick check instead.
- Two representations exist, and reviews have to watch that the optional one
  doesn't creep into decision logic.

### Neutral

- Adding par-level arithmetic later needs a new ADR that supersedes point 3.

## Alternatives considered

- **State only.** Simplest, but a receipt saying three cans arrived would lose
  the three.
- **Numeric stock with par levels.** Enables sufficiency checks, but drifts
  unless every use is logged, and invites precision the evidence can't
  support.
