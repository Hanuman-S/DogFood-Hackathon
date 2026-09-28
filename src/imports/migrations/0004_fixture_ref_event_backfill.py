"""Fill FixtureRef.event from each ref's object: event -> itself, track/team/project -> its event,
score -> its project's event, judge -> its membership's event; user refs stay NULL (install-wide).
A ref whose object no longer exists is deleted here: 0005's constraint would refuse it, and nothing
could resolve it anyway. Side effect: the fixture importer finds rows by their refs, so running
`import_fixtures` again re-creates the row whose orphaned ref was removed (the organizers' file still
lists it). The reverse is a no-op."""

from django.db import migrations

SOURCES = {
    "event": ("events", "Event", None),
    "track": ("events", "Track", "event_id"),
    "team": ("teams", "Team", "event_id"),
    "project": ("projects", "Project", "event_id"),
    "judge": ("events", "EventMembership", "event_id"),
    "score": ("scoring", "Score", "project__event_id"),
}


def backfill(apps, schema_editor):
    FixtureRef = apps.get_model("imports", "FixtureRef")
    for kind, (app, model_name, path) in SOURCES.items():
        model = apps.get_model(app, model_name)
        refs = FixtureRef.objects.filter(kind=kind, event__isnull=True)
        ids = list(refs.values_list("object_id", flat=True))
        if not ids:
            continue
        if path is None:
            events = {pk: pk for pk in model.objects.filter(pk__in=ids).values_list("pk", flat=True)}
        else:
            events = dict(model.objects.filter(pk__in=ids).values_list("pk", path))
        for ref in refs:
            event_id = events.get(ref.object_id)
            if event_id is None:
                ref.delete()  # its object is gone: nothing can resolve it
            else:
                FixtureRef.objects.filter(pk=ref.pk).update(event_id=event_id)


class Migration(migrations.Migration):

    dependencies = [
        ("imports", "0003_fixture_ref_event"),
        ("events", "0007_event_comments_enabled"),
        ("teams", "0001_initial"),
        ("projects", "0003_comment"),
        ("scoring", "0013_score_judge_restrict"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
