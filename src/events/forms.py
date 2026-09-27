from django import forms
from django.utils.text import slugify

from events.models import CustomQuestion, Event, Prize, QuestionKind, Track

DATETIME_FORMAT = "%Y-%m-%dT%H:%M"


class UTCDateTimeInput(forms.DateTimeInput):
    """A datetime-local picker. The server's time zone is UTC, so what is typed is UTC."""

    input_type = "datetime-local"

    def __init__(self, **kwargs):
        super().__init__(format=DATETIME_FORMAT, **kwargs)


class EventForm(forms.ModelForm):
    class Meta:
        model = Event
        fields = [
            "name", "slug", "tagline", "description",
            "starts_at", "submissions_open_at", "submissions_close_at", "judging_ends_at",
            "min_team_size", "max_team_size",
        ]
        labels = {
            "slug": "url name",
            "starts_at": "event starts (UTC)",
            "submissions_open_at": "submissions open (UTC)",
            "submissions_close_at": "submissions close (UTC)",
            "judging_ends_at": "judging ends (UTC)",
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
            "starts_at": UTCDateTimeInput(),
            "submissions_open_at": UTCDateTimeInput(),
            "submissions_close_at": UTCDateTimeInput(),
            "judging_ends_at": UTCDateTimeInput(),
            "min_team_size": forms.NumberInput(attrs={"min": 1, "max": 20, "placeholder": "e.g. 1"}),
            "max_team_size": forms.NumberInput(attrs={"min": 1, "max": 20, "placeholder": "e.g. 4"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slug"].required = False
        for name in ("starts_at", "submissions_open_at", "submissions_close_at", "judging_ends_at"):
            self.fields[name].input_formats = [DATETIME_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"]

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
        opens = cleaned.get("submissions_open_at")
        closes = cleaned.get("submissions_close_at")
        judging = cleaned.get("judging_ends_at")
        starts = cleaned.get("starts_at")
        # Mirrors the database's CHECK constraints, so the organizer gets a sentence, not a 500.
        if opens and closes and opens >= closes:
            self.add_error("submissions_close_at", "Submissions must close after they open.")
        if closes and judging and judging < closes:
            self.add_error("judging_ends_at", "Judging cannot end before submissions close.")
        if starts and closes and starts > closes:
            self.add_error("starts_at", "The event must start before submissions close.")
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
    email = forms.EmailField(widget=forms.EmailInput(attrs={"placeholder": "the email of an existing account"}))
    tracks = forms.ModelMultipleChoiceField(
        queryset=None, required=False, widget=forms.CheckboxSelectMultiple,
        help_text="leave all unticked to judge every track",
    )

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tracks"].queryset = event.visible_tracks()


class ExtendDeadlineForm(forms.Form):
    new_close = forms.DateTimeField(
        label="new close (UTC)", widget=UTCDateTimeInput(), input_formats=[DATETIME_FORMAT, "%Y-%m-%d %H:%M"],
    )
    reason = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "shown in the audit log, e.g. Wi-Fi outage at the venue"}),
    )


class TeamExtensionForm(forms.Form):
    team = forms.ModelChoiceField(queryset=None, empty_label="-- choose a team --")
    until = forms.DateTimeField(
        label="extended until (UTC)", widget=UTCDateTimeInput(), input_formats=[DATETIME_FORMAT, "%Y-%m-%d %H:%M"],
    )
    reason = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "e.g. Upload failed at 23:28, confirmed in Discord"}),
    )

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        from teams.models import Team

        self.fields["team"].queryset = Team.objects.filter(event=event).order_by("name")
