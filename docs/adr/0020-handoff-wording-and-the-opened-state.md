# ADR 0020: One module words every handoff, and "opened" is the only thing we learn after a link is made

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (design delegated in #149; review this before building on it)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) epic #142; issue #149; builds on ADR 0014, ADR 0015, ADR 0016 and ADR 0018

## Context

A handoff is a link. The old system told the household things it didn't
know, such as that groceries were ordered. Two channels now speak about a
handoff, OpenClaw and the dashboard, and each could drift into saying more
than is true. Instacart's launch checklist (ADR 0014) also fixes the button
text and forbids some words. The backlog asks for honest wording in every
channel, every attempt state rendered, the list export always one tap away,
and counts of how handoffs turn out.

## Decision

1. **All handoff wording is built in `fulfillment/wording.py`.** The
   sentence OpenClaw is given (`summary`), the list of things to check
   (`needs_your_check`), the button text and the "this is not a purchase"
   note come from there. The dashboard template adds only layout.
2. **A list of banned phrases, enforced by test.** "ordered", "purchased",
   "on the way", "bought", "paid", "delivered", "free delivery" and
   "partner" may not appear in any handoff sentence, in the handoff
   template, in a rendered handoff page, or anywhere in a handoff API
   response body. The test checks every attempt state.
3. **The button says "Shop on Instacart."** That is one of the two labels
   Instacart approves. Issue #149 suggested "Review in Instacart"; the
   provider's rule wins.
4. **`opened` is recorded when a member follows the link through our own
   redirect**, `GET /handoff/attempts/{id}/open`. It stores the first time
   and nothing else, then redirects to the link stored on the attempt. The
   destination never comes from the request. The link itself is untouched:
   no parameter is added to it (ADR 0014).
5. **`opened` is a shown status, not a stored one.** Like `expired`, it is
   worked out on reading: a created attempt with an open time is `opened`
   until its link expires, then `expired`.
6. **The dashboard page is read-only.** It shows the latest plan's most
   recent handoff, what to check, the button, and the export. Drafting and
   approving a handoff name a household member, so they stay with OpenClaw
   and `/api/v2` (ADR 0002, ADR 0016).
7. **The list export is always there.** `GET /handoff/export.txt` serves
   the latest plan as text whether or not any other adapter is enabled or
   working.
8. **Handoff counts join the metrics** (ADR 0015): `created`, `opened`,
   `abandoned` (expired without being opened), `reconciled` (a confirmed
   receipt points at it), `failed`, and `corrected_before_handoff` (a draft
   the member rejected, or that went stale because the plan changed first).
   Counts only.

## Consequences

**Easier:**

- Changing how we talk about a handoff is a change to one file, checked by
  one test.
- The dogfood can see whether links get opened and reconciled.

**Harder:**

- `opened` is known only when the member uses our redirect. A link OpenClaw
  pastes directly is never marked opened, and would count as abandoned when
  it expires. OpenClaw should give the member `open_path`, or the metric
  undercounts.
- The redirect changes state on a `GET`. It is idempotent and only ever
  sets a first-open time, but it is a deliberate exception to "changes go
  through `POST`".
- The banned list is English and literal. It catches the phrases we have
  slipped on before, not every way of implying a purchase.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Let each channel word its own messages | That is how "ordered" got said. |
| Store `opened` as a status | Then `expired` and `opened` would fight over one column; both are facts about time. |
| Track opens with a parameter on the Instacart link | Altering the link breaks Instacart's attribution rules (ADR 0014). |
| Let the dashboard draft and approve handoffs | The dashboard has no notion of which member is acting. |
| Skip `opened` | Then "abandoned" can't be told from "used", and the dogfood learns nothing. |
