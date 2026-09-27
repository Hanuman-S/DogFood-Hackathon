#!/bin/sh
# Boot: wait for Postgres, migrate, seed demo accounts (DEMO_MODE=1), import the organizers'
# fixture data (SEED_FIXTURES=1), then serve.
# Every step is idempotent: it runs on every restart and must never wipe real data.
set -eu

echo "=================================================================="
echo " DOGFOOD portal starting"
echo "=================================================================="

if [ -z "${DJANGO_SECRET_KEY:-}" ] && [ -n "${DJANGO_SECRET_KEY_FILE:-}" ]; then
    echo "--> SECRET_KEY: using the generated key in ${DJANGO_SECRET_KEY_FILE} (created now if missing)"
    python /app/src/config/secret_key.py "$DJANGO_SECRET_KEY_FILE"
fi

echo "--> waiting for the database"
tries=0
until python /app/src/manage.py shell -c "from django.db import connection; connection.ensure_connection()" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -ge "${DB_WAIT_ATTEMPTS:-60}" ]; then
        echo "    database unreachable after $tries attempts; last error:" >&2
        python /app/src/manage.py shell -c "from django.db import connection; connection.ensure_connection()" >&2 || true
        exit 1
    fi
    sleep 1
done

echo "--> applying migrations"
python /app/src/manage.py migrate --noinput

echo "--> removing expired sessions"
python /app/src/manage.py clearsessions

if [ "${DEMO_MODE:-0}" = "1" ]; then
    # Demo accounts first, so the fixture event can be handed to the demo organizer.
    echo "--> seeding demo accounts (create-only)"
    python /app/src/manage.py seed_demo
fi

if [ "${SEED_FIXTURES:-0}" = "1" ]; then
    echo "--> importing the organizers' fixture data (create-only; SEED_FIXTURES=0 to skip)"
    python /app/src/manage.py import_fixtures
    if [ "${DEMO_MODE:-0}" = "1" ]; then
        # Demo only: a closed community vote on the fixture event (needs the import above).
        python /app/src/manage.py seed_demo --votes
    fi
fi

if [ "${DEMO_MODE:-0}" != "1" ]; then
    echo "--> DEMO_MODE=0: no demo accounts. Create the first admin with:"
    echo "    docker compose exec web python src/manage.py create_account --email you@example.org --name You --admin"
fi

echo "=================================================================="
echo " Portal ready on ${PORTAL_BASE_URL:-http://localhost:8080}"
echo "=================================================================="
exec "$@"
