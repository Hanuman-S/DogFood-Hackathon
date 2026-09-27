"""Forms for the project editor.

Forms here do **field shape** only -- lengths, required-ness for a draft, which tracks are
offered. Every rule that matters (the deadline, who may edit, whether a project is complete
enough to submit, whether an uploaded file is really an image) lives in `projects.services` and is
re-checked there, because the API posts the same data without ever constructing a form.

The custom questions are added as fields at runtime: an organizer can add a question mid-event,
and a hard-coded form would not know about it.
"""

from __future__ import annotations

from django import forms

from events.models import CustomQuestion, QuestionKind, Track
from projects.models import TAGLINE_MAX_LENGTH, Project
from projects.services import MAX_PROJECT_TAGS

ANSWER_PREFIX = "question_"


class ProjectForm(forms.Form):
    """Draft and edit. Only `name` is required, because a draft needs only a name.

    Everything else is required to *submit*, which `services.submit_project` enforces against
    `missing_to_submit` -- not here. Making them required here would stop a team from saving a
    half-finished draft at 3 a.m., which is the opposite of what a draft is for.
    """

    name = forms.CharField(label="Project name", max_length=200)
    tagline = forms.CharField(
        label="Tagline",
        max_length=TAGLINE_MAX_LENGTH,
        required=False,
        help_text=f"One line, at most {TAGLINE_MAX_LENGTH} characters.",
    )
    description = forms.CharField(
        label="Description",
        required=False,
        widget=forms.Textarea(attrs={"rows": 12}),
        help_text="Markdown. Links are kept; images are not — upload screenshots below.",
    )
    track = forms.ModelChoiceField(
        label="Track", queryset=Track.objects.none(), required=False, empty_label="— none —"
    )
    repo_url = forms.CharField(label="Repository URL", required=False)
    demo_video_url = forms.CharField(
        label="Demo video URL",
        required=False,
        help_text="Shown as a link. Videos are not embedded, so the page needs no external host.",
    )
    live_url = forms.CharField(label="Live demo URL", required=False)
    tags = forms.CharField(
        label="Tags",
        required=False,
        help_text=f"Comma separated, at most {MAX_PROJECT_TAGS}. Lowercased automatically.",
    )

    def __init__(self, *args, event=None, project: Project | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event
        self.project = project

        self.fields["track"].queryset = Track.objects.filter(event=event).order_by("name")

        self.questions = list(
            CustomQuestion.objects.filter(event=event).order_by("order", "id")
        )
        answers = (
            {answer.question_id: answer.value for answer in project.answers.all()}
            if project is not None
            else {}
        )
        for question in self.questions:
            self.fields[f"{ANSWER_PREFIX}{question.pk}"] = _answer_field(
                question, answers.get(question.pk, "")
            )

    def answers(self) -> dict[int, str]:
        """`{question_id: value}` for `services.save_answers`."""
        return {
            question.pk: self.cleaned_data.get(f"{ANSWER_PREFIX}{question.pk}", "")
            for question in self.questions
        }

    def content(self) -> dict:
        """The project's own fields, ready to hand to a service function."""
        return {
            "name": self.cleaned_data["name"],
            "tagline": self.cleaned_data["tagline"],
            "description": self.cleaned_data["description"],
            "track": self.cleaned_data["track"],
            "repo_url": self.cleaned_data["repo_url"],
            "demo_video_url": self.cleaned_data["demo_video_url"],
            "live_url": self.cleaned_data["live_url"],
        }

    @classmethod
    def initial_for(cls, project: Project) -> dict:
        return {
            "name": project.name,
            "tagline": project.tagline,
            "description": project.description,
            "track": project.track_id,
            "repo_url": project.repo_url,
            "demo_video_url": project.demo_video_url,
            "live_url": project.live_url,
            "tags": ", ".join(
                project.project_tags.values_list("tag__name", flat=True)
            ),
        }


def _answer_field(question: CustomQuestion, initial: str) -> forms.Field:
    """One form field per custom question. Never required here -- see the class docstring.

    A question marked `required` is required to *submit*; leaving it blank in a draft is fine, so
    the requirement is checked by `services.missing_to_submit` rather than by the form. The label
    still says so, so nobody is surprised at submission time.
    """
    label = question.prompt + (" (required to submit)" if question.required else "")

    if question.kind == QuestionKind.BOOLEAN:
        return forms.BooleanField(
            label=label, required=False, initial=(initial or "").lower() == "true"
        )
    if question.kind == QuestionKind.CHOICE:
        return forms.ChoiceField(
            label=label,
            required=False,
            initial=initial,
            choices=[("", "— no answer —")] + [(str(c), str(c)) for c in question.choices or []],
        )
    if question.kind == QuestionKind.LONG_TEXT:
        return forms.CharField(
            label=label, required=False, initial=initial, widget=forms.Textarea(attrs={"rows": 4})
        )
    if question.kind == QuestionKind.URL:
        return forms.CharField(label=label, required=False, initial=initial)
    return forms.CharField(label=label, required=False, initial=initial, max_length=300)


class ImageForm(forms.Form):
    """Just the file and its alt text. The file itself is verified in `services.verify_image`.

    Deliberately a plain `FileField` rather than Django's `ImageField`: `ImageField` calls
    `Image.open()` to decide validity and would report "upload a valid image" for a
    decompression bomb, a 20 MB file and an unsupported format alike. The service layer draws
    those distinctions and produces a message that says which one it was.
    """

    image = forms.FileField(label="Image")
    alt_text = forms.CharField(
        label="Alt text",
        max_length=200,
        required=False,
        help_text="What the image shows, for screen readers. Leave blank if it is decorative.",
    )
