"""Test-only overrides. Selected by pytest.ini."""

import os

# Compose passes DJANGO_SECRET_KEY as an empty string (the generated key is for the running portal),
# so fill it in when empty, not only when unset. A known key is fine here: tests run in demo mode.
os.environ["DJANGO_SECRET_KEY"] = os.environ.get("DJANGO_SECRET_KEY") or "test-only-secret"
os.environ["DEMO_MODE"] = "1"

from config.settings import *  # noqa: E402,F401,F403

# Hashing with Argon2 on every test user would dominate the suite's runtime.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# The manifest storage needs `collectstatic`; tests render templates without it.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

ALLOWED_HOSTS = ["testserver", "localhost"]
DEMO_MODE = True
