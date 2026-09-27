"""Every write to a project.

Order of checks in every write, and why:

    submission window  ->  permission  ->  validation  ->  write

The window check comes first so that, once deadlines are enforced, a late write is refused
*as late* -- never disguised as a permission or validation error.
"""

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.roles import can_compete_in
from core import audit
from core.deadlines import check_submission_window
from core.models import AuditAction
from events.services import can_manage
from projects.models import Answer, Project, ProjectImage, Status, Tag
from teams import services as team_services


class ProjectRuleError(Exception):
    """A refused project action, with a sentence the UI can show as-is."""


# --- who may do what ----------------------------------------------------------------------


def can_edit(user, project):
    """Any member of the project's team may edit it (S6)."""
    return team_services.is_member(user, project.team)


def can_view(user, project):
    """Submitted projects in a published event are public. Drafts are visible to the team and
    to the event's organizers."""
    if project.is_submitted and project.event.is_published:
        return True
    return can_edit(user, project) or can_manage(user, project.event)


# --- completeness ---------------------------------------------------------------------------


def missing_for_submission(project, answers=None):
    """What still has to be filled in before `project` can be submitted, as field -> sentence."""
    missing = {}
    if not project.name.strip():
        missing["name"] = "Give the project a name."
    if not project.tagline.strip():
        missing["tagline"] = "Add a one-line tagline."
    if not project.description.strip():
        missing["description"] = "Describe the project."
    if not project.repo_url:
        missing["repo_url"] = "Link the repository."
    if project.event.visible_tracks().exists() and project.track_id is None:
        missing["track"] = "Choose a track."
    size, minimum = project.team.members.count(), project.event.min_team_size
    if size < minimum:
        missing["team"] = (
            f"This event needs teams of at least {minimum}; yours has {size}. Share your invite link."
        )
    if answers is None:
        stored = {a.question_id: a.value for a in project.answers.all()}
        answers = {q: stored.get(q.pk, "") for q in project.event.visible_questions()}
    for question, value in answers.items():
        if question.required and not value:
            missing[f"q_{question.pk}"] = "This question is required."
    return missing


# --- writes -------------------------------------------------------------------------------


def start_project(request, event, name):
    """Start the team's project. A participant with no team gets a team of one (T4)."""
    user = request.user
    check_submission_window(
        request, event, team_services.team_of(user, event), action="start a project", needs_open=True
    )
    problem = can_compete_in(user, event)
    if problem:
        raise ProjectRuleError(problem)
    if not event.is_published:
        raise ProjectRuleError("This event is not open to participants yet.")
    name = (name or "").strip()[:120]
    if not name:
        raise ProjectRuleError("Give the project a name.")
    with transaction.atomic():
        team = team_services.team_of(user, event)
        if team is None:
            team = team_services.solo_team(request, event)
        elif Project.objects.filter(team=team).exists():
            raise ProjectRuleError("Your team already has a project in this event.")
        project = Project.objects.create(team=team, name=name, last_edited_by=user)
    audit.record(AuditAction.PROJECT_CREATED, request=request, subject=project.name, event=event.slug)
    return project


def update_project(request, project, form):
    """Save the edit form. Returns the project, or raises ProjectRuleError with `.missing`
    when the edit would leave a *submitted* project incomplete."""
    check_submission_window(request, project.event, project.team, action="edit the project", needs_open=True)
    if not can_edit(request.user, project):
        raise ProjectRuleError("Only members of this team can edit its project.")

    answers = form.answers()
    old_thumbnail = project.thumbnail.name
    candidate = form.save(commit=False)
    upload = form.cleaned_data.get("thumbnail_upload")
    if form.cleaned_data.get("remove_thumbnail"):
        candidate.thumbnail = None
    if project.is_submitted:
        missing = missing_for_submission(candidate, answers)
        if missing:
            error = ProjectRuleError(
                "A submitted project must stay complete. Fill these in, or withdraw it to draft."
            )
            error.missing = missing
            raise error

    with transaction.atomic():
        if upload:
            candidate.thumbnail.save(upload.name, upload, save=False)
        candidate.last_edited_by = request.user
        candidate.save()
        tags = [Tag.objects.get_or_create(name=name)[0] for name in form.cleaned_data["tags"]]
        candidate.tags.set(tags)
        for question, value in answers.items():
            Answer.objects.update_or_create(project=candidate, question=question, defaults={"value": value})
    if old_thumbnail and candidate.thumbnail.name != old_thumbnail:
        candidate.thumbnail.storage.delete(old_thumbnail)  # replaced or removed: no orphan file
    audit.record(
        AuditAction.PROJECT_UPDATED, request=request, subject=candidate.name,
        fields=[f for f in form.changed_data if not f.startswith("q_")] + (
            ["answers"] if any(f.startswith("q_") for f in form.changed_data) else []
        ),
    )
    return candidate


def submit_project(request, project):
    check_submission_window(request, project.event, project.team, action="submit the project", needs_open=True)
    if not can_edit(request.user, project):
        raise ProjectRuleError("Only members of this team can submit its project.")
    if project.is_submitted:
        return project
    missing = missing_for_submission(project)
    if missing:
        error = ProjectRuleError("Not ready to submit yet: " + " ".join(missing.values()))
        error.missing = missing
        raise error
    project.status = Status.SUBMITTED
    project.submitted_at = timezone.now()
    project.last_edited_by = request.user
    project.save(update_fields=["status", "submitted_at", "last_edited_by", "updated_at"])
    audit.record(AuditAction.PROJECT_SUBMITTED, request=request, subject=project.name)
    return project


def unsubmit_project(request, project):
    check_submission_window(request, project.event, project.team, action="withdraw the project", needs_open=True)
    if not can_edit(request.user, project):
        raise ProjectRuleError("Only members of this team can withdraw its project.")
    if not project.is_submitted:
        return project
    project.status = Status.DRAFT
    project.submitted_at = None
    project.last_edited_by = request.user
    project.save(update_fields=["status", "submitted_at", "last_edited_by", "updated_at"])
    audit.record(AuditAction.PROJECT_UNSUBMITTED, request=request, subject=project.name)
    return project


def add_image(request, project, image_file, caption=""):
    check_submission_window(request, project.event, project.team, action="add an image", needs_open=True)
    if not can_edit(request.user, project):
        raise ProjectRuleError("Only members of this team can add images.")
    with transaction.atomic():
        # Lock the project so two parallel uploads cannot both pass the count check.
        Project.objects.select_for_update().get(pk=project.pk)
        count = project.images.count()
        if count >= settings.MAX_PROJECT_IMAGES:
            raise ProjectRuleError(f"A project can have at most {settings.MAX_PROJECT_IMAGES} images.")
        image = ProjectImage(project=project, caption=caption.strip()[:140], order=count)
        image.image.save(image_file.name, image_file, save=False)
        image.save()
        Project.objects.filter(pk=project.pk).update(updated_at=timezone.now(), last_edited_by=request.user)
    audit.record(AuditAction.PROJECT_IMAGE_ADDED, request=request, subject=project.name)
    return image


def remove_image(request, project, image):
    check_submission_window(request, project.event, project.team, action="remove an image", needs_open=True)
    if not can_edit(request.user, project):
        raise ProjectRuleError("Only members of this team can remove images.")
    image.image.delete(save=False)
    image.delete()
    Project.objects.filter(pk=project.pk).update(updated_at=timezone.now(), last_edited_by=request.user)
    audit.record(AuditAction.PROJECT_IMAGE_REMOVED, request=request, subject=project.name)
