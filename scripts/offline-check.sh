#!/usr/bin/env bash
# Prove the running portal needs no network access, and run the organizers' checker against it.
#
#   ./scripts/offline-check.sh            # -> acceptance-report-offline.txt
#
# What this does, and why it is shaped this way:
#
# `docker-compose.offline.yml` marks the compose network `internal: true`, which removes its
# gateway. Neither container then has any route off that network -- no DNS, no PyPI, no CDN. The
# portal must still migrate, import, seed and serve every page, because every asset it needs is
# vendored into the image.
#
# An internal network also disables port publishing, so the host cannot reach :8080 and the
# checker has to run *inside* the sealed network. It runs in a one-off container built from the
# same image, talking to `http://web:8000`, which is why the report's `portal:` line differs from
# the one in `acceptance-report.txt`. Everything else about the two runs is identical -- same
# clone, same image, same fixture file.
#
# The image must already exist: BUILDING needs PyPI. That is the documented boundary -- "no
# runtime network", not "no network ever".

set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT="${OFFLINE_PROJECT:-dogfood-offline}"
REPORT="${1:-acceptance-report-offline.txt}"
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.offline.yml)

cleanup() {
    "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "--> building the image (this step needs the network)"
"${COMPOSE[@]}" build

echo "--> starting the stack on a network with no route off it"
"${COMPOSE[@]}" up -d

echo "--> waiting for the portal to report healthy"
for _ in $(seq 1 60); do
    status="$("${COMPOSE[@]}" ps --format '{{.Service}} {{.Status}}' | awk '/^web/ {print $0}')"
    case "$status" in
        *healthy*) break ;;
    esac
    sleep 3
done

IMAGE="$("${COMPOSE[@]}" images web --format '{{.Repository}}:{{.Tag}}' | tail -1)"
NETWORK="${PROJECT}_default"

echo "--> confirming the network really is sealed"
docker run --rm --network "$NETWORK" --entrypoint python "$IMAGE" -c "
import socket, sys, urllib.request

for host in ['pypi.org', 'cdn.jsdelivr.net', 'fonts.googleapis.com']:
    try:
        socket.gethostbyname(host)
        sys.exit(f'FAIL: {host} resolved -- the network is not isolated')
    except OSError:
        print(f'  {host}: no DNS, as required')
try:
    urllib.request.urlopen('http://1.1.1.1', timeout=5)
    sys.exit('FAIL: reached 1.1.1.1 -- the network is not isolated')
except Exception:
    print('  1.1.1.1: unreachable, as required')
print('  portal in-network:', urllib.request.urlopen('http://web:8000/healthz', timeout=5).status)
"

echo "--> running the organizers' checker from inside the sealed network"
# `.dogfood.toml` advertises localhost:8080 for the documented host-side run; inside the network
# the portal is `web:8000`. Only the base URL is rewritten, into a throwaway copy -- the
# organizers' file and ours are both left alone.
docker run --rm --network "$NETWORK" \
    -v "$(pwd):/probe:ro" \
    --entrypoint sh "$IMAGE" -c \
    'sed "s|http://localhost:8080|http://web:8000|" /probe/.dogfood.toml > /tmp/offline.dogfood.toml \
     && python /probe/acceptance/run.py /tmp/offline.dogfood.toml' \
    | tee "$REPORT"

echo
echo "--> written to $REPORT"
