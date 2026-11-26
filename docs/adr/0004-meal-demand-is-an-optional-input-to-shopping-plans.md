# ADR 0004: Meal demand is an optional input to shopping plans

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) "Decisions still required" 6; issue #140; shapes P1.8 (#143) and P0.6 (#135)

## Context

Meal parsing and recipe scaling are most of what the application does today,
and the current shopping list starts from a list of meals. The pivot makes
replenishment the core of the product. The shopping-plan builder (#143) has
to say how the two relate. They already collide: an ingredient that is both
in a meal and on the restock queue appears twice (#141).

## Decision

1. **Meal planning stays, as one source of demand among several.** Recipes,
   parsing and scaling keep working.
2. **A shopping plan can be built from replenishment needs alone.** No part
   of the plan builder, the approval flow or a fulfillment adapter may require
   a meal.
3. **Demand is merged by canonical household item, and every reason is
   kept.** An item needed by a meal and by a restock is one plan line with
   two reasons.
4. **Claude remains a parser.** It extracts structure from a meal
   description; the plan builder is deterministic for the same input state.

## Consequences

### Positive

- The product is useful in a week when nobody plans meals.
- Existing meal code and its tests are kept.
- A handoff can carry both staples and recipe ingredients.

### Negative

- The plan builder handles two demand sources and the merge rule between
  them, which is more work than a needs-only builder.

### Neutral

- #141 is fixed by point 3 as part of #135 and #143, not patched separately
  in the consolidator.

## Alternatives considered

- **Meal planning as the central input.** Leans on the most developed code,
  but gives little value without a weekly meal plan and puts Claude's parsing
  in the path of every plan.
- **Freeze meals and build plans from needs only.** A smaller first builder,
  but the first handoff would miss the week's recipe ingredients, and #143
  would need a second pass.
