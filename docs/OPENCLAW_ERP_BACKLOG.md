# Grocery Butler: OpenClaw-Managed Household ERP Backlog

- **Status:** Proposed product backlog
- **Decision:** Retire the private Safeway API integration as a product feature
- **Product thesis:** Grocery Butler is the durable household system of record; OpenClaw is the conversational and visual operator; retailer fulfillment is an authorized, replaceable handoff.

## Executive intent

Grocery Butler should help a household answer four questions reliably:

1. What do we usually keep?
2. What do we probably have right now, and how certain are we?
3. What needs attention or replenishment?
4. How can a human approve and complete that replenishment through an authorized retailer experience?

The product should not promise perfect perpetual inventory or autonomous purchasing. Household inventory is noisy: packages are partly full, items move, receipts differ from orders, and people forget to report consumption. The useful product is a low-friction **household replenishment memory** that makes uncertainty visible and reduces invisible labor.

## Product boundary

### Grocery Butler owns

- Canonical household items and staple policies
- Inventory observations, confidence, provenance, and current inferred state
- Household preferences, brands, dietary constraints, and substitution rules
- Replenishment needs and their reasons
- Shopping plans independent of any retailer
- Draft actions, approvals, audit history, and reconciliation
- Connector contracts for authorized fulfillment handoffs

### OpenClaw owns

- Conversational and multimodal interaction
- Turning photos, voice, and chat into proposed observations
- Asking targeted clarification questions when confidence is low
- Orchestrating review and approval
- Operating an authorized retailer surface when permitted
- Reporting results back for reconciliation

### Retailer or authorized commerce provider owns

- Product catalog, current prices, promotions, and availability
- Authentication and account security
- Cart, fulfillment slots, payment, substitutions, and checkout
- Final order and fulfillment truth

### Explicit non-goals

- Reverse-engineering private retailer APIs
- Storing retailer passwords or payment credentials
- Unattended purchasing
- Claiming exact quantities when evidence only supports `on_hand`, `low`, or `out`
- Making browser automation a guaranteed integration contract
- Building meal planning, receipt OCR, barcode scanning, and multi-retailer checkout simultaneously

## Safety and product principles

1. **Evidence before mutation.** A photo or model inference creates an observation proposal. It does not silently overwrite inventory.
2. **Uncertainty is data.** Store confidence, source, timestamp, and ambiguity; expose them in UX.
3. **One household vocabulary.** “Peanut butter,” a photographed jar, a recipe ingredient, and a retailer SKU must resolve through a canonical household item.
4. **Draft → confirm → execute.** Money-moving or destructive actions always require explicit human confirmation against an immutable draft.
5. **Server-authoritative plans.** Clients reference a stored plan; they do not supply trusted prices, totals, or item identity at execution time.
6. **Retailers are adapters.** Core inventory and replenishment logic must not import retailer-specific models.
7. **Reconcile reality.** Ordered, fulfilled, received, and put away are different states.
8. **Optimize for correction cost.** A useful inference that takes one tap to fix is better than a sophisticated inference that is hard to inspect.
9. **Local-first privacy posture.** Retain images only when useful and consented to; derived observations should not require permanent photo storage.
10. **Household trust over automation theater.** Never represent a draft cart, browser click, or retailer acknowledgment as a completed order without evidence.

## Target workflow

1. A household member photographs a pantry shelf, refrigerator area, or individual staple.
2. OpenClaw extracts candidate items, rough stock states, package cues, and uncertainties.
3. Grocery Butler resolves candidates against the household catalog and stores a proposed observation batch.
4. OpenClaw asks only the questions that materially change replenishment decisions.
5. The user confirms or corrects the batch; inventory state and replenishment needs update atomically.
6. Grocery Butler builds a retailer-neutral shopping plan with reasons, preferences, and substitution rules.
7. OpenClaw hands the approved plan to an authorized retailer surface or guides the user through it.
8. The user reviews the retailer's authoritative cart and explicitly confirms checkout there.
9. Order or receipt evidence is imported after fulfillment; Grocery Butler reconciles what actually arrived.

## Domain model changes

Introduce these concepts before expanding UI:

- **HouseholdItem** — canonical identity, display name, aliases, category, preferred unit, staple policy, active flag.
- **InventoryObservation** — item, observed state/quantity, source, confidence, observed-at, evidence reference, actor, notes.
- **InventorySnapshot** — materialized current inference derived from observations; includes freshness and confidence.
- **EvidenceBatch** — one photo/session/receipt and the proposals derived from it.
- **ReplenishmentNeed** — item, target state/quantity, priority, reason, status, provenance, snooze/ignore state.
- **ShoppingPlan** — retailer-neutral, versioned collection of needs and meal-driven items.
- **FulfillmentDraft** — immutable adapter output for review; expires and records external destination.
- **FulfillmentAttempt** — authorized handoff attempt with status and external reference, never assumed successful.
- **Receipt/OrderObservation** — what was ordered, fulfilled, substituted, refunded, or received.
- **ItemMapping** — scoped mapping between HouseholdItem and an external catalog identifier with confidence and verification date.

Use an append-only observation/audit model for inventory changes. A corrected inference should supersede or counteract an earlier observation rather than erase history.

## Backlog structure

Priority meanings:

- **P0:** Required to make the pivot safe and internally coherent
- **P1:** Required for a useful private household pilot
- **P2:** Required to validate an external product
- **P3:** Expansion after evidence of retention

Effort uses relative sizes: **S** (1–2 focused days), **M** (3–5 days), **L** (1–2 weeks), **XL** (must be split).

---

## Epic 0 — Retire the private Safeway product surface

**Outcome:** The repository no longer claims or exposes an unsupported Safeway ordering capability.

### P0.1 Decision record and architecture language — S

- Add an ADR recording why the private API was retired.
- Rewrite README mission, architecture, setup, and diagrams around OpenClaw + authorized fulfillment adapters.
- Remove claims that a Safeway cart can be built or submitted reliably.
- Mark old roadmap and Safeway verification plans as superseded.

**Acceptance:** A new operator cannot infer that private Safeway credentials or endpoints are supported.

### P0.2 Remove runtime Safeway checkout paths — L

- Remove Safeway credential configuration and feature flags.
- Remove `/order/preview`, `/order/submit`, and confirmation handlers that depend on Safeway models, or replace them with retailer-neutral plan endpoints.
- Remove/retire CLI and legacy Discord order commands.
- Remove private API client, pipeline, product search, selector, substitution, cart, and submission modules after extracting genuinely retailer-neutral logic.
- Add a migration for obsolete retailer-specific tables; preserve historical audit records in an archive/export if any exist.

**Acceptance:** Production can run with no Safeway configuration; no private Safeway HTTP request is reachable; test and dependency surfaces shrink accordingly.

### P0.3 Close or reclassify existing Safeway issues — S

- Close live-API verification and checkout-specific bugs as `wontfix/pivot` with a link to the ADR.
- Reopen retailer-neutral defects (duplicate replenishment, provenance, reconciliation) as new scoped issues.
- Do not close security or general UX defects merely because their original path involved Safeway.

**Relevant existing issues:** #32, #60 and checkout-specific portions of #73, #97, #117, #120. Review each rather than bulk-closing.

---

## Epic 1 — Stabilize the household system of record

**Outcome:** Inventory changes are auditable, deduplicated, and safe to drive decisions.

### P0.4 Canonical household item identity — L

- Separate item identity from display name and free-text ingredient strings.
- Add aliases and normalization without silently merging distinct products.
- Support staple policy and preferred brand/size on the canonical item.
- Provide merge and unmerge operations with audit history.

**Acceptance:** Meal ingredients, inventory entries, observations, and replenishment needs reference one stable item ID.

### P0.5 Observation ledger and derived snapshot — L

- Add append-only `inventory_observations`.
- Derive the current stock state from the latest valid evidence and explicit corrections.
- Record actor, source (`human`, `photo`, `receipt`, `order`, `system`), confidence, and timestamps.
- Define freshness by item class; stale state becomes `unknown`, not silently `on_hand`.

**Acceptance:** Every visible stock state explains “why we think this” and can be corrected without destroying history.

### P0.6 Replenishment need state machine — M

- Replace implicit restock derivation with explicit needs: `suggested`, `approved`, `deferred`, `dismissed`, `planned`, `ordered`, `received`, `cancelled`.
- Make reasons first-class: low stock, out, recurring staple, recipe demand, manual request.
- Deduplicate overlapping meal and restock demand by canonical item.

**Acceptance:** One item cannot appear twice merely because it has two reasons; all reasons remain visible.

### P0.7 Migration and concurrency hardening — M

- Move migrations out of request/store constructors.
- Ensure one migration runner with database locking/advisory locking.
- Add downgrade/recovery documentation and backup verification.

**Acceptance:** Concurrent application workers cannot race migrations; startup failure is deterministic and observable.

---

## Epic 2 — OpenClaw contract and safe agency

**Outcome:** OpenClaw can operate the ERP through stable, narrow, inspectable capabilities.

### P0.8 Capability-oriented API v2 — L

Create endpoints/tools for:

- Search/list household items and current state
- Propose an observation batch
- Review/confirm/correct/reject a batch
- Create/update/snooze replenishment needs
- Build and retrieve a shopping plan
- Draft and confirm fulfillment handoff
- Record fulfillment/receipt results
- Retrieve explanations and audit history

Use idempotency keys on all mutations and stable machine-readable error codes.

**Acceptance:** OpenClaw never needs database access or retailer credentials; every mutation is attributable and retry-safe.

### P0.9 Immutable approval envelopes — M

- Store the exact payload being approved, including plan version and item IDs.
- Hash/bind confirmations to that payload and actor.
- Expire drafts and reject stale plans when underlying needs changed.
- Distinguish approval to hand off from approval to purchase; the latter belongs in the retailer's authorized surface.

**Acceptance:** A changed plan cannot execute under an earlier confirmation.

### P1.1 Explainability response contract — S

For each suggestion, return concise reasons such as:

- “Marked low from pantry photo 2 hours ago (0.82 confidence)”
- “Usually replenished every 12–16 days; last received 18 days ago”
- “Needed by two meals; one unit already estimated on hand”

**Acceptance:** OpenClaw can answer “why is this on the list?” without reconstructing domain logic itself.

---

## Epic 3 — Photo inventory capture

**Outcome:** A household member can walk through the kitchen taking pictures and produce a reviewable inventory update with minimal correction effort.

### P1.2 Evidence upload and privacy controls — M

- Accept images through a short-lived upload flow.
- Strip unnecessary metadata.
- Make retention explicit: delete after extraction by default; optionally retain thumbnails/evidence for a configurable period.
- Record consent and deletion status.
- Define file-size, type, malware, and rate limits.

**Acceptance:** A user can delete source images without deleting confirmed structured observations.

### P1.3 Vision extraction schema — L

For each candidate, extract:

- Candidate name and visible brand/variant
- Package count or rough fill state when observable
- Proposed stock state
- Confidence per field
- Bounding region or textual evidence
- Ambiguity and suggested clarification

Do not infer unseen items or exact quantities from occluded shelves.

**Acceptance:** Model output is schema-validated; invalid or low-confidence results become review items, not mutations.

### P1.4 Item resolution and learning loop — L

- Resolve candidates against HouseholdItem aliases and prior confirmed images.
- Present top matches when ambiguous.
- Learn household-specific aliases and packaging only after confirmation.
- Detect likely duplicate sightings within one capture session.

**Acceptance:** Repeated scans become faster without allowing one bad inference to permanently poison mappings.

### P1.5 Batch review UX — L

- Mobile-first review with `confirm all high-confidence` plus fast per-item correction.
- Group new, changed, unchanged, ambiguous, and ignored candidates.
- Support swipe/tap states for on hand, low, out, unknown.
- Show the relevant crop when available.
- Apply the confirmed batch atomically.

**Acceptance:** A 20-item shelf scan can be reviewed in under 60 seconds in usability testing, with no silent low-confidence changes.

### P1.6 Capture-session guidance — M

- Let OpenClaw guide room/zone scans (“pantry top shelf,” “freezer door”).
- Detect likely gaps and ask targeted follow-ups, not generic rescans.
- Avoid double counting across overlapping photos.

**Acceptance:** A user can stop and resume a session; the system clearly distinguishes unreviewed evidence from confirmed state.

---

## Epic 4 — Shopping intelligence

**Outcome:** Grocery Butler creates useful, retailer-neutral plans that reflect household intent and uncertainty.

### P1.7 Staple policy and par levels — M

- Support qualitative policies first: always keep, replenish when low, buy only for meals, never auto-suggest.
- Add optional target quantities only where household behavior supports them.
- Support cadence hints and package-size preferences.

**Acceptance:** Each staple has a comprehensible replenishment policy; no quantity precision is required for basic use.

### P1.8 Shopping-plan builder — L

- Consolidate approved needs and meal demand by canonical item.
- Account for confidence and freshness; surface uncertain items for checking.
- Preserve all reasons and preference constraints.
- Version and store each plan.

**Acceptance:** The same input state yields a deterministic plan before optional model-assisted enrichment.

### P1.9 Preference and substitution policy — M

- Model hard constraints separately from soft preferences.
- Capture acceptable brands, sizes, organic/generic preference, and “do not substitute.”
- Require review when no candidate satisfies a hard constraint.

**Acceptance:** An adapter can consume preferences without receiving free-form household secrets or unrelated context.

### P1.10 Uncertainty-aware reminders — M

- Suggest targeted checks for stale or uncertain high-priority items.
- Allow snooze, dismiss, and “stop asking about this item.”
- Rate-limit prompts and learn preferred reminder timing.

**Acceptance:** The system reduces mental load rather than creating a new notification inbox.

---

## Epic 5 — Legitimate fulfillment adapters

**Outcome:** A shopping plan can move toward purchase without private APIs or false guarantees.

### P1.11 Retailer-neutral adapter interface — L

Define capabilities rather than assuming every adapter supports checkout:

- `export_list`
- `open_search`
- `shopping_list_link`
- `draft_cart`
- `deep_link_cart`
- `read_cart`
- `read_order`
- `import_receipt`

Each adapter declares authorization method, supported operations, data freshness, and whether human interaction is required.

`shopping_list_link` is the capability the official Instacart adapter actually supports: it creates a shareable Instacart Marketplace shopping-list page from line items. The user then picks a store, reviews product matches, and checks out in Instacart. The public Developer Platform documentation (read 2026-11-25) describes no cart read, order read, receipt access, webhook, or checkout by API, so the Instacart adapter must not declare `read_cart`, `read_order`, or `import_receipt`. Tracked in #144 (interface and universal export) and epic #142 (Instacart).

**Acceptance:** The core works with a plain text/list export adapter and contains no retailer imports.

### P1.12 Universal handoff MVP — M

- Export a grouped checklist in shareable text/JSON/CSV.
- Generate retailer search links per item where terms permit.
- Support copy/paste or share-sheet handoff.
- Keep plan state independent of whether an external cart was created.

**Acceptance:** The household can use Grocery Butler with any grocery store today.

### P2.1 Authorized browser-assist prototype — L

- Run through the user's logged-in browser session; never collect credentials.
- Navigate only after explicit approval of the plan.
- Pause for substitutions, material price differences, fulfillment slot, and checkout.
- Require the human to review and confirm purchase in the retailer UI.
- Capture a result only from visible confirmation or imported order evidence.
- Document retailer terms and disable adapters where automation is prohibited.

**Acceptance:** Browser assistance cannot independently submit payment and never reports success based only on clicks or navigation.

### P2.2 Official integration discovery spike — M

Research and score, with counsel where appropriate:

- Retailer affiliate/developer programs
- Instacart or other authorized commerce platforms
- Retailer-supported deep links and list/cart APIs
- OpenAI/OpenClaw-compatible retailer apps/connectors
- Receipt/order import options (email forwarding, user upload, supported APIs)

Score reach, approval likelihood, economics, data rights, checkout support, maintenance cost, and lock-in.

**Instacart status (2026-11-25):** the standard Instacart Developer Platform is the preferred first candidate. Its documented handoff is `POST /idp/v1/products/products_link`, which returns only a shareable shopping-list link. Verified constraints that shape the adapter:

- Line-item filters are ignored when UPCs or product IDs are sent, and health filters are a closed set (organic, gluten-free, fat-free, vegan, kosher, sugar-free, low-fat). A hard constraint such as an allergen exclusion can't be expressed and must be surfaced for review, never sent as a soft filter.
- Shopping-list links have no default expiry, so the adapter sets one.
- Product matching is by name (or UPC) and not guaranteed; the user resolves matches in Instacart.

**Decision (2026-11-26, [ADR 0014](adr/0014-instacart-developer-platform-as-first-handoff-adapter.md)):** build it, against a development key first. The scored comparison with Kroger, Walmart, Instacart's MCP servers and the list export is in [`docs/research/fulfillment-options.md`](research/fulfillment-options.md). What #139 settled: a production key is requested in the dashboard and stays inert until Instacart approves a recorded demo, about 30 to 40 days; an expired link's page is deleted unless it was shopped or saved; quantities go in `line_item_measurements` with units from a published list; the rate limit is per second with no published number; the affiliate program is optional. Still needing a person before a production key: a full read of the Developer Terms and privacy addendum, and whether a single-household tool is eligible. Delivery is epic #142.

**Acceptance:** Produce a written build/partner/avoid decision; no implementation begins from undocumented endpoints.

### P2.3 Receipt and fulfillment reconciliation — L

- Import receipt/order evidence from an authorized source or user upload.
- Match fulfilled and substituted items to HouseholdItems.
- Present ambiguities for review.
- Update received state only after confirmation or high-quality authoritative evidence.

**Acceptance:** Ordered, substituted, refunded, and received items are distinguishable; inventory is not updated merely because a plan was handed off.

---

## Epic 6 — Household UX and trust

**Outcome:** Non-technical household members can understand and correct the system quickly.

### P1.13 Today dashboard — M

Show only actionable information:

- Needs review
- Probably low/out
- Approved shopping plan
- Awaiting fulfillment/reconciliation
- Stale high-priority items

**Acceptance:** Internal enum names and implementation concepts do not leak into household UI.

### P1.14 Safe interaction patterns — M

- Loading and progress states for model calls
- Double-submit protection
- Undo for low-risk changes
- Confirmations for destructive bulk actions
- Persistent, actionable errors
- Preserve form input on validation failure

**Acceptance:** Existing known UX defects around disappearing errors, destructive actions, and lost input are covered by regression tests.

### P2.4 Household roles and attribution — L

- Owner/admin/member roles
- Actor attribution for observations and approvals
- Household invitation/removal and session revocation
- Permission boundary for purchasing handoffs

**Acceptance:** One household member cannot approve a money-moving handoff unless explicitly authorized.

### P2.5 Accessibility and kitchen usability — M

- Screen-reader and keyboard pass
- Large touch targets and adequate contrast
- Low-bandwidth/photo-upload fallback
- Plain-language status and confidence labels

**Acceptance:** Automated accessibility checks pass and at least one real mobile kitchen walkthrough is completed.

---

## Epic 7 — Product readiness, privacy, and operations

**Outcome:** The system can safely support design-partner households.

### P0.10 Web security baseline — M

- CSRF protection for browser mutations
- CSP and standard security headers
- Secret-safe configuration representations and logs
- Request limits and upload validation
- Dependency and container scanning in CI

### P1.15 Data lifecycle — M

- Export household data
- Delete household data and retained evidence
- Document retention and subprocessors
- Encrypt transport and backups; define restoration tests

### P1.16 Observability and failure recovery — M

- Structured audit events and correlation IDs
- Metrics for extraction failures, correction rate, stale inventory, adapter failures, and reconciliation gaps
- Admin-safe diagnostics without exposing household images or secrets
- Tested backup restore and retry behavior

### P2.6 Multi-tenant foundation — XL; split before implementation

- Tenant isolation
- Account and household membership
- Billing boundaries
- Rate/usage limits
- Support tooling with privacy-aware impersonation alternatives

**Gate:** Do not build until private pilot retention supports external product investment.

---

## Epic 8 — Validation and business evidence

**Outcome:** Product decisions follow observed household value, not feature enthusiasm.

### P1.17 Geoff household dogfood protocol — S

Run a four-week protocol:

- Weekly guided pantry/fridge scan
- Ad hoc “low/out” updates through OpenClaw
- One shopping-plan handoff per week
- Post-shop receipt reconciliation
- Log corrections, missing items, false suggestions, time spent, and avoided mental work

### P2.7 Design-partner pilot — M operational effort

- Recruit 10–20 households already using a personal AI agent or willing to use OpenClaw.
- Eight-week pilot with explicit privacy consent and direct support.
- Interview the person currently carrying grocery mental load, not only the technical installer.

### P2.8 Pricing test — S

Test separately:

- Self-hosted open-source core
- Managed hosting/support
- Paid setup/concierge
- Household operations bundle beyond groceries

Do not infer willingness to pay from usage alone.

## Success metrics and gates

### Core product metrics

- Weekly active households
- Weekly confirmed observation batches
- Median correction time per 20 detected items
- Candidate precision and item-resolution accuracy
- Percentage of replenishment suggestions accepted, corrected, deferred, and dismissed
- Shopping-plan correction rate
- Inventory freshness for declared staples
- Reconciliation completion rate
- Support minutes per active household
- Self-reported mental-load reduction

### Pilot gates

Proceed from Geoff-only dogfood to design partners when:

- No unsupported retailer API remains reachable
- 90% of state changes have provenance and actor attribution
- A 20-item photo batch has median review time under 60 seconds
- No known path can purchase or claim purchase without explicit confirmation/evidence
- Backup restore and evidence deletion have been tested

Proceed from design partners to paid beta when:

- At least 50% of households remain weekly active at week eight
- At least 60% of suggested replenishment items are accepted without correction
- Median support burden is under 30 minutes per household per month
- At least five households complete the workflow in four consecutive weeks
- At least five households commit to a real price, not a hypothetical survey answer

Pause or narrow the product if:

- Photo review takes as long as manually checking staples
- Inventory drift causes repeated missed or duplicate purchases
- Browser adapters require constant retailer-specific repair
- The grocery manager in the household does not trust the suggestions
- Retention depends on founder-operated concierge work that pricing cannot support

## Recommended delivery sequence

### Milestone A — Honest pivot (1–2 weeks)

- P0.1–P0.3: ADR, docs, remove private Safeway runtime, issue triage
- P0.10: minimum web security fixes that should not wait
- Deliverable: a smaller, honest application with retailer-neutral language

### Milestone B — Trustworthy ERP core (3–5 weeks)

- P0.4–P0.9: canonical items, observation ledger, needs, migrations, API v2, approval envelopes
- Deliverable: OpenClaw can safely manage inventory and plans without photos or retailer integration

Milestones keep their letters, but Milestone C now follows the first handoff in Milestone E ([ADR 0005](adr/0005-photo-capture-after-the-fulfillment-handoff.md)).

### Milestone D — Useful replenishment loop (2–4 weeks)

- P1.7–P1.12 and P1.17
- Deliverable: policy-aware shopping plan with universal handoff; four-week dogfood begins

### Milestone E — Legitimate fulfillment and reconciliation (research first)

- P2.1–P2.3 only after written terms/authorization review
- Instacart (epic #142) is the first concrete adapter, and it comes after Milestone B's versioned plans and approval envelopes (#143, #137) and Milestone D's adapter interface, universal export and hard constraints (#144, #145). Order: #139 decision, then #146 tracer, #147 translation, #148 link creation, #149 handoff UX; #150 reconciliation runs alongside.
- Creating an Instacart link is a handoff, not an order. Grocery Butler records created, opened, expired, or failed, and changes inventory only from confirmed reconciliation.
- Deliverable: one authorized handoff path and truthful post-order reconciliation

### Milestone C — Photo walk-through MVP (3–5 weeks, after Milestone E's first handoff)

- P1.2–P1.6 plus P1.13–P1.14
- Whole-shelf capture; photos are deleted when their batch closes
- Deliverable: photo → proposals → fast review → auditable inventory update

### Milestone F — External validation

- P2.4–P2.8 after dogfood gates pass
- Deliverable: 10–20 household design-partner pilot and evidence-based pricing decision

## First issue set to create

Create these issues first; do not explode the entire document into tickets until Milestone A establishes the architecture:

1. `ADR: Pivot from private Safeway API to authorized fulfillment adapters`
2. `Remove private Safeway configuration, endpoints, commands, and runtime modules`
3. `Rewrite README and roadmap around OpenClaw-managed household ERP`
4. `Triage existing issues after retailer-integration pivot`
5. `Design canonical HouseholdItem and alias model`
6. `Design append-only InventoryObservation ledger and derived snapshot`
7. `Design ReplenishmentNeed state machine and deduplication rules`
8. `Move database migrations out of request/store construction`
9. `Specify OpenClaw capability API v2 and idempotency contract`
10. `Bind approvals to immutable, expiring server-stored drafts`
11. `Add CSRF, CSP, upload limits, and secret-safe diagnostics`
12. `Research authorized retailer handoff options and document terms constraints`

## Decisions still required

All seven original questions are settled; each links to its ADR. One, the fulfillment path, is settled with conditions that only Geoff can meet; they are listed in its ADR.

1. **Deployment model:** one household for the first production release, with no tenancy in the schema. [ADR 0002](adr/0002-single-household-pilot-with-named-members.md)
2. **Photo retention:** a source photo is kept until its evidence batch closes, then deleted. [ADR 0005](adr/0005-photo-capture-after-the-fulfillment-handoff.md)
3. **First photo workflow:** whole-shelf capture. [ADR 0005](adr/0005-photo-capture-after-the-fulfillment-handoff.md)
4. **Quantities:** qualitative state, with an optional quantity only when stated or read from authoritative evidence. [ADR 0003](adr/0003-qualitative-inventory-state-with-optional-quantity.md)
5. **Fulfillment path:** decided in [ADR 0014](adr/0014-instacart-developer-platform-as-first-handoff-adapter.md). The Instacart shopping list link is the first handoff adapter, built against a development key. A production key waits on Geoff's read of the terms and Instacart's approval; until then the list export is the household's real handoff.
6. **Meal planning:** kept as an optional source of demand; plans work without it. [ADR 0004](adr/0004-meal-demand-is-an-optional-input-to-shopping-plans.md)
7. **Household identity:** named members without login. [ADR 0002](adr/0002-single-household-pilot-with-named-members.md)

## Definition of done for every backlog item

- Acceptance criteria have automated tests where behavior is deterministic.
- Model-assisted behavior has schema validation, confidence handling, and evaluation fixtures.
- Mutations are idempotent where retries are possible.
- Audit/provenance behavior is explicit.
- Mobile and OpenClaw workflows are both considered.
- Privacy and retention impacts are documented.
- Errors are actionable and do not expose secrets or internal exceptions.
- Documentation and API contracts are updated in the same change.
- Relevant lint, type, security, migration, and test gates pass.
- The final diff is reviewed for accidental retention of retailer-specific coupling.

## Product north star

The winning moment is not “the AI placed an order.” It is:

> The household realizes it no longer depends on one person's memory to know what is needed, why it is needed, and what should happen next—and that a human still retains clear control over what gets purchased.
