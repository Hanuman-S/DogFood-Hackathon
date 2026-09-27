"""Test-only overrides. Selected by pytest.ini."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret")

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
