# ADR 0019: Check reminders are rate-limited suggestions about kept items we no longer know about

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (design delegated in #169; review this before building on it)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) P1.10; issue #169; builds on ADR 0007, ADR 0015 and ADR 0017

## Context

Inventory goes stale. The ledger says so (ADR 0007), but nothing asks anyone
to look. A reminder for every stale item would turn the product into the
inbox it is meant to remove. The backlog asks for prompts only about stale
or uncertain high-priority items, with snooze, dismiss, "stop asking", a
rate limit and a learned time of day.

## Decision

1. **A candidate is a kept item we no longer know about.** Kept means its
   policy is `always_keep` or `replenish_when_low` (ADR 0017). Not knowing
   means one of: never checked, stale (past its freshness window), or
   uncertain (the last observation's confidence is below 0.8). An item
   bought only for meals, or with no policy, is never a candidate.
2. **Ranked by how much it matters.** `always_keep` before
   `replenish_when_low`, then the longest unchecked, then by name.
3. **A daily limit, 3 by default.** The household sets it with the
   `check_reminders_per_day` preference, 0 to 20; 0 turns reminders off. A
   missing or unreadable value falls back to the default: a broken setting
   must never remove the limit. The day is the UTC day.
4. **Reading and issuing are separate.** `GET /reminders/checks` shows what
   could be asked and records nothing. `POST /reminders/checks/issue` takes
   the next few, never more than today's remaining allowance, and records
   each as issued with who took it.
5. **An issued item rests for three days.** It isn't issued again inside
   that time, so one unanswered question isn't repeated every morning. A
   fresh, confident observation removes it from the candidates altogether.
6. **Three holds a member can place, each recorded with its actor.**

   | Hold | Lasts until |
   |------|-------------|
   | Snooze | The number of days asked for, 1 to 90 |
   | Dismiss | New evidence arrives: any observation after the one it was dismissed against |
   | Stop asking | A member turns it back on |

   Resume clears all three.
7. **The suggested hour is learned from the household's own reports.** It
   is the most common UTC hour at which members recorded observations
   themselves in the last 60 days, the earliest on a tie, and nothing until
   there are five. Photo batches and receipts don't count: they say when a
   job ran, not when a person looks at the kitchen.
8. **Reminders are suggestions to OpenClaw.** Nothing here sends anything.
   OpenClaw decides whether, when and how to ask.

## Consequences

**Easier:**

- The household is asked about a few things that matter, not everything
  that aged.
- "Stop asking about salt" works and stays worked.
- Every reminder and every choice has an actor and a time.

**Harder:**

- A new install has a dozen staples nobody has checked, so the first days'
  reminders are all "never checked". The limit keeps that to three a day,
  and one kitchen scan clears them.
- The hour is UTC. OpenClaw must convert it to the household's time zone.
- The limit is per day, not per conversation. OpenClaw can still choose to
  ask all three at once.
- Issue is read-then-write, not one atomic claim. Two simultaneous issue
  calls could together exceed the limit by the size of one call. With one
  assistant per household that is accepted; a multi-writer deployment
  should claim rows instead.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Remind about every stale item | That is the inbox. |
| Let OpenClaw rate-limit itself | The limit is a household setting and has to hold whichever channel asks. |
| Dismiss for a fixed time | Then it is a snooze. "Until something changes" is what a person means by dismissing. |
| Learn the hour from when reminders are answered | There are too few answers early on, and it would chase its own timing. |
| Send notifications from here | Delivery belongs to OpenClaw (out of scope in #169). |
