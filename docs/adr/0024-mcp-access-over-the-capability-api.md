# ADR 0024: Agents reach Grocery Butler over MCP, through a wrapper around the capability API

- **Status:** Accepted, with conditions
- **Date:** 2026-11-27
- **Decider:** Geoff (decision sheet `2026-11-27-mcp-access` and three follow-up answers, 2026-11-27; the details below are proposed for review)
- **Source:** Epics #241 and #242; builds on ADR 0002, ADR 0009, ADR 0010, ADR 0016 and ADR 0021

## Context

One agent operates Grocery Butler today. OpenClaw reads a skill file and
runs `openclaw/grocery-butler/gb.py`, which signs each `/api/v2` request
with a shared secret (ADR 0009). That works for an agent that can run a
script on the household's server. It does not work for an agent that
can't: most assistants connect to outside systems through the Model
Context Protocol (MCP), where a server offers named tools with typed
inputs.

Geoff wants every persistent agent a person might use to be able to
operate Grocery Butler, and sees that as the route to serving more than
one household. Three settled decisions stand in the way of doing all of
that at once:

- ADR 0002 keeps the product to one household, with no login. A member's
  name is attribution, not authentication.
- The backlog gates multi-tenant work (P2.6) on evidence from the pilot.
- ADR 0021 keeps the household's server off the public internet.

So this ADR settles the shape of the MCP channel and records where the
hosted service is headed, without starting either.

## Decision

1. **MCP is a channel, and it wraps `/api/v2`.** The MCP server is a
   separate program. Each tool call becomes one signed `/api/v2` request.
   It imports nothing from `grocery_butler` and holds no business rule. A
   rule that is missing goes into a domain service and the API first.
2. **Tools are shaped around jobs, about a dozen of them,** not one per
   route. A test holds the tool list to the routes documented in
   `docs/api/v2.md`: every route belongs to exactly one tool.
3. **An MCP caller may do everything OpenClaw may,** including approving
   and executing a handoff. The API's own checks are unchanged: the draft's
   hash, the idempotency key, the acting member (ADR 0010, ADR 0016). Tool
   descriptions tell the agent to show the person the draft and get a yes.
4. **The server is built on the official `mcp` Python SDK, as an optional
   extra.** Its dependencies are pinned and audited, and are never
   installed in the production image (the precedent is ADR 0022).
5. **The first version serves one household, over stdio.** The agent's
   client starts the server on a machine that can reach the API. This adds
   no listener and changes nothing in ADR 0002 or ADR 0021. It is epic
   #241.
6. **The intended destination is a hosted service at
   `mcp.creekmasons.com`, on Railway,** reached over a public URL with
   OAuth, by any agent. People sign in through Clerk on a Creekmasons page
   and subscribe through Stripe. Geoff builds both in a separate
   repository; this repository verifies the tokens it is handed and reads
   whether a subscription is active. It is epic #242.
7. **The skill and `gb.py` stay.** MCP is a second way in, not a
   replacement.

### Conditions

- **Nothing is built yet.** Geoff asked for the plan only. Epic #241
  starts when he says so.
- **Epic #242 is blocked** until the P2.6 gate is lifted and a further ADR
  (#250) is accepted. That ADR must answer what this one leaves open:
  which API the hosted server wraps, since the household's API listens
  only on its own server; whether the Geoff household moves onto the
  hosted service; how a signed-in user maps to a household and a member;
  whether approval over a public channel needs a second step; and what a
  public service owes a household for export, deletion and notice (ADR
  0013 covers one household).
- **Until then ADR 0002 and ADR 0021 stand as written.** This ADR
  supersedes neither.

## Consequences

- An agent that speaks MCP can operate the household's data with the same
  rules, refusals and audit trail OpenClaw gets, because it goes through
  the same contract.
- There are two descriptions of the same capabilities to keep in step:
  the skill's tables and the tool list. Both are held to `docs/api/v2.md`
  by tests.
- A new dependency tree arrives (a web framework, an HTTP client, a token
  library and others). It is confined to the extra.
- In the stdio version the member is still whatever the agent says, as
  with OpenClaw. That is acceptable only while the caller holds the shared
  secret on a trusted machine. The hosted service must take the member
  from a verified token instead (#253).
- Full scope means a second agent can approve a purchase handoff. On the
  household's own machines that matches what OpenClaw does. On a public
  service the person is further away, which is why the follow-up ADR must
  decide on a second step.
- Choosing a wrapper means the hosted service needs a tenant-aware API
  behind it. The wrapper cannot isolate households by itself.

## Alternatives considered

- **Mount MCP inside the Flask app.** One process fewer, but a third
  channel to keep in step with `/api/v1` and `/api/v2`, an async
  streaming transport inside a synchronous app, and the SDK's dependencies
  in the production image.
- **A separate service on the same database.** It would have to repeat
  the member, idempotency and error handling the API's channel layer does,
  with two programs writing one database.
- **One tool per route.** Mechanical to generate, but 58 similar tools
  crowd an agent's context and make its permission prompts name routes
  instead of jobs.
- **One generic request tool.** Smallest to build. A client would ask
  permission once and the same prompt would then cover any action,
  including an approval.
- **Read-only, or read and record without approval.** Safer on a public
  channel. Geoff chose full scope so one agent can finish the job.
- **Write the protocol by hand with the standard library.** No new
  dependencies, as with `gb.py`, but the protocol changes between
  revisions and a hand-written server could not offer an HTTP transport.
- **Keep it on the tailnet,** with stdio only or a tailnet URL. That
  fits ADR 0021 as it is, and remains the first version. It cannot serve
  agents that connect from a vendor's cloud.
