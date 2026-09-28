"""FixtureRef.event (nullable for now) and a wider `source`. Schema only: the backfill is 0004 and the
constraint 0005, because Postgres refuses to ALTER a table in the same transaction that updated its
rows while deferred foreign-key checks are pending."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("imports", "0002_fixture_judge_refs"),
        ("events", "0007_event_comments_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="fixtureref",
            name="event",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                    related_name="fixture_refs", to="events.event"),
        ),
        migrations.AlterField(
            model_name="fixtureref",
            name="source",
            field=models.CharField(default="dogfood-fixtures", max_length=160),
        ),
    ]
