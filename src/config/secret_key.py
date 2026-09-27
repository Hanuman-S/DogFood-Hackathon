"""Where SECRET_KEY comes from. Standard library only: the entrypoint runs this before Django.

1. DJANGO_SECRET_KEY, if set (a real deployment should set it from its own secret store).
2. Otherwise the file at DJANGO_SECRET_KEY_FILE. On first boot the entrypoint creates it with a
   random key (`python config/secret_key.py <path>`), in a Docker volume -- never in the repo, never
   in the image -- so `docker compose up` stays one command, every install gets its own key, and
   the key survives restarts and rebuilds. `docker compose down -v` deletes it with the database.

Outside DEMO_MODE the portal refuses to start with a key anyone could know (`check`).

Everything keyed from SECRET_KEY uses its own derived key (core.keys), so no two purposes share a
key. Changing SECRET_KEY logs everyone out, invalidates every voter link and open-link cookie, and
resets the IP-based rate limits and integrity clustering (their hashes change).
"""

import os
import secrets
import sys

# Keys that appear in this repository or its history: any of them is public knowledge.
KNOWN_DEFAULT_KEYS = frozenset({
    "insecure-compose-default-change-me",
    "dev-only-insecure-key",
    "build-only",
    "test-only-secret",
    "change-me",
    "changeme",
    "secret",
})
MIN_LENGTH = 32


def read_key_file(path):
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def ensure_key_file(path):
    """The key in `path`, creating the file with a new random key if it does not exist yet. Created
    with O_EXCL and mode 0600, so two containers starting at once cannot both write one."""
    existing = read_key_file(path)
    if existing:
        return existing
    key = secrets.token_urlsafe(64)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return read_key_file(path)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    return key


def check(key, *, demo_mode):
    """A sentence if `key` must not be used, else "". Demo mode is a published-password laptop demo
    anyway, so there a known key is tolerated; anywhere else it is refused."""
    if not key:
        return "No SECRET_KEY: set DJANGO_SECRET_KEY, or DJANGO_SECRET_KEY_FILE for a generated one."
    if demo_mode:
        return ""
    if key in KNOWN_DEFAULT_KEYS:
        return "SECRET_KEY is a known default from this repository; set your own (DJANGO_SECRET_KEY)."
    if len(key) < MIN_LENGTH:
        return f"SECRET_KEY is too short ({len(key)} characters); use at least {MIN_LENGTH}."
    return ""


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: secret_key.py <path of the key file>")
    ensure_key_file(sys.argv[1])  # never prints the key
