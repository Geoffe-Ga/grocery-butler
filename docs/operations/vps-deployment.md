# Deploying on the household's server

How to run Grocery Butler on the server OpenClaw already runs on
([ADR 0021](../adr/0021-self-host-the-dogfood-on-the-openclaw-server.md)).
Every command below is run on that server, over SSH, unless it says
otherwise.

The layout: two containers from `deploy/vps/compose.yaml`, the app and
Postgres. Both listen only on the server's loopback address. OpenClaw calls
the app directly. People reach the dashboard over the tailnet through
`tailscale serve`.

CI starts this same stack nightly with `./scripts/compose-smoke.sh` and
checks it over HTTP, so a compose file or image that can't boot is found
before you deploy it.

## What the server needs

- Docker with the compose plugin (`docker compose version` prints a version),
  running, and usable by your account: `docker ps` prints a header row and no
  error. If it doesn't, see the first two rows of
  [When something is wrong](#when-something-is-wrong).
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
   ```

   You want `200`: the guard admits a request made on the server. If you
   get `403`, `docker compose logs app | grep network_guard_rejected` shows
   a line whose `remote_addr` is the address Docker really delivers requests
   from. Stop and report that address rather than widening the list by
   guesswork: it means the assumption in ADR 0021 is wrong for this server.

   Then check the guard keeps something out. This asks from a second
   container on the same Docker network, an address that is not on the
   allow-list. It uses the image you just built:

   ```bash
   docker run --rm --network grocery-butler_internal --entrypoint python grocery-butler-app -c "
   import urllib.request, urllib.error
   try:
       print(urllib.request.urlopen('http://app:8000/').status)
   except urllib.error.HTTPError as e:
       print(e.code)
   "
   docker compose logs app | grep network_guard_rejected
   ```

   You want `403`, then a `network_guard_rejected path=/ remote_addr=172.30.198.x`
   line. A `200` here means the guard is not enforcing: stop and report it.

   The guard's own startup line (`network_guard_enabled ...`) is logged at
   info level, which gunicorn does not print, so don't look for it. A
   rejection is a warning and does show.

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
   5432 (or your `DB_HOST_PORT`). On the server, `sudo ss -ltnp | grep -E ':(8000|5432)\b'` should
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

A timer on the server takes an encrypted backup every night and keeps the
newest fourteen. Once a week you copy one off the server. The nightly copy
covers a mistake or a bad migration; only the weekly copy covers losing the
server.

The backup scripts run on the server, outside the containers, and reach
Postgres on its loopback port.

### Set it up once

```bash
cd ~/grocery-butler
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -e .

mkdir -p ~/.config/grocery-butler ~/.config/systemd/user
cp deploy/vps/backup.env.example ~/.config/grocery-butler/backup.env
chmod 600 ~/.config/grocery-butler/backup.env
```

Open `~/.config/grocery-butler/backup.env` in an editor and fill in the
three values. The passphrase also goes in your password manager: lose it and
the backups can't be read. It sits on the server so the timer can run
unattended. That costs little, because anyone who has the server already
has the live database; the passphrase protects the copies that leave it.

The units expect the checkout at `~/grocery-butler`. Install and start the
timer, and let it run while you are logged out:

```bash
cp deploy/vps/systemd/grocery-butler-backup.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now grocery-butler-backup.timer
sudo loginctl enable-linger "$USER"
```

Run it once by hand to see it work, and check the result:

```bash
systemctl --user start grocery-butler-backup.service
systemctl --user status grocery-butler-backup.service
ls -l "$(grep '^BACKUP_DIR=' ~/.config/grocery-butler/backup.env | cut -d= -f2)"
```

### Every week

1. **Check the timer ran.** `systemctl --user list-timers grocery-butler-backup.timer`
   shows the last and next run, and the newest file in the backup directory
   should be dated today or yesterday. A failed night shows in
   `journalctl --user -u grocery-butler-backup.service`.
2. **Copy the newest backup off the server**, from your own machine:

   ```bash
   scp <server>:<backup directory>/<newest file> ~/grocery-butler-backups/
   ```

3. **Write the file's date in the week's log.** A backup that lives only on
   the server is lost with the server.

### Before a deploy that includes a migration

Take one by hand first, so it has a name the nightly pruning leaves alone:

```bash
cd ~/grocery-butler && . .venv/bin/activate
set -a; . ~/.config/grocery-butler/backup.env; set +a
./scripts/backup.sh "$BACKUP_DIR/before-$(date -u +%Y-%m-%d)-deploy.json.gpg"
```

### Restore

Stop the app first so nothing writes: `docker compose stop app`. Run
`./scripts/restore.sh` as the [data lifecycle runbook](data-lifecycle.md)
describes, then `docker compose start app`. That runbook also has the
restore drill.

## Logs

```bash
cd grocery-butler/deploy/vps
docker compose logs --tail 200 app
docker compose logs app | grep '"correlation_id"'
```

Docker keeps ten files of 10 MB per service and drops the oldest. Audit
events (ADR 0015) live only there.

## Moving data from an older deployment

The dogfood starts with an empty database (ADR 0021), so this is not part of
the first deploy. Delete the old deployment and its database once the server
is confirmed, so no copy of household data is left behind.

If you later find an earlier deployment does hold data worth keeping: take a backup of it with
`./scripts/backup.sh` (its `DATABASE_URL`), then restore that file into this
server's database as above. Both sides are Postgres, which a restore
requires. Then shut the old deployment down.

## When something is wrong

| What you see | What to do |
|---|---|
| `permission denied while trying to connect to the docker API` | Your account is not in the `docker` group. `sudo usermod -aG docker "$USER"`, then log out and back in. Being in that group is as good as root on the server; use `sudo docker ...` each time instead if you'd rather not. |
| `Cannot connect to the Docker daemon` | The daemon is not running. `sudo systemctl enable --now docker.socket`, then `sudo systemctl start docker`. If the log (`sudo journalctl -u docker -n 40`) says `no sockets found via socket activation`, it is the socket unit that was stopped; run `sudo systemctl reset-failed docker` first. |
| `docker compose up` says a variable must be set | Fill it in `deploy/vps/.env`. |
| The app container keeps restarting | `docker compose logs app`. A missing secret or a failed migration is named on the last lines. |
| `403` from `127.0.0.1:8000` | Step 5 above. |
| The tailnet address doesn't load | `tailscale serve status`, and check HTTPS certificates are on for the tailnet. |
| `docker compose up` says a pool or subnet overlaps | Another Docker network on the server already uses `172.30.198.0/24`. Report it rather than editing one side: the subnet, its gateway and the guard's allow-list in `compose.yaml` must change together, and a test holds them to each other. |
| `failed to bind host port 127.0.0.1:5432` | Another Postgres on the server has the port. Add `DB_HOST_PORT=5433` (or any free port) to `deploy/vps/.env` and run `docker compose up --detach` again. Use that port in the `DATABASE_URL` in `backup.env`, and in step 7's check. Nothing inside Docker changes. |
| `failed to bind host port 127.0.0.1:8000` | Something else on the server uses 8000. Report it rather than changing the port by hand: the port is also in the compose file, `tailscale serve` and OpenClaw's base URL. |
