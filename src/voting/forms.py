"""The organizer's voting form. It only shapes and type-checks input: the rules (the window
against the submission close, what locks once voting opens, the budget) are voting.services."""

from django import forms

from events.forms import utc_datetime_field

from .models import CREDIT_BUDGET_MAX, AccessMode, Method


class VotingConfigForm(forms.Form):
    opens_at = utc_datetime_field("voting opens (UTC)")
    closes_at = utc_datetime_field("voting closes (UTC)")
    method = forms.ChoiceField(choices=Method.choices, widget=forms.RadioSelect)
    credit_budget = forms.IntegerField(
        min_value=1, max_value=CREDIT_BUDGET_MAX, initial=16, label="credits per voter",
        help_text="quadratic only (one person, one vote is always 1). a project's influence from one "
        "ballot is the square root of the credits placed on it.",
    )
    access_mode = forms.ChoiceField(
        choices=AccessMode.choices, widget=forms.RadioSelect, initial=AccessMode.AUTHENTICATED,
        help_text="only logged-in voting is available so far.",
    )
    accounts_before_open_only = forms.BooleanField(
        required=False, initial=True, label="only accounts created before voting opens",
    )
