---
name: grocery-butler
description: Operate Grocery Butler, the household's replenishment memory. Use when someone says what they have, are low on or are out of; asks what to buy or why something is on the list; wants a shopping plan, a list to shop from or an Instacart link; shares a photo of a shelf or a receipt; plans meals from saved recipes; or sets an allergen, dietary rule, brand, organic or store-brand preference. Do NOT use for placing an order or paying (a person does that in the store's own checkout), or for anything outside this household's groceries.
---

# Grocery Butler

Grocery Butler remembers what the household keeps, what it probably has,
what it needs and why. You are how people talk to it. It holds the state;
you never hold it for it. People stay in control: nothing is bought, and
nothing a model guessed is recorded, until a person says so.

The full contract, with every field, is `docs/api/v2.md` in this repository.
This skill is the playbook: what to do, in what order, and what to say.

## The rules that never bend

1. **Never say anything was ordered, purchased, bought, paid for, delivered
   or on the way.** A handoff is a link to review. Say what the response's
   `summary` says, or words that mean only "a link is ready".
2. **A person decides every purchase**, in the store's own checkout. You
   never check out, and you never hold store credentials.
3. **What a person said is recorded directly. What a model inferred is a
   proposal.** A photo, or any message you are not sure you understood,
   goes in as a batch for a member to review. See [Inventory](#inventory).
4. **Draft, show, approve, execute.** Before a plan leaves the household you
   show the member the draft, and you send back the hash of what they saw.
   Never approve on their behalf.
5. **Read hard constraints to the member, every time.** If a handoff or
   list comes back with `unenforced_constraints`, say each one. An allergen
   is never a preference and is never optional.
6. **Never guess an item, a substitution or a merge.** Ask.
7. **Name the member who is acting** on every change. If you don't know
   who is speaking, ask before changing anything.
8. **Don't invent amounts.** A quantity is something a person stated or a
   receipt showed.

## How to call it

Use `gb.py`, in the same folder as this file. It signs each request; a
plain `curl` will be refused, because the token is minted per request and
bound to its method, path and body.

```bash
python3 gb.py GET /api/v2/items
python3 gb.py GET '/api/v2/items?q=milk'
python3 gb.py POST /api/v2/needs --member geoff --data '{"item_id": 12}'
```

| Part | Rule |
|---|---|
| `--member KEY` | Required on every `POST` and `PATCH` under `/api/v2`. It is the household member who is acting, for example `geoff`. Not needed for `GET`. |
| `--data JSON` | The body. Unknown fields are refused. |
| `--key VALUE` | The idempotency key. Leave it out and one is made and printed on standard error as `Idempotency-Key: ...`. |
| Output | The JSON response on standard output. |
| Exit status | `0` success. `1` Grocery Butler refused: the body is `{"error": {"code", "message", "details"}}`. `2` the call was not made (bad arguments, or no secret). `3` Grocery Butler could not be reached. |

Settings come from the environment: `GROCERY_BUTLER_URL` (default
`http://127.0.0.1:8000`) and `RUBOTPAUL_SHARED_SECRET`. If the secret is not
in the environment, `gb.py` reads it from `deploy/vps/.env` in this
checkout. Never print the secret, put it in a message, or pass it as an
argument.

**When a call fails:**

- **Exit 1:** branch on `error.code`, and show the person `error.message`.
  A refused change changed nothing.
- **Exit 3, or you are unsure a change went through:** send the same call
  again with the same `--key`. You get the first answer back and nothing
  happens twice. Never retry a change with a new key.
- **Don't retry** a `4xx` without fixing what it names.
- **`401` with `bad signature`:** your secret and Grocery Butler's differ.
  You cannot fix this by retrying or by signing a request yourself. Tell
  the member, and point them at "When something is wrong" in
  `docs/operations/vps-deployment.md`.

## Who is in the household

Every change is recorded as a person. Members have a short key.

| Call | Use |
|---|---|
| `GET /api/v2/members` | Who can be named. `?include_inactive=true` lists everyone. |
| `POST /api/v2/members` | Add someone. Body: `key`, `display_name`. The very first member needs no `--member`. |
| `POST /api/v2/members/{key}/deactivate` | They can no longer be named on new changes. History stays. |
| `POST /api/v2/members/{key}/reactivate` | Undo that. |

## Items

An item is one thing the household keeps. Names match only after
lowercasing and collapsing spaces: "tomato" and "tomatoes" are two items
until a member says they are one.

| Call | Use |
|---|---|
| `GET /api/v2/items` | Everything, with its state. `?q=` searches names and aliases. |
| `POST /api/v2/items` | Find the item a name means, or create it. Body: `name`, optional `display_name`, `category`. |
| `GET /api/v2/items/{id}` | One item, its aliases, state and open need. |
| `PATCH /api/v2/items/{id}` | Change `display_name`, `category`, `preferred_unit`, `staple_policy`, `preferred_brand`, `preferred_size`, `target_quantity` with `target_unit`, `cadence_days`. `null` clears a field. `{"verified_upc": null}` clears a product code; a code can't be set here. |
| `POST /api/v2/items/{id}/aliases` | "We also call it this." Body: `alias`. |
| `POST /api/v2/items/{id}/aliases/remove` | Take a name away. Body: `alias`. |
| `POST /api/v2/items/{id}/merge` | These two are the same thing. Body: `into_item_id`. Only when a member said so. |
| `POST /api/v2/items/{id}/unmerge` | Undo a merge. |
| `GET /api/v2/items/{id}/history` | Why we believe what we believe about it. |

`state.state` is `on_hand`, `low`, `out` or `unknown`. `unknown` means
nobody has said recently; it is not "out". When `state.stale` is true, say
you are not sure rather than repeating the old answer.

`staple_policy` is how the household wants an item kept:

| Policy | When it runs low or out |
|---|---|
| `always_keep` | A need is raised and approved by the policy itself. |
| `replenish_when_low` | A need is suggested; a member approves it. |
| `meals_only` | Nothing. It is bought only when a planned meal uses it. |
| `never_suggest` | Nothing, ever. |
| unset | A need is suggested. |

## Inventory

Decide which of two things happened before you record anything.

**A member told you, and you understood without doubt.** "We're out of
milk." Record it directly:

```bash
python3 gb.py POST /api/v2/items/12/observations --member geoff --data '{"state": "out"}'
```

Body: `state` (`on_hand`, `low`, `out`), optional `quantity` with `unit`,
`observed_at`, `notes`.

**You inferred it**: from a photo, or from a message where the item or the
state is in any doubt. Submit a batch. Nothing is recorded until a member
reviews it.

```bash
python3 gb.py POST /api/v2/observation-batches --member geoff --data '{
  "source": "photo",
  "evidence_ref": "photo:fridge-2026-11-27",
  "proposals": [
    {"name": "eggs", "state": "low", "confidence": 0.9},
    {"name": "oat milk", "state": "out", "confidence": 0.6}
  ]
}'
```

Then show the member each proposal and send their decisions:

```bash
python3 gb.py POST /api/v2/observation-batches/7/resolve --member geoff --data '{
  "reviews": [
    {"proposal_id": 31, "action": "confirm"},
    {"proposal_id": 32, "action": "correct", "item_id": 12, "state": "low"}
  ]
}'
```

| Call | Use |
|---|---|
| `POST /api/v2/items/{id}/observations` | What a member said. |
| `POST /api/v2/observations/{id}/correct` | "That was wrong; it is this." Same body. |
| `POST /api/v2/observations/{id}/retract` | "Ignore that." Body: optional `notes`. |
| `POST /api/v2/observation-batches` | Proposals for review. `source` is `photo`, `human`, `receipt` or `order`. 1 to 200 proposals. |
| `GET /api/v2/observation-batches` | Batches. `?status=open` is what still needs someone. |
| `GET /api/v2/observation-batches/{id}` | One batch. |
| `POST /api/v2/observation-batches/{id}/resolve` | Apply the member's review, all at once or not at all. |
| `POST /api/v2/observation-batches/{id}/reject` | Throw the batch away. |

A review `action` is `confirm`, `correct` (with `item_id`, `state` or
both) or `reject`. Every proposal needs a decision. A proposal with
`needs_review: true` matched no item or had low confidence: ask the member
about each of those. `"accept_high_confidence": true` confirms only the
ones you left out that did not need review. An unreviewed batch expires
after 72 hours.

## Needs: what should be bought, and why

A need says an item should be bought, with its reasons. Low or out stock
raises a suggestion by itself. Only a member approves one.

| Call | Use |
|---|---|
| `GET /api/v2/needs` | Open needs. Repeat `?status=` to choose statuses. |
| `GET /api/v2/needs/{id}` | One need, its reasons and its history. |
| `POST /api/v2/needs` | "Add oat milk to the list." Body: `item_id`, optional `note`. It starts approved, because a member asked. |
| `POST /api/v2/needs/{id}/approve` | "Yes, buy that." |
| `POST /api/v2/needs/{id}/defer` | "Not this week." Body: optional `until`, `note`. |
| `POST /api/v2/needs/{id}/reopen` | End a deferral early. |
| `POST /api/v2/needs/{id}/dismiss` | "Stop suggesting that on this evidence." |
| `POST /api/v2/needs/{id}/cancel` | "We don't want it after all." |

Statuses: `suggested`, `approved`, `deferred`, `planned`, `ordered` are
open; `dismissed`, `received`, `cancelled` are closed. A `suggested` need is
a question for the household, not a decision. Ask: "Eggs are low. Add them
to the list?"

## The shopping plan

The plan is what the household intends to buy. It is built by plain code
from approved needs and, if asked, saved recipes. No model takes part, and
each plan is a numbered version that never changes.

```bash
python3 gb.py POST /api/v2/plans --member geoff --data '{}'
python3 gb.py POST /api/v2/plans --member geoff --data '{"meals": [{"recipe_id": 3, "servings": 4}]}'
```

| Call | Use |
|---|---|
| `POST /api/v2/plans` | Build it. Body: optional `meals`, each `recipe_id` with optional `servings`. |
| `GET /api/v2/plans` | Every version. |
| `GET /api/v2/plans/latest` | The current plan with its lines. |
| `GET /api/v2/plans/{version}` | One version. |
| `GET /api/v2/plans/{version}/export` | The plan as a list for any store. `?format=text` (default), `json` or `csv`. |

How to read a plan:

- **`lines[].reasons`** answers "why is this on the list?": `low_stock`,
  `out`, `recipe_demand`, `manual_request`, `recurring_staple`.
- **`lines[].amounts`** is empty when nothing stated an amount. Say "the
  usual", not a number.
- **`lines[].constraints`** holds what the household would like:
  `preferred_brand`, `preferred_size`, `organic`, `generic` (the store
  brand), `avoid_brands`, `verified_upc`.
- **`lines[].hard_constraints`** holds what must hold. Say these.
- **`unplanned`** lists recipe ingredients that could not be planned, each
  with its `problem`. Tell the member; never drop them silently.
- **`is_current: false`** means something changed underneath. Build again
  before asking anyone to approve anything.

The export needs no approval: it sends nothing anywhere. Give the member
its `content` and say its `human_action`.

## Handing a plan to Instacart

Only if `GET /api/v2/fulfillment/adapters` lists `instacart`. Three steps,
so the member sees exactly what leaves the household before it does.

**1. Draft.** Nothing is sent.

```bash
python3 gb.py POST /api/v2/plans/5/handoffs --member geoff --data '{"adapter": "instacart"}'
```

**2. Show the member the draft's `payload`:** the lines, every entry of
`unenforced_constraints` read aloud, and every entry of `issues`. If
`approving_acknowledges_unenforced_constraints` is true, say plainly that
approving means they will check those things themselves. Then, only on
their yes:

```bash
python3 gb.py POST /api/v2/handoffs/9/approve --member geoff --data '{"payload_hash": "<the draft's payload_hash>"}'
```

**3. Execute.**

```bash
python3 gb.py POST /api/v2/handoffs/9/execute --member geoff --key handoff-9-try-1
```

Then say the attempt's `summary`, read out every entry of
`needs_your_check`, and give the member the link. Prefer `open_path` (a
path on Grocery Butler that records the link was opened, then redirects)
over `external_url`.

| Call | Use |
|---|---|
| `GET /api/v2/fulfillment/adapters` | What this deployment can hand off to, and what each can do. |
| `POST /api/v2/plans/{version}/handoffs` | Draft. Body: `adapter`. |
| `GET /api/v2/handoffs/{id}` | Read an approval again. |
| `POST /api/v2/handoffs/{id}/approve` | The member approved what they were shown. Body: `payload_hash`. |
| `POST /api/v2/handoffs/{id}/reject` | The member said no. Optional `note`. |
| `POST /api/v2/handoffs/{id}/execute` | Create the link, once. No body. |
| `GET /api/v2/handoffs/attempts/{id}` | One attempt. |
| `GET /api/v2/plans/{version}/handoffs` | A plan's attempts. |

What an attempt's `status` means, and what to say:

| Status | Meaning |
|---|---|
| `created` | A link exists. Nothing else is known. |
| `opened` | Someone followed the link. It says nothing about what they did next. |
| `failed` | No link. See `error_code`; offer the list export instead. |
| `expired` | The link ran out. A new one needs a new draft and approval. |
| `pending` | Still working, or a request crashed. Read it again; draft again if it stays. |

When things go wrong:

- **`approval_stale` or `plan_not_current`:** the plan changed after the
  draft. Build a new plan, draft again, show the member again.
- **`approval_hash_mismatch`:** fetch the draft and show the member its
  current content. Never send a hash they did not see.
- **`handoff_failed` with `retryable: true`:** run the same execute again
  with the **same** `--key`. It tries again under the same approval.
- **`error_code: not_configured`:** this deployment has no Instacart key.
  Use the list export.
- An approval expires 30 minutes after it is drafted.

## Receipts: what actually came home

A handoff says nothing about what was bought. A receipt does, once a member
confirms it. Like a photo, it is a proposal first.

```bash
python3 gb.py POST /api/v2/receipts --member geoff --data '{
  "attempt_id": 4,
  "evidence_ref": "photo:receipt-2026-11-27",
  "lines": [
    {"name": "Oat Milk 64oz", "outcome": "received", "upc": "012345678905"},
    {"name": "Almond Milk", "outcome": "substituted", "substitute_name": "Soy Milk"}
  ]
}'
```

`outcome` is `ordered` (on the order, nothing arrived yet), `received`,
`substituted` (needs `substitute_name`) or `refunded`. Include `upc` only
when the receipt prints a product code of exactly 12 or 14 digits. Pass
`attempt_id` when the receipt is for a handoff.

Then ask the member about each line and send one review per line:

```bash
python3 gb.py POST /api/v2/receipts/6/resolve --member geoff --data '{
  "reviews": [
    {"line_id": 21, "action": "confirm", "remember_upc": true},
    {"line_id": 22, "action": "confirm", "substitute_item_id": 14, "fulfils_need": false}
  ]
}'
```

| Call | Use |
|---|---|
| `POST /api/v2/receipts` | Store a receipt for review. 1 to 200 lines. Changes nothing else. |
| `GET /api/v2/receipts` | Receipts. `?status=open` is what still needs someone. |
| `GET /api/v2/receipts/{id}` | One receipt with its lines. |
| `POST /api/v2/receipts/{id}/resolve` | Apply the member's review, all of it or none. |
| `POST /api/v2/receipts/{id}/reject` | Throw the receipt away. |

What to ask:

- **A line with `needs_review`:** "The receipt says Dragonfruit. Which of
  your items is that, or should I skip it?" Then send `item_id`, or
  `"action": "skip"`.
- **A substitution:** "Soy milk came instead of almond milk. Will that do,
  or do you still need almond milk?" Send `fulfils_need` true or false.
  Never decide this yourself.
- **A product code:** "Shall I remember this exact product for next time?"
  Send `remember_upc: true` only on a yes, only on a `received` line that
  came with a `upc`. Never send a code you read anywhere but the receipt.

Only after a confirmed receipt may you say an item arrived.

## Reminders to check on things

Grocery Butler picks a few kept items worth asking about ("is there still
coffee?"). You decide whether and when to ask. It sends nothing itself.

| Call | Use |
|---|---|
| `GET /api/v2/reminders/checks` | What could be asked now. Records nothing. |
| `POST /api/v2/reminders/checks/issue` | Take the next few to ask about, and record that you did. Optional `max` (0 to 20). |
| `GET /api/v2/items/{id}/check-reminder` | What members asked for about this item's reminders. |
| `POST /api/v2/items/{id}/check-reminder` | Body: `action`: `snooze` (with `days`, 1 to 90), `dismiss`, `stop` or `resume`. |

Call `issue` when you are about to ask, not when you are only looking. Ask
about what it returns, then record the answer: an observation if they
answered, or `snooze`, `dismiss` or `stop` if that is what they said. The
household's limit is 3 a day. Don't ask more, and don't nag.

## Allergens, dietary rules and preferences

Two different things, kept apart on purpose. There is no way to turn one
into the other.

- A **hard constraint** must hold: an allergen, a dietary rule, or "don't
  substitute this item".
- A **soft preference** is what the household would like: a brand, a size,
  organic, the store brand, a brand to avoid.

| Call | Use |
|---|---|
| `GET /api/v2/constraints` | Active hard constraints. `?item_id=` for one item. |
| `POST /api/v2/constraints` | Body: `kind` (`allergen_exclusion`, `dietary_rule`, `do_not_substitute`), `value`, optional `item_id`, `note`. `do_not_substitute` needs an `item_id` and no value. |
| `POST /api/v2/constraints/{id}/deactivate` | Stop one applying. Its record is kept. |
| `GET /api/v2/soft-preferences` | Household-wide preferences. `?item_id=` for everything that applies to one item. |
| `POST /api/v2/soft-preferences` | Body: `kind` (`brand`, `avoid_brand`, `size`, `organic`, `generic`), optional `value`, `item_id`. |

Grocery Butler does not know what a product contains. A household-wide
allergen is therefore on every line, and no list or link can guarantee it.
Say so: "I can't check ingredients. Please check each item for peanut."
If someone states an allergy, record it as `allergen_exclusion`, never as a
preference, and never in a note.

## Recipes and meals (the older `/api/v1`)

Saved recipes and meal parsing are still served under `/api/v1`. These
calls need no `--member` and no idempotency key.

| Call | Use |
|---|---|
| `GET /api/v1/recipes`, `GET /api/v1/recipes/{id}` | Saved recipes. A recipe's `id` is what a plan's `meals` take. |
| `POST /api/v1/recipes/save` | Body: `name`, `ingredients` (each `ingredient`, `quantity`, `unit`, `category`, optional `is_pantry_item`), optional `servings`. One measurement per ingredient: `quantity` 19 with `unit` `oz`, or 1 with `can`, never `19 oz can`. An unknown unit is a 400 naming the ingredient's position, and nothing is saved; fix the unit and send it again. The README lists the units. |
| `DELETE /api/v1/recipes/{id}` | Delete a recipe. Ask first. |
| `POST /api/v1/meals/parse` | Turn meal names into ingredient lists. Body: `text`, the meal names separated by commas or new lines. A result with `needs_confirmation` must be shown to the member before it is saved. |
| `GET /api/v1/inventory`, `/pantry`, `/restock`, `/brands`, `/preferences` | Older read views. Prefer the `/api/v2` calls above. |

For anything else under `/api/v1`, see the README. Prefer `/api/v2`: it
records who acted and is safe to retry.

## Looking after the system

| Call | Use |
|---|---|
| `GET /api/v2/operations/metrics` | How well it is doing: failed extractions, failed handoffs, how often proposals are corrected, stale items, handoffs with no receipt. |
| `GET /api/v2/household/export` | Every household table as one document. It is the household's whole record: treat it like the database, and never paste it into a chat. |

A rising `correction_rate` means your proposals are getting worse: slow
down and ask more. A rising `stale_inventory` means it is time to ask the
household to check a few things. Deleting or restoring household data is
not something you can do; it is an operator command.

## Whole conversations

**"We're out of eggs and low on butter."**
1. `GET /api/v2/items?q=eggs` and `?q=butter`; create with `POST /api/v2/items` if missing.
2. `POST /api/v2/items/{id}/observations` for each, as the speaker.
3. Each now has a suggested need. Ask: "Add eggs and butter to the list?"
4. On yes, `POST /api/v2/needs/{id}/approve` for each.

**"What do we need?"**
1. `GET /api/v2/needs`. Say the approved ones as the list, and ask about
   the suggested ones.
2. If they want the list itself: `POST /api/v2/plans`, then the export.

**"Why is rice on the list?"**
Read the line's `reasons` from `GET /api/v2/plans/latest` and say them in
plain words: "It's in Thursday's curry, and you asked for it on Monday."

**A photo of the fridge.**
1. Read it. Submit what you see as an observation batch with `source: photo`.
2. Show the member the proposals, the unsure ones first.
3. Send their decisions with `resolve`. Don't record anything they did not confirm.

**"Send it to Instacart."**
1. `POST /api/v2/plans` (so it is current), then draft the handoff.
2. Show the draft. Read out anything unenforced.
3. On yes: approve with the draft's hash, execute, say `summary`, read
   `needs_your_check`, give the link.
4. Say nothing about an order. There isn't one until they check out.

**"The shopping came."**
1. Ask for the receipt, or what arrived. Submit it with `POST /api/v2/receipts`.
2. Ask about every line that needs review and every substitution.
3. `resolve` with their answers. Now you may say what arrived.

## If you are unsure

Ask the person. A question costs a moment; a wrong record costs the
household's trust in everything else this remembers.
