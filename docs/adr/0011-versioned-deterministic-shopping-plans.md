# ADR 0011: Shopping plans are immutable versions built by a deterministic rule

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (design delegated in #143; review this before building on it)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) P1.8; issue #143; builds on ADR 0004, ADR 0006, ADR 0008 and ADR 0010

## Context

The shopping list today is a transient result: `/api/v1/shopping-list/preview`
computes it from meal names and the old restock queue, sometimes with Claude
merging the lines. Nothing about it can be approved, because it has no
identity that survives the next request. Approval envelopes (ADR 0010) need
a subject with a version and a fingerprint. Fulfillment adapters (#144) need
one stable input. The backlog requires that "the same input state yields a
deterministic plan before optional model-assisted enrichment".

## Decision

1. **A plan is a numbered, immutable version.** `shopping_plans` holds one
   row per version with a hash of its content. `shopping_plan_lines` holds
   its lines. Neither is ever updated. The household has one sequence of
   versions; the latest is the current plan.
2. **A plan is built from two inputs, and no model.** The needs a member
   has approved (ADR 0008), and, optionally, the meals a member asks to
   shop for (ADR 0004). The builder is plain code. The same inputs always
   give the same content and the same hash.
3. **One line per canonical item.** Demand is merged by household item
   (ADR 0006). A line lists every reason it exists: each approved need
   reason and each recipe that wants the item.
4. **Amounts are stated only where something states them.** A recipe states
   an amount; a low-stock need does not (ADR 0003). A line's amounts are the
   recipe demand for the item, summed within a unit dimension (cups with
   gallons, never cups with jars). A line with no recipe demand has no
   amount; it means "buy your usual".
5. **Pantry staples are left out of recipe demand.** An ingredient the
   recipe marks as a pantry item, or whose item is a pantry staple, adds
   nothing on its own. If the household is low on it, that arrives as a
   need, which a member approves like any other. Inventory still overrides
   the staple exclusion, through needs.
6. **Building the same thing twice makes no new version.** If the content
   hash equals the latest version's, the builder returns that version. Any
   change in the inputs produces version N+1; older versions stay readable.
7. **A plan's needs become `planned`.** Storing a version moves the approved
   needs on it to `planned`. A later version that no longer contains a need
   leaves it where it is; needs are closed by people or by reconciliation,
   not by the builder.
8. **A plan's fingerprint is the hash of what the builder would produce
   now.** An approval envelope for a plan is stale when a fresh build from
   the plan's own meals would no longer hash to the plan's content hash.
9. **Lines carry preferences as constraints.** A line copies the item's
   preferred brand and size. Hard constraints (#145) extend this field.

## Consequences

### Positive

- Approvals and adapters have a stable, hashable thing to bind to.
- "Why is this on the list?" is answered by data on the line.
- A model can't change what is on the plan.

### Negative

- Without Claude's merging, near-duplicate ingredient names that are
  different items stay different lines until a member merges the items.
  That is ADR 0006's trade, now visible on the list.
- A need-only line has no amount. The handoff has to say "the usual" or use
  the item's preferred size, and the member settles it at the retailer.
- Recipes saved before items existed are linked by a backfill; an
  ingredient with no item can't be planned and is reported, not dropped.

### Neutral

- `/api/v1/shopping-list/preview`, the dashboard's list and the
  consolidator are unchanged. They are retired with v1.
- Optional model-assisted enrichment (a nicer search term, say) can be added
  later as a step that reads a stored version and never alters it.

## Alternatives considered

- **Keep computing the list on request.** Nothing to migrate, and nothing
  an approval could bind to.
- **A mutable "current plan" row.** Simpler reads, but an approval could
  outlive the content it was given for, which is the defect P0.9 forbids.
- **Let Claude consolidate before storing.** Smoother lists, at the cost of
  a plan that differs between two builds of the same inputs.
