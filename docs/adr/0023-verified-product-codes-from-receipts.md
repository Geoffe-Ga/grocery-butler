# ADR 0023: A product code is learned only from a receipt line a member confirms

- **Status:** Accepted
- **Date:** 2026-11-27
- **Decider:** Geoff (chose "learned from receipts" on the decision sheet of 2026-11-26; the details below are proposed for review)
- **Source:** Issue #215; builds on ADR 0010, ADR 0011, ADR 0012, ADR 0014 and ADR 0018

## Context

The Instacart link matches a line to a product by searching its name. A
product code (a UPC) matches one exact product instead. The translation
could already send a code, but nothing stored one, so it never did.

A wrong code is worse than no code. A name search that goes wrong shows
something plausible that the member can see is wrong. A wrong code sends
the wrong product with full confidence. So the question is not only where
to keep a code but what is allowed to put one there. The options on the
sheet were to wait for evidence, to let a member type codes in, or to
learn them from receipts.

## Decision

1. **A household item can hold one verified product code**, with who
   verified it and when: `verified_upc`, `upc_verified_by`,
   `upc_verified_at` (migration 026). A code is exactly 12 or 14 digits.
2. **The only way in is a confirmed receipt line.** A receipt line may
   carry the code printed on the receipt (`upc`). When a member confirms
   that line, they may also ask for the code to be remembered
   (`remember_upc`). The code is then saved on the item in the same
   transaction as the confirmation, and the item's history records the
   receipt and line it came from.
3. **Three refusals.** A code is not learned from a skipped line (nobody
   vouched for it), from a line that was only ordered (nothing arrived),
   or from a substituted line (the code belongs to a different product).
   Asking for any of these fails the whole review, like any other invalid
   review.
4. **It is opt-in per line.** Confirming a line does not save its code.
   OpenClaw asks first.
5. **It cannot be typed in.** `PATCH /items/{id}` accepts `verified_upc`
   only as `null`, which clears it.
6. **The code travels on the plan line**, in `constraints.verified_upc`.
   It is part of the plan's content, so part of the hash an approval is
   bound to (ADR 0010, ADR 0011): the member approves a handoff that says
   which lines go by code. An adapter needs no database access to use it.
7. **An adapter that takes codes sends the item by code** and reports any
   filter that the code makes it ignore, as it already did. The list
   export shows the code.

## Consequences

- No code exists until the household's first confirmed receipt carries
  one. The feature costs nothing until then and proves itself or doesn't
  from real receipts.
- It depends on receipts carrying codes. Whether the household's receipts
  do is not known yet. If they don't, nothing is learned and nothing
  breaks; a typed-in path could be added later as its own decision.
- A code can go stale when packaging changes. The member sees `sent by
  UPC` at the handoff, sees the product in the retailer's cart before
  paying, and can clear the code.
- A learned code makes the plan's content differ, so the next plan is a
  new version. A handoff approved before the code was learned is stale,
  which is correct: what would be sent has changed.
- Product codes are sent to Instacart for the lines that have them. They
  identify a product, not a person.
- Two items can end up with the same code. The translation already sends
  it once and reports the other line.
- One item holds one code. A household that buys two sizes of the same
  thing under one item keeps whichever was confirmed last.

## Alternatives considered

- **Wait for dogfood evidence that name matching is a problem.** No
  schema for a guess. This was my lean. Geoff chose to build now, from
  receipts, which is also the option that needs no typing and has the
  strongest evidence.
- **Let a member type a code in.** Works without receipts. Rejected:
  twelve digits per item is a chore, and a typed code is a statement,
  where a confirmed receipt line is a purchase that happened.
- **Save the code automatically on every confirmed line.** Fewer
  questions. Rejected: a receipt reader can misread digits, and a store
  can ring up a near-identical product. A person decides, per line.
- **Give adapters a lookup into the item table.** The plan would stay
  smaller. Rejected: the code would then be outside what the member
  approved, and adapters would need database access they don't have.
