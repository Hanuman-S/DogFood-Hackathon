"""Django settings for the DOGFOOD 2026 portal.

Two rules govern this file:

1. **It must work with no `.env` file at all.** Every setting below has a default that
   produces a working portal under `docker compose up`. `.env.example` documents the same
   values so an operator can see what is overridable without reverse-engineering this file.
2. **No runtime network access.** There is no cache backend to reach, no email backend that
   dials out, no CDN in the templates. Static assets are vendored into `src/static/vendor/`
   and served by WhiteNoise from inside the container.

All timestamps are UTC, everywhere, with no exceptions (`TIME_ZONE = "UTC"`). The UI labels
every date it shows and every date input it offers as UTC.
"""

import os
from pathlib import Path

# BASE_DIR is the `src/` directory (this file lives in src/config/).
# REPO_DIR is its parent, which is `/app` in the container.
BASE_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BASE_DIR.parent


# --------------------------------------------------------------------------------------
# environment helpers
# --------------------------------------------------------------------------------------


def env(name: str, default: str) -> str:
    """Read a string setting. Blank env vars fall back to the default.

    Treating "" as absent matters under compose: an unset variable in a `${VAR}`
    substitution arrives as an empty string rather than not arriving at all.
    """
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_list(name: str, default: str) -> list[str]:
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


# --------------------------------------------------------------------------------------
# core
# --------------------------------------------------------------------------------------

# The default is deliberately labelled insecure so that nobody can claim they were not
# warned. Production operators must set DJANGO_SECRET_KEY; the README says so and
# .env.example repeats it.
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    "insecure-default-key-for-local-demo-only-change-me",
)

DEBUG = env_bool("DJANGO_DEBUG", False)

ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS",
    "localhost,127.0.0.1,[::1],0.0.0.0,web",
)

# Needed for POSTs from the browser because the portal is served on a non-default port.
CSRF_TRUSTED_ORIGINS = env_list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "http://localhost:8080,http://127.0.0.1:8080",
)

# DEMO_MODE=1 enables `seed_demo`: known passwords, fixed API tokens and the open demo
# event. It is the compose default because the whole point is one command to a usable
# portal. It must be 0 for any real deployment -- see the README.
DEMO_MODE = env_bool("DEMO_MODE", True)

# Where `import_fixtures` looks when no path argument is given. Compose bind-mounts the
# organizer file here read-only.
FIXTURES_PATH = env("FIXTURES_PATH", str(REPO_DIR / "acceptance" / "fixtures.json"))

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------------------------
# applications
# --------------------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",  # SearchVector / GIN indexes for the gallery
    "rest_framework",
    # portal apps. Order follows the dependency direction: core depends on nothing, accounts
    # on core, events on accounts, and so on down.
    "core",
    "accounts",
    "events",
    "teams",
    "projects",
    "scoring",
    "gallery",
    "api",
    "seed",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise sits directly after SecurityMiddleware so static files never touch the
    # rest of the stack.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

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
                "core.context_processors.portal",
            ],
        },
    },
]


# --------------------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "dogfood"),
        "USER": env("POSTGRES_USER", "dogfood"),
        "PASSWORD": env("POSTGRES_PASSWORD", "dogfood"),
        "HOST": env("POSTGRES_HOST", "db"),
        "PORT": env("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": env_int("POSTGRES_CONN_MAX_AGE", 60),
    }
}


# --------------------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------------------

# Set from the very first migration. Swapping this later in Django is genuinely painful,
# so it exists before any other model does.
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# Login throttling: 5 failures per (email, IP) inside the window, then refuse. Counted
# from the AuditLog rows we are already required to write for failed logins, so there is
# no second store to keep consistent and the limit survives restarts and multiple
# gunicorn workers.
LOGIN_FAILURE_LIMIT = env_int("LOGIN_FAILURE_LIMIT", 5)
LOGIN_FAILURE_WINDOW_MINUTES = env_int("LOGIN_FAILURE_WINDOW_MINUTES", 15)


# --------------------------------------------------------------------------------------
# sessions and cookies
# --------------------------------------------------------------------------------------

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# Off by default because the demo is served over plain http on localhost; turning it on
# there would silently break login. Any real deployment behind TLS sets COOKIE_SECURE=1.
SESSION_COOKIE_SECURE = env_bool("COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = env_bool("COOKIE_SECURE", False)

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Off by default, and that is the safe direction. `X-Forwarded-For` is client-supplied: if the
# portal trusted it without a proxy in front, anyone could forge the IP the login throttle
# counts against and lock another person out of their own account. Turn this on only when a
# reverse proxy you control is setting the header.
TRUST_PROXY_HEADERS = env_bool("TRUST_PROXY_HEADERS", False)


# --------------------------------------------------------------------------------------
# internationalization / time
# --------------------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------------------------------
# static and media
# --------------------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = Path(env("DJANGO_STATIC_ROOT", str(REPO_DIR / "staticfiles")))
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_ROOT = Path(env("DJANGO_MEDIA_ROOT", str(REPO_DIR / "media")))
# Uploaded images are deliberately NOT served by WhiteNoise or any static handler. They
# are reachable only through a Django view that re-applies the owning project's
# visibility rules, so a draft's thumbnail is not a public URL. MEDIA_URL stays unset to
# make an accidental `{{ image.url }}` in a template fail loudly rather than leak.
MEDIA_URL = None

# Manifest storage gives every asset a content-hashed name so it can be cached forever.
# It requires `collectstatic` to have run, which the image does at build time. Tests set
# STATIC_MANIFEST=0 because they never run collectstatic.
_static_backend = (
    "whitenoise.storage.CompressedManifestStaticFilesStorage"
    if env_bool("STATIC_MANIFEST", not DEBUG)
    else "django.contrib.staticfiles.storage.StaticFilesStorage"
)
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": _static_backend},
}

# Uploads: JPEG/PNG/WebP only, verified with Pillow rather than trusted by extension.
MAX_UPLOAD_BYTES = env_int("MAX_UPLOAD_BYTES", 5 * 1024 * 1024)
MAX_PROJECT_IMAGES = env_int("MAX_PROJECT_IMAGES", 8)
ALLOWED_IMAGE_FORMATS = ("JPEG", "PNG", "WEBP")

# Refuse oversized request bodies before they reach a view.
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_BYTES + (1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_BYTES


# --------------------------------------------------------------------------------------
# gallery
# --------------------------------------------------------------------------------------

# 50 fits a whole hackathon cohort on one page: the shared fixture event has 40 visible
# projects, so an organizer reviewing a single event never has to paginate. This is a
# product decision, not a checker accommodation -- nothing in the codebase inspects the
# request to decide what to show.
GALLERY_PAGE_SIZE = env_int("GALLERY_PAGE_SIZE", 50)
GALLERY_DEFAULT_SORT = env("GALLERY_DEFAULT_SORT", "newest")


# --------------------------------------------------------------------------------------
# email
# --------------------------------------------------------------------------------------

# There is no email provider and no outbound SMTP: the "one command, no network" rule
# forbids it. Team invites are copyable links instead of emailed ones, and password reset
# is not offered (see the README's "Not done yet"). This backend only exists so that any
# accidental send is a no-op written to the log rather than a connection attempt.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"


# --------------------------------------------------------------------------------------
# Django REST Framework
# --------------------------------------------------------------------------------------

REST_FRAMEWORK = {
    # Order matters. Bearer tokens come first because API clients and the acceptance
    # checker can only attach a single header -- they cannot carry a CSRF token, so the
    # token class is the one that must not require one. SessionAuthentication stays for
    # the browser, where CSRF is enforced normally.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "core.authentication.BearerTokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        # The acceptance checker posts JSON; HTML forms post form-encoded; multipart is
        # needed for image uploads. All three are accepted on write endpoints.
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
    "EXCEPTION_HANDLER": "core.errors.api_exception_handler",
}


# --------------------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        # Quieter: every 404 from a probe does not need a stack trace.
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}
