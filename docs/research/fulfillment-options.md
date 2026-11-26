# Authorized fulfillment options: research and scores

- **Date:** 2026-11-26
- **Issue:** #139 (backlog P2.2), for epic #142
- **Decision recorded in:** [ADR 0014](../adr/0014-instacart-developer-platform-as-first-handoff-adapter.md)

## How to read this

**How the sources were read.** The session that wrote this could not open
provider sites directly. Every provider fact below comes from a search
tool's summary of the official page linked beside it, read on 2026-11-26.
That is good enough to choose a direction. It is not good enough to accept
legal terms. Before requesting a production key, a person must read the
Instacart Developer Terms in full; the open points are listed under
[Still unknown](#still-unknown).

**What is being decided.** ADR 0001 retired the private Safeway API. A
fulfillment adapter may use only documented, authorized interfaces, never
stores retailer credentials, and never checks out. The question is which
authorized path to build first after the universal list export.

## Options

### 1. Instacart Developer Platform: shopping list link

`POST /idp/v1/products/products_link` creates a shopping list page on
Instacart Marketplace and returns a shareable link. The person opens the
link, picks a store, and shops and pays in Instacart.
([Create shopping list page](https://docs.instacart.com/developer_platform_api/api/products/create_shopping_list_page))

| Fact | Source |
|------|--------|
| `title` and `line_items` are required. In a line item only `name` is required, and it is used as the search term. | [Create shopping list page](https://docs.instacart.com/developer_platform_api/api/products/create_shopping_list_page) |
| Quantities go in a `line_item_measurements` array. The `quantity` and `unit` fields on the line item were deprecated on 2026-03-18. A quantity of zero or less is a 400. | [Create shopping list page](https://docs.instacart.com/developer_platform_api/api/products/create_shopping_list_page), [Changelog](https://docs.instacart.com/developer_platform_api/api/changelog/) |
| Only units on the Units of measurement page may be sent; an unsupported unit makes quantity matching fail. The page lists volume units (cup, fl oz, ml, liter and others), weight units (gram, kilogram, lb, oz) and count units (each, bunch, can, head, package, packet), with accepted abbreviations. | [Units of measurement](https://docs.instacart.com/developer_platform_api/api/units_of_measurement) |
| `upcs` and `product_ids` are optional and mutually exclusive. When either is sent, `filters` is ignored. | [Create shopping list page](https://docs.instacart.com/developer_platform_api/api/products/create_shopping_list_page) |
| `filters` holds `brand_filters` and `health_filters`. Brand names are case-sensitive and must match Instacart's spelling. Health values are a closed set: `ORGANIC`, `GLUTEN_FREE`, `FAT_FREE`, `VEGAN`, `KOSHER`, `SUGAR_FREE`, `LOW_FAT`. Ten or fewer per item is recommended. If no listed brand is available, Instacart may show alternatives. | [Filters](https://docs.instacart.com/developer_platform_api/guide/tutorials/filters), [FAQ](https://docs.instacart.com/developer_platform_api/faq/) |
| `expires_in` is the number of days until the link expires, at most 365. Recipe links default to 30 days; shopping list links have no default. An expired page that was never shopped or saved is deleted. | [Create shopping list page](https://docs.instacart.com/developer_platform_api/api/products/create_shopping_list_page), [Changelog](https://docs.instacart.com/developer_platform_api/api/changelog/) |
| The docs advise caching the returned URL and regenerating it only when the list changes. | [Create shopping list page](https://docs.instacart.com/developer_platform_api/api/products/create_shopping_list_page) |
| The landing page can carry a `partner_linkback_url`. | [Create shopping list page](https://docs.instacart.com/developer_platform_api/api/products/create_shopping_list_page) |
| A 429 means the per-second rate limit was exceeded. No number is published. The docs say to retry later with exponential backoff and to contact a representative if it repeats. | [API reference overview](https://docs.instacart.com/developer_platform_api/api/overview), [Errors](https://docs.instacart.com/developer_platform_api/errors) |
| Keys are created in the Developer Dashboard as Development or Production. The full key is shown once. Development and production are different hosts. | [Get an API key](https://docs.instacart.com/developer_platform_api/get_started/api-keys) |
| A production key stays "Pending approval" and does not work until Instacart approves a recorded demo of the integration. Instacart replies within 5 business days of the request, and quotes 30 to 40 days on average from access request to production key. | [Approval process](https://docs.instacart.com/developer_platform_api/guide/concepts/launch_activities/approval_process), [Pre-launch checklist](https://docs.instacart.com/developer_platform_api/guide/concepts/launch_activities/pre-launch_checklist/) |
| The demo must show the flow up to the button, a click that lands on an Instacart page, and a link Instacart can use to check item matching. | [Pre-launch checklist](https://docs.instacart.com/developer_platform_api/guide/concepts/launch_activities/pre-launch_checklist/) |
| The button text must be "Shop ingredients" or "Shop on Instacart", in one of three specified themes. | [Pre-launch checklist](https://docs.instacart.com/developer_platform_api/guide/concepts/launch_activities/pre-launch_checklist/), [CTA design](https://docs.instacart.com/developer_platform_api/guide/concepts/design/cta_design/) |
| Copy rules: don't say "Free Delivery", don't call the relationship a "partner" or "partnership", make no delivery-speed claim. | [Pre-launch checklist](https://docs.instacart.com/developer_platform_api/guide/concepts/launch_activities/pre-launch_checklist/) |
| The affiliate program runs through Impact and is optional. After approval Instacart appends tracking parameters to links itself; adding them by hand breaks attribution. | [Conversion tracking and affiliate payments](https://docs.instacart.com/developer_platform_api/guide/concepts/launch_activities/conversions_and_payments) |
| Terms: the API license is limited and revocable, for building an application that works with the platform. Data from a user's interaction with the API may not be used to build user profiles, target users, or build a competing product. Request only the minimum data needed. Instacart may set API limits. Instacart may reproduce and display content submitted through the API for the purpose of the API. Follow the attribution rules Instacart communicates. | [Developer Terms](https://docs.instacart.com/developer_platform_api/guide/terms_and_policies/developer_terms) |
| Guidelines: the application must not target users under 18. | [Developer guidelines](https://docs.instacart.com/developer_platform_api/guide/terms_and_policies/developer_guidelines) |

What the platform does **not** offer, as far as the pages show: reading a
cart, reading an order, a receipt, a price, or item availability. The link
is one-way.

A separate endpoint, `GET /idp/v1/retailers`, lists retailers near a postal
code. It needs its own key and is documented for recipe pages.
([Get nearby retailers](https://docs.instacart.com/developer_platform_api/api/retailers/get_nearby_retailers))
It doesn't change the design: the person picks the store in Instacart.

### 2. Instacart MCP servers

Two sets of pages describe MCP access, and they disagree.

- The Developer Platform tutorial describes a server with two tools,
  `create-recipe` and `create-shopping-list`, authenticated with the same
  API key as a bearer token.
  ([MCP tutorial](https://docs.instacart.com/developer_platform_api/guide/tutorials/mcp))
- A newer section describes servers that find retailers, search products
  and manage carts, authenticated by the user's Instacart login through
  OAuth, with onboarding through an Instacart representative.
  ([Instacart MCP Servers](https://docs.instacart.com/mcp_servers),
  [Use Instacart's MCP server](https://docs.instacart.com/mcp_servers/get-started/use-instacarts-mcp-server))

The first is the same capability as option 1 behind a different transport.
The second is a cart capability Grocery Butler would not own: OpenClaw, as
the MCP client, would hold the user's session. It is worth watching, and it
needs a representative, so it is not a first adapter.

### 3. Kroger public API

The public Cart API adds items to a signed-in customer's cart. It requires
the OAuth2 Authorization Code grant, with scopes such as `cart.basic:rw`.
Customer accounts don't exist in the certification environment, so cart
calls can only be tested in production.
([Cart API](https://developer.kroger.com/reference/api/cart-api-public),
[Customer authorization](https://developer.kroger.com/documentation/public/security/customer))

This is a real `draft_cart` capability, for Kroger banners only. It means
holding a customer's refresh token. That is an authorized grant, not a
stored password, but `Authorization` deliberately has no member for it yet,
and adding one is its own decision. The terms of use were not retrieved.

### 4. Walmart add-to-cart links

A documented URL adds item ids and quantities to a Walmart cart. Access to
the product APIs that supply those item ids goes through Impact and a
Walmart business manager who decides whether a company fits their partner
network.
([Add To Cart](https://walmart.io/docs/atc/v1/add-to-cart),
[Grocery introduction](https://www.walmart.io/docs/opd/product-snapshot/grocery-introduction))

A `deep_link_cart` capability that depends on an approval a household
project is unlikely to get, and on mapping every item to a Walmart item id.

### 5. Universal list export

Built (#144). Text, CSV or JSON, for any store. No approval, no provider,
no lock-in, and nothing automatic.

### 6. Receipt and order import

No option above reads an order back. Reconciliation (#150) therefore rests
on evidence the household supplies: a receipt photo, a pasted or forwarded
order confirmation, or a manual check-off. No provider API was found that a
household project could use to read its own orders.

### 7. Browser assistance

Backlog P2.1. Not scored: it is allowed only after a written review of
each retailer's terms, and nothing here changes that.

## Scores

1 is worst, 5 is best. "Lock-in" scores freedom from it, so 5 means none.

| Option | Reach | Approval likelihood | Economics | Data rights | Checkout support | Maintenance | Lock-in | Total |
|--------|:-----:|:-------------------:|:---------:|:-----------:|:----------------:|:-----------:|:-------:|:-----:|
| 1. Instacart shopping list link | 5 | 3 | 4 | 3 | 3 | 4 | 4 | 26 |
| 5. Universal list export | 5 | 5 | 5 | 5 | 1 | 5 | 5 | 31 |
| 3. Kroger public cart | 2 | 3 | 4 | 3 | 4 | 2 | 3 | 21 |
| 2. Instacart MCP (cart) | 5 | 2 | 3 | 2 | 4 | 2 | 2 | 20 |
| 4. Walmart add-to-cart | 3 | 1 | 4 | 3 | 4 | 2 | 3 | 20 |

Why Instacart scores as it does:

- **Reach 5.** Many retailers behind one integration, and the person picks
  the store.
- **Approval 3.** A development key is self-service. A production key needs
  a demo, specific button text and copy, and about a month. Nothing found
  says a single-household project is eligible or ineligible.
- **Economics 4.** No fee was found. The affiliate program is optional.
- **Data rights 3.** Item names, quantities and filters go to Instacart,
  which may reproduce them to serve the page. The limits on our side are
  acceptable. What Instacart may do beyond serving the page is not
  answered.
- **Checkout support 3.** It fills a list the person then shops. It is not
  a cart, and nothing comes back.
- **Maintenance 4.** One documented endpoint with a changelog. One
  deprecation in 2026 so far.
- **Lock-in 4.** The adapter interface hides it; the export still works.

The export scores highest because it costs nothing and risks nothing. It
also does nothing for the person beyond a list. Instacart is the best
option that moves the list closer to a purchase.

## Answers to the open questions in #139

| Question | Answer |
|----------|--------|
| Program eligibility | Not stated. The terms describe building and distributing an application; no rule for personal or non-commercial use was found. **Unknown: Instacart technical support or the key request itself answers it.** |
| How a production key is issued | Self-service request in the dashboard, then a demo review. The key is inert until approved. 30 to 40 days on average. |
| What happens when a link expires | The page is deleted if it was never shopped or saved. Shopping list links have no default expiry, so the adapter must always send `expires_in`. |
| Affiliate obligations | The program is optional and separate (Impact). Never add tracking parameters by hand. |
| Data rights | See the terms row above. The full terms and the privacy addendum were not read. **Unknown in detail: needs a person's read.** |
| Supported units | A published list in three groups. An unsupported unit breaks matching, so the adapter needs an explicit unit map and a fallback of sending no measurement. |
| Rate limits | Per-second, number unpublished. Back off exponentially on 429. |
| Nearby Retailers, recipe pages | Neither changes the design. |

## Still unknown

Each needs a person, because it is a legal reading or an account action.

1. **The full Developer Terms and privacy addendum**, read directly. The
   copy the search tool indexed may be old. Owner: Geoff.
2. **Whether a single-household, non-commercial tool can get a production
   key.** Owner: Geoff, by asking Instacart or applying.
3. **Whether a development-key link can be used to place a real order.**
   The development host is separate; nothing found says its links are
   shoppable. Owner: Geoff, with a development key, once #148 exists.
4. **Kroger's terms of use**, if option 3 is ever taken up.

## Follow-up changes to the epic's issues

- **#147 (translation).** Use `line_item_measurements`, never the
  deprecated `quantity` and `unit`. Map units through an explicit table of
  supported units; send no measurement for a unit not on it. Send
  `health_filters` only for the closed set, and only from a soft
  preference. Never translate a hard constraint into a filter: report it as
  unenforced (ADR 0012). If a UPC is ever sent, filters are ignored, so a
  line with a UPC reports all its preferences as not applied.
- **#148 (link creation).** Always send `expires_in`. Store the returned
  URL with the attempt and reuse it for the same plan version, as the docs
  advise. Back off on 429, bounded. Never append tracking parameters.
- **#149 (handoff UX).** The button reads "Shop on Instacart". No delivery
  claims; never "partner". An expired link is a state the person sees.
- **#150 (reconciliation).** Nothing comes back from Instacart. It stays
  provider-neutral and evidence-based.
- **Epic #142 done-when.** Split in two: "works end to end with a
  development key" is ours to finish; "works with a production key" waits
  on Instacart's approval and is tracked separately.
