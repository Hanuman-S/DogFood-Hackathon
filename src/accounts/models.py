"""User accounts and API tokens.

The custom user model exists from the first migration, because email-as-identifier is a
decision you cannot walk back in Django without a painful table rewrite.

Two flags live on the user rather than on an event membership, and only two:

* `is_platform_admin` -- the platform operator. Can do anything, including reach
  `/admin/`. There is no such thing as an "admin of one event"; that role is `organizer`.
* `can_create_events` -- granted by an admin. Orthogonal to every event role, because a
  person who may start a new event is not thereby a member of anyone else's.

Everything else about who someone is -- participant, judge, organizer -- is **per event**
and lives in `events.EventMembership`. A global "is a judge" flag would be wrong: the same
person is a judge at one hackathon and a competitor at the next.
"""

from __future__ import annotations

import hashlib
import secrets

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models

from core import clock

# 32 bytes of entropy, URL-safe base64 -> 43 characters from [A-Za-z0-9_-].
#
# The alphabet matters beyond aesthetics: tokens are pasted into `.dogfood.toml`, and the
# acceptance checker's fallback TOML parser truncates a line at the first `#`. A token that
# cannot contain `#`, `"` or whitespace is a token that cannot break that file.
TOKEN_BYTES = 32


class UserManager(BaseUserManager):
    """Manager for a user model whose identifier is an email address."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra):
        if not email:
            raise ValueError("A user must have an email address.")
        email = self.normalize_email(email).strip().lower()
        user = self.model(email=email, **extra)
        if password:
            user.set_password(password)
        else:
            # Imported fixture participants have no password until DEMO_MODE gives them
            # one. An unusable password is not a blank password: it cannot be matched by
            # any input, so these accounts are inert rather than open.
            user.set_unusable_password()
        user.full_clean(exclude=["password", "last_login"])
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_platform_admin", False)
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra):
        """Used by `manage.py createsuperuser`, which is the documented production
        bootstrap when DEMO_MODE=0.

        All three flags are set together on purpose: `is_platform_admin` is what the
        portal's own permission layer reads, while `is_staff`/`is_superuser` are what
        `django.contrib.admin` reads. Setting one without the others produces an account
        that is an admin in one half of the system and not the other.
        """
        extra["is_platform_admin"] = True
        extra["is_staff"] = True
        extra["is_superuser"] = True
        extra.setdefault("can_create_events", True)
        if not password:
            raise ValueError("A superuser must have a password.")
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(
        unique=True,
        help_text="Login identifier. Stored lowercased.",
    )
    display_name = models.CharField(
        max_length=120,
        help_text="Shown in the gallery next to the team's projects.",
    )

    # Platform-wide flags. See the module docstring for why there are only two.
    is_platform_admin = models.BooleanField(
        default=False,
        help_text="Platform operator: full access, including the Django admin.",
    )
    can_create_events = models.BooleanField(
        default=False,
        help_text="May create new events. Granted by a platform admin.",
    )

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(
        default=False,
        help_text="Required by django.contrib.admin. Kept in step with is_platform_admin.",
    )

    # Traceable back to the organizer fixture file when the account came from there.
    external_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        help_text="Stable id from an import (e.g. jdg_02). Null for portal signups.",
    )

    created_at = models.DateTimeField(default=clock.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    # Prompted for by `manage.py createsuperuser`, alongside email and password.
    REQUIRED_FIELDS = ["display_name"]

    class Meta:
        ordering = ["email"]
        # No explicit index on `email` or `external_id`: `unique=True` already creates a
        # B-tree index on each, which serves the equality lookups (login, import upsert)
        # that are the only way either column is queried. A second index on the same column
        # would cost write throughput and disk for nothing.

    def __str__(self) -> str:
        return f"{self.display_name} <{self.email}>"

    def save(self, *args, **kwargs):
        # Emails are compared case-insensitively everywhere (login, invites, imports), so
        # they are stored in one canonical case rather than being lowercased at each of
        # those call sites.
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def get_short_name(self) -> str:
        return self.display_name or self.email

    def get_full_name(self) -> str:
        return self.display_name or self.email


class ApiTokenQuerySet(models.QuerySet):
    def active(self):
        return self.filter(revoked_at__isnull=True, user__is_active=True)


class ApiToken(models.Model):
    """A bearer token for the JSON API.

    Only the SHA-256 of the token is stored. The plaintext is shown to its owner once, at
    creation, and is unrecoverable afterwards -- a stolen database backup yields no usable
    credentials.

    SHA-256 rather than a slow password hash is the right choice *here* specifically because
    the token is 32 bytes of CSPRNG output, not a human-chosen secret: there is no dictionary
    to attack, so key stretching buys nothing and would cost a KDF on every API request.
    That reasoning does not transfer to passwords, which go through Django's hasher.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="api_tokens",
    )
    name = models.CharField(
        max_length=120,
        help_text="What this token is for, e.g. 'acceptance checker'.",
    )
    token_hash = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        help_text="SHA-256 hex digest of the token. The plaintext is never stored.",
    )
    created_at = models.DateTimeField(default=clock.now, editable=False)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = ApiTokenQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        # `token_hash` is unique, so authentication's lookup-by-digest already has an index.

    def __str__(self) -> str:
        state = "revoked" if self.revoked_at else "active"
        return f"{self.name} ({self.user.email}, {state})"

    # -- token handling -----------------------------------------------------------------

    @staticmethod
    def hash_token(plaintext: str) -> str:
        """Hash a token for storage or lookup.

        Note there is no salt, and that is deliberate: a lookup has to find the row from the
        token alone, so the digest must be deterministic. Safe because the input is
        high-entropy random, per the class docstring.
        """
        return hashlib.sha256(plaintext.strip().encode("utf-8")).hexdigest()

    @staticmethod
    def generate_plaintext() -> str:
        return secrets.token_urlsafe(TOKEN_BYTES)

    @classmethod
    def issue(cls, *, user: User, name: str, plaintext: str | None = None):
        """Create a token, returning `(instance, plaintext)`.

        `plaintext` is only ever passed by `seed_demo`, which needs the demo tokens to keep
        a fixed value across `docker compose down -v` so that `.dogfood.toml` stays valid.
        Those values still arrive here and are still hashed like any other token -- demo
        mode changes where the secret comes from, never how it is stored.
        """
        secret = plaintext or cls.generate_plaintext()
        token = cls.objects.create(
            user=user,
            name=name,
            token_hash=cls.hash_token(secret),
        )
        return token, secret

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def revoke(self) -> None:
        if self.revoked_at is None:
            self.revoked_at = clock.now()
            self.save(update_fields=["revoked_at"])
