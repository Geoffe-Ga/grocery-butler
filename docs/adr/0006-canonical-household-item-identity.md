# ADR 0006: Canonical household items, with aliases and reversible merges

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (design delegated in #133; review this before building on it)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) P0.4; issue #133

## Context

Recipe ingredients, inventory rows, pantry staples and brand preferences are
joined by free-text names. "Peanut butter" and "peanut  butter" are two
things, and nothing can carry a staple policy or a preferred brand for "the
peanut butter we buy". The observation ledger (#134), replenishment needs
(#135) and shopping plans (#143) all need one stable id per thing the
household keeps. The backlog asks for aliases "without silently merging
distinct products" and for merge and unmerge "with audit history".

## Decision

1. **`household_items` is the identity table.** A row has a stable integer
   id, a normalized unique `name`, a `display_name`, a category, a preferred
   unit, a staple policy, a preferred brand and size, and an `active` flag.
   One household, so no household column (ADR 0002).
2. **Every name an item answers to is a row in `household_item_aliases`,**
   including its own name. An alias is globally unique, so a piece of text
   resolves to at most one item. Resolution is one alias lookup.
3. **Normalization is deliberately weak:** lowercase, trim, collapse
   whitespace. No stemming, plural folding or fuzzy matching. "Tomato" and
   "tomatoes" stay two items until a person adds an alias or merges them.
4. **Unknown text creates an item; it never guesses.** `resolve_or_create`
   makes a new item when no alias matches. Adding an alias that already
   belongs to another item is an error, not a merge.
5. **A merge is a pointer, not a rewrite.** Merging item A into item B sets
   `A.merged_into_id = B` and deactivates A. Nothing that references A is
   rewritten; readers follow the pointer to the canonical item. Unmerge
   clears the pointer and reactivates A, which restores the earlier state
   exactly. Only a canonical item can be a merge target, so chains don't
   form.
6. **Every identity change is an event.** `household_item_events` is
   append-only and records created, updated, alias added or removed, merged
   and unmerged, each with an actor and a JSON detail.
7. **Existing rows are linked, not replaced.** `recipe_ingredients`,
   `pantry_staples` and `household_inventory` gain a nullable `item_id`. The
   migration backfills it from each row's name, and the write paths set it
   from then on. The text columns stay for now; later issues move reads onto
   the id.
8. **Staple policy is a column here and behaviour later.** The four
   qualitative values from P1.7 are defined now (`always_keep`,
   `replenish_when_low`, `meals_only`, `never_suggest`), default unset.
   `pantry_staples` remains the rule the consolidator uses until #168.

## Consequences

### Positive

- One id per thing, which the ledger, needs and plans can reference.
- A wrong merge costs one action to undo and loses nothing.
- Spelling variants are handled by data (aliases) that a person approved.

### Negative

- Weak normalization means more duplicate items at first, and someone has to
  merge them. That is the price of never merging two real products by
  accident.
- Readers must resolve the canonical item instead of trusting a stored id.
  The store offers one method for that.
- Two rows of an old table can point at items that are later merged, so
  those tables can hold two rows for one canonical item until the ledger
  (#134) replaces them.

### Neutral

- Brand preferences still match by text; moving them onto items belongs to
  the preference work (#145).
- Model-suggested aliases (photo resolution, #164) must go through the same
  explicit `add_alias`, after confirmation.

## Alternatives considered

- **Rewrite references on merge.** Simple reads, but unmerge would need a
  record of every row moved, and a mistake is hard to reverse.
- **Aggressive normalization (singularize, strip brand words).** Fewer
  duplicates on day one, at the cost of silently joining distinct products.
- **Use the inventory row as the identity.** No new table, but items that
  appear only in recipes or plans would have no identity, and inventory
  state would be tangled with naming.
