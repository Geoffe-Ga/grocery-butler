# ADR 0001: Pivot from the private Safeway API to authorized fulfillment adapters

- **Status:** Accepted
- **Date:** 2026-11-25
- **Decider:** Geoff
- **Source:** [OpenClaw-managed household ERP backlog](../OPENCLAW_ERP_BACKLOG.md), Epic 0 (P0.1)

## Context

Grocery Butler was built to plan meals, track household inventory, and then
order groceries from Safeway. The ordering half talked to Safeway's private
web API: an httpx client authenticating through an Okta-issued bearer token,
product search, a product selector, Claude-ranked substitutions, a cart
builder, and an order submitter, all wired together by `SafewayPipeline`.

That integration never became a product capability:

- **It was never verified.** Issue #60 found that the Okta client id looks
  like an authorization-server id, that `/abs/pub/web/orders` models no
  payment method and no pickup or delivery slot reservation, and that no real
  order has ever been confirmed through the client. Submission shipped
  disabled behind `SAFEWAY_ORDER_SUBMISSION_ENABLED`.
- **It depends on undocumented, unauthorized endpoints.** Safeway publishes no
  API for this. A reverse-engineered client can break at any time, and using
  it is not a relationship the retailer agreed to.
- **It requires holding secrets we should never hold.** The household's
  retailer password lived in our configuration, and the checkout path was the
  only reason payment and fulfillment concerns entered the codebase.
- **It made the server trust things it couldn't verify.** Prices, totals and
  fulfillment fees on a staged order came from the client (#73, #120), so the
  money-moving path was the least trustworthy part of the system.
- **It pulled effort away from the part that works.** Most open defects and
  most of the code surface were checkout plumbing, while the household value
  (knowing what we keep, what we have, and what we need) sat in a smaller
  core.

Meanwhile, the household already drives Grocery Butler through an agent:
RubotPaul, the household's OpenClaw assistant, calls the `/api/v1` blueprint.
OpenClaw can handle conversation, photos, clarification, and operating a
retailer's own surface on the user's behalf. That changes what Grocery Butler
needs to own.

## Decision

Retire the private Safeway API integration as a product feature, and
reposition Grocery Butler as the household's durable system of record, with
OpenClaw as its conversational and visual operator and retailer fulfillment
as an authorized, replaceable handoff.

1. **Grocery Butler owns** canonical household items, inventory observations
   and their provenance, preferences and substitution rules, replenishment
   needs with reasons, retailer-neutral shopping plans, draft actions,
   approvals, audit history, and reconciliation.
2. **OpenClaw owns** conversation, turning photos, voice and chat into
   *proposed* observations, targeted clarification, approval orchestration,
   and operating an authorized retailer surface when permitted.
3. **The retailer or an authorized commerce provider owns** its catalog,
   prices, availability, authentication, cart, fulfillment slots, payment,
   checkout, and the final truth about what was ordered.
4. **Fulfillment goes through adapters** behind a retailer-neutral interface
   whose capabilities are declared per adapter (`export_list`, `open_search`,
   `draft_cart`, `deep_link_cart`, `read_cart`, `read_order`,
   `import_receipt`). Core inventory and replenishment logic never imports a
   retailer-specific model. The first adapter is a plain list export that
   works with any store.
5. **No adapter is built from undocumented endpoints.** Any retailer-specific
   path starts with a written review of the retailer's terms and the
   available authorized programs (backlog P2.2).
6. **The private Safeway runtime is removed**, not just disabled: its
   configuration, feature flags, `/api/v1/order/*` endpoints, CLI and Discord
   order commands, and the client, pipeline, search, selector, substitution,
   cart, and submission modules. Obsolete retailer tables are archived by a
   migration rather than dropped, so historical audit rows survive.

## Consequences

### Positive

- Production runs with no retailer credentials and no reachable private
  retailer HTTP request.
- The money-moving path, and its class of client-trust bugs, leaves the
  codebase.
- The test, dependency, and configuration surfaces shrink, and the remaining
  code is the household core the backlog builds on.
- Any grocery store works on day one through list export.

### Negative

- Grocery Butler can no longer build a Safeway cart, even for review. Until an
  authorized adapter exists, the household completes checkout itself,
  guided by an exported plan.
- Product-level knowledge the cart path collected (cached Safeway product
  mappings) stops being used. It is archived, not deleted.
- Some checkout-adjacent behavior (restock marking after an order) has no
  home until receipt and order reconciliation lands (backlog P2.3).

### Neutral

- The draft → confirm → execute audit trail (`pending_actions`) stays. It
  still guards preference changes and will guard fulfillment handoffs, bound
  to immutable server-stored drafts (backlog P0.9).
- `/api/v1` stays the agent contract until the capability-oriented v2 API is
  specified (backlog P0.8).

## Alternatives considered

1. **Keep the integration behind the disabled flag.** Rejected: unreachable
   code still implies a supported capability, still carries credential
   configuration, and still costs maintenance and review on every change.
2. **Finish verifying the private API (#60's checklist).** Rejected: success
   would still leave the product dependent on an endpoint Safeway never
   offered, with credential handling and checkout liability we don't want.
3. **Browser automation as the integration contract.** Deferred, not
   adopted as a contract: an authorized browser-assist prototype that runs
   in the user's own session, pauses for every material choice, and never
   submits payment is backlog P2.1, gated on the terms review in P2.2.

## Supersedes

- The Safeway ordering phase of [`plans/ROADMAP.md`](../../plans/ROADMAP.md)
  and [`plans/mealbot-implementation-prompt.md`](../../plans/mealbot-implementation-prompt.md).
- The live-API verification plans in #31, #32 and #60, and the "Safeway
  Order Submission Status (v1.0)" section the README used to carry.
