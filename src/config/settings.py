"""Settings for the DOGFOOD portal.

Every value comes from the environment with a default that works inside `docker compose up`.
Nothing here reaches the network: no CDN, no hosted database, no auth provider, no mail API.
"""

import os
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from config.secret_key import check as check_secret_key
from config.secret_key import read_key_file

BASE_DIR = Path(__file__).resolve().parent.parent  # .../src


def env(name, default=""):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return env(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    return int(env(name, str(default)))


def env_list(name, default=""):
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


# --- core -------------------------------------------------------------------------------

DEBUG = env_bool("DJANGO_DEBUG", False)
# DJANGO_SECRET_KEY, else the key file the entrypoint generates on first boot (a Docker volume, never
# the repo). A known key is refused outside DEMO_MODE, below. See config/secret_key.py.
SECRET_KEY = env("DJANGO_SECRET_KEY") or read_key_file(env("DJANGO_SECRET_KEY_FILE")) or (
    "dev-only-insecure-key" if DEBUG else ""
)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
CSRF_TRUSTED_ORIGINS = env_list(
    "DJANGO_CSRF_TRUSTED_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080"
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # ours -- shared foundations
    "core",
    "accounts",
    # ours -- the hackathon domain, shared by every portal
    "events",
    "teams",
    "projects",
    "imports",
    "scoring",
    "voting",
    "records",
    # ours -- one app per audience, so each can be owned by a different teammate
    "public",
    "participant",
    "judge",
    "organizer",
    "platform_admin",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Must come after AuthenticationMiddleware: it replaces request.user for Bearer requests.
    "accounts.middleware.BearerTokenMiddleware",
    "accounts.middleware.SessionActivityMiddleware",
    # Late or early participant writes -> HTTP 409, from the service check or the DB trigger.
    "core.middleware.DeadlineMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "accounts.context_processors.identity",
            ],
        },
    },
]

# --- database ---------------------------------------------------------------------------
# Postgres whenever POSTGRES_HOST is set (always, under compose). Without it, a local SQLite
# file lets a teammate run `manage.py runserver` with no Docker at all. The submission itself
# always runs on Postgres.

if env("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB", "dogfood"),
            "USER": env("POSTGRES_USER", "dogfood"),
            "PASSWORD": env("POSTGRES_PASSWORD", "dogfood"),
            "HOST": env("POSTGRES_HOST"),
            "PORT": env("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR.parent / "dev.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# A URL typed without a scheme ("github.com/x") is read as https://, not http://.
FORMS_URLFIELD_ASSUME_HTTPS = True

# --- authentication ---------------------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "accounts:login"
LOGOUT_REDIRECT_URL = "public:home"

# Argon2id first: memory-hard, the current OWASP recommendation. PBKDF2 stays as a fallback so
# a hash made by any other Django install still verifies, and is upgraded on next login.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Public sign-up always creates a *participant*. Judges, organizers and admins are created by
# someone who already holds a higher role. Set to 0 to close sign-up entirely.
ALLOW_SIGNUP = env_bool("ALLOW_SIGNUP", True)

# Login throttle, counted from audit-log rows in Postgres: survives restarts and is shared by
# every gunicorn worker (an in-memory counter would be neither).
LOGIN_FAILURE_LIMIT = env_int("LOGIN_FAILURE_LIMIT", 5)          # per (email, IP)
LOGIN_IP_FAILURE_LIMIT = env_int("LOGIN_IP_FAILURE_LIMIT", 30)   # per IP, any email
LOGIN_FAILURE_WINDOW = timedelta(minutes=env_int("LOGIN_FAILURE_WINDOW_MINUTES", 15))

# Community voting (voting/services.py, voting/integrity.py). Vote writes -- opening a ballot,
# casting, changing, and refused attempts -- per voter and per IP hash, in a sliding window. The
# per-IP limit is generous: a venue's voters often share one address.
VOTE_RATE_WINDOW = timedelta(minutes=env_int("VOTE_RATE_WINDOW_MINUTES", 10))
VOTE_RATE_PER_VOTER = env_int("VOTE_RATE_PER_VOTER", 30)
VOTE_RATE_PER_IP = env_int("VOTE_RATE_PER_IP", 300)
# Integrity flags (shown to organizers; nothing is ever removed automatically).
VOTE_FLAG_WINDOW = timedelta(minutes=env_int("VOTE_FLAG_WINDOW_MINUTES", 10))
VOTE_FLAG_IP_BALLOTS = env_int("VOTE_FLAG_IP_BALLOTS", 3)
VOTE_FLAG_NEW_ACCOUNT = timedelta(minutes=env_int("VOTE_FLAG_NEW_ACCOUNT_MINUTES", 10))

# Comments on gallery projects (projects/comments.py): logged-in posts and refused posts per account
# and per IP hash, in a sliding window, counted from audit rows (anonymous attempts are not counted).
# Per account is the primary control; the per-IP limit is generous because a venue's people often
# share one address. An identical body from the same account on the same project within
# COMMENT_DUPLICATE_WINDOW is refused.
COMMENT_RATE_WINDOW = timedelta(minutes=env_int("COMMENT_RATE_WINDOW_MINUTES", 10))
COMMENT_RATE_PER_USER = env_int("COMMENT_RATE_PER_USER", 5)
COMMENT_RATE_PER_IP = env_int("COMMENT_RATE_PER_IP", 200)
# Anonymous attempts (always refused, 401) are audited up to this many per IP hash per window, then
# once more as a throttle row, then not at all until the window slides.
COMMENT_ANON_RATE_PER_IP = env_int("COMMENT_ANON_RATE_PER_IP", 60)
COMMENT_DUPLICATE_WINDOW = timedelta(minutes=10)

# Only trust X-Forwarded-For behind a proxy you run; otherwise anyone can forge their IP.
TRUST_PROXY_HEADERS = env_bool("TRUST_PROXY_HEADERS", False)

# --- sessions ---------------------------------------------------------------------------
# Server-side sessions in Postgres (django.contrib.sessions, db backend): the cookie holds only
# a random key, so a session can be revoked by deleting its row.

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "dogfood_session"
SESSION_COOKIE_AGE = int(timedelta(days=14).total_seconds())  # "remember this terminal"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = env_bool("COOKIE_SECURE", False)  # 1 behind TLS
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
CSRF_COOKIE_SAMESITE = "Lax"
# How often a request may refresh a session's "last seen" timestamp (one UPDATE per window).
SESSION_ACTIVITY_RESOLUTION = timedelta(minutes=5)

# Flash messages live in the server-side session, not in a cookie.
MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

# --- security headers -------------------------------------------------------------------

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# --- i18n / time ------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

# --- static files -----------------------------------------------------------------------
# Fonts and CSS are vendored under src/static: the UI renders with the network off.

STATIC_URL = "/static/"
STATIC_ROOT = Path(env("DJANGO_STATIC_ROOT", str(BASE_DIR.parent / "staticfiles")))
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

# --- uploads ----------------------------------------------------------------------------
# Project images live on a local volume (compose: `media`). They are served by a Django view
# that applies the same visibility rules as the project itself -- a draft's screenshots are
# not public just because someone guessed a URL.

MEDIA_ROOT = Path(env("DJANGO_MEDIA_ROOT", str(BASE_DIR.parent / "media")))
# Ed25519 signing keys for issued records (records/keys.py): private PEM files, 0600 in a 0700
# directory. In compose this is inside the `secrets` named volume, next to the SECRET_KEY file.
SIGNING_KEY_DIR = Path(env("DJANGO_SIGNING_KEY_DIR", str(BASE_DIR.parent / "secrets" / "signing")))
MEDIA_URL = "/media/"
MAX_IMAGE_BYTES = env_int("MAX_IMAGE_BYTES", 5 * 1024 * 1024)

# Event bundles (imports/bundle.py writes them, the import reads them). The export refuses anything
# the import would refuse, so every bundle this install writes can be taken back in.
BUNDLE_MAX_BYTES = env_int("BUNDLE_MAX_BYTES", 100 * 1024 * 1024)                 # the zip itself
BUNDLE_MAX_TOTAL_BYTES = env_int("BUNDLE_MAX_TOTAL_BYTES", 300 * 1024 * 1024)     # uncompressed
BUNDLE_MAX_EVENT_JSON_BYTES = env_int("BUNDLE_MAX_EVENT_JSON_BYTES", 50 * 1024 * 1024)
BUNDLE_MAX_MEDIA_BYTES = MAX_IMAGE_BYTES                                           # per image
BUNDLE_MAX_ENTRIES = env_int("BUNDLE_MAX_ENTRIES", 2000)
MAX_IMAGE_PIXELS = 40_000_000             # refuse decompression bombs before decoding them
MAX_PROJECT_IMAGES = env_int("MAX_PROJECT_IMAGES", 8)
MAX_PROJECT_TAGS = env_int("MAX_PROJECT_TAGS", 50)
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# --- fixture import -------------------------------------------------------------------------
# The organizers' shared dataset, imported on boot when SEED_FIXTURES=1 (compose default).
FIXTURES_PATH = env("FIXTURES_PATH", str(BASE_DIR.parent / "acceptance" / "fixtures.json"))

# --- demo mode --------------------------------------------------------------------------
# DEMO_MODE=1 seeds one account per role with a published password and fixed API tokens, so
# a judge can log in straight away and .dogfood.toml keeps working across database resets.
# Never enable it for a real event.

DEMO_MODE = env_bool("DEMO_MODE", False)

_key_problem = check_secret_key(SECRET_KEY, demo_mode=DEMO_MODE)
if _key_problem:
    raise ImproperlyConfigured(_key_problem)
DEMO_PASSWORD = env("DEMO_PASSWORD", "dogfood-demo")
DEMO_TOKENS = {
    "admin": env("DEMO_TOKEN_ADMIN"),
    "organizer": env("DEMO_TOKEN_ORGANIZER"),
    "judge_a": env("DEMO_TOKEN_JUDGE_A"),
    "judge_b": env("DEMO_TOKEN_JUDGE_B"),
    "participant": env("DEMO_TOKEN_PARTICIPANT"),
}
PORTAL_BASE_URL = env("PORTAL_BASE_URL", "http://localhost:8080")

# --- logging ----------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
}
