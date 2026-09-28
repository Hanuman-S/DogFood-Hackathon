"""Signed records (C2): the install's Ed25519 keys, keys that came in with an event bundle, and the
records it issues. Models and rules only; every write goes through records/keys.py or
records/services.py.

* SigningKey: this install's keys, public halves only. The private key is a PEM file in the secrets
  volume (records/keys.py), never in the database, the repo or the image. At most one key is active
  (retired_at IS NULL, a partial unique index); which key signs is decided by that row, never by which
  PEM files exist. Retired keys stay published, so the records they signed keep verifying. A Postgres
  trigger refuses deleting a key and any change but retiring it once.
* ForeignSigningKey: public keys of other installs that came with an imported bundle. Published apart
  from this install's own, and never used to sign.
* IssuedRecord: one signed statement about one person in one event. Immutable except revocation (a
  Postgres trigger: no DELETE, and an UPDATE may only set the three revoke fields, once). PROTECT on
  the event: an event with issued records is permanent, like one with reviews or ballots.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

ED25519 = "Ed25519"


class SigningKey(models.Model):
    kid = models.CharField(max_length=16, primary_key=True)  # sha256(raw public key)[:16], hex
    alg = models.CharField(max_length=16, default=ED25519)
    public_key = models.CharField(max_length=64)  # the 32-byte raw key, base64
    created_at = models.DateTimeField(default=timezone.now)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "kid"]
        constraints = [
            models.UniqueConstraint(fields=["alg"], condition=Q(retired_at__isnull=True),
                                    name="signing_key_one_active"),
            models.CheckConstraint(condition=Q(alg=ED25519), name="signing_key_alg_ed25519"),
        ]

    def __str__(self):
        return f"{self.kid} ({'retired' if self.retired_at else 'active'})"


class ForeignSigningKey(models.Model):
    kid = models.CharField(max_length=16, primary_key=True)
    alg = models.CharField(max_length=16, default=ED25519)
    public_key = models.CharField(max_length=64)
    created_at = models.DateTimeField(null=True, blank=True)   # as the other install published it
    retired_at = models.DateTimeField(null=True, blank=True)
    imported_from = models.CharField(max_length=64)            # the bundle's sha256
    imported_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["imported_at", "kid"]
        constraints = [models.CheckConstraint(condition=Q(alg=ED25519), name="foreign_key_alg_ed25519")]


class RecordKind(models.TextChoices):
    JUDGE = "judge_participation", "Judge participation"
    PARTICIPANT = "participant", "Participant"
    WINNER = "winner", "Winner"


def winner_slot(track, place, peoples_choice):
    """The uniqueness discriminator of a winner record: which win, by (track, place, People's Choice).
    `track` is a track name ("" for the overall ranking). Judge and participant records use ""."""
    return f"track={track}|place={int(place)}|peoples_choice={'yes' if peoples_choice else 'no'}"


class IssuedRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=24, choices=RecordKind.choices)
    slot = models.CharField(max_length=200, blank=True)  # winner_slot() for winners, "" otherwise
    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="issued_records")
    subject_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    payload = models.JSONField()
    payload_text = models.TextField()   # canonical(payload), exactly the bytes that were signed
    signature = models.CharField(max_length=128)  # base64 of the 64-byte Ed25519 signature
    kid = models.CharField(max_length=16)
    is_foreign = models.BooleanField(default=False)  # came with a bundle, signed by another install
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+")
    issued_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    revoke_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-issued_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "subject_user", "kind", "slot"],
                                    condition=Q(revoked_at__isnull=True), name="record_one_active_per_slot"),
            models.CheckConstraint(
                condition=(Q(revoked_at__isnull=True, revoked_by__isnull=True, revoke_reason="")
                           | (Q(revoked_at__isnull=False) & ~Q(revoke_reason=""))),
                name="record_revoke_fields_together"),
            models.CheckConstraint(condition=Q(kind__in=[k.value for k in RecordKind]), name="record_kind_valid"),
        ]
        indexes = [models.Index(fields=["subject_user", "-issued_at"], name="record_subject_idx")]

    def __str__(self):
        return f"{self.kind} record {self.id}"
