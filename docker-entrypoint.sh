#!/bin/sh
# Container entrypoint: run database migrations before starting the server.
#
# Railway (and any other Docker-based host) has no Heroku-style `release`
# phase, so migrations must run inside the container's own boot sequence
# instead (issue #58). `set -eu` aborts the container immediately if the
# migration step fails, rather than booting gunicorn against a stale schema.
set -eu

python -m grocery_butler.db.migrate

exec "$@"
