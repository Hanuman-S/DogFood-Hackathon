from django import forms
from django.utils.text import slugify

from events.models import CustomQuestion, Event, Prize, QuestionKind, Track


class UTCDateTimeWidget(forms.SplitDateTimeWidget):
    """A date box (with the browser's calendar) and a time box, side by side.

    One combined datetime-local box was replaced because some browsers' pickers set only the
    day of it, leaving the month and year to be typed by hand. Separate date and time inputs are
    the browsers' plain, reliable pickers. Autofill is off, so a new event starts with every date
    empty. The server's time zone is UTC, so what is typed is UTC.
    """

    template_name = "widgets/utc_datetime.html"

    def __init__(self, attrs=None):
        super().__init__(
            attrs=attrs,
            date_attrs={"type": "date", "autocomplete": "off"},
            time_attrs={"type": "time", "autocomplete": "off", "aria-label": "time (UTC)"},
            date_format="%Y-%m-%d",
            time_format="%H:%M",
        )

    def id_for_label(self, id_):
        # The label names the date box; the time box carries its own aria-label.
        return f"{id_}_0" if id_ else id_


class UTCDateTimeField(forms.SplitDateTimeField):
    """A date and a time, both needed. Says which half is missing rather than a generic error:
    the browser's calendar fills in only the date box, so a forgotten time is the usual case."""

    def clean(self, value):
        if isinstance(value, (list, tuple)) and len(value) == 2:
            date, time = (v.strip() if isinstance(v, str) else v for v in value)
            if date and not time:
                raise forms.ValidationError(self.error_messages["invalid_time"], code="invalid_time")
            if time and not date:
                raise forms.ValidationError(self.error_messages["invalid_date"], code="invalid_date")
        return super().clean(value)


def utc_datetime_field(label, required=True, help_text=""):
    return UTCDateTimeField(
        label=label, required=required, help_text=help_text,
        widget=UTCDateTimeWidget(),
        input_date_formats=["%Y-%m-%d"], input_time_formats=["%H:%M", "%H:%M:%S"],
        error_messages={
            "required": "Pick a date and a time (UTC).",
            "incomplete": "Pick a date and a time (UTC).",
            "invalid_date": "Pick a date as well as the time.",
            "invalid_time": "Pick a time as well as the date.",
        },
    )


# The timeline, in the only order the database accepts. Labels double as error sentences.
TIMELINE = [
    ("starts_at", "event starts"),
    ("submissions_open_at", "submissions open"),
    ("submissions_close_at", "submissions close"),
    ("judging_starts_at", "judging starts"),
    ("judging_ends_at", "judging ends"),
    ("results_at", "results"),
]


class EventForm(forms.ModelForm):
    starts_at = utc_datetime_field("event starts (UTC)")
    submissions_open_at = utc_datetime_field("submissions open (UTC)")
    submissions_close_at = utc_datetime_field("submissions close (UTC)")
    judging_starts_at = utc_datetime_field("judging starts (UTC)")
    judging_ends_at = utc_datetime_field("judging ends (UTC)")
    results_at = utc_datetime_field(
        "results (UTC)", required=False, help_text="optional: leave empty for 'to be announced'",
    )

    class Meta:
        model = Event
        fields = [
            "name", "slug", "tagline", "description",
            "starts_at", "submissions_open_at", "submissions_close_at",
            "judging_starts_at", "judging_ends_at", "results_at",
            "min_team_size", "max_team_size",
        ]
        labels = {
            "slug": "url name",
            "min_team_size": "min team size",
            "max_team_size": "max team size",
        }
        help_texts = {
            "slug": "lower-case letters, digits and hyphens; used in links: /events/<url name>",
            "description": "markdown",
            "min_team_size": "smaller teams can form, but cannot submit",
            "max_team_size": "1 to 20",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. Spring Hack 2027"}),
            "slug": forms.TextInput(attrs={"placeholder": "leave empty to make one from the name"}),
            "tagline": forms.TextInput(attrs={"placeholder": "one sentence, e.g. 48 hours to build developer tools"}),
            "description": forms.Textarea(attrs={"rows": 6, "placeholder": "Markdown. What the event is about, rules, judging, anything participants should read first."}),
            "min_team_size": forms.NumberInput(attrs={"min": 1, "max": 20, "placeholder": "e.g. 1"}),
            "max_team_size": forms.NumberInput(attrs={"min": 1, "max": 20, "placeholder": "e.g. 4"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slug"].required = False
        # A published event page needs to say what the event is.
        self.fields["tagline"].required = True
        self.fields["description"].required = True

    def clean_slug(self):
        slug = self.cleaned_data.get("slug") or slugify(self.cleaned_data.get("name", ""))
        slug = slugify(slug)[:60]
        if not slug:
            raise forms.ValidationError("Give the event a url name.")
        clash = Event.objects.filter(slug=slug).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("Another event already uses this url name.")
        return slug

    def clean_max_team_size(self):
        size = self.cleaned_data["max_team_size"]
        if not 1 <= size <= 20:
            raise forms.ValidationError("Team size must be between 1 and 20.")
        if self.instance.pk:
            from teams.models import Team
            largest = max((t.members.count() for t in Team.objects.filter(event=self.instance)), default=0)
            if size < largest:
                raise forms.ValidationError(
                    f"A team already has {largest} members; the limit cannot go below that."
                )
        return size

    def clean_min_team_size(self):
        size = self.cleaned_data["min_team_size"]
        if not 1 <= size <= 20:
            raise forms.ValidationError("Minimum team size must be between 1 and 20.")
        if self.instance.pk and size > self.instance.min_team_size:
            # Raising the bar must not silently invalidate work already submitted.
            from django.db.models import Count

            from projects.models import Project, Status

            short = (
                Project.objects.filter(event=self.instance, status=Status.SUBMITTED)
                .annotate(n=Count("team__members")).filter(n__lt=size).count()
            )
            if short:
                raise forms.ValidationError(
                    f"{short} submitted project{'s' if short != 1 else ''} come from teams smaller "
                    f"than {size}. Ask them to recruit or withdraw first."
                )
        return size

    def clean(self):
        cleaned = super().clean()
        low, high = cleaned.get("min_team_size"), cleaned.get("max_team_size")
        if low and high and low > high:
            self.add_error("min_team_size", "The minimum cannot be larger than the maximum.")
        # Mirrors the database's CHECK constraints, so the organizer gets a sentence, not a 500:
        # each date must come strictly after the one before it (results only when set).
        previous = None
        for name, label in TIMELINE:
            when = cleaned.get(name)
            if when is None:
                if name != "results_at":
                    previous = None  # missing or invalid: its own error says so
                continue
            if previous is not None and when <= previous[1]:
                self.add_error(name, f"{label.capitalize()} must come after {previous[0]}.")
            previous = (label, when)
        return cleaned


class TrackForm(forms.ModelForm):
    class Meta:
        model = Track
        fields = ["name", "description", "order"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. Developer tools"}),
            "description": forms.TextInput(attrs={"placeholder": "optional, e.g. Things that make building faster"}),
            "order": forms.NumberInput(attrs={"min": 0}),
        }

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        clash = Track.objects.filter(event=self.event, name__iexact=name).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("This event already has a track with that name.")
        return name


class PrizeForm(forms.ModelForm):
    class Meta:
        model = Prize
        fields = ["title", "value", "rank", "track", "description"]
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "e.g. Grand prize"}),
            "value": forms.TextInput(attrs={"placeholder": "e.g. $800, or Swag box"}),
            "rank": forms.NumberInput(attrs={"min": 1}),
            "description": forms.TextInput(attrs={"placeholder": "optional, e.g. Includes mentoring sessions"}),
        }

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["track"].queryset = Track.objects.filter(event=event)
        self.fields["track"].empty_label = "overall (no track)"


class QuestionForm(forms.ModelForm):
    class Meta:
        model = CustomQuestion
        fields = ["prompt", "help_text", "kind", "choices", "required", "order"]
        widgets = {
            "prompt": forms.TextInput(attrs={"placeholder": "e.g. What did you cut, and why?"}),
            "help_text": forms.TextInput(attrs={"placeholder": "optional hint shown under the question"}),
            "choices": forms.Textarea(attrs={"rows": 3, "placeholder": "single choice only, one option per line, e.g.\nWeb\nMobile\nCLI"}),
            "order": forms.NumberInput(attrs={"min": 0}),
        }

    def __init__(self, *args, event=None, kind_locked=False, **kwargs):
        super().__init__(*args, **kwargs)
        if kind_locked:
            self.fields["kind"].disabled = True
            self.fields["kind"].help_text = "locked: this question already has answers"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("kind") == QuestionKind.CHOICE:
            options = [o for o in (cleaned.get("choices") or "").splitlines() if o.strip()]
            if len(options) < 2:
                self.add_error("choices", "A single-choice question needs at least two options.")
        return cleaned


class AddOrganizerForm(forms.Form):
    email = forms.EmailField(widget=forms.EmailInput(attrs={"placeholder": "the email of an existing account"}))


class AddJudgeForm(forms.Form):
    email = forms.EmailField(widget=forms.EmailInput(attrs={"placeholder": "e.g. ada@example.org"}))
    tracks = forms.ModelMultipleChoiceField(
        queryset=None, required=False, widget=forms.CheckboxSelectMultiple,
        help_text="leave all unticked to judge every track",
    )

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tracks"].queryset = event.visible_tracks()


class ExtendDeadlineForm(forms.Form):
    new_close = utc_datetime_field("new close (UTC)")
    reason = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "shown in the audit log, e.g. Wi-Fi outage at the venue"}),
    )


class ExtendJudgingForm(forms.Form):
    new_end = utc_datetime_field("new judging end (UTC)")
    reason = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "shown in the audit log, e.g. two judges fell ill"}),
    )


class TeamExtensionForm(forms.Form):
    team = forms.ModelChoiceField(queryset=None, empty_label="-- choose a team --")
    until = utc_datetime_field("extended until (UTC)")
    reason = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "e.g. Upload failed at 23:28, confirmed in Discord"}),
    )

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        from teams.models import Team

        self.fields["team"].queryset = Team.objects.filter(event=event).order_by("name")
