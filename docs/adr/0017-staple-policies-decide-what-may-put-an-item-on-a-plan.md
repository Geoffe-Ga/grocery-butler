# ADR 0017: An item's staple policy decides what may put it on a plan

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (design delegated in #168; review this before building on it)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) P1.7; issue #168; builds on ADR 0003, ADR 0006, ADR 0008 and ADR 0011

## Context

Pantry staples are a flat list: an item is on it or not, and a recipe never
buys a listed item. That can't say "always have coffee", or "buy paprika
only when a dish needs it", or "stop suggesting saffron". The item model
already had a `staple_policy` field with four values and nothing read it.
The backlog asks for plain-language policies with no quantity precision
required, an optional target amount, and existing staples migrating to
"replenish when low".

## Decision

1. **Four policies, one per item, all optional.**

   | Policy | Running low or out | A planned meal that uses it |
   |--------|--------------------|-----------------------------|
   | `always_keep` | Raises a need the policy approves at once | Not bought for the meal; it is kept in stock |
   | `replenish_when_low` | Raises a suggestion a member approves | Not bought for the meal; it is kept in stock |
   | `meals_only` | Raises nothing | Bought for the meal |
   | `never_suggest` | Raises nothing | Not bought, and the plan says so |
   | none | Raises a suggestion (as before) | Bought, unless it is on the old staple list |

   A member can always ask for any item by name, whatever its policy.
2. **`always_keep` is a standing approval.** Choosing it is the household
   saying "buy this whenever it runs low". So its need is approved by the
   system, with a note naming the policy, and cancelled the same way once
   the item is back on hand. A need a member approved themselves is never
   cancelled by the policy. A dismissal still holds.
3. **Policies shape the existing rule; they don't replace it.** The rule in
   `ReplenishmentNeeds.sync_item` (ADR 0008) is unchanged for items with no
   policy. A policy only suppresses the inventory reasons or adds the
   standing approval.
4. **`never_suggest` is reported, not silent.** When a recipe uses such an
   item, the plan lists it under `unplanned` with the reason, so nothing
   disappears from a recipe without a trace.
5. **A target amount is optional and whole.** `target_quantity` and
   `target_unit` are set together or not at all. When nothing else states an
   amount for a line (a need with no recipe behind it), the target is the
   amount on the plan. With no target the line means "buy the usual", as
   before (ADR 0003). `cadence_days` is stored as a hint and drives nothing
   yet.
6. **Package size stays where it is.** The existing `preferred_size` is the
   package-size preference; no second field.
7. **The flat staple list still works and stays consistent.** Migration 020
   gives every listed staple `replenish_when_low`, following a merge to the
   surviving item, and leaves any policy already chosen. Adding a staple
   sets that policy if the item has none. Removing one clears it, unless the
   household chose a different policy on the item itself. Where an item has
   a policy, the policy wins over the list.
8. **A policy change resyncs the need.** Changing an item through the API
   runs the sync, so a suggestion raised under the old policy is withdrawn
   and a low item under `always_keep` is approved, without waiting for the
   next observation.
9. **Item reads tolerate an older schema.** Migration hooks before 020 read
   items through `HouseholdItemStore` while the three new columns don't
   exist yet. The store selects every column and treats the new ones as
   optional. The committed backups from schema 17 restore through this
   path on every test run.

## Consequences

**Easier:**

- The household says once how it wants each staple handled, in words.
- The plan can carry a real amount for a staple without anyone counting.
- Check reminders (#169) get a priority signal: `always_keep` matters most.

**Harder:**

- An `always_keep` item reaches a plan with no person approving that need.
  The plan itself, and any handoff, are still approved by a person, and the
  purchase always is.
- Two places describe staples: the policy and the old list. The policy
  wins, and the list is kept in step, but the list should go once the
  dashboard is rebuilt on items.
- `SELECT *` for items trades a named column list for tolerance of an older
  schema. A dropped or renamed column would now fail at the reader, not the
  query.
- A changed policy changes what a plan's inputs produce, so a plan built
  before the change is no longer current. That is correct, and it means a
  pending handoff approval goes stale.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Keep the flat list and add a second "always keep" list | Two lists, and still no way to say "meals only" or "never". |
| Auto-approve nothing; `always_keep` only sorts suggestions first | Then it does nothing a person can feel. The point of the policy is not being asked. |
| Par levels with a required quantity | Needs a count nobody wants to keep (ADR 0003). The target stays optional. |
| Drop `never_suggest` items from recipes silently | A recipe ingredient vanishing without a note is how a missing dinner ingredient happens. |
| Put the new columns in a separate table | The hooks would hit a missing table instead of a missing column; the same problem with a join added. |
