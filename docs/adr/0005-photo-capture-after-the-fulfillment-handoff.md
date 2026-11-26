# ADR 0005: Photo capture comes after the fulfillment handoff, by whole shelf, with photos kept until the batch closes

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) "Decisions still required" 2 and 3, and "Recommended delivery sequence"; issue #140; shapes Epic 3 (#162 to #167)

## Context

The backlog orders photo capture (Milestone C, three to five weeks) before
the replenishment loop (Milestone D) and the authorized handoff (Milestone
E). Nothing in the Instacart epic (#142) depends on photos: the observation
ledger accepts evidence from people through chat and the dashboard. Two
further choices block the photo work itself: how long source photos live
(P1.2, #162) and what the first capture flow photographs (P1.3, #163).

## Decision

1. **Sequence.** Photo capture is built after the ERP core, shopping plans
   and the first authorized handoff are working. Until then, inventory
   updates come from chat and the dashboard. The observation ledger (#134)
   still records source, confidence and an evidence reference from the
   start, so photo evidence needs no ledger rework.
2. **Retention.** A source photo is kept only while its evidence batch is
   open. It is deleted when the batch is confirmed or rejected, or when the
   batch expires unreviewed after a short, configured period. Confirmed
   observations never depend on the photo. Each image's deletion is recorded.
3. **First workflow.** The first photo flow is whole-shelf capture: one photo
   of a shelf or fridge area yields many candidates, including items the
   household has not declared.

## Consequences

### Positive

- The first full loop, from needs to an approved handoff, arrives three to
  five weeks sooner, and the first release stores no images.
- Dogfood (#173) shows whether stale inventory is a real problem before the
  photo work is paid for.
- The review screen can show the crop a candidate came from, which keeps
  corrections to one tap.
- A shelf scan can find items nobody thought to declare.

### Negative

- This reorders the backlog's delivery sequence: Milestone C moves after
  Milestone E's first handoff.
- Until photos land, freshness depends on people reporting what is low or
  out. Stale state becomes `unknown` and plans surface it for a check.
- Whole-shelf capture is the hardest version of the vision problem:
  occlusion, duplicate sightings across photos and unknown products arrive
  together. The backlog recommended narrowing the first pilot to 10 to 20
  declared staples; this decision does not.
- Open batches hold photos of the inside of the home until they close, so
  the feature needs storage, an expiry sweep and a delete-now control.

### Neutral

- The backlog's acceptance test for review, a 20-item shelf scan reviewed in
  under 60 seconds, is the check on point 3. If whole-shelf review can't meet
  it, matching against declared staples is the fallback, recorded in a new
  ADR.
- The model must not infer items it can't see. Low-confidence candidates
  become review items, never changes (P1.3).

## Alternatives considered

- **Photos before the handoff (the backlog's order).** Fresher inventory for
  the first plans, at the cost of three to five weeks before any handoff
  works.
- **Drop photo capture from the roadmap.** Smallest product, but it removes
  the step the backlog's target workflow starts with.
- **Delete photos right after extraction.** The smallest privacy footprint,
  but the reviewer can't see what the model saw.
- **Keep photos for a period after confirmation.** Allows auditing a wrong
  inference days later, at the cost of a standing archive of the home.
- **One staple per photo, or shelf photos matched only against declared
  staples.** Easier and more accurate, but the first is barely faster than
  tapping the dashboard and the second misses undeclared items by design.
