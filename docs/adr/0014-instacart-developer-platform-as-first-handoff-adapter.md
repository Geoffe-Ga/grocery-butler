# ADR 0014: Build the Instacart shopping list link as the first handoff adapter

- **Status:** Accepted, with conditions (see "Before a production key")
- **Date:** 2026-11-26
- **Decider:** Geoff (research delegated in #139; the conditions below need his own read)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) P2.2 and P1.11; issue #139; epic #142; [research](../research/fulfillment-options.md); builds on ADR 0001, ADR 0010 and ADR 0012

## Context

ADR 0001 allows fulfillment only through documented, authorized interfaces.
The universal list export (#144) is built. The next step is a path that
moves a shopping plan closer to a purchase the person completes themselves.
[The research](../research/fulfillment-options.md) scored five options.

The session that did the research could not open provider sites. It read
search-tool summaries of the official pages. That limits how far this
decision can go.

## Decision

1. **Build.** The first handoff adapter targets the Instacart Developer
   Platform's shopping list link, `POST /idp/v1/products/products_link`.
   Not partner, not avoid: a development key is self-service, the endpoint
   is documented, and it needs no stored retailer credential.
2. **The capability is `shopping_list_link`, and only that.** The adapter
   must not claim `draft_cart`, `deep_link_cart`, `read_cart`, `read_order`
   or `import_receipt`. The platform returns a link and nothing else.
3. **Authorization is `provider_program`.** One API key for the deployment,
   read from the environment. No household member's Instacart login is ever
   seen or stored.
4. **Development key first.** The epic is done for our purposes when the
   handoff works end to end against the development host. A production key
   is a separate track that waits on Instacart's review.
5. **Constraints the code must honor:**
   - Send quantities in `line_item_measurements`. The `quantity` and `unit`
     fields on a line item are deprecated.
   - Send only units from Instacart's published list, through an explicit
     map. For any other unit, send the item with no measurement.
   - Send `health_filters` only from the closed set Instacart documents,
     and only for a soft preference.
   - Never express a hard constraint as a filter. Instacart can enforce
     none of them, so the adapter reports every hard constraint as
     unenforced and a person reviews before the handoff (ADR 0012).
   - Always send `expires_in`. Shopping list links have no default.
   - Keep the returned link with the handoff attempt and reuse it for the
     same plan version. Create a new one only when the plan changes.
   - On 429, back off exponentially, a bounded number of times.
   - Never add affiliate or tracking parameters to a link.
   - Don't build profiles from what people do with a link. Record only
     that a link was created, opened, expired or failed.
   - The button says "Shop on Instacart". No delivery claims, and never
     the word "partner".
6. **A link is a handoff, never an order.** Creating one needs an approved
   envelope bound to the plan version (ADR 0010). Inventory changes only
   from reconciliation evidence the household supplies (#150), because
   Instacart sends nothing back.
7. **Kroger, Walmart and Instacart's cart-capable MCP servers are
   deferred.** Each needs something this project doesn't have: a stored
   customer grant, a partner approval, or a representative.

**Chosen for the dogfood (Geoff, 2026-11-26):** a development key now, against
Instacart's development host. A production key is requested only after the
conditions below are met.

## Before a production key

These are conditions on this ADR, and each needs Geoff:

1. Read the Developer Terms and privacy addendum in full, at the source.
2. Find out whether a single-household, non-commercial tool is eligible.
3. Record a demo and request the key. Allow about a month.

If the terms forbid something in "Decision", amend this ADR before the
production key is used. Until then, the household's real handoff path
during the dogfood is the list export, with the Instacart link exercised
against the development host.

## Consequences

**Easier:**

- One adapter reaches many retailers, and the person picks the store.
- No credential belonging to a person is held, so ADR 0001 is not strained.
- The adapter is small: translate lines, create a link, record the attempt.

**Harder:**

- Nothing is known after the click. Whether anything was bought comes only
  from the household.
- Allergen and other hard constraints can't be enforced by the provider.
  Every handoff that carries one must put it in front of a person.
- Production use depends on an approval we don't control, and on button
  text and copy rules we must follow exactly.
- Item matching is by name. A canonical name that searches badly gives a
  bad list, and we can't see the result.

## Alternatives considered

| Option | Why not first |
|--------|---------------|
| Stay with the list export only | Costs nothing and stays. It doesn't shorten the trip from plan to cart. |
| Kroger public Cart API | A real cart, for one family of stores, at the price of holding a customer's OAuth grant and testing in production. Its own ADR if ever wanted. |
| Walmart add-to-cart links | Needs item ids from an API gated behind a partner review a household project is unlikely to pass. |
| Instacart MCP servers with cart tools | Needs a representative, and the user's session would live in the MCP client. Worth watching; not ours to build first. |
| Wait for a full terms review before any code | The development-key work is reversible and touches no production data. Waiting buys nothing; the production key is where the review gates. |
