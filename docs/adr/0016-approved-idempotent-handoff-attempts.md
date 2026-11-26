# ADR 0016: A handoff to an outside service is drafted, approved, then executed once and recorded as an attempt

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (design delegated in #148; review this before building on it)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) P1.11 and epic #142; issue #148; builds on ADR 0001, ADR 0010, ADR 0012, ADR 0014 and ADR 0015

## Context

The list export sends nothing anywhere, so it needs no approval. The
Instacart adapter sends a household's shopping list to another company and
gets back a link. That must happen only for a plan a member has seen and
approved, only once however often the request is repeated, and it must
never be recorded as more than it is. Approval envelopes exist (ADR 0010)
and nothing used them yet.

## Decision

1. **Three steps.** `draft` stores an approval envelope and sends nothing.
   `approve` is a member echoing the hash of what they were shown.
   `execute` claims the envelope once, calls the adapter and records a
   fulfillment attempt.
2. **The draft is the whole disclosure.** Its payload holds the adapter,
   the plan version and hash, each line with its amounts, every hard
   constraint the adapter can't enforce, and every issue the adapter's
   translation reports. Approving that payload is the member's
   acknowledgement of the unenforced constraints; there is no separate
   checkbox to forget.
3. **Bound to the plan's inputs.** The envelope's fingerprint is the plan's
   content hash. Drafting, approving and executing each compare it with
   what the plan's inputs would produce now. If a need changed, the
   approval is stale and nothing is sent.
4. **One attempt per idempotency key.** The attempt row is inserted, with a
   unique key, before the envelope is claimed and before anything is sent.
   A repeated request finds its attempt and returns it. If claiming the
   envelope fails, the pending row is removed and nothing happened.
5. **One execution per approval.** Claiming an envelope is a conditional
   update, so of several requests with different keys only one sends. A
   second link for the same plan needs a second draft and approval.
6. **A passing fault can be retried under the same key.** If the provider
   was unreachable or rate limited after the bounded retries (ADR 0015),
   the attempt is `failed` with `retryable` true. Repeating the request
   with the same key takes the attempt back with a conditional update and
   sends again, provided the approval has not run out and the plan has not
   changed. Any other failure is final for that approval.
7. **Statuses say only what is known.** `pending`, `created`, `failed`,
   and `expired` (worked out on reading, once a created link's expiry has
   passed). There is no "ordered". A created attempt changes no need and no
   inventory. Only reconciliation evidence does (#150).
8. **Only adapters that send something outside can be drafted for.** An
   adapter whose authorization is `none` is refused and pointed at the
   export.
9. **The provider key lives in one module.** `INSTACART_API_KEY` is read
   from the environment by the registry and handed to `HttpTransport`,
   which sends it in one header to one of two fixed hosts over HTTPS. It
   is not in `repr`, in any error or in any log line. Errors carry a
   stable code and a status, never the request. `INSTACART_ENV` chooses
   `stub` (the default), `development` or `production`; an unknown value or
   a missing key gives a transport that refuses every call. It never falls
   back to a real host.
10. **Tests never call Instacart.** Every test uses a fake transport or a
    fake sender, and a suite-wide fixture replaces the real sender with one
    that fails the test.

## Consequences

**Easier:**

- "Did we send this plan anywhere, when, approved by whom?" has one answer,
  in `fulfillment_attempts` and the envelope it points to.
- OpenClaw can retry a timed-out request without thinking: the same key is
  always safe.
- A second adapter gets approval, idempotency, retries and an audit trail
  by being registered.

**Harder:**

- An attempt left `pending` by a crash stays pending. Its approval is
  spent, so the member approves a new draft. That is the safe direction:
  we never send twice to recover.
- The envelope's 30 minutes covers retries too. A provider outage longer
  than that needs a new approval.
- Three round trips for one link. That is the cost of the member seeing
  exactly what leaves the house.
- `production` is selectable in code before ADR 0014's conditions are met.
  The guard is that nobody has a production key until Instacart approves
  one, which is after Geoff's own read of the terms.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Reuse `pending_actions` | It predates envelopes and binds an approval to an id, not to content. A changed plan could execute under an old approval. |
| One call that approves and executes | The member would approve something they have not been shown, and a retry could not tell "approve again" from "send again". |
| Retry inside the request until it works | Holds a request open through an outage and hides the failure. A recorded, retryable failure is honest and cheap to repeat. |
| Rely on the API's idempotency layer alone | It stores successful responses only, so a failed execute would send again under any key. The attempt row makes the domain idempotent whoever calls it. |
| Mark needs `ordered` when a link is created | A link is not an order. The household may never open it. |
