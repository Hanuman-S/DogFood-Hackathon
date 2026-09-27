"""An upsert that reports what it did, and that defaults to never overwriting existing rows.

Two problems this solves.

**1. `update_or_create` writes unconditionally.** It issues an UPDATE every time, bumping
`auto_now` timestamps -- so "a second import changes nothing" would be false in exactly the way
that matters: an organizer diffing `updated_at` would see hundreds of rows touched by a boot that
imported no new data. `upsert` compares field by field and writes only on a real difference.

**2. An idempotent import can still be an authoritative one, which is worse.** Comparing against
the fixture and writing back on any difference makes the importer behave like a
config-management tool that reverts manual changes. Since the entrypoint runs it on *every* boot,
that would mean: an organizer extends a deadline, someone restarts the container, and the
deadline silently snaps back to the fixture value. The brief explicitly requires organizers to be
able to extend `submissions_close_at`, so that is not a hypothetical -- it would break a T1
feature, and it contradicts the rule that seeding never overwrites user-created data.

So `sync=False` (the default) is **create-only**: missing rows are inserted, existing rows are
left exactly as they are. A row that exists but differs from the fixture is reported as
`preserved`, with the differing field names, so the import report tells an operator what diverged
instead of hiding it. `sync=True` restores the overwriting behaviour and is reachable only via
`manage.py import_fixtures --sync`, run deliberately by a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import models

CREATED = "created"
UPDATED = "updated"
UNCHANGED = "unchanged"
PRESERVED = "preserved"


@dataclass
class Tally:
    """Per-entity counters for the import report."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    # Rows that exist and differ from the fixture, left untouched in create-only mode.
    preserved: int = 0
    skipped: list[str] = field(default_factory=list)
    divergences: list[str] = field(default_factory=list)

    def record(self, outcome: str) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)

    def skip(self, reason: str) -> None:
        self.skipped.append(reason)

    def diverged(self, description: str) -> None:
        self.divergences.append(description)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.unchanged + self.preserved

    def summary(self) -> str:
        parts = [f"{self.total} total", f"{self.created} created"]
        if self.updated:
            parts.append(f"{self.updated} updated")
        if self.unchanged:
            parts.append(f"{self.unchanged} unchanged")
        if self.preserved:
            parts.append(f"{self.preserved} preserved (differ from fixture, left alone)")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        return ", ".join(parts)


def _identify(instance: models.Model, lookup: dict) -> str:
    """A short human-readable identity for the import report."""
    external = getattr(instance, "external_id", None)
    if external:
        return f"{type(instance).__name__} {external}"
    key = lookup.get("email") or lookup.get("key") or lookup.get("slug") or instance.pk
    return f"{type(instance).__name__} {key}"


def _current_value(instance: models.Model, name: str):
    """Read a field for comparison without triggering a lazy FK fetch.

    For a relation, read the `*_id` attribute: `getattr(obj, "track")` would query the database
    once per field per row, turning a 41-project import into hundreds of pointless queries.
    """
    django_field = instance._meta.get_field(name)
    if django_field.is_relation:
        return getattr(instance, django_field.attname)
    return getattr(instance, name)


def _target_value(instance: models.Model, name: str, value):
    """Normalize an assigned value to the form `_current_value` returns."""
    django_field = instance._meta.get_field(name)
    if django_field.is_relation:
        return value.pk if isinstance(value, models.Model) else value
    return value


def upsert(
    model: type[models.Model],
    *,
    lookup: dict,
    fields: dict,
    validate: bool = True,
    validate_exclude: tuple[str, ...] = (),
    sync: bool = False,
    tally: Tally | None = None,
    label: str = "",
) -> tuple[models.Model, str]:
    """Create a row, or report on the existing one without touching it.

    Args:
        lookup: the natural or external key identifying the row. Used for the query and, on
            creation, as part of the new row's values.
        fields: the columns the source governs. Columns absent from this dict are never
            considered, which is what lets `seed_demo` set a password on an imported account
            without a later import having any opinion about it.
        validate: run `full_clean` before saving. On by default: writing rows the application's
            own validators would reject is a silent change to the meaning of the data.
        sync: when False (the default) an existing row is **never modified** -- see the module
            docstring. When True, differing fields are written, which is what
            `import_fixtures --sync` does.
        tally, label: optional, used to record which fields diverged when a row is preserved.

    Returns:
        `(instance, outcome)`: `created`, `unchanged`, `preserved` or (only with `sync=True`)
        `updated`.
    """
    instance = model.objects.filter(**lookup).first()

    if instance is None:
        instance = model(**lookup, **fields)
        if validate:
            instance.full_clean(exclude=list(validate_exclude) or None)
        instance.save()
        return instance, CREATED

    changed = [
        name
        for name, value in fields.items()
        if _current_value(instance, name) != _target_value(instance, name, value)
    ]

    if not changed:
        return instance, UNCHANGED

    if not sync:
        # The row exists and has drifted from the fixture. Leave it: the live value is somebody's
        # deliberate edit until proven otherwise, and a boot-time job is in no position to
        # decide it was a mistake. Report it so the divergence is visible rather than silent.
        if tally is not None:
            identity = label or _identify(instance, lookup)
            tally.diverged(f"{identity}: {', '.join(sorted(changed))}")
        return instance, PRESERVED

    for name in changed:
        setattr(instance, name, fields[name])
    if validate:
        instance.full_clean(exclude=list(validate_exclude) or None)
    # Save only the columns that differed, plus any auto_now column the model maintains. Listing
    # them keeps the UPDATE narrow and makes a concurrent write to an untouched column safe.
    auto_now = [
        f.name
        for f in model._meta.fields
        if getattr(f, "auto_now", False)
    ]
    instance.save(update_fields=[*changed, *auto_now])
    return instance, UPDATED
