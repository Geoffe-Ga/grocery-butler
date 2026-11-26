# ADR 0012: Hard constraints and soft preferences are different types

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (design delegated in #145; review this before building on it)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) P1.9; issue #145; builds on ADR 0006 and ADR 0011

## Context

What the household must not buy and what it would rather buy are stored the
same way today. Dietary restrictions are one free-text preference that only
the recipe parser reads. Brand rules are prefer or avoid. A fulfillment
adapter (#144) can translate "prefer Clover" into a filter it is free to
ignore. It must never do that with "no peanut products". The backlog asks
that an adapter can't silently weaken a hard constraint, and that a person
reviews whenever one can't be satisfied.

## Decision

1. **Two separate types.** A `HardConstraint` is something that must hold. A
   `SoftPreference` is something the household would like. They are
   different classes, stored in different tables and carried in different
   fields. Nothing converts one into the other.
2. **Hard constraint kinds.** `allergen_exclusion` (a named allergen or
   ingredient that must not be bought), `dietary_rule` (a named rule such
   as vegetarian or kosher), and `do_not_substitute` (this item, exactly as
   asked, or nothing).
3. **Soft preference kinds.** `brand`, `avoid_brand`, `size`, `organic` and
   `generic`.
4. **Scope.** Either type applies to the whole household or to one
   household item (ADR 0006). `do_not_substitute` is always about one item.
5. **A plan line carries its hard constraints.** The plan builder (ADR 0011)
   puts every applicable hard constraint, household-wide and item-specific,
   on each line in its own field. They are part of the plan's content hash:
   adding or removing one makes the current plan stale and the next build a
   new version.
6. **An adapter enforces a constraint or reports it. It never drops it.** An
   adapter declares which hard constraint kinds it can enforce by itself.
   For every other hard constraint on a plan, its handoff result lists the
   constraint as unenforced, for a person to check. The shared contract
   tests hold every adapter to this.
7. **Removing a hard constraint is recorded, not erased.** A constraint is
   deactivated with the actor and time, and stays in the table.
8. **Existing data is kept where it is and read through the new types.**
   - The free-text dietary preference is copied, normalized but not reworded, into one
     household `dietary_rule` by the migration. The original stays, because
     the recipe parser still reads it.
   - An item's preferred brand and size, and the brand rules table, are
     read as soft preferences. Nothing is moved or rewritten.
   - `organic` and `generic` are new and stored in a new table.

## Consequences

### Positive

- An allergen exclusion can't end up as a filter an adapter may ignore.
- The household sees its hard constraints on every plan line and export.
- Changing a hard constraint invalidates approvals built on the old plan.

### Negative

- The migrated dietary rule is whatever text was typed before. It is not an
  allergen exclusion, and no adapter can enforce it. Someone has to enter
  real allergen exclusions by hand; a migration can't guess them.
- Allergen and dietary values are free text ("peanut", "kiwi"), with a list
  of common allergens offered as guidance. An adapter can only enforce
  values it recognizes; the rest are reported as unenforced.
- Soft preferences now come from three places behind one reader. That is
  the cost of not rewriting existing rows.

### Neutral

- Household-wide hard constraints appear on every line, including lines
  they have nothing to do with. That is deliberate: Grocery Butler doesn't
  know what a product contains, so it can't decide which lines an allergen
  matters for.
- The recipe parser's use of the dietary text is unchanged.

## Alternatives considered

- **One table with a `strength` column.** Fewer tables, and a single
  `UPDATE` away from turning an allergen exclusion into a preference.
- **Parse the dietary text into typed constraints during migration.**
  Convenient, and wrong the first time it reads "no nuts except almonds".
- **A closed list of allergens.** Easier to map, but a household can need
  to exclude something no list has.
