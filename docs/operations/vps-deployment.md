# Deploying on the household's server

How to run Grocery Butler on the server OpenClaw already runs on
([ADR 0021](../adr/0021-self-host-the-dogfood-on-the-openclaw-server.md)).
Every command below is run on that server, over SSH, unless it says
otherwise.

The layout: two containers from `deploy/vps/compose.yaml`, the app and
Postgres. Both listen only on the server's loopback address. OpenClaw calls
the app directly. People reach the dashboard over the tailnet through
`tailscale serve`.

## What the server needs

- Docker with the compose plugin (`docker compose version` prints a version).
- Tailscale, signed in to the household's tailnet, with HTTPS certificates
  turned on for the tailnet (Tailscale admin console, DNS page).
- `git`, and for backups `gpg` and Python 3.11 or newer.

## First deploy

1. **Get the code.**

   ```bash
   git clone https://github.com/Geoffe-Ga/grocery-butler.git
   cd grocery-butler
   ```

   If the repository is private, give the server read access first, with a
   read-only deploy key or `gh auth login`.

2. **Write the secrets file.** It never leaves the server.

   ```bash
   cd deploy/vps
   cp env.example .env
   chmod 600 .env
   ```

   Open `.env` in an editor and fill in the three required values. Generate
   each with `openssl rand -hex 32`. `RUBOTPAUL_SHARED_SECRET` must be the
   same value OpenClaw uses. Don't paste any of them into a chat, an issue
   or a commit.

3. **Start it.**

   ```bash
   docker compose up --detach --build
   docker compose ps
   ```

   Both services should show `healthy` within a minute. The app migrates
   the database before it serves anything; if a migration fails the app
   container stops and `docker compose logs app` says why.

4. **Check it answers on the server.**

   ```bash
   curl --silent http://127.0.0.1:8000/health
   ```

5. **Confirm the boundary guard trusts the right address.** This is the
   check in #110, and it takes one request:

   ```bash
   curl --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8000/
   docker compose logs app | grep network_guard
   ```

   You want `200`, and a startup line
   `network_guard_enabled allowed_cidrs=127.0.0.0/8,::1/128,172.30.198.1/32`.
   If you get `403`, the log has a `network_guard_rejected` line whose
   `remote_addr` is the address Docker really delivers requests from. Stop
   and report that address rather than widening the list by guesswork: it
   means the assumption in ADR 0021 is wrong for this server.

6. **Put the dashboard on the tailnet.**

   ```bash
   sudo tailscale serve --bg 8000
   tailscale serve status
   ```

   The status shows an `https://<server>.<tailnet>.ts.net` address. Open it
   from a phone on the tailnet. Never run `tailscale funnel`: that publishes
   the dashboard to the internet.

7. **Check the public side is closed.** From a device that is *not* on the
   tailnet, the server's public address must not answer on port 8000 or
   5432. On the server, `sudo ss -ltnp | grep -E ':(8000|5432)\b'` should
   list only `127.0.0.1`.

8. **Point OpenClaw at it.** Set OpenClaw's Grocery Butler base URL to
   `http://127.0.0.1:8000/api/v1` (and `http://127.0.0.1:8000/api/v2` for
   the capability API). Plain HTTP is right here: the request never leaves
   the server. Use `127.0.0.1`, not `localhost`, which can resolve to an
   address the port is not bound to.

Then carry on with [Before week 1](dogfood-protocol.md#before-week-1).

## Updating

```bash
cd grocery-butler
git pull
cd deploy/vps
docker compose up --detach --build
docker compose ps
```

Take a backup first whenever the pull includes a new file under
`grocery_butler/db/migrations/`. Migrations are forward-only.

## Backups

The backup scripts run on the server, outside the containers, and reach
Postgres on its loopback port. Set them up once:

```bash
cd grocery-butler
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -e .
```

Then, each time:

```bash
cd grocery-butler
. .venv/bin/activate
export BACKUP_PASSPHRASE   # set its value from your password manager
export DATABASE_URL="postgresql://butler:$(grep '^POSTGRES_PASSWORD=' deploy/vps/.env | cut -d= -f2)@127.0.0.1:5432/grocery_butler"
./scripts/backup.sh "backups/$(date -u +%Y-%m-%d).json.gpg"
```

Copy the file off the server (for example with `scp`, run from your own
machine). A backup that lives only on the server is lost with the server.
The rest, including the restore drill, is in the
[data lifecycle runbook](data-lifecycle.md).

To restore, stop the app first so nothing writes: `docker compose stop app`,
run `./scripts/restore.sh` as that runbook describes, then
`docker compose start app`.

## Logs

```bash
cd grocery-butler/deploy/vps
docker compose logs --tail 200 app
docker compose logs app | grep '"correlation_id"'
```

Docker keeps ten files of 10 MB per service and drops the oldest. Audit
events (ADR 0015) live only there.

## Moving data from an older deployment

If an earlier deployment holds data worth keeping: take a backup of it with
`./scripts/backup.sh` (its `DATABASE_URL`), then restore that file into this
server's database as above. Both sides are Postgres, which a restore
requires. Then shut the old deployment down.

## When something is wrong

| What you see | What to do |
|---|---|
| `docker compose up` says a variable must be set | Fill it in `deploy/vps/.env`. |
| The app container keeps restarting | `docker compose logs app`. A missing secret or a failed migration is named on the last lines. |
| `403` from `127.0.0.1:8000` | Step 5 above. |
| The tailnet address doesn't load | `tailscale serve status`, and check HTTPS certificates are on for the tailnet. |
| `docker compose up` says a pool or subnet overlaps | Another Docker network on the server already uses `172.30.198.0/24`. Report it rather than editing one side: the subnet, its gateway and the guard's allow-list in `compose.yaml` must change together, and a test holds them to each other. |
| Port 5432 is already in use | Set `DB_HOST_PORT` in `.env` to a free port and use it in `DATABASE_URL` for backups. |
