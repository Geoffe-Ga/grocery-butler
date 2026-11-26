# Architecture Decision Records

Each file records one decision: the context, what was decided, and what it
costs. A new decision gets the next number; a reversed decision gets a new
ADR that supersedes the old one, rather than an edit.

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-pivot-from-private-safeway-api-to-authorized-fulfillment-adapters.md) | Pivot from the private Safeway API to authorized fulfillment adapters | Accepted |
| [0002](0002-single-household-pilot-with-named-members.md) | Single-household pilot with named members and no login | Accepted |
| [0003](0003-qualitative-inventory-state-with-optional-quantity.md) | Qualitative inventory state with an optional quantity | Accepted |
| [0004](0004-meal-demand-is-an-optional-input-to-shopping-plans.md) | Meal demand is an optional input to shopping plans | Accepted |
| [0005](0005-photo-capture-after-the-fulfillment-handoff.md) | Photo capture comes after the fulfillment handoff, by whole shelf, with photos kept until the batch closes | Accepted |
| [0006](0006-canonical-household-item-identity.md) | Canonical household items, with aliases and reversible merges | Accepted |
| [0007](0007-append-only-inventory-observations.md) | Append-only inventory observations and a snapshot derived on read | Accepted |
| [0008](0008-explicit-replenishment-needs.md) | Explicit replenishment needs, one per item, with first-class reasons | Accepted |
| [0009](0009-capability-api-v2-contract.md) | Capability API v2 contract: named actors, idempotency keys, coded errors | Accepted |
| [0010](0010-immutable-approval-envelopes.md) | Approvals are bound to immutable, expiring envelopes | Accepted |
| [0011](0011-versioned-deterministic-shopping-plans.md) | Shopping plans are immutable versions built by a deterministic rule | Accepted |
| [0012](0012-hard-constraints-and-soft-preferences.md) | Hard constraints and soft preferences are different types | Accepted |
| [0013](0013-household-data-lifecycle.md) | One JSON export is the household's portable copy and its backup | Accepted |
| [0014](0014-instacart-developer-platform-as-first-handoff-adapter.md) | Build the Instacart shopping list link as the first handoff adapter | Accepted, with conditions |

A status of "Accepted, with conditions" means the decision stands and work
may start, but the ADR names something a person must still do before part
of it takes effect. The ADR's own section lists the conditions.

The product backlog these decisions serve is
[`docs/OPENCLAW_ERP_BACKLOG.md`](../OPENCLAW_ERP_BACKLOG.md).
