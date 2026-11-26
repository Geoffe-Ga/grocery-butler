# ADR 0018: What arrived is recorded from a receipt a member confirms

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (design delegated in #150; review this before building on it)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) P2.3; issue #150; builds on ADR 0001, ADR 0007, ADR 0008, ADR 0014 and ADR 0016

## Context

A handoff creates a link. Nothing comes back from Instacart (ADR 0014), so
the system can't know whether anything was bought, swapped or refunded. The
old design marked inventory restocked when an order was submitted, which is
how it came to claim purchases it had no evidence for (#97, #117). The
backlog asks that received state change only after confirmation or
authoritative evidence, and that ordered, substituted, refunded and received
stay distinguishable.

## Decision

1. **A receipt is a proposal.** `POST /receipts` stores lines and changes
   nothing: no observation, no need. It is the same shape as an observation
   batch (ADR 0009): submitted by anyone, applied only by a member's
   review, all at once or not at all.
2. **Four outcomes per line**, and they stay four:

   | Outcome | On confirmation: inventory | On confirmation: the item's need |
   |---------|----------------------------|----------------------------------|
   | `ordered` | Nothing | Moves to `ordered` |
   | `received` | The item is on hand, source `receipt` | Moves to `received` |
   | `substituted` | The substitute is on hand, source `receipt` | `received` if the member says it will do, else back to `approved` |
   | `refunded` | Nothing | Back to `approved`: still wanted |

3. **A substitution always needs a person.** Whether almond milk will do
   for oat milk is the household's call, so the review must say
   `fulfils_need` true or false. The substitute's amount is not copied from
   the line, because that amount was for the item that didn't come.
4. **Matching never creates an item.** Receipt wording is matched by alias.
   A line that matches nothing waits in review; a member names the item or
   skips the line. A receipt with an unresolved line can't be confirmed, and
   then nothing in it is written.
5. **A need walks forward one allowed step at a time.** A receipt that says
   an item arrived moves its need through the steps ADR 0008 allows up to
   `received`, each recorded with the receipt's number. Evidence of
   receipt is also evidence of an order, so no step is skipped or forged.
   An item bought with no open need is simply on hand.
6. **A receipt may point at a handoff attempt**, and takes the attempt's
   plan version. It doesn't have to: a shop from a paper list reconciles the
   same way.
7. **A handoff nobody reconciled is a gap.** The `reconciliation_gaps`
   metric (ADR 0015) now also counts created handoffs older than three days
   that no confirmed receipt points at.
8. **Provider-neutral, and no model in this module.** The lines arrive
   structured. Reading a photo or an email into lines is OpenClaw's job,
   and its result is still only a proposal.

## Consequences

**Easier:**

- "Did it arrive?" has an answer backed by who confirmed it and what
  evidence they pointed at.
- A refund or a rejected substitute puts the item back on the next plan
  without anyone remembering to ask.
- The dogfood can measure reconciliation honestly.

**Harder:**

- Reconciling is work the household has to do. If it is skipped, needs sit
  at `planned` and the gap metric climbs. That is the truth, shown.
- A need can now reach `ordered` and `received`, which nothing did before.
  Anything that assumed those states never happen must be checked.
- Partial quantities aren't modelled: a line is received or not. Two of
  three bags arriving is one `received` line and a note.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Reuse observation batches with source `receipt` | A batch can say "on hand". It can't say ordered, refunded, or "this came instead of that", and it knows nothing of needs. |
| Mark needs `ordered` when a handoff is created | A link is not an order (ADR 0016). |
| Auto-confirm lines that match an item exactly | A receipt read by a model is an inference. "Evidence before mutation" has no exact-match exception. |
| Let a receipt create unknown items | One misread line would add a permanent item. A member naming the item costs seconds. |
| Read orders from the provider | The documented API has no order or receipt access (ADR 0014). |
