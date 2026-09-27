#!/bin/sh
# Container entrypoint: get the database ready, get the data in, then serve.
#
# Every step here is idempotent. The entrypoint runs on every boot -- including a restart of
# a long-lived deployment -- so nothing in it may wipe, reset or overwrite data a real user
# created. `migrate` is idempotent by design; the data-loading steps added in phase 2 upsert
# by stable external id.
#
# POSIX sh, not bash: the slim image has no bash and there is no reason to install one.

set -eu

echo "=============================================================="
echo " Dogfood portal starting"
echo "=============================================================="

# --- 1. wait for postgres -------------------------------------------------------------
#
# compose already gates `web` on the database's healthcheck, so this loop should normally
# pass on its first attempt. It stays because `depends_on` only covers compose: anyone
# running this image against an external database (which is the whole point of a
# self-hostable portal) gets the same protection.
#
# The probe uses Django's own connection, so it proves the exact thing that matters -- these
# credentials, this database, reachable by this driver -- rather than merely that a port is
# open.
echo "--> waiting for the database"
ATTEMPTS=0
MAX_ATTEMPTS="${DB_WAIT_ATTEMPTS:-60}"
DB_PROBE='
from django.db import connection
connection.ensure_connection()
'
until python /app/src/manage.py shell --command "$DB_PROBE" >/dev/null 2>&1; do
    ATTEMPTS=$((ATTEMPTS + 1))
    if [ "$ATTEMPTS" -ge "$MAX_ATTEMPTS" ]; then
        echo "    database still unreachable after ${MAX_ATTEMPTS} attempts; giving up." >&2
        echo "    last error:" >&2
        # Re-run without silencing output so the operator sees the actual failure rather
        # than only the timeout.
        python /app/src/manage.py shell --command "$DB_PROBE" >&2 || true
        exit 1
    fi
    sleep 1
done
echo "    database is reachable"

# --- 2. schema ------------------------------------------------------------------------
echo "--> applying migrations"
python /app/src/manage.py migrate --noinput

# --- 3. shared fixture data -----------------------------------------------------------
#
# The organizers' fixture file is bind-mounted read-only by compose. If it is missing -- someone
# running the bare image without that mount -- the command says so and returns successfully, so
# the portal boots empty instead of crash-looping on a missing input file.
#
# SEED_FIXTURES=0 turns this off. A production deployment does not want 121 invented users and a
# closed demo event appearing in its database, and an operator should not have to comment out a
# line in an entrypoint to prevent it. It defaults to on because `docker compose up` must reach a
# seeded portal -- the acceptance checker looks for fixture project titles in the gallery.
#
# Note the import is create-only: it inserts missing rows and never modifies existing ones, so
# running it on every boot cannot revert an organizer's edits. `--sync` opts into overwriting and
# is never used here.
if [ "${SEED_FIXTURES:-1}" = "1" ]; then
    echo "--> importing organizer fixtures (create-only; set SEED_FIXTURES=0 to skip)"
    python /app/src/manage.py import_fixtures
else
    echo "--> SEED_FIXTURES=0: skipping the fixture import."
    echo "    The portal starts with whatever is already in the database."
fi

# --- 4. demo data and credentials -----------------------------------------------------
#
# seed_demo refuses to run unless DEMO_MODE=1. That guard lives inside the command, not in this
# branch, so invoking it directly cannot bypass it.
if [ "${DEMO_MODE:-1}" = "1" ]; then
    echo "--> seeding demo accounts, tokens and the open demo event"
    python /app/src/manage.py seed_demo
else
    echo "--> DEMO_MODE=0: no demo users, no fixed tokens, no known passwords."
    echo "    Bootstrap an admin with:"
    echo "      docker compose exec web python /app/src/manage.py createsuperuser"
fi

echo "=============================================================="
echo " Portal ready on http://localhost:8080"
echo "=============================================================="

# `exec` so gunicorn becomes PID 1 and receives SIGTERM directly: `docker compose down` then
# shuts workers down gracefully instead of waiting out the kill timeout.
exec "$@"
