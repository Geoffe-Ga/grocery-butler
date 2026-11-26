# ADR 0009: Capability API v2 contract: named actors, idempotency keys, coded errors

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (design delegated in #136; review this before building on it)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) P0.8; issue #136; applies ADR 0002

## Context

`/api/v1` grew one endpoint at a time around the retired order flow. Its
errors are free text, a retried write can apply twice, and the only identity
it records is the calling service. OpenClaw now needs to operate the new
core (items, the observation ledger, needs, and later plans and handoffs)
without database access, and the backlog requires that "every mutation is
attributable and retry-safe".

## Decision

1. **A new blueprint at `/api/v2`, beside `/api/v1`.** v1 stays as it is
   until OpenClaw has moved; nothing in v1 changes. v2 is specified in
   [`docs/api/v2.md`](../api/v2.md), which is the contract.
2. **Same transport security as v1.** Every request carries the HMAC bearer
   token bound to its method, path and body, and passes the tailnet guard.
   v2 adds no new credential.
3. **Every mutation names a household member.** The `X-Household-Member`
   header carries a member key from the `household_members` table
   (ADR 0002). A mutation without it, or naming an unknown or inactive
   member, is refused. The recorded actor is `member@channel`, for example
   `geoff@rubotpaul`, so the audit trail shows both who and through what.
   This is attribution, not authentication: the token authenticates the
   channel, and the channel vouches for who is speaking.
4. **Every mutation carries an `Idempotency-Key`.** The server stores the
   key with a hash of the request. Repeating the same request with the same
   key returns the stored response and changes nothing. Reusing a key for a
   different request is an error. Only successful responses are stored: a
   failed request changed nothing, so retrying it runs it again.
5. **Errors are coded.** Every error body is
   `{"error": {"code": ..., "message": ..., "details": ...}}`. The `code`
   is a stable, documented, lowercase identifier a client can branch on.
   The `message` is for a person and may change.
6. **Capabilities, not tables.** Endpoints are named for what OpenClaw is
   doing (propose an observation batch, confirm it, defer a need), and each
   calls one domain service. No business rule lives in the blueprint.
7. **Model inferences arrive as proposals.** A model-derived inventory
   update is submitted as an observation batch that a member confirms,
   corrects or rejects. Only a confirmed proposal is written to the ledger.
   A statement a member made directly may be recorded directly.
8. **v2 lands in slices.** The contract and the first capabilities (members,
   items, state) come first; observation batches and needs next; plans,
   handoffs and receipts arrive with their own domain work (#143, #144,
   #148, #150). The spec marks what is live.

## Consequences

### Positive

- A retry after a timeout can't double-apply a change.
- Every change says which household member made it and through which
  channel.
- OpenClaw can branch on error codes instead of matching text.

### Negative

- Two API versions exist until OpenClaw moves and v1 is retired.
- Idempotency records are stored state that needs pruning; records older
  than 30 days are deleted as new ones are written.
- A member key in a header can be set by anything holding the channel's
  token. That is the trust model ADR 0002 accepted.

### Neutral

- A member is created without an `X-Household-Member` header whenever the
  household has no active member, because there is nobody to name. That is
  the very first member, and also the case where every member has been
  deactivated; otherwise the household would be locked out of its own API.
  The call is attributed to the channel alone.

## Alternatives considered

- **Extend `/api/v1` in place.** No second version, but its error shape and
  missing idempotency can't change without breaking the client that depends
  on them today.
- **Idempotency by natural keys only.** Works for some writes (adding an
  alias twice is harmless) but not for appends such as observations.
- **Member identity inside the bearer token.** Stronger binding, but
  OpenClaw would have to mint tokens per member, and the vendored token
  format is shared with other RubotPaul services.
