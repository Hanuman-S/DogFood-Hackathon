"""The shape event.json must have, checked field by field before anything is written.

Derived from the same section list the export uses (imports/bundle.py SECTIONS) and each model's own
fields, so the writer and the checker cannot drift apart: every row must have exactly the keys the
export writes -- no more, no fewer -- each of the right type, within the column's length and
choices, every reference must name a row of the right section in this bundle, rows must be numbered
1..n in order, and ids inside JSON columns must be where imports/bundle_ids.py declares them, naming
rows that exist. A problem raises SchemaError with the first thing wrong, in words.
"""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import models

from core.models import AuditAction

from . import bundle_ids
from .bundle import FORMAT, SECTIONS, VERSION, model_of

MEDIA_PATH = re.compile(r"media/[0-9a-f]{64}\.(jpg|png|webp)")
BUNDLE_ID = re.compile(r"([a-z_]+)#([1-9][0-9]*)")
PSEUDONYM = re.compile(r"v_[0-9a-f]{16}")
EXTRA_KEYS = {"projects": {"tags"}, "ballots": {"voter"}}
TOP_LEVEL = {"format", "version", "users", "fixture_refs", "audit", *(s.name for s in SECTIONS)}


class SchemaError(Exception):
    pass


def _fail(where, what):
    raise SchemaError(f"{where}: {what}")


def expected_fields(section):
    """(name, field) of every column a row of this section carries, as the export writes them."""
    model = model_of(section)
    out = []
    for f in model._meta.concrete_fields:
        if f.primary_key or f.name in section.exclude or isinstance(f, models.GeneratedField):
            continue
        if f.is_relation and f.related_model._meta.label == "events.Event":
            continue
        out.append((f.name, f))
    return out


class Checker:
    def __init__(self, body, media_paths):
        self.body = body
        self.media_paths = set(media_paths)
        self.referenced_media = set()
        self.ids = {}

    def run(self):
        body = self.body
        if not isinstance(body, dict):
            _fail("event.json", "is not an object")
        keys = set(body)
        if keys != TOP_LEVEL:
            extra, missing = sorted(keys - TOP_LEVEL), sorted(TOP_LEVEL - keys)
            _fail("event.json", f"top-level keys differ (unexpected {extra}, missing {missing})")
        if body["format"] != FORMAT or body["version"] != VERSION:
            _fail("event.json", f"format/version {body['format']!r}/{body['version']!r} do not match the manifest's")
        self._numbered("users", body["users"], "users")
        for section in SECTIONS:
            rows = body[section.name]
            if section.one:
                if rows is None and section.name == "event":
                    _fail("event", "is missing")
                if rows is not None and not isinstance(rows, dict):
                    _fail(section.name, "is not an object or null")
            else:
                self._numbered(section.name, rows, section.name)
        self._users()
        for section in SECTIONS:
            rows = [self.body[section.name]] if section.one else self.body[section.name]
            for row in rows:
                if row is not None:
                    self._row(section, row)
        self._fixture_refs()
        self._audit()
        unreferenced = self.media_paths - self.referenced_media
        if unreferenced:
            _fail("media", f"{len(unreferenced)} file(s) no row refers to, e.g. {sorted(unreferenced)[0]}")

    # --- structure ---

    def _numbered(self, where, rows, prefix):
        if not isinstance(rows, list):
            _fail(where, "is not a list")
        ids = []
        for n, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                _fail(f"{where}[{n}]", "is not an object")
            if row.get("id") != f"{prefix}#{n}":
                _fail(f"{where}[{n}]", f"id {row.get('id')!r} should be {prefix}#{n} (rows are numbered 1..n in order)")
            ids.append(row["id"])
        self.ids[prefix] = set(ids)

    def _users(self):
        seen = set()
        for row in self.body["users"]:
            where = row["id"]
            if set(row) != {"id", "email", "name"}:
                _fail(where, f"keys should be id, email, name; got {sorted(row)}")
            email, name = row["email"], row["name"]
            if not isinstance(email, str) or not isinstance(name, str):
                _fail(where, "email and name must be text")
            try:
                validate_email(email)
            except ValidationError:
                _fail(where, f"{email!r} is not an email address")
            if len(email) > 254 or len(name) > 120:
                _fail(where, "email or name too long")
            if email.lower() in seen:
                _fail(where, f"{email!r} appears twice")
            seen.add(email.lower())

    def _ref(self, where, section, value, nullable):
        if value is None:
            if not nullable:
                _fail(where, "must not be null")
            return
        if not isinstance(value, str) or not BUNDLE_ID.fullmatch(value) or value not in self.ids.get(section, ()):
            _fail(where, f"{value!r} is not a row of {section} in this bundle")

    # --- one row, generically ---

    def _row(self, section, row):
        where = row.get("id", section.name)
        fields = expected_fields(section)
        wanted = {name for name, _ in fields} | EXTRA_KEYS.get(section.name, set())
        if not section.one:
            wanted.add("id")
        if set(row) != wanted:
            extra, missing = sorted(set(row) - wanted), sorted(wanted - set(row))
            _fail(where, f"keys differ (unexpected {extra}, missing {missing})")
        for name, f in fields:
            self._value(f"{where}.{name}", section, f, row[name])
        if section.name == "projects":
            tags = row["tags"]
            if not isinstance(tags, list) or not all(isinstance(t, str) and 0 < len(t) <= 40 for t in tags) \
                    or len(tags) > settings.MAX_PROJECT_TAGS or len(set(tags)) != len(tags):
                _fail(f"{where}.tags", "must be a list of distinct names of 1-40 characters")
        if section.name == "ballots" and not (isinstance(row["voter"], str) and PSEUDONYM.fullmatch(row["voter"])):
            _fail(f"{where}.voter", "must be a pseudonym v_<16 hex>")

    def _value(self, where, section, f, value):
        if f.is_relation:
            target = f.related_model._meta.label
            if target == settings.AUTH_USER_MODEL:
                return self._ref(where, "users", value, f.null)
            from .bundle import BY_MODEL
            return self._ref(where, BY_MODEL[target].name, value, f.null)
        if value is None:
            if not f.null:
                _fail(where, "must not be null")
            return
        if isinstance(f, models.FileField):
            if value == "" and f.blank:
                return
            if not isinstance(value, str) or not MEDIA_PATH.fullmatch(value) or value not in self.media_paths:
                _fail(where, f"{value!r} is not a media file of this bundle")
            self.referenced_media.add(value)
        elif isinstance(f, models.JSONField):
            self._json(where, section, f, value)
        elif isinstance(f, models.BooleanField):
            if not isinstance(value, bool):
                _fail(where, "must be true or false")
        elif isinstance(f, (models.IntegerField, models.BigIntegerField)):
            if isinstance(value, bool) or not isinstance(value, int):
                _fail(where, "must be a whole number")
            if type(f).__name__.startswith("Positive") and value < 0:
                _fail(where, "must not be negative")
            if abs(value) > 2 ** 63 - 1:
                _fail(where, "is out of range")
        elif isinstance(f, models.DateTimeField):
            try:
                parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
            except ValueError:
                parsed = None
            if parsed is None or parsed.tzinfo is None:
                _fail(where, f"{value!r} is not a date and time with an offset")
        elif isinstance(f, models.DecimalField):
            try:
                number = Decimal(value) if isinstance(value, str) else None
            except InvalidOperation:
                number = None
            if number is None or not number.is_finite():
                _fail(where, f"{value!r} is not a decimal")
            sign, digits, exponent = number.as_tuple()
            whole = max(len(digits) + exponent, 0)
            if -exponent > f.decimal_places or whole > f.max_digits - f.decimal_places:
                _fail(where, f"{value!r} does not fit {f.max_digits} digits with {f.decimal_places} decimals")
        elif isinstance(f, (models.CharField, models.TextField)):
            if not isinstance(value, str):
                _fail(where, "must be text")
            if "\x00" in value:
                _fail(where, "contains a NUL character")
            if f.max_length and len(value) > f.max_length:
                _fail(where, f"is longer than {f.max_length} characters")
            if value == "" and not f.blank:
                _fail(where, "must not be empty")
        else:
            _fail(where, f"has a column type the import does not know ({type(f).__name__})")
        if f.choices and value not in {c[0] for c in f.flatchoices}:
            _fail(where, f"{value!r} is not one of the allowed values")

    def _json(self, where, section, f, value):
        specs = bundle_ids.TABLE.get((model_of(section)._meta.label, f.name), {})

        def mapper(namespace, id_value, typ):
            if isinstance(id_value, str) and id_value.startswith(bundle_ids.MISSING_PREFIX):
                if not re.fullmatch(rf"{bundle_ids.MISSING_PREFIX}{namespace}#[1-9][0-9]*", id_value) \
                        or namespace not in bundle_ids.DELETABLE:
                    _fail(where, f"{id_value!r} is not a valid missing-row name for {namespace}")
                return id_value
            if id_value not in self.ids.get(namespace, ()):
                _fail(where, f"{id_value!r} is not a row of {namespace} in this bundle")
            return id_value

        try:
            bundle_ids.rewrite(value, specs, mapper, where)
        except bundle_ids.UnknownIdField as error:
            _fail(where, str(error))

    # --- the two extra sections ---

    def _fixture_refs(self):
        refs = self.body["fixture_refs"]
        if not isinstance(refs, list):
            _fail("fixture_refs", "is not a list")
        wanted = {"kind", "object", "source", "external_id", "duplicate_of", "note", "created_at"}
        for n, ref in enumerate(refs, start=1):
            where = f"fixture_refs[{n}]"
            if not isinstance(ref, dict) or set(ref) != wanted:
                _fail(where, f"keys should be {sorted(wanted)}")
            section = {"project": "projects", "score": "scores"}.get(ref["kind"])
            if section is None:
                _fail(where, f"kind {ref['kind']!r} is not project or score")
            self._ref(f"{where}.object", section, ref["object"], False)
            for key, limit in (("source", 60), ("external_id", 120), ("duplicate_of", 120), ("note", 300)):
                if not isinstance(ref[key], str) or len(ref[key]) > limit or "\x00" in ref[key]:
                    _fail(f"{where}.{key}", f"must be text of at most {limit} characters")
            self._datetime(f"{where}.created_at", ref["created_at"])

    def _audit(self):
        self._numbered("audit", self.body["audit"], "audit")
        actions = set(AuditAction.values)
        wanted = {"id", "created_at", "action", "actor", "actor_email", "subject", "detail"}
        for row in self.body["audit"]:
            where = row["id"]
            if set(row) != wanted:
                _fail(where, f"keys should be {sorted(wanted)}")
            if row["action"] not in actions:
                _fail(f"{where}.action", f"{row['action']!r} is not a known audit action")
            self._ref(f"{where}.actor", "users", row["actor"], True)
            for key in ("actor_email", "subject"):
                if not isinstance(row[key], str) or len(row[key]) > 254:
                    _fail(f"{where}.{key}", "must be text of at most 254 characters")
            if not isinstance(row["detail"], dict):
                _fail(f"{where}.detail", "must be an object")
            self._datetime(f"{where}.created_at", row["created_at"])

    def _datetime(self, where, value):
        try:
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None:
            _fail(where, f"{value!r} is not a date and time with an offset")


def check(body, media_paths):
    """Raise SchemaError on the first problem; return the checker (its media references) otherwise."""
    checker = Checker(body, media_paths)
    checker.run()
    return checker
