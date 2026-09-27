"""The project form: the standard submission fields plus one field per custom question.

Drafts may be incomplete, so only the project name is required while editing. Completeness is
checked when a project is submitted -- and on every save of an already-submitted project, so a
submission can never be edited into an incomplete state.
"""

from django import forms
from django.conf import settings
from django.core.validators import URLValidator

from events.models import QuestionKind
from projects.images import clean_image
from projects.models import Project

web_url = URLValidator(schemes=["http", "https"])


def normalize_tags(raw):
    """'Python, Django ,python , ML' -> ['python', 'django', 'ml'] (order kept, no duplicates)."""
    seen, tags = set(), []
    for part in (raw or "").replace("\n", ",").split(","):
        tag = " ".join(part.strip().lower().split())[:40]
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def answer_field(question):
    common = {"label": question.prompt, "help_text": question.help_text, "required": False}
    if question.kind == QuestionKind.LONG:
        return forms.CharField(max_length=5000, widget=forms.Textarea(attrs={"rows": 4}), **common)
    if question.kind == QuestionKind.URL:
        return forms.URLField(validators=[web_url], assume_scheme="https", **common)
    if question.kind == QuestionKind.CHOICE:
        choices = [("", "-- choose --")] + [(c, c) for c in question.choice_list()]
        return forms.ChoiceField(choices=choices, **common)
    if question.kind == QuestionKind.CHECKBOX:
        return forms.BooleanField(**common)
    return forms.CharField(max_length=300, **common)


def answer_to_text(question, value):
    if question.kind == QuestionKind.CHECKBOX:
        return "yes" if value else ""
    return (value or "").strip()


class ProjectForm(forms.ModelForm):
    tags = forms.CharField(
        required=False,
        help_text=f"comma-separated, up to {settings.MAX_PROJECT_TAGS}",
        widget=forms.TextInput(attrs={"placeholder": "e.g. python, django, postgres (separate with commas)", "data-tags": ""}),
    )
    thumbnail_upload = forms.FileField(
        required=False, label="thumbnail",
        help_text="JPEG, PNG, WebP or GIF, under 5 MB. Re-encoded on upload; metadata is removed.",
        widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png,image/webp,image/gif"}),
    )
    remove_thumbnail = forms.BooleanField(required=False, label="remove the current thumbnail")

    class Meta:
        model = Project
        fields = ["name", "tagline", "description", "track", "repo_url", "demo_video_url", "live_url"]
        labels = {
            "name": "project name",
            "demo_video_url": "demo video url",
            "repo_url": "repository url",
            "live_url": "live link",
        }
        help_texts = {
            "description": "markdown: headings, lists, links, code blocks, tables",
            "demo_video_url": "a hosted video: YouTube, Vimeo, Loom…",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. Quiet Hours"}),
            "tagline": forms.TextInput(attrs={"placeholder": "one sentence on what it does, e.g. Mutes notifications while you code"}),
            "description": forms.Textarea(attrs={"rows": 12, "placeholder": "Markdown. A good outline:\n\n## What it does\n## How we built it\n## What we cut, and why\n## What is next"}),
            "repo_url": forms.URLInput(attrs={"placeholder": "e.g. https://github.com/your-team/your-project"}),
            "demo_video_url": forms.URLInput(attrs={"placeholder": "e.g. https://youtu.be/abc123 (YouTube, Vimeo or Loom)"}),
            "live_url": forms.URLInput(attrs={"placeholder": "optional: where a judge can try it, e.g. https://demo.example.org"}),
        }

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event
        self.fields["track"].queryset = event.visible_tracks()
        self.fields["track"].empty_label = "-- choose a track --"
        for name in ("repo_url", "demo_video_url", "live_url"):
            self.fields[name].validators.append(web_url)
            self.fields[name].assume_scheme = "https"
        self.questions = list(event.visible_questions())
        answers = {}
        if self.instance.pk:
            answers = {a.question_id: a.value for a in self.instance.answers.all()}
            self.fields["tags"].initial = ", ".join(self.instance.tags.values_list("name", flat=True))
        for q in self.questions:
            field = answer_field(q)
            stored = answers.get(q.pk, "")
            field.initial = bool(stored) if q.kind == QuestionKind.CHECKBOX else stored
            self.fields[f"q_{q.pk}"] = field
        if not (self.instance.pk and self.instance.thumbnail):
            del self.fields["remove_thumbnail"]

    def question_fields(self):
        return [self[f"q_{q.pk}"] for q in self.questions]

    def clean_tags(self):
        tags = normalize_tags(self.cleaned_data.get("tags"))
        if len(tags) > settings.MAX_PROJECT_TAGS:
            raise forms.ValidationError(f"At most {settings.MAX_PROJECT_TAGS} tags; you have {len(tags)}.")
        return tags

    def clean_thumbnail_upload(self):
        upload = self.cleaned_data.get("thumbnail_upload")
        return clean_image(upload) if upload else None

    def answers(self):
        """{question: text} for every visible question, from cleaned data."""
        return {q: answer_to_text(q, self.cleaned_data.get(f"q_{q.pk}")) for q in self.questions}


class ImageForm(forms.Form):
    image = forms.FileField(
        widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png,image/webp,image/gif"})
    )
    caption = forms.CharField(max_length=140, required=False, widget=forms.TextInput(attrs={"placeholder": "optional, e.g. The judge dashboard"}))

    def clean_image(self):
        return clean_image(self.cleaned_data["image"])


class StartProjectForm(forms.Form):
    name = forms.CharField(max_length=120, widget=forms.TextInput(attrs={"placeholder": "a working title, e.g. Quiet Hours (you can change it later)"}))
