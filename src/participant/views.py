"""The participant portal: join events, form teams, write and submit the project."""

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts.guards import portal_required
from core import deadlines
from events.models import Event, Phase
from events.services import get_visible_event
from projects import services as project_services
from projects.forms import ImageForm, ProjectForm, StartProjectForm
from projects.models import Project
from teams import services as team_services
from teams.models import Team, TeamMember


def _event_url(event):
    return reverse("participant:event", kwargs={"slug": event.slug})


@never_cache
@portal_required("participant")
def home(request):
    memberships = (
        TeamMember.objects.filter(user=request.user)
        .select_related("team__event", "team__captain")
        .order_by("-team__event__submissions_close_at")
    )
    mine = []
    for m in memberships:
        project = Project.objects.filter(team=m.team).first()
        mine.append({"event": m.team.event, "team": m.team, "project": project})
    joined = {row["event"].pk for row in mine}
    open_events = [
        e for e in Event.objects.published().order_by("submissions_close_at")
        if e.phase in (Phase.UPCOMING, Phase.OPEN) and e.pk not in joined
    ]
    from records.models import IssuedRecord
    my_records = IssuedRecord.objects.filter(subject_user=request.user).select_related("event").order_by("-issued_at")
    return render(request, "participant/home.html", {"mine": mine, "open_events": open_events,
                                                     "my_records": my_records})


@never_cache
@portal_required("participant")
def event(request, slug):
    event = get_visible_event(request.user, slug)
    team = team_services.team_of(request.user, event)
    project = Project.objects.filter(team=team).first() if team else None
    return render(
        request, "participant/event.html",
        {
            "event": event,
            "team": team,
            "members": team.members.select_related("user") if team else [],
            "project": project,
            "is_captain": bool(team and team.captain_id == request.user.pk),
            "invite_url": request.build_absolute_uri(reverse("join", kwargs={"token": team.invite_token})) if team else "",
            "start_form": StartProjectForm(),
            "window": deadlines.window(event, team),
        },
    )


# --- teams ----------------------------------------------------------------------------------


def _team_action(request, action, *args):
    try:
        return action(request, *args), None
    except team_services.TeamRuleError as error:
        messages.error(request, str(error))
        return None, error


@require_POST
@portal_required("participant")
def team_create(request, slug):
    event = get_visible_event(request.user, slug)
    team, error = _team_action(request, team_services.create_team, event, request.POST.get("name", ""))
    if team:
        messages.success(request, f'team "{team.name}" created. share the invite link below.')
    return redirect(_event_url(event))


def _my_team(request, team_id):
    team = get_object_or_404(Team.objects.select_related("event"), pk=team_id)
    if not team_services.is_member(request.user, team):
        raise Http404("No such team.")
    return team


@require_POST
@portal_required("participant")
def team_rename(request, team_id):
    team = _my_team(request, team_id)
    _, error = _team_action(request, team_services.rename_team, team, request.POST.get("name", ""))
    if not error:
        messages.success(request, "team renamed.")
    return redirect(_event_url(team.event))


@require_POST
@portal_required("participant")
def team_reset_link(request, team_id):
    team = _my_team(request, team_id)
    _, error = _team_action(request, team_services.reset_invite_link, team)
    if not error:
        messages.success(request, "new invite link made. the old one no longer works.")
    return redirect(_event_url(team.event))


@require_POST
@portal_required("participant")
def team_captain(request, team_id, member_id):
    team = _my_team(request, team_id)
    member = get_object_or_404(TeamMember, pk=member_id, team=team)
    _, error = _team_action(request, team_services.transfer_captain, team, member)
    if not error:
        messages.success(request, f"{member.user.name} is now the captain.")
    return redirect(_event_url(team.event))


@require_POST
@portal_required("participant")
def team_remove(request, team_id, member_id):
    team = _my_team(request, team_id)
    member = get_object_or_404(TeamMember, pk=member_id, team=team)
    _, error = _team_action(request, team_services.remove_member, team, member)
    if not error:
        messages.success(request, f"{member.user.name} was removed from the team.")
    return redirect(_event_url(team.event))


@require_POST
@portal_required("participant")
def team_leave(request, team_id):
    """Leave the team. When the caller is the last member, leaving deletes the team and its
    draft project, so that case first shows a page asking to confirm (no JS dialogs: the CSP
    forbids inline scripts, and a page works everywhere)."""
    team = _my_team(request, team_id)
    event = team.event
    project = getattr(team, "project", None)
    last_one = not team.members.exclude(user=request.user).exists()
    if (
        last_one and request.POST.get("confirm") != "yes"
        and not deadlines.window(event, team).is_closed
        and not (project is not None and project.is_submitted)
    ):
        return render(request, "participant/leave_confirm.html", {"team": team, "event": event, "project": project})
    remaining, error = _team_action(request, team_services.leave_team, team)
    if not error:
        if remaining is None:
            messages.success(request, "you left. your team and its draft project were deleted.")
        else:
            messages.success(request, "you left the team.")
    return redirect(_event_url(event))


def join(request, token):
    """The invite link. Open to everyone: a visitor sees which team and event it is for, and
    is asked to log in or sign up first."""
    team = Team.objects.select_related("event", "captain").filter(invite_token=token).first()
    if team is None or not team.event.is_published:
        return render(request, "participant/join.html", {"invalid": True}, status=404)
    problem = team_services.join_problem(request.user, team) if request.user.is_authenticated else ""
    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect(f"{reverse('accounts:login')}?next={request.path}")
        try:
            team_services.join_team(request, token)
        except team_services.TeamRuleError as error:
            messages.error(request, str(error))
            return redirect(request.path)
        messages.success(request, f'you joined "{team.name}".')
        return redirect(_event_url(team.event))
    return render(
        request, "participant/join.html",
        {"team": team, "event": team.event, "problem": problem, "size": team.size},
    )


# --- projects -------------------------------------------------------------------------------


@require_POST
@portal_required("participant")
def project_start(request, slug):
    event = get_visible_event(request.user, slug)
    try:
        project = project_services.start_project(request, event, request.POST.get("name", ""))
    except (project_services.ProjectRuleError, team_services.TeamRuleError) as error:
        messages.error(request, str(error))
        return redirect(_event_url(event))
    messages.success(request, "project started as a draft. fill it in, then submit.")
    return redirect("participant:project_edit", project_id=project.pk)


def _my_project(request, project_id):
    project = get_object_or_404(Project.objects.select_related("team", "event", "track"), pk=project_id)
    if not project_services.can_edit(request.user, project):
        raise Http404("No such project.")
    return project


@never_cache
@portal_required("participant")
def project_edit(request, project_id):
    project = _my_project(request, project_id)
    event = project.event
    if request.method == "POST":
        # Window first: a late save is refused as late (409), before the form is even read.
        deadlines.check_submission_window(request, event, project.team, action="edit the project", needs_open=True)
    form = ProjectForm(request.POST or None, request.FILES or None, instance=project, event=event)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                project_services.update_project(request, project, form)
                then = request.POST.get("then")
                # "save & submit" / "save & preview" live in this form, so what was typed is saved
                # first. (They used to be separate forms and silently dropped unsaved edits.)
                if then == "submit" and not project.is_submitted:
                    project.refresh_from_db()
                    try:
                        project_services.submit_project(request, project)
                        messages.success(request, "saved and submitted. you can keep editing until the deadline.")
                    except project_services.ProjectRuleError as error:
                        messages.success(request, "saved.")
                        messages.error(request, f"not submitted: {error}")
                elif then == "preview":
                    return redirect("participant:project_preview", project_id=project.pk)
                else:
                    messages.success(request, "saved.")
                return redirect("participant:project_edit", project_id=project.pk)
            except project_services.ProjectRuleError as error:
                for field, sentence in getattr(error, "missing", {}).items():
                    form.add_error(field if field in form.fields else None, sentence)
                form.add_error(None, str(error))
        status = 400
        project.refresh_from_db()
    missing = project_services.missing_for_submission(project)
    return render(
        request, "participant/project_edit.html",
        {
            "event": event, "project": project, "form": form, "missing": missing,
            "image_form": ImageForm(), "images": project.images.all(),
            "popular_tags": _popular_tags(),
            "max_images": settings.MAX_PROJECT_IMAGES,
            "window": deadlines.window(event, project.team),
        },
        status=status,
    )


def _popular_tags(limit=16):
    from django.db.models import Count

    from projects.models import Tag

    return list(
        Tag.objects.annotate(n=Count("projects")).filter(n__gt=0).order_by("-n", "name")
        .values_list("name", flat=True)[:limit]
    )


@require_POST
@portal_required("participant")
def project_submit(request, project_id):
    project = _my_project(request, project_id)
    try:
        project_services.submit_project(request, project)
        messages.success(request, "submitted. you can keep editing until the deadline.")
    except project_services.ProjectRuleError as error:
        messages.error(request, str(error))
    return redirect("participant:project_edit", project_id=project.pk)


@require_POST
@portal_required("participant")
def project_unsubmit(request, project_id):
    project = _my_project(request, project_id)
    try:
        project_services.unsubmit_project(request, project)
        messages.success(request, "withdrawn to draft. it is hidden from the gallery until you submit again.")
    except project_services.ProjectRuleError as error:
        messages.error(request, str(error))
    return redirect("participant:project_edit", project_id=project.pk)


@require_POST
@portal_required("participant")
def image_add(request, project_id):
    project = _my_project(request, project_id)
    deadlines.check_submission_window(request, project.event, project.team, action="add an image", needs_open=True)
    form = ImageForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect(reverse("participant:project_edit", kwargs={"project_id": project.pk}) + "#images")
    try:
        project_services.add_image(request, project, form.cleaned_data["image"], form.cleaned_data["caption"])
        messages.success(request, "image added.")
    except project_services.ProjectRuleError as error:
        messages.error(request, str(error))
    return redirect(reverse("participant:project_edit", kwargs={"project_id": project.pk}) + "#images")


@require_POST
@portal_required("participant")
def image_remove(request, project_id, image_id):
    project = _my_project(request, project_id)
    image = get_object_or_404(project.images, pk=image_id)
    try:
        project_services.remove_image(request, project, image)
        messages.success(request, "image removed.")
    except project_services.ProjectRuleError as error:
        messages.error(request, str(error))
    return redirect(reverse("participant:project_edit", kwargs={"project_id": project.pk}) + "#images")


@never_cache
@portal_required("participant")
def project_preview(request, project_id):
    project = _my_project(request, project_id)
    return render(request, "projects/project_detail.html", {"project": project, "preview": True})
