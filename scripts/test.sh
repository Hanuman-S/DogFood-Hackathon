#!/usr/bin/env bash
# Run the test suite inside the web image, against the compose Postgres.
#
#   ./scripts/test.sh                 # everything
#   ./scripts/test.sh tests/test_deadlines.py -vv
#
# Tests run in the container rather than on the host so that the Python version, the pinned
# dependencies and the database are the same ones the portal runs on. There is nothing to
# install on the host beyond Docker.
#
# The repo is bind-mounted at /workspace so the tests run against the working tree, not whatever
# was baked into the image at build time. An empty mount is an error (exit 5), never a pass.
#
# `--ds` is not redundant with pytest.ini: the image sets DJANGO_SETTINGS_MODULE=config.settings,
# and pytest-django lets that env var beat the ini file -- which silently runs the suite on the
# production settings (manifest static storage, Argon2) instead of config.settings_test.

set -euo pipefail

cd "$(dirname "$0")/.."

# The host path Docker should mount. Git Bash shows some folders under its own names (the Windows
# temp folder is `/tmp`, a checkout may be `/c/...`), and Docker would take `/tmp/...` as a path
# inside its VM: an empty /workspace, zero tests collected. `pwd -W` gives the Windows path under
# Git Bash; elsewhere it fails and plain `pwd` is used (same as offline-check.sh).
HOST_DIR="$(pwd -W 2>/dev/null || pwd)"

# The database must be up; `run` starts its dependencies, and the compose healthcheck gate
# means it is accepting connections before pytest tries to create the test database.
status=0
docker compose run --rm     -v "${HOST_DIR}:/workspace"     -w /workspace     --entrypoint python     web -m pytest --ds=config.settings_test "$@" || status=$?

# pytest exits 5 when it collected nothing: a wrong mount looks exactly like that, so say so
# instead of letting an empty run pass for a green one.
if [ "$status" -eq 5 ]; then
    echo "error: no tests were collected. Is ${HOST_DIR} mounted into the container?" >&2
    echo "  On Windows Git Bash, run as: MSYS_NO_PATHCONV=1 ./scripts/test.sh" >&2
fi
exit "$status"
