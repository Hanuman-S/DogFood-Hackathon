"""Rubric forms. They only shape and type-check input: the rules (weights above 0, the
lock, keys unique, scored criteria stay) live in `scoring.services`, so any API enforces the same."""

from decimal import Decimal

from django import forms

from scoring.models import WINNERS_TOP_N_MAX, ResultVisibility
from scoring.services import RubricRow


class CriterionRowForm(forms.Form):
    """One line of the rubric editor."""

    id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    label = forms.CharField(max_length=120, widget=forms.TextInput(attrs={"placeholder": "e.g. Functionality"}))
    key = forms.SlugField(
        max_length=60, required=False,
        widget=forms.TextInput(attrs={"placeholder": "made from the label"}),
    )
    weight = forms.DecimalField(
        max_digits=6, decimal_places=3, min_value=Decimal("0.001"),
        widget=forms.NumberInput(attrs={"step": "0.001", "min": "0.001", "data-weight": ""}),
    )
    def to_row(self, order):
        data = self.cleaned_data
        return RubricRow(
            id=data.get("id"), key=data.get("key") or "", label=data.get("label") or "",
            weight=data.get("weight"), order=order, delete=bool(data.get("DELETE")),
        )


class BaseRubricFormSet(forms.BaseFormSet):
    @staticmethod
    def counts(form):
        """An untouched blank row is not a criterion; an existing one always is."""
        return form.has_changed() or bool(form.cleaned_data.get("id"))

    def rows(self):
        """The submitted rubric, in the order shown."""
        rows = []
        for form in self.forms:
            if self.counts(form):
                rows.append(form.to_row(order=len(rows) + 1))
        return rows


RubricFormSet = forms.formset_factory(
    CriterionRowForm, formset=BaseRubricFormSet, extra=1, can_delete=True, max_num=20,
    validate_max=True,
)


def rubric_initial(criteria):
    return [
        {"id": c.pk, "label": c.label, "key": c.key, "weight": c.weight.normalize()}
        for c in criteria
    ]


class CriterionTextForm(forms.Form):
    """What judges read: label, description, and a written anchor per level of the scale."""

    label = forms.CharField(max_length=120)
    description = forms.CharField(
        required=False, max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "what judges should look for"}),
    )

    def __init__(self, *args, criterion, **kwargs):
        super().__init__(*args, **kwargs)
        self.criterion = criterion
        for level in range(criterion.min_value, criterion.max_value + 1):
            self.fields[f"level_{level}"] = forms.CharField(
                label=f"level {level}", required=False, max_length=300,
                initial=criterion.level_descriptions.get(str(level), ""),
                widget=forms.TextInput(attrs={"placeholder": f"what a {level} looks like (optional, leave empty to skip)"}),
            )

    def level_fields(self):
        return [self[f"level_{n}"] for n in range(self.criterion.min_value, self.criterion.max_value + 1)]

    def levels(self):
        return {
            str(n): self.cleaned_data.get(f"level_{n}", "")
            for n in range(self.criterion.min_value, self.criterion.max_value + 1)
        }


class FinalWeightsForm(forms.Form):
    """The judge/community split. Rules (sum 100, the lock): scoring.services.set_final_weights."""

    judge_weight = forms.IntegerField(min_value=0, max_value=100, label="judges (%)")
    community_weight = forms.IntegerField(min_value=0, max_value=100, label="community vote (%)")


class ResultSettingsForm(forms.Form):
    """Who sees the published result. Rules and the audit row: scoring.services.set_result_settings."""

    visibility = forms.ChoiceField(
        choices=ResultVisibility.choices, widget=forms.RadioSelect,
        help_text="applies only once a final result is published. organizers and admins always see everything.",
    )
    winners_top_n = forms.IntegerField(
        min_value=1, max_value=WINNERS_TOP_N_MAX, label="winners overall",
        help_text="the top places named as winners overall (a tie on the cut includes everyone on it), "
        "plus the top project of each track.",
    )
