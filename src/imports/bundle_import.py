"""Importing an event bundle (C1c): always a NEW event, never a merge, written in one transaction.

Only what imports/bundle_validate.py passed is written; a failure after that (a database constraint,
a trigger) rolls everything back, images included, and is answered as one audited 400. That matters:
an event with reviews is permanent (Score RESTRICT), so a half-written import could never be removed.

What the import decides, not the bundle:
* the slug -- the bundle's, or with "-2", "-3", ... on a clash (under an advisory lock);
* publication -- an imported event always arrives unpublished (the source's state is in the audit row);
* secrets -- a new ballot secret and open-link nonce for the vote, a new invite token for every team;
* accounts -- a platform admin's import matches accounts by email and creates the missing ones with no
  usable password (the operator sets one with `changepassword`; there is no outbound mail). Anyone
  else who may import (an event creator) gets a NEW placeholder account for every person in the bundle
  (imported-<n>-<8 hex>@import.invalid, the display name, no password), so a non-admin can never
  attach an existing account -- anyone's, their own included -- to an event;
* the cross-validation seed -- M2 derives it from the slug; if the slug had to change, the source's
  seed is pinned in the event's engine overrides (unless they set one), so a recompute matches;
* the importer becomes an organizer of the new event (refused, 400, if they compete in it);
* fixture refs are filed under `bundle:<sha256>:<new slug>`, unique per import;
* imported snapshots and tallies carry `imported_from` = the bundle's sha256.

Rows go in in the export's section order (each section's references point backwards), through the
three audited bypasses: past-deadline projects and teams, ballots outside their window, weights after
judging opened. Immutable rows (snapshots, tallies, publications) are inserted, never updated. Ids
inside JSON columns go from bundle ids to the new primary keys (imports/bundle_ids.py). Audit rows are
kept as source history: their ids are the source's, `detail.source_history` is true, and the source
slug is replaced by the new one where it names the event.
"""

import secrets
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, models, transaction

from accounts.models import User
from accounts.roles import Role
from core import audit
from core.deadlines import deadline_bypass
from core.models import AuditAction, AuditLog

from . import bundle_ids
from .bundle import SECTIONS, BY_MODEL, BundleError, model_of
from .bundle_schema import expected_fields
from .bundle_validate import validate

SLUG_LOCK = "dogfood:event-slug"


def import_event(path, *, actor, origin=None):
    """Import the bundle at `path` as a new event; returns it. Refusals: ImportForbidden (403),
    BundleError (400, with a code), each audited once; nothing else is written."""
    checked = validate(path, actor=actor, origin=origin)
    written = []  # media files stored before the commit, removed again if it fails
    try:
        with transaction.atomic():
            event = _Writer(checked, actor, origin, written).run()
    except BundleError as error:
        _remove(written)
        _refused(error, actor, origin)
    except (KeyError, ValueError, bundle_ids.UnknownIdField) as error:
        # Validation checks every reference first; this is the write path guarding itself anyway.
        _remove(written)
        _refused(BundleError("invalid_bundle", f"a reference could not be resolved: {str(error)[:120]}"),
                 actor, origin)
    except (IntegrityError, DatabaseError) as error:
        _remove(written)
        cause = getattr(getattr(error, "__cause__", None), "diag", None)
        name = getattr(cause, "constraint_name", None) or type(error).__name__
        _refused(BundleError("import_conflict", f"the database refused a row ({name}); nothing was imported"),
                 actor, origin)
    except BaseException:
        _remove(written)
        raise
    return event


def _refused(error, actor, origin):
    audit.record(AuditAction.EVENT_IMPORT_REFUSED, origin=origin, actor=actor, reason=error.code,
                 detail_text=error.detail[:300])
    raise error


def _remove(names):
    from django.core.files.storage import default_storage

    for name in names:
        try:
            default_storage.delete(name)
        except OSError:
            pass


class _Writer:
    def __init__(self, checked, actor, origin, written):
        self.checked, self.body, self.actor, self.origin = checked, checked.body, actor, origin
        self.written = written
        self.pk = {}          # section -> {bundle id: new pk}
        self.users = {}       # bundle user id -> User
        self.late_times = []  # (model, pk, {auto_now field: value}) restored after insert

    def run(self):
        source = self.body["event"]
        self._users()
        self._check_importer()
        slug = self._free_slug(source["slug"])
        self.slug, self.source_slug = slug, source["slug"]
        reason = f"event import {self.checked.sha256[:12]}"
        from scoring.services import weights_bypass
        from voting.services import voting_bypass

        with deadline_bypass(None, reason, subject=slug, actor=self.actor, origin=self.origin), \
                voting_bypass(reason, actor=self.actor, origin=self.origin, subject=slug), \
                weights_bypass(reason, actor=self.actor, origin=self.origin, subject=slug):
            for section in SECTIONS:
                self._section(section)
            self._organizer()
            self._fixture_refs()
            self._restore_times()
            self.cv_seed_pinned = self._pin_cv_seed()
        self._audit_history()
        counts = {s.name: (1 if self.body[s.name] else 0) if s.one else len(self.body[s.name]) for s in SECTIONS}
        audit.record(AuditAction.EVENT_IMPORTED, origin=self.origin, actor=self.actor, subject=slug, event=slug,
                     sha256=self.checked.sha256, source_event=self.source_slug,
                     source_published=bool(source["is_published"]), rows=counts,
                     users_created=self.created_users, placeholder_accounts=self.placeholders,
                     records=self.records,
                     cv_seed_pinned=self.cv_seed_pinned)
        return self.event

    # --- accounts and the importer ---

    def _users(self):
        self.created_users = 0
        self.placeholders = not self.actor.is_platform_admin
        for row in self.body["users"]:
            if self.placeholders:
                n = row["id"].split("#")[1]
                email = f"imported-{n}-{secrets.token_hex(4)}@import.invalid"
                user = User.objects.create_user(email, None, name=row["name"])  # unusable password
                self.created_users += 1
            else:
                user = User.objects.filter(email__iexact=row["email"]).first()
                if user is None:
                    user = User.objects.create_user(row["email"], None, name=row["name"])  # unusable password
                    self.created_users += 1
            self.users[row["id"]] = user

    def _check_importer(self):
        me = {bid for bid, user in self.users.items() if user.pk == self.actor.pk}
        competing = [m for m in self.body["memberships"] if m["user"] in me and m["role"] == Role.PARTICIPANT]
        if competing:
            raise BundleError("importer_is_competitor", "you are a participant of this event, so you cannot be "
                              "its organizer; import it as another admin")

    def _free_slug(self, wanted):
        from events.models import Event

        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [SLUG_LOCK])
        slug, n = wanted, 1
        while Event.objects.filter(slug=slug).exists():
            n += 1
            suffix = f"-{n}"
            slug = wanted[:60 - len(suffix)] + suffix
        return slug

    def _organizer(self):
        from events.models import EventMembership

        EventMembership.objects.get_or_create(event=self.event, user=self.actor, role=Role.ORGANIZER,
                                              defaults={"added_by": self.actor})

    # --- rows ---

    def _records(self, section):
        """Signed records and the public keys they need: for a platform admin's import only (anyone else's
        import skips both, counted). A key that is this install's own stays only in the own list; any other
        becomes a ForeignSigningKey (published apart, never used to sign). A record whose id already
        exists here (the same bundle imported again) is skipped: a record is one statement, wherever it
        is shown."""
        from records.models import ForeignSigningKey, IssuedRecord, SigningKey

        rows = self.body[section.name]
        self.pk[section.name] = {}
        self.records = {"imported": 0, "skipped_existing": 0, "skipped_not_admin": 0, "keys_imported": 0}
        if self.placeholders:
            self.records["skipped_not_admin"] = len(rows)
            return
        own = set(SigningKey.objects.values_list("kid", flat=True))
        for key in self.body["signing_keys"]:
            if key["kid"] in own or ForeignSigningKey.objects.filter(kid=key["kid"]).exists():
                continue
            ForeignSigningKey.objects.create(
                kid=key["kid"], alg=key["alg"], public_key=key["public_key"],
                created_at=_datetime(key["created_at"]), retired_at=_datetime(key["retired_at"]),
                imported_from=self.checked.sha256)
            self.records["keys_imported"] += 1
        model = model_of(section)
        for row in rows:
            if IssuedRecord.objects.filter(pk=row["record_id"]).exists():
                self.records["skipped_existing"] += 1
                continue
            obj = model(**self._values(section, row))
            obj.id = row["record_id"]
            obj.is_foreign = row["kid"] not in own
            obj.save(force_insert=True)
            self.records["imported"] += 1

    def _section(self, section):
        if section.name == "issued_records":
            return self._records(section)
        rows = self.body[section.name]
        rows = ([rows] if rows is not None else []) if section.one else rows
        model = model_of(section)
        self.pk[section.name] = {}
        for row in rows:
            obj = model(**self._values(section, row))
            self._special(section, obj, row)
            obj.save(force_insert=True)
            if section.name == "event":
                self.event = obj
            elif not section.one:  # one-per-event rows have no bundle id: nothing refers to them
                self.pk[section.name][row["id"]] = obj.pk
            if section.name == "projects" and row["tags"]:
                from projects.models import Tag
                obj.tags.set([Tag.objects.get_or_create(name=name)[0] for name in row["tags"]])
            stamps = {f.name: _datetime(row[f.name]) for _, f in expected_fields(section)
                      if getattr(f, "auto_now", False) and row.get(f.name)}
            if stamps:
                self.late_times.append((model, obj.pk, stamps))

    def _values(self, section, row):
        values = {}
        for name, f in expected_fields(section):
            value = row[name]
            if f.is_relation:
                target = f.related_model._meta.label
                if target == settings.AUTH_USER_MODEL:
                    values[name] = self.users[value] if value else None
                else:
                    values[f.attname] = self.pk[BY_MODEL[target].name][value] if value else None
            elif isinstance(f, models.FileField):
                values[name] = self._media(value) if value else ""
            elif isinstance(f, models.JSONField):
                specs = bundle_ids.TABLE.get((model_of(section)._meta.label, f.name), {})
                values[name] = bundle_ids.rewrite(value, specs, self._to_pk, f"{section.name}.{name}")
            elif value is None:
                values[name] = None
            elif isinstance(f, models.DateTimeField):
                values[name] = _datetime(value)
            elif isinstance(f, models.DecimalField):
                values[name] = Decimal(value)
            else:
                values[name] = value
        for f in model_of(section)._meta.concrete_fields:
            if f.is_relation and f.related_model._meta.label == "events.Event" and section.name != "event":
                values[f.attname] = self.event.pk
        return values

    def _special(self, section, obj, row):
        if section.name == "event":
            obj.slug = self.slug
            obj.is_published = False  # an import always arrives unpublished
        elif section.name == "voting_config":
            from voting import links
            obj.ballot_secret = secrets.token_hex(32)
            obj.open_link_nonce = links.new_nonce()
        elif section.name == "ballots":
            obj.voter_cookie = row["voter"]  # the pseudonym: a voter no account or link can be tied to
        elif section.name in ("result_snapshots", "tally_snapshots"):
            obj.imported_from = self.checked.sha256

    def _to_pk(self, namespace, value, typ):
        if isinstance(value, str) and value.startswith(bundle_ids.MISSING_PREFIX):
            return value
        pk = self.pk[namespace][value]
        return pk if typ is int else str(pk)

    def _media(self, bundle_path):
        from django.core.files.storage import default_storage
        from projects.models import image_path

        content = self.checked.images[bundle_path]
        content.seek(0)
        name = default_storage.save(image_path(None, content.name), content)
        self.written.append(name)
        return name

    def _restore_times(self):
        """auto_now fields set "now" on insert; put the source's values back (same transaction)."""
        for model, pk, stamps in self.late_times:
            model.objects.filter(pk=pk).update(**stamps)

    def _pin_cv_seed(self):
        """The seed M2's cross-validation would have used on the source (derived from its slug), pinned
        when the slug changed -- only then: a kept slug derives the same seed by itself. Not when the
        overrides already name one. Returns the pinned seed, or None."""
        if self.slug == self.source_slug:
            return None
        from scoring.engine.config import seed_for
        from scoring.models import EventScoringConfig

        config = EventScoringConfig.objects.filter(event=self.event).first()
        overrides = dict(config.overrides) if config else {}
        if overrides.get("cv_seed") is not None:
            return None
        seed = seed_for(self.source_slug)
        overrides["cv_seed"] = seed
        if config:
            EventScoringConfig.objects.filter(pk=config.pk).update(overrides=overrides)
        else:
            EventScoringConfig.objects.create(event=self.event, overrides=overrides)
        return seed

    def _fixture_refs(self):
        from imports.models import FixtureRef

        source = f"bundle:{self.checked.sha256}:{self.slug}"
        for ref in self.body["fixture_refs"]:
            section = "projects" if ref["kind"] == "project" else "scores"
            FixtureRef.objects.create(source=source, kind=ref["kind"], external_id=ref["external_id"],
                                      object_id=self.pk[section][ref["object"]], duplicate_of=ref["duplicate_of"],
                                      note=ref["note"], created_at=_datetime(ref["created_at"]), event=self.event)

    def _audit_history(self):
        rows = []
        for row in self.body["audit"]:
            detail = dict(row["detail"])
            if detail.get("event") == self.source_slug:
                detail["event"] = self.slug
            detail["source_history"] = True
            rows.append(AuditLog(
                created_at=_datetime(row["created_at"]), action=row["action"],
                actor=self.users[row["actor"]] if row["actor"] else None, actor_email=row["actor_email"],
                subject=self.slug if row["subject"] == self.source_slug else row["subject"], detail=detail,
            ))
        AuditLog.objects.bulk_create(rows)


def _datetime(value):
    return datetime.fromisoformat(value) if value else None
