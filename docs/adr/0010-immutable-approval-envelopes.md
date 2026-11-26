# ADR 0010: Approvals are bound to immutable, expiring envelopes

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (design delegated in #137; review this before building on it)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) P0.9 and safety principles 4 and 5; issue #137

## Context

`pending_actions` stages a change and confirms it by id. Nothing ties the
confirmation to the exact content the person saw, to the state it was drawn
from, or, beyond a name, to who confirmed. Issue #120 showed where that
leads: a confirmed order carried values the client supplied at execution
time. The order path is gone (ADR 0001), but shopping plans (#143) and
fulfillment handoffs (#144, #148) are about to need approvals, and the
backlog's acceptance test is blunt: "A changed plan cannot execute under an
earlier confirmation."

## Decision

1. **An approval envelope stores exactly what is being approved.** A draft
   holds the full payload as canonical JSON, its SHA-256 hash, what it is
   about (a subject type, id and version, for example plan 12 version 3),
   and a fingerprint of the state the payload was built from. None of these
   can be changed after the draft is created.
2. **Approving means echoing the hash.** To approve, the caller sends back
   the payload hash it showed the member. A different hash is refused, so
   an approval can only ever apply to the content that was displayed.
3. **An approval is bound to its actor.** The approval records the member,
   the time, and a binding digest over the envelope id, payload hash, actor
   and time. Before anything executes, the stored payload is re-hashed and
   the binding recomputed; a mismatch means the row was altered, and the
   envelope is refused.
4. **Drafts and approvals expire.** An envelope has a deadline, 30 minutes
   by default. Past it, it can't be approved or executed.
5. **A stale envelope is refused.** The service that owns the subject
   supplies the subject's current fingerprint when approving and again when
   executing. If it differs from the one stored at draft time, the envelope
   becomes `stale`: the plan changed, or the needs under it did, and the
   member must be shown a new draft.
6. **Execution is claimed once.** Moving `approved` to `executed` is a
   single conditional update, so two concurrent executions can't both win.
7. **Execution takes nothing from the client.** The executor reads the
   payload from the envelope. A caller can't supply prices, items or
   quantities at execution time.
8. **The only scope is `handoff`.** An envelope approves handing a plan to a
   fulfillment adapter. It never approves a purchase: that approval belongs
   in the retailer's own surface (ADR 0001). No `purchase` scope exists.

`pending_actions` keeps serving `/api/v1`'s brand and preference changes
until v1 is retired. New approvals use envelopes.

## Consequences

### Positive

- What a member approved is exactly what executes, or nothing does.
- A change to the plan or to the needs under it invalidates the approval
  without the approver having to notice.
- Approvals are attributable and tamper-evident.

### Negative

- Any service that wants approvals must define a fingerprint of its
  subject's state and keep it honest. A fingerprint that leaves something
  out lets that something change unnoticed.
- A short deadline means a member who steps away has to be shown the draft
  again. That is the intent, and it costs a second look.
- Two approval mechanisms exist until v1 goes.

### Neutral

- The binding digest detects accidental or casual alteration of a stored
  row. It is not a signature: someone who can rewrite the row can recompute
  it. Signing with a server secret can be added without changing the model.
- The envelope is a mechanism. Plans and handoffs start using it in #143,
  #144 and #148, and the API capability arrives with them.

## Alternatives considered

- **Extend `pending_actions`.** One table, but its rows are confirmed by id
  alone and its payload is mutable JSON; retrofitting immutability under a
  live v1 contract risks both.
- **Approve by subject id and version only.** Simple, but it doesn't catch a
  change in the needs under a plan whose version didn't move, and it doesn't
  prove which content the member saw.
- **Have the client send the payload back for comparison.** Equivalent to
  echoing the hash, with far larger requests and more ways to get
  canonicalization wrong.
