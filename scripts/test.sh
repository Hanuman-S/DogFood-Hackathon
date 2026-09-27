#!/usr/bin/env bash
# Run the test suite inside the web image, against the compose Postgres.
#
#   ./scripts/test.sh                 # everything
#   ./scripts/test.sh tests/test_clock.py -vv
#
# Tests run in the container rather than on the host so that the Python version, the pinned
# dependencies and the database are the same ones the portal runs on. There is nothing to
# install on the host beyond Docker.
#
# The repo is bind-mounted at /workspace because `tests/` and `pytest.ini` are deliberately
# not baked into the image -- the image ships the application, not the test suite.

set -euo pipefail

cd "$(dirname "$0")/.."

# The database must be up; `run` starts its dependencies, and the compose healthcheck gate
# means it is accepting connections before pytest tries to create the test database.
exec docker compose run --rm \
    -v "$(pwd):/workspace" \
    -w /workspace \
    --entrypoint python \
    web -m pytest "$@"
