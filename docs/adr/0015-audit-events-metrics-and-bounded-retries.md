# ADR 0015: Audit events as structured logs, five metrics read on demand, and one bounded retry

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (design delegated in #172; review this before building on it)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) P1.16; issue #172; builds on ADR 0007, ADR 0008 and ADR 0009

## Context

During the dogfood, a failure has to be visible to one person reading logs
and one assistant reading an API. Today a model reply that can't be parsed
is a warning line and an empty result; nothing ties the lines of one request
together; nothing counts. The backlog asks for audit events with correlation
ids, metrics for extraction failures, correction rate, stale inventory,
adapter failures and reconciliation gaps, and tested retry behavior. A hosted
dashboard or alerting service is out of scope.

## Decision

1. **An audit event is one line of JSON on the `grocery_butler.audit`
   logger.** It carries the event name, a UTC time, a correlation id and a
   few fields. Events are logs, not rows: the durable record of what changed
   is already in the domain's own history tables (ADR 0007, ADR 0008).
2. **Every request gets a correlation id.** A caller may send
   `X-Correlation-ID` (8 to 64 letters, digits, hyphens or underscores);
   otherwise one is made. It is returned on every response and is on every
   event the request emits. An id that doesn't match the pattern is replaced,
   so a caller can't forge a log line.
3. **Every state-changing request emits `http.request`.** The hook is
   app-wide and registered first, so it also records requests that
   authentication, the tailnet guard or CSRF reject. It records the method,
   the route pattern (never the raw path), the status and the acting member.
4. **A capture session has its own id.** A batch is submitted in one request
   and reviewed in another. Its events carry `capture_session`
   (`batch-<id>`) as well as each request's correlation id. Guided capture
   sessions (#167) will set the same field.
5. **Nothing sensitive is logged.** A field whose name suggests an image,
   bytes or a secret is redacted whatever its value. Raw bytes are redacted
   under any name. Long text is cut at 500 characters. Extraction events
   record a stage and a reason, never the prompt or the reply.
6. **Five metrics, read on demand** at `GET /api/v2/operations/metrics`:

   | Metric | Source |
   |--------|--------|
   | Extraction failures | Counted when `extraction.failed` is emitted |
   | Adapter failures | Counted when `adapter.failed` is emitted |
   | Correction rate | Derived from decided proposals |
   | Stale inventory | Derived from the observation ledger |
   | Reconciliation gaps | Derived from needs ordered more than 3 days ago |

   The two counted metrics leave no other trace, so they are stored as one
   row per counter per day (`operational_counters`, migration 018). The
   other three are computed from rows that already exist and so can't drift.
7. **Counting hangs off the audit event.** The code that detects a failure
   emits one event. A counter bound to the request advances if the event is
   a counted one. Domain code never touches the metrics store, and a
   counting failure is logged without breaking the work.
8. **One retry helper, for calls that leave the process.**
   `call_with_retry` tries at most three times, pausing 0.5 s then 1 s
   (doubling, capped at 8 s), and emits `call.attempt` for every try. It
   retries timeouts, connection failures and a provider's 408, 429 and 5xx.
   Anything else fails at once as itself. The Anthropic SDK's own retries
   are turned off so the two don't multiply.
9. **Every adapter handoff goes through one runner**
   (`fulfillment/runner.py`), which applies the retry and emits
   `adapter.handoff_created` or `adapter.failed`.

## Consequences

**Easier:**

- One id finds everything a request did. One field finds a whole capture
  session.
- OpenClaw can read the five numbers and tell the household when something
  is drifting.
- The Instacart adapter (#146 to #148) gets retries, failure counting and
  an audit trail by going through the runner.

**Harder:**

- Audit lines live wherever the platform keeps logs. Railway's log
  retention is the retention. Anything that must outlive it belongs in a
  domain table.
- A retried model call can hold a request for about 1.5 seconds longer.
- "Reconciliation gap" is defined on needs today, because handoff attempts
  and receipts don't exist yet. #148 and #150 should move the definition to
  attempts without a matching receipt.
- The audit logger is given its own stderr handler, because the root logger
  drops INFO by default and would silently discard the trail.

## Alternatives considered

| Option | Why not |
|--------|---------|
| An `audit_events` table | A second history beside the domain's own, holding the same facts, growing forever and needing its own retention rule. |
| In-process counters | Each gunicorn worker would count separately and forget on every deploy. |
| Prometheus, OpenTelemetry or a hosted service | Out of scope, and more to run than one household needs. The audit lines are structured so one can be added later. |
| Keep the SDK's retries | They work, but they are invisible: no event per attempt, and nothing a test can observe without the SDK's HTTP layer. |
| Retry inside each adapter | Every adapter would get it slightly differently, and one would forget to count its failures. |
