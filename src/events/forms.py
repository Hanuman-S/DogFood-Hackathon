"""Organizer-facing forms.

Every datetime field is labelled UTC and uses `datetime-local` inputs, which browsers render in the
*viewer's* timezone with no indication of which one. That is a genuine hazard for a deadline, so the
label says UTC, the help text says UTC, and `UTCDateTimeField` treats the submitted value as UTC
rather than as local time.
"""

from __future__ import annotations

import datetime as dt

from django import forms

from events.models import CustomQuestion, Event, Prize, QuestionKind, Role, Track

UTC = dt.timezone.utc


class UTCDateTimeField(forms.DateTimeField):
    """A datetime field whose input is always interpreted as UTC.

    Django would otherwise attach `TIME_ZONE` to a naive submitted value. That happens to be UTC
    here, so this is belt and braces -- but it makes the intent explicit at the field level, so a
    deployment that ever changes `TIME_ZONE` for display purposes cannot silently shift every
    deadline an organizer has typed.
    """

    widget = forms.DateTimeInput(
        attrs={"type": "datetime-local", "step": 60}, format="%Y-%m-%dT%H:%M"
    )
    input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"]

    def to_python(self, value):
        parsed = super().to_python(value)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)


class EventForm(forms.ModelForm):
    starts_at = UTCDateTimeField(label="Event starts (UTC)")
    submissions_open_at = UTCDateTimeField(label="Submissions open (UTC)")
    submissions_close_at = UTCDateTimeField(
        label="Submissions close (UTC)",
        help_text="The last accepted submission is one second before this instant.",
    )
    judging_ends_at = UTCDateTimeField(
        label="Judging ends (UTC)", required=False, help_text="Optional. Used by judging, not by T1."
    )

    class Meta:
        model = Event
        fields = [
            "name",
            "description",
            "starts_at",
            "submissions_open_at",
            "submissions_close_at",
            "judging_ends_at",
            "max_team_size",
            "gallery_public",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 6})}
        help_texts = {
            "gallery_public": "When off, submitted projects are hidden from the public gallery.",
            "max_team_size": "Maximum members per team.",
        }

    def clean(self):
        cleaned = super().clean()
        # Delegates to `Event.clean()` so the field-level messages an organizer sees come from the
        # same place the database constraints are mirrored, rather than being written twice.
        return cleaned


class TrackForm(forms.ModelForm):
    class Meta:
        model = Track
        fields = ["name", "description", "order"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


class PrizeForm(forms.ModelForm):
    class Meta:
        model = Prize
        fields = ["name", "description", "value_text", "track", "order"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
        labels = {"value_text": "Value", "track": "Track (optional)"}
        help_texts = {
            "value_text": "Free text, e.g. “800 USD”. Prizes are often not cash.",
            "track": "Leave blank for an event-wide prize.",
        }

    def __init__(self, *args, event: Event, **kwargs):
        super().__init__(*args, **kwargs)
        # Scoped so an organizer cannot attach a prize to another event's track.
        self.fields["track"].queryset = Track.objects.filter(event=event)
        self.fields["track"].required = False


class CustomQuestionForm(forms.ModelForm):
    choices_text = forms.CharField(
        label="Options",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "One option per line"}),
        help_text="One per line. Only used for a choice question.",
    )

    class Meta:
        model = CustomQuestion
        fields = ["prompt", "kind", "required", "order", "show_in_gallery"]
        help_texts = {
            "required": "Required questions must be answered to submit — a draft can still be saved.",
            "show_in_gallery": "When on, the answer is shown publicly on the project page.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.choices:
            self.fields["choices_text"].initial = "\n".join(self.instance.choices)

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("kind")
        raw = cleaned.get("choices_text") or ""
        options = [line.strip() for line in raw.splitlines() if line.strip()]

        if kind == QuestionKind.CHOICE:
            if len(options) < 2:
                self.add_error("choices_text", "A choice question needs at least two options.")
            elif len(set(options)) != len(options):
                self.add_error("choices_text", "Options must be distinct.")
            cleaned["choices"] = options
        else:
            if options:
                self.add_error(
                    "choices_text",
                    f"Options only apply to a choice question, not {kind}.",
                )
            cleaned["choices"] = []
        return cleaned


class MembershipForm(forms.Form):
    """Add a judge or co-organizer by email."""

    email = forms.EmailField(
        label="Email address",
        help_text="The person must already have an account: this deployment sends no email.",
    )
    role = forms.ChoiceField(
        label="Role",
        choices=[(Role.JUDGE, "Judge"), (Role.ORGANIZER, "Co-organizer")],
    )
    tracks = forms.ModelMultipleChoiceField(
        label="Tracks (judges only)",
        queryset=Track.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Which tracks this judge is responsible for.",
    )

    def __init__(self, *args, event: Event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tracks"].queryset = Track.objects.filter(event=event)

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()
