#!/usr/bin/env bash
# scripts/compose-smoke.sh - Start the real deployment and check it answers.
#
# Builds and starts deploy/vps/compose.yaml (the app image and Postgres)
# under a throwaway project name, with secrets generated for this run, and
# checks over HTTP that the thing we deploy actually works:
#
#   1. Both containers become healthy (the app migrates Postgres on boot).
#   2. /health answers.
#   3. The dashboard answers 200, not 403: the network boundary guard admits
#      the address Docker really delivers requests from (ADR 0021, #110).
#   4. A signed API call writes a member and a second one reads it back.
#   5. The same call without a token is refused.
#   6. The member is still there after the app restarts.
#   7. Both published ports are bound to 127.0.0.1.
#
# Every other test runs the app in-process on SQLite. This is the only one
# that runs the image, gunicorn, the entrypoint and Postgres together.
#
# Not part of ./scripts/check-all.sh or the CI gate: it needs a Docker
# daemon and takes minutes. .github/workflows/compose-smoke.yml runs it
# nightly and when the image or the compose file changes.
#
# Usage: ./scripts/compose-smoke.sh [--verbose]
#
# Needs Docker with the compose plugin, curl and openssl. It uses its own
# project name and volume, and Docker fails the start if ports 8000 or 5432
# are already taken, so it can't disturb a real deployment on the same
# machine. Everything it creates is removed on exit.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

[[ "${1:-}" == "--verbose" ]] && set -x

PROJECT="grocery-butler-smoke"
BASE="http://127.0.0.1:8000"
MEMBER="smoke"
WAIT_SECONDS=240

for tool in docker curl openssl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "✗ $tool is not installed. Install it to run the smoke test." >&2
        exit 2
    fi
done

WORK="$(mktemp -d)"
COMPOSE=(docker compose --project-name "$PROJECT" --env-file "$WORK/env"
    --file deploy/vps/compose.yaml)

cleanup() {
    "${COMPOSE[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

fail() {
    echo "✗ $1" >&2
    echo "--- container logs ---" >&2
    "${COMPOSE[@]}" logs --no-color --tail 200 >&2 || true
    exit 1
}

# Secrets for this run only. They never leave the temporary directory.
umask 077
{
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 32)"
    echo "RUBOTPAUL_SHARED_SECRET=$(openssl rand -hex 32)"
    echo "FLASK_SECRET_KEY=$(openssl rand -hex 32)"
} > "$WORK/env"

# Print the status code of a request; the body goes to $WORK/body.
# A request that can't connect prints 000 instead of failing the script.
http() {
    local code
    code="$(curl --silent --show-error --max-time 20 \
        --output "$WORK/body" --write-out '%{http_code}' "$@")" || code="000"
    echo "$code"
}

# Mint a bearer token inside the app container, where the code and the
# shared secret already are. Arguments: method, path, body.
token_for() {
    "${COMPOSE[@]}" exec -T app python -c '
import sys
from grocery_butler.auth_middleware import RequestBinding, mint_token
method, path, body = sys.argv[1:4]
print(mint_token("smoke", RequestBinding.of(method, path, body.encode())))
' "$1" "$2" "$3"
}

expect() {
    [[ "$2" == "$3" ]] || fail "$1: expected $2, got $3"
    echo "✓ $1"
}

echo "=== Compose smoke test ==="

"${COMPOSE[@]}" up --detach --build --wait --wait-timeout "$WAIT_SECONDS" \
    >/dev/null || fail "The stack did not become healthy."
echo "✓ Both containers are healthy"

expect "/health answers" 200 "$(http "$BASE/health")"
expect "The guard admits a request from this machine" 200 "$(http "$BASE/")"

MEMBERS_PATH="/api/v2/members"
BODY="{\"key\": \"$MEMBER\", \"display_name\": \"Smoke test\"}"
TOKEN="$(token_for POST "$MEMBERS_PATH" "$BODY")" \
    || fail "Could not mint a token in the app container."
expect "A signed request creates a member" 201 "$(http --request POST \
    --header "Authorization: Bearer $TOKEN" \
    --header "Content-Type: application/json" \
    --header "Idempotency-Key: compose-smoke-0001" \
    --data "$BODY" "$BASE$MEMBERS_PATH")"

expect "The same request without a token is refused" 401 "$(http --request POST \
    --header "Content-Type: application/json" \
    --header "Idempotency-Key: compose-smoke-0002" \
    --data "$BODY" "$BASE$MEMBERS_PATH")"

read_members() {
    local token
    token="$(token_for GET "$MEMBERS_PATH" "")" \
        || fail "Could not mint a token in the app container."
    http --header "Authorization: Bearer $token" "$BASE$MEMBERS_PATH"
}

expect "A signed request reads members" 200 "$(read_members)"
grep --quiet "\"$MEMBER\"" "$WORK/body" || fail "The member was not stored."
echo "✓ The member is in Postgres"

"${COMPOSE[@]}" restart app >/dev/null || fail "The app did not restart."
"${COMPOSE[@]}" up --detach --wait --wait-timeout "$WAIT_SECONDS" >/dev/null \
    || fail "The app did not become healthy after a restart."
expect "The app answers after a restart" 200 "$(read_members)"
grep --quiet "\"$MEMBER\"" "$WORK/body" \
    || fail "The member did not survive a restart."
echo "✓ The member survived a restart"

expect_loopback() {
    local address
    address="$("${COMPOSE[@]}" port "$1" "$2")" \
        || fail "No published port for $1."
    [[ "$address" == 127.0.0.1:* ]] \
        || fail "$1 is published on $address, not on 127.0.0.1."
}
expect_loopback app 8000
expect_loopback db 5432
echo "✓ Both ports are bound to 127.0.0.1"

echo "✓ The deployment starts, migrates, authenticates and keeps its data"
