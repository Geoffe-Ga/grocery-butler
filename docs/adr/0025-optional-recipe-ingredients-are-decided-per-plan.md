# ADR 0025: An optional recipe ingredient is decided by a member, per plan, before the plan is stored

- **Status:** Accepted
- **Date:** 2026-11-27
- **Decider:** Geoff (asked for the behaviour on 2026-11-27; the design below was delegated, so review it before building on it, and see "Open questions")
- **Source:** Geoff's request of 2026-11-27 for first-class optional recipe ingredients; builds on ADR 0004, ADR 0009, ADR 0010, ADR 0011 and ADR 0017

## Context

Every saved recipe ingredient was required. A recipe could not say "chips
go with this, if you want them". The household's sandwiches sometimes come
with Original Ruffles, sometimes with Cheddar Cheese Ruffles, and sometimes
with neither; its quesadillas sometimes come with salsa and chips. Saving
those as ordinary ingredients buys them every time. Leaving them out loses
them. Either way the system would be deciding something only a person
knows, which is what "Claude is a parser, not a planner" and "evidence
before mutation" exist to prevent.

Three things constrain the answer:

- A plan is an immutable version, and its content hash is what an approval
  is bound to (ADR 0010, ADR 0011). A plan must be rebuildable from its own
  inputs, or it can never be told current from stale.
- A change through `/api/v2` is idempotent and stores only successes
  (ADR 0009). A refused request changes nothing and may be retried.
- Quantities and units are never invented. `/api/v1/recipes/save` already
  refuses a unit it does not know.

## Decision

1. **An ingredient is required or optional.** `recipe_ingredients` gains
   `is_optional BOOLEAN NOT NULL DEFAULT FALSE` (migration 027), and
   `Ingredient` gains `is_optional`, default `False`. Every existing row
   and every client that does not send the field means "required", as
   before. An optional ingredient keeps every ordinary field and the same
   strict unit and quantity rules.
2. **Only a confirmed save marks an ingredient optional.** That is
   `/api/v1/recipes/save` today. The meal parser never sets it: whether
   something is optional is not for a model to say.
3. **The builder never decides an optional ingredient.** `POST /plans` for
   a recipe with optional ingredients must carry, on that meal, one
   decision per optional ingredient: `{"ingredient_id", "include"}`.
   Without them the build is refused.
4. **The refusal is the confirmation requirement.** It is a 409
   `optional_decisions_required` whose `details.meals` lists, by meal,
   each optional ingredient with its id, name, category, notes and the
   amount the recipe states, scaled to the servings asked for. The same
   request always produces the same requirement.
5. **Nothing is stored until the decisions are complete.** There is no
   pending or draft plan row. A plan that was never confirmed does not
   exist, so it cannot become the current plan, be approved, or be handed
   off. No need moves to `planned`.
6. **Decisions answer one exact question.** Each meal's requirement
   carries an `optional_token`: a hash of the recipe id, the servings and
   every optional ingredient as shown. Decisions must come back with it.
   If the recipe or the servings changed in between, or the token is
   missing, the answer is 409 `optional_decisions_stale` with the current
   requirement, and the member is asked again.
7. **Bad decisions are refused, never repaired.** A decision for an
   ingredient that is not one of the recipe's optional ingredients, or a
   second decision for the same ingredient, is 400
   `optional_decisions_invalid`. `include` must be a JSON boolean. One kind
   of problem is reported at a time, in a fixed order: duplicate, stale,
   unknown, missing.
8. **After confirmation, included means required.** An included optional
   ingredient goes through exactly the code a required one does: the
   pantry-item rule, the item's staple policy (ADR 0017), merging by
   canonical item, amounts, soft preferences, hard constraints, the
   export and every handoff. An excluded one adds nothing and is not on
   the plan.
9. **The decisions are part of the plan.** They are stored in the plan's
   `meals`, sorted by ingredient, and so are part of its content hash. The
   token is not stored. A meal with no decisions serializes exactly as it
   did before, so every plan stored before this change keeps its hash and
   stays current.
10. **A stored plan goes stale when its question changes.** Rebuilding a
    plan's own meals to fingerprint it (ADR 0011) checks its recorded
    decisions against the recipe's optional ingredients now. If one was
    added or removed the plan reads `is_current: false`; reading it never
    fails.
11. **Each plan is asked afresh.** No decision is remembered as a default
    for the next plan.
12. **The older shopping list refuses rather than guesses.** The
    `Consolidator`, behind `/api/v1/shopping-list/preview`, the dashboard
    form, the CLI's `plan` and the retired bot, raises when a meal still
    carries an optional ingredient. The v1 preview answers 409 and the
    caller resolves each one in the meal it sends. The CLI takes
    `--include-optional` and `--exclude-optional`. The dashboard form and
    the bot, which cannot ask, say what is undecided and make no list.

## Consequences

### Positive

- The system cannot buy, or silently skip, something only a person can
  decide. The question reaches the member with real amounts.
- No new table, no new route, no expiry job. The confirmation step reuses
  the contract's own rule that a refusal changes nothing.
- The flow is safe to retry at every point: asking twice gives the same
  requirement; confirming twice gives the same plan version.
- Plans, approvals and handoffs made before this change are untouched.

### Negative

- Planning a meal with optional ingredients costs two requests and one
  question to a person, every time.
- The requirement arrives as an error status. A client that treats every
  4xx as a failure must learn this one code.
- A stored plan's decisions name ingredient rows. Replacing a recipe's
  ingredients gives them new ids, so that plan reads as stale even if
  nothing a person would notice changed. Stale is the safe direction.
- The dashboard cannot plan a meal that has optional ingredients, and its
  add-recipe form cannot mark one. Both wait on the form rewrite in #260.

### Neutral

- An optional pantry item is still asked about, and, like a required
  pantry item, adds nothing to the plan when included.
- The household export and restore (ADR 0013) carry the new column
  without change, because they read the schema.

## Open questions

- **Either/or.** "Original Ruffles or Cheddar Cheese Ruffles" is modelled
  as two independent optional ingredients. A member can include neither,
  either, or both; the server does not know they are alternatives, and
  OpenClaw's skill is told to ask about them together. If "both" must be
  impossible, or one choice among several must be required ("pick a
  bread"), that needs a group on the ingredient rows (a group key and a
  rule such as "at most one"). The backlog does not decide this. The
  simpler model was built because it satisfies every scenario given and a
  group can be added later without changing how a single optional
  ingredient is decided.
- **Remembered answers.** The household may come to want "we always take
  the salsa". That would be a standing preference, with its own audit
  trail, and is a separate decision.

## Alternatives considered

- **A stored draft plan with a confirm call.** `POST /plans` would store a
  pending row and return its id; a second route would confirm it. It
  reads as a classic draft, confirm, execute. Rejected for now: it needs a
  table, a route, an expiry rule and a second idempotency story, and a
  stored draft is one more thing that could be mistaken for a plan. The
  stateless refusal gives the same guarantee, that nothing unconfirmed is
  ever current, with nothing to clean up. If drafts are later wanted for
  other reasons (editing a plan before it is stored, say), this decision
  does not block them.
- **Return the requirement as a 2xx.** Friendlier to naive clients.
  Rejected: a successful mutation is stored under its idempotency key, so
  the "question" would be replayed to a retry that now carries answers,
  and a 2xx from `POST /plans` would no longer mean "this is the plan".
  `reviews_missing` on observation batches already sets the precedent of
  a 409 that lists what still needs a decision.
- **Include by default, or exclude by default.** One request instead of
  two. Rejected: it is exactly the silent assumption the request rules
  out.
- **Decide by ingredient name instead of id.** Nicer to type. Rejected for
  the API: two optional ingredients of one recipe could share a name, and
  an id from the requirement cannot be mistyped into a different
  ingredient. The CLI, which has no requirement to read ids from, does
  match by name and refuses a name it does not find.
- **Let the model that parses a recipe mark ingredients optional.**
  Rejected: it would let an inference change what gets asked and bought.
