"""A separate judging start, an optional results date, and a strictly ordered timeline:

    event starts < submissions open < submissions close < judging starts < judging ends < results

Existing events predate both rules, so before the new constraints go on, each is brought into
line with the smallest change that keeps its meaning (the numbers are in JUDGING.md):

* **judging starts** one hour after submissions close -- or halfway to the judging end, if
  judging was shorter than two hours;
* **event starts** that coincided with (or followed) submissions opening move to one hour before
  it. The seeds and the fixture importer set both to the same instant before this.

Results stay empty (to be announced).
"""

from datetime import timedelta

from django.db import migrations, models
from django.db.models import F, Q

GAP = timedelta(hours=1)


def fit_existing_events(apps, schema_editor):
    Event = apps.get_model("events", "Event")
    for event in Event.objects.all():
        judging = event.judging_ends_at - event.submissions_close_at
        event.judging_starts_at = event.submissions_close_at + (GAP if judging >= 2 * GAP else judging / 2)
        if event.starts_at >= event.submissions_open_at:
            event.starts_at = event.submissions_open_at - GAP
        event.save(update_fields=["judging_starts_at", "starts_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0003_judging_extension_and_invites"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="judging_starts_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="event",
            name="results_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RemoveConstraint(model_name="event", name="event_judging_after_submissions"),
        migrations.RemoveConstraint(model_name="event", name="event_starts_before_close"),
        migrations.RunPython(fit_existing_events, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="event",
            name="judging_starts_at",
            field=models.DateTimeField(),
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.CheckConstraint(
                condition=Q(starts_at__lt=F("submissions_open_at")),
                name="event_starts_before_submissions_open",
            ),
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.CheckConstraint(
                condition=Q(submissions_close_at__lt=F("judging_starts_at")),
                name="event_judging_starts_after_close",
            ),
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.CheckConstraint(
                condition=Q(judging_starts_at__lt=F("judging_ends_at")),
                name="event_judging_window_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.CheckConstraint(
                condition=Q(results_at__isnull=True) | Q(judging_ends_at__lt=F("results_at")),
                name="event_results_after_judging",
            ),
        ),
    ]
