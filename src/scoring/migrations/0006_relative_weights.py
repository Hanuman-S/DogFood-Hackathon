"""Weights are relative again (each above 0; the share is weight / sum).

0003 had rescaled every rubric into percentages adding up to exactly 100.000, which turned equal
weights 1 / 1 / 1 into 33.334 / 33.333 / 33.333: no longer equal, so the first criterion quietly
broke ties between otherwise equal reviews. This migration puts such equal splits back to 1 each.
Every other rubric keeps its numbers: percentages are valid relative weights (50 / 30 / 20 still
means half, three tenths and a fifth).

A weight of 0 is no longer allowed (CHECK criterion_weight_positive). A rubric whose weights are
all 0 becomes equal weights 1; a rubric with only some zeros is refused, loudly, because deciding
what those criteria should weigh is the organizer's call.
"""

from decimal import Decimal

import django.core.validators
from django.db import migrations, models

STEP = Decimal("0.001")


def equal_split_of_100(n):
    """What 0003 turned n equal weights into: e.g. 3 -> 33.334, 33.333, 33.333."""
    base, extra = divmod(100000, n)
    return [(base + (1 if i < extra else 0)) * STEP for i in range(n)]


def to_relative(apps, schema_editor):
    Criterion = apps.get_model("scoring", "Criterion")
    for event_id in Criterion.objects.values_list("event_id", flat=True).distinct():
        criteria = list(Criterion.objects.filter(event_id=event_id).order_by("order", "key", "id"))
        weights = [Decimal(c.weight) for c in criteria]
        if all(w == 0 for w in weights) or weights == equal_split_of_100(len(weights)):
            Criterion.objects.filter(pk__in=[c.pk for c in criteria]).update(weight=Decimal(1))
        elif any(w <= 0 for w in weights):
            raise RuntimeError(
                f"event {event_id}: some rubric weights are 0, which is no longer allowed. "
                "Set them above 0 before migrating."
            )


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0004_judging_start_results_strict_timeline"),
        ("scoring", "0005_snapshot_publication_triggers"),
    ]

    operations = [
        migrations.RunPython(to_relative, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="criterion",
            name="weight",
            field=models.DecimalField(
                decimal_places=3, default=1, max_digits=6,
                help_text="Relative weight, above 0: the criterion's share of the score is its weight over the sum of the event's weights (1 / 1 / 1 = exact thirds). Decimal, not float, so what the organizer typed is exactly what is stored.",
                validators=[django.core.validators.MinValueValidator(Decimal("0.001"))],
            ),
        ),
        migrations.AddConstraint(
            model_name="criterion",
            constraint=models.CheckConstraint(condition=models.Q(("weight__gt", 0)), name="criterion_weight_positive"),
        ),
    ]
