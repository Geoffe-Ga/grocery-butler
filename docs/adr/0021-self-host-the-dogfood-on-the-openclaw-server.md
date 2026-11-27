# ADR 0021: Self-host the dogfood on the server OpenClaw already runs on

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (chose the server on 2026-11-26; the compose layout and the `tailscale serve` front are proposed here for review)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) delivery sequence, dogfood; issues #110, #173 and #196; builds on ADR 0002, ADR 0013 and ADR 0015

## Context

The docs describe one deployment, on Railway, reached over the tailnet.
That shape has two weak points for a household pilot:

- **The boundary is unproven.** Railway puts a shared public proxy in front
  of the app. The network boundary guard has to tell tailnet traffic from
  public traffic by the peer address that proxy and a Tailscale sidecar
  present, and that has never been checked on a live service (#110).
- **The caller is somewhere else.** OpenClaw, the only API client, runs on a
  server the household already operates. Every call would leave that server,
  cross the tailnet and come back.

That server is on the tailnet, is reached by SSH, and can run Docker.
OpenClaw is installed on it directly and calls its other tools over HTTP.

## Decision

1. **The dogfood runs on the OpenClaw server**, from
   `deploy/vps/compose.yaml`: the app, built from this repository's
   `Dockerfile`, and Postgres 16 with its data in a named volume. Railway
   stops being a documented target.
2. **Nothing listens on a public address.** Both published ports are bound
   to `127.0.0.1`. The compose file is tested for that.
3. **OpenClaw calls `http://127.0.0.1:8000`.** The request never leaves the
   server, so it needs no TLS. The HMAC bearer token is still required.
4. **People reach the dashboard through `tailscale serve`**, which gives it
   an HTTPS address on the tailnet and forwards to the loopback port.
   Tailscale Funnel, which would publish it to the internet, is never
   enabled.
5. **The guard's allow-list is loopback plus one address.** A request to
   the published port reaches the app from the compose network's gateway.
   The network's subnet is pinned so the compose file can name that gateway
   as a `/32`. The runbook has the operator confirm it from the app's logs
   on first start.
6. **Secrets live in `deploy/vps/.env` on the server**, mode 600, ignored by
   git and left out of the image. A missing required secret stops
   `docker compose up`.
7. **A deploy is a person running two commands** on the server: `git pull`,
   then `docker compose up --detach --build`. The entrypoint migrates before
   gunicorn starts, as before. Nothing deploys on a push.
8. **Backups run on the server and are copied off it** (ADR 0013). A
   systemd timer takes an encrypted backup every night and keeps the newest
   fourteen; a person copies one off the server every week (Geoff,
   2026-11-26). The passphrase therefore lives in a file on the server,
   readable only by its owner. The database port is published on loopback
   for the backup scripts.

9. **The server starts with an empty database** (Geoff, 2026-11-26). Nothing
   is moved from the old deployment. Members, constraints and items are
   entered in the dogfood's first week, as its protocol already asks.

10. **Verified on the server on 2026-11-27.** A request made on the server
    was admitted (200). The same request from a second container on the
    compose network, at `172.30.198.4`, was refused (403) and logged. Both
    published ports were bound to `127.0.0.1`. The compose smoke test now
    makes both requests on every run.

## Consequences

- The boundary no longer rests on what a third party's proxy reports. The
  operating system refuses a public connection before the app sees it, and
  the guard remains as a second check.
- The guard can no longer tell one tailnet device from another: everything
  arrives from the gateway. Who may reach the dashboard is decided by the
  tailnet's own access rules. ADR 0002 already assumes one trusted
  household.
- The dashboard is HTTPS-only for people, which is what #196 was waiting
  for before setting the session cookie's `Secure` flag.
- The household now operates a database. Disk loss on the server loses
  everything since the last backup that was copied off it. The weekly
  backup in the dogfood protocol matters more than it did.
- Deploys are manual and can lag `main`. For one household that is a
  feature: a migration runs when someone is watching, after a backup.
- Audit events (ADR 0015) are Docker's log files on the server, rotated at
  ten files of 10 MB per service. Older lines are gone.
- The app shares a server with OpenClaw. If either exhausts memory or disk,
  both suffer.
- Whatever the old Railway deployment holds is left behind. Until that
  deployment and its database are deleted, a copy of old household data
  stays with a provider the household no longer uses.

## Alternatives considered

- **Stay on Railway.** Managed Postgres and deploys on push. Rejected: the
  boundary check is still open, the public proxy is one more party handling
  household data, and the only API client is elsewhere.
- **Run without Docker, as a systemd service in a virtualenv.** Closest to
  how OpenClaw itself is installed. Rejected: it adds a second, untested
  way to build the app. The image is what CI builds and scans.
- **Host networking, with gunicorn bound to loopback.** The guard would see
  true peer addresses. Rejected: the image binds `0.0.0.0`, so one missed
  setting would put the app on the server's public address.
- **Bind the app to the server's tailnet address instead of
  `tailscale serve`.** No extra component. Rejected: plain HTTP on the
  tailnet, so no `Secure` cookie, and the port publish fails if Tailscale
  starts after Docker.
- **SQLite in a volume instead of Postgres.** One container. Rejected:
  production has always been Postgres, CI tests against it, and a backup
  restores only into the dialect it came from.
