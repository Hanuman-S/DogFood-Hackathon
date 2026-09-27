"""Accounts: the user, their API tokens, and their live sessions.

Three tables, three jobs:

* `User`        -- identity and role. Email is the login name.
* `ApiToken`    -- long-lived Bearer tokens for scripts and the acceptance checker. Only a
                   SHA-256 digest is stored; the raw token is shown once, at creation.
* `UserSession` -- one row per logged-in browser, pointing at Django's own session row, so a
                   user can see where they are logged in and end any of those sessions.
"""

import hashlib
import secrets

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from accounts.roles import Role


def normalize_email(email):
    """Lower-case the whole address. Two accounts differing only in case are one person."""
    return (email or "").strip().lower()


class UserManager(BaseUserManager):
    use_in_migrations = True

    def get_by_natural_key(self, email):
        return self.get(email=normalize_email(email))

    def create_user(self, email, password=None, *, name="", role=Role.PARTICIPANT):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=normalize_email(email), name=name.strip(), role=role)
        user.set_password(password)  # None -> unusable password
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, name="Admin", **_):
        """Used by `manage.py createsuperuser`: an admin is the top role."""
        return self.create_user(email, password, name=name, role=Role.ADMIN)


class User(AbstractBaseUser):
    email = models.EmailField(max_length=254, unique=True)
    name = models.CharField(max_length=120)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.PARTICIPANT)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    class Meta:
        constraints = [
            # Defence in depth: the app lower-cases emails, the database refuses case twins
            # even if some future code path forgets to.
            models.UniqueConstraint(Lower("email"), name="user_email_ci_unique"),
            models.CheckConstraint(
                condition=models.Q(role__in=Role.values), name="user_role_valid"
            ),
        ]

    def __str__(self):
        return f"{self.name} <{self.email}>"

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    # --- what Django's admin site needs, derived from the role -----------------------------
    # There is no separate staff flag or permission table to drift out of sync with the role:
    # admins may use the database admin, nobody else may.

    @property
    def is_staff(self):
        return self.is_active and self.role == Role.ADMIN

    @property
    def is_superuser(self):
        return self.is_staff

    def has_perm(self, perm, obj=None):
        return self.is_staff

    def has_perms(self, perm_list, obj=None):
        return self.is_staff

    def has_module_perms(self, app_label):
        return self.is_staff

    def get_short_name(self):
        return self.name.split(" ")[0] if self.name else self.email

    def get_full_name(self):
        return self.name


# --- API tokens -----------------------------------------------------------------------------

TOKEN_PREFIX = "dfk_"


def digest_token(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ApiTokenQuerySet(models.QuerySet):
    def active(self):
        return self.filter(revoked_at__isnull=True, user__is_active=True)

    def authenticate(self, raw):
        """The active token matching `raw`, or None.

        Looking up by digest means the comparison happens inside a B-tree index on a hash of
        the secret, not on the secret itself, so response timing reveals nothing useful.
        """
        if not raw:
            return None
        return self.active().select_related("user").filter(digest=digest_token(raw)).first()


class ApiToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=60)
    # First few characters of the raw token, kept so a user can tell their tokens apart.
    prefix = models.CharField(max_length=12)
    digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = ApiTokenQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.prefix}… ({self.name})"

    @classmethod
    def issue(cls, user, name, raw=None):
        """Create a token. Returns (token_row, raw_secret); the raw secret is never stored."""
        raw = raw or TOKEN_PREFIX + secrets.token_urlsafe(32)
        token = cls.objects.create(
            user=user, name=name, prefix=raw[:8], digest=digest_token(raw)
        )
        return token, raw

    @property
    def is_active(self):
        return self.revoked_at is None


# --- sessions -------------------------------------------------------------------------------


class UserSession(models.Model):
    """Metadata about one logged-in browser.

    The session itself lives in Django's `django_session` table; `session_key` is that row's
    primary key. Deleting both ends the session everywhere, immediately -- which a stateless
    signed cookie (a JWT, say) could not do.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sessions")
    session_key = models.CharField(max_length=40, unique=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-last_seen_at"]

    def __str__(self):
        return f"{self.user.email} @ {self.ip or '?'}"

    @property
    def device(self):
        """A short, human label for the user agent. Deliberately crude: no UA-parsing library."""
        ua = self.user_agent
        browser = next(
            (name for key, name in [
                ("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                ("Chrome/", "Chrome"), ("Safari/", "Safari"), ("curl/", "curl"),
                ("python", "Python script"),
            ] if key.lower() in ua.lower()),
            "Unknown client",
        )
        system = next(
            (name for key, name in [
                ("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iOS"),
                ("iPad", "iPadOS"), ("Mac OS X", "macOS"), ("Linux", "Linux"),
            ] if key in ua),
            "",
        )
        return f"{browser} on {system}" if system else browser
