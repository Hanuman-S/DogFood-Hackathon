"""Every event-scoped ref (all kinds but `user`) now names its event."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("imports", "0004_fixture_ref_event_backfill"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="fixtureref",
            constraint=models.CheckConstraint(condition=models.Q(("kind", "user"), ("event__isnull", False),
                                                                 _connector="OR"),
                                              name="fixture_ref_event_scoped_has_event"),
        ),
    ]
