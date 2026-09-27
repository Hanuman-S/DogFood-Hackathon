"""Settings for the test suite.

A separate module rather than environment variables set in `conftest.py`, because
pytest-django calls `django.setup()` from its `pytest_load_initial_conftests` hook -- that is,
*before* the root conftest is imported. Anything conftest puts in `os.environ` therefore
arrives too late to affect a setting that is evaluated at import time, which is a genuinely
confusing failure to debug. Pointing `DJANGO_SETTINGS_MODULE` at this module makes the
overrides unambiguous and order-independent.

Note what is deliberately *not* overridden: `DEBUG` stays False, so tests exercise the same
error handling, the same 404 behaviour and the same template configuration as production. A
suite that runs with DEBUG=True can pass while the deployed portal returns a 500.
"""

from config.settings import *  # noqa: F401,F403

# The suite never runs `collectstatic`, so manifest storage would fail to resolve any
# `{% static %}` tag. Plain storage serves the same URLs without the hashing step.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Demo seeding is a behaviour under test, not an ambient condition. Tests that want it turn
# it on explicitly with `override_settings(DEMO_MODE=True)`.
DEMO_MODE = False

SECRET_KEY = "test-only-key-not-used-to-sign-anything-real"

# Tests create hundreds of users; the default PBKDF2 hasher would spend most of the suite's
# runtime stretching passwords that no attacker will ever see. The hashing algorithm itself
# is Django's, not ours, and is not what these tests are checking.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Uploaded files go to a throwaway directory rather than the media volume, so a test that
# writes an image cannot leave anything behind in a real deployment's storage.
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="dogfood-test-media-"))
