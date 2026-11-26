# ADR 0002: Single-household pilot with named members and no login

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) "Decisions still required" 1 and 7; issue #140

## Context

The ERP core (#133 to #137) is about to add its main tables: household items,
observations, replenishment needs, shopping plans and approval envelopes.
Each one either carries a household boundary or doesn't, and each mutation
records an actor.

Today there is one deployment, on Railway, reachable only over the tailnet.
The only identity is the `caller_id` inside the `/api/v1` HMAC token, which is
in practice always RubotPaul. The dashboard has no identity at all.
`pending_actions` already stores a `requester` and a `resolver`, and both
default to that caller. The backlog gates multi-tenancy (P2.6) behind pilot
retention, and its pilot gate asks for 90% of state changes to carry
provenance and actor attribution.

## Decision

1. **The first production release serves one household.** New tables carry no
   `household_id`. Tenant isolation, accounts and billing stay out of scope
   until the backlog's gate for P2.6 is met.
2. **People are named household members, without login.** A small members
   table holds the people in the household. Every mutation records a member
   as its actor, next to the channel it came through.
   - OpenClaw states which member is speaking on each `/api/v1` mutation.
   - The dashboard asks who is using the device once and remembers it.
   - The CLI takes the member from a flag or an environment variable.
3. **A member name is attribution, not authentication.** The tailnet guard
   and the HMAC bearer token remain the security boundary. No permission
   check may rely on the member alone.

## Consequences

### Positive

- The ERP core starts without auth or tenancy work in front of it.
- Observations and approvals say who made them, which meets the pilot's
  attribution gate.
- The members table is what accounts and roles (P2.4) would later attach to.

### Negative

- Supporting a second household later means a migration that adds a household
  scope to every table. For a one-household database that is a column with a
  default; the real cost is the isolation and auth work that comes with it.
- Anyone on the tailnet can act under any member's name. This is acceptable
  because approval to hand off is not approval to purchase: checkout still
  happens in the retailer's own surface.
- Attribution through OpenClaw is only as good as OpenClaw's report of who is
  speaking.

### Neutral

- Store methods should take their scope and actor as explicit arguments, so a
  household scope can be threaded through later without a redesign.

## Alternatives considered

- **One household with a `household_id` seam on new tables.** Cheap now, but a
  column nothing enforces can be mistaken for isolation, and every query and
  test would carry a parameter with one possible value.
- **Multi-tenant now.** The backlog sizes it XL and gates it; it would also
  mean leaving the tailnet-only model before the product is proven at home.
- **Channel-only attribution.** Nothing to build, but the audit trail could
  never say who approved a handoff.
- **Accounts and roles now (P2.4).** One to two weeks of auth work and login
  friction on a kitchen phone, to stop impersonation inside one household.
