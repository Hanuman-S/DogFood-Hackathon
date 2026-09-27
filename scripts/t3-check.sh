#!/usr/bin/env bash
# Run the T3 (community voting) probes against the running portal and save their output.
#
#   docker compose up -d        # portal must be up first (a fresh boot: docker compose down -v first)
#   ./scripts/t3-check.sh
#
# Writes t3-report.txt in the repo root, exactly as the probes printed it -- failures included.
# The report is committed as-is; there is nothing to edit by hand. The probes are
# scripts/t3_check.py (standard library only); they read acceptance/run.py's helpers but never
# change anything under acceptance/. What they change on the demo data is listed at the top of
# scripts/t3_check.py.

set -euo pipefail

cd "$(dirname "$0")/.."

CONFIG=".dogfood.toml"
REPORT="t3-report.txt"

# The checker is standard-library-only and runs on any Python 3. `python3` is the usual name, but
# on Windows it is often a Microsoft Store stub that exits without running anything, so fall back
# to `python` and check the version rather than trusting the name.
find_python() {
    for candidate in python3 python py; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' \
                >/dev/null 2>&1; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

if ! PYTHON="$(find_python)"; then
    echo "error: no Python 3 interpreter found on PATH." >&2
    echo "  The checker needs any Python 3 and no dependencies." >&2
    exit 1
fi

BASE_URL="$(sed -n 's/^base_url[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' "$CONFIG" | head -1)"
BASE_URL="${BASE_URL:-http://localhost:8080}"

# Fail early with a clear message rather than letting every check report "no response", which
# looks like seven bugs instead of one stopped container.
if ! "$PYTHON" -c "
import sys, urllib.request
try:
    urllib.request.urlopen('${BASE_URL}/healthz', timeout=5)
except Exception as exc:
    print(f'  {type(exc).__name__}: {exc}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null; then
    echo "error: the portal is not answering at ${BASE_URL}/healthz" >&2
    echo "  Start it first:  docker compose up -d" >&2
    echo "  Then wait for:   docker compose ps   (web must be 'healthy')" >&2
    exit 1
fi

echo "running the T3 probes with ${PYTHON} against ${BASE_URL}..."

# The checker always exits 0, even when checks fail -- it reports rather than gates -- so the
# pipeline below does not need to tolerate a non-zero status.
"$PYTHON" scripts/t3_check.py "$CONFIG" | tee "$REPORT"

echo
echo "written to ${REPORT}"
