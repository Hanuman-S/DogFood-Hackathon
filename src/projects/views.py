"""Project views: draft, edit, submit, and the protected media endpoints.

Thin, like the rest of the portal's views: resolve, call a service function, render. Two patterns
recur and are worth stating once.

**404, never 403, for a project the caller may not see.** Every lookup goes through
`perms.visible_projects(request.user)`, so another team's draft is indistinguishable from a
project that never existed. A 403 would confirm it exists.

**A refusal is rendered with its own status code.** `PortalError.status_code` is passed straight to
`render()`, so a write refused by the deadline answers 409 in the browser exactly as it does in the
API. A participant told "closed" under a 200 has no way -- for a script, a screen reader, or a log
-- to tell that anything failed.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST, require_http_methods

from core import permissions as perms
from core.deadlines import submissions_are_open
from core.errors import PortalError
from events.models import Event
from projects import services
from projects.forms import ImageForm, ProjectForm
from projects.models import Project, ProjectImage
from teams.models import TeamMember


def _visible_project(request, project_id: int) -> Project:
    """A project the caller may see, or 404."""
    return get_object_or_404(
        perms.visible_projects(request.user).select_related("event", "team", "track"),
        pk=project_id,
    )


def _editable_project(request, project_id: int) -> Project:
    """A project the caller may see; editability is decided by the service layer.

    The view resolves and the service refuses, rather than the view deciding both -- the API needs
    the same refusal and must not depend on a view having made it.
    """
    return _visible_project(request, project_id)


# --------------------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------------------


@login_required
@require_http_methods(["GET", "POST"])
def project_create(request, slug: str):
    event = get_object_or_404(perms.visible_events(request.user), slug=slug)

    membership = (
        TeamMember.objects.filter(event=event, user=request.user)
        .select_related("team")
        .first()
    )
    if membership is None:
        messages.info(request, "Create or join a team first — projects belong to teams.")
        return redirect(reverse("team_create", args=[event.slug]))
    team = membership.team

    existing = team.projects.filter(duplicate_of__isnull=True).first()
    if existing:
        # One active project per team. Sending them to the editor is more useful than an error
        # page that tells them what they cannot do.
        return redirect(reverse("project_edit", args=[existing.pk]))

    form = ProjectForm(request.POST or None, event=event)
    status = 200

    if request.method == "POST" and form.is_valid():
        try:
            project = services.create_project(
                actor=request.user,
                event=event,
                team=team,
                request=request,
                tags=form.cleaned_data["tags"],
                answers=form.answers(),
                **form.content(),
            )
        except PortalError as error:
            form.add_error(None, error.message)
            status = error.status_code
        else:
            messages.success(
                request, f"Saved “{project.name}” as a draft. It is not submitted yet."
            )
            return redirect(reverse("project_edit", args=[project.pk]))

    return render(
        request,
        "projects/project_form.html",
        {
            "form": form,
            "event": event,
            "team": team,
            "project": None,
            "submissions_open": submissions_are_open(event),
        },
        status=status,
    )


# --------------------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------------------


def project_detail(request, project_id: int):
    """Public for a gallery-visible project; 404 for anything else the caller may not see."""
    project = _visible_project(request, project_id)
    is_member = perms.is_team_member(request.user, project.team)
    is_staff = perms.is_organizer(request.user, project.event) or perms.is_admin(request.user)

    answers = (
        project.answers.select_related("question")
        .filter(question__show_in_gallery=True)
        .order_by("question__order")
    )
    if is_member or is_staff:
        # The owning team and the organizers see every answer, including the ones the organizer
        # chose not to publish.
        answers = project.answers.select_related("question").order_by("question__order")

    return render(
        request,
        "projects/project_detail.html",
        {
            "project": project,
            "event": project.event,
            "team": project.team,
            "description_html": services.render_markdown(project.description),
            "tags": project.project_tags.select_related("tag").order_by("tag__name"),
            "images": project.images.order_by("order", "id"),
            "answers": answers,
            "is_member": is_member,
            "is_staff": is_staff,
            "can_edit": perms.can_edit_project(request.user, project),
            "submissions_open": submissions_are_open(project.event),
            "missing": services.missing_to_submit(project) if is_member else [],
        },
    )


# --------------------------------------------------------------------------------------
# edit and submit
# --------------------------------------------------------------------------------------


@login_required
@require_http_methods(["GET", "POST"])
def project_edit(request, project_id: int):
    project = _editable_project(request, project_id)
    event = project.event
    status = 200

    if request.method == "POST":
        form = ProjectForm(request.POST, event=event, project=project)
        if form.is_valid():
            try:
                services.update_project(
                    actor=request.user,
                    project=project,
                    request=request,
                    tags=form.cleaned_data["tags"],
                    answers=form.answers(),
                    **form.content(),
                )
            except PortalError as error:
                form.add_error(None, error.message)
                status = error.status_code
            else:
                messages.success(request, "Saved.")
                return redirect(reverse("project_edit", args=[project.pk]))
    else:
        form = ProjectForm(initial=ProjectForm.initial_for(project), event=event, project=project)

    project.refresh_from_db()
    return render(
        request,
        "projects/project_form.html",
        {
            "form": form,
            "event": event,
            "team": project.team,
            "project": project,
            "image_form": ImageForm(),
            "images": project.images.order_by("order", "id"),
            "missing": services.missing_to_submit(project),
            "can_edit": perms.can_edit_project(request.user, project),
            "max_images": settings.MAX_PROJECT_IMAGES,
            "submissions_open": submissions_are_open(event),
        },
        status=status,
    )


@login_required
@require_POST
def project_submit(request, project_id: int):
    project = _editable_project(request, project_id)
    try:
        services.submit_project(actor=request.user, project=project, request=request)
    except PortalError as error:
        messages.error(request, error.message)
    else:
        messages.success(
            request,
            "Submitted. You can keep editing until the deadline — the submission time stays "
            "as it is now.",
        )
    return redirect(reverse("project_edit", args=[project.pk]))


# --------------------------------------------------------------------------------------
# images
# --------------------------------------------------------------------------------------


@login_required
@require_POST
def image_add(request, project_id: int):
    project = _editable_project(request, project_id)
    form = ImageForm(request.POST, request.FILES)

    if not form.is_valid():
        messages.error(request, "Choose an image file to upload.")
        return redirect(reverse("project_edit", args=[project.pk]))

    try:
        services.add_image(
            actor=request.user,
            project=project,
            upload=form.cleaned_data["image"],
            alt_text=form.cleaned_data["alt_text"],
            request=request,
        )
    except PortalError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, "Image added.")
    return redirect(reverse("project_edit", args=[project.pk]))


@login_required
@require_POST
def image_remove(request, image_id: int):
    image = _visible_image(request, image_id)
    project = image.project
    try:
        services.remove_image(actor=request.user, image=image, request=request)
    except PortalError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, "Image removed.")
    return redirect(reverse("project_edit", args=[project.pk]))


@login_required
@require_POST
def thumbnail_set(request, project_id: int):
    project = _editable_project(request, project_id)
    upload = request.FILES.get("image")
    if upload is None:
        messages.error(request, "Choose an image file to upload.")
        return redirect(reverse("project_edit", args=[project.pk]))

    try:
        services.set_thumbnail(
            actor=request.user, project=project, upload=upload, request=request
        )
    except PortalError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, "Thumbnail updated.")
    return redirect(reverse("project_edit", args=[project.pk]))


# --------------------------------------------------------------------------------------
# protected media
# --------------------------------------------------------------------------------------
#
# Uploaded files are served only from here. `settings.MEDIA_URL` is None and no static handler is
# pointed at MEDIA_ROOT, so there is no other way to reach them: an image belonging to a draft is
# as private as the draft.


def _visible_image(request, image_id: int) -> ProjectImage:
    image = get_object_or_404(
        ProjectImage.objects.select_related("project", "project__event", "project__team"),
        pk=image_id,
    )
    if not perms.can_view_project(request.user, image.project):
        raise Http404
    return image


def image_serve(request, image_id: int):
    """Stream one gallery image, re-applying the owning project's visibility rules."""
    image = _visible_image(request, image_id)
    if not image.image:
        raise Http404
    return _media_response(image.image, image.content_type, image.project)


def thumbnail_serve(request, project_id: int):
    """Stream a project's thumbnail, under the same rules."""
    project = _visible_project(request, project_id)
    if not project.thumbnail:
        raise Http404
    return _media_response(project.thumbnail, project.thumbnail_content_type, project)


def _media_response(field_file, content_type: str, project: Project) -> FileResponse:
    """One place where uploaded bytes leave the portal, and the headers that go with them."""
    try:
        handle = field_file.open("rb")
    except FileNotFoundError as exc:
        # The row survived its file: a restored database against an empty media volume, or the
        # residual gap documented on `services._delete_file_on_commit`. Not a 500.
        raise Http404 from exc

    response = FileResponse(
        handle,
        # The format Pillow decoded at upload time, recorded on the row. Never guessed from the
        # filename, and never left for the browser to sniff.
        content_type=content_type or "application/octet-stream",
    )
    # Belt and braces around that: even with a correct Content-Type, a browser that sniffs could
    # decide a stored file is HTML and run it on this origin.
    response.headers["X-Content-Type-Options"] = "nosniff"

    if perms.is_publicly_visible(project):
        response.headers["Cache-Control"] = "private, max-age=3600"
    else:
        # A draft's image must not sit in any shared cache, where the next viewer would be handed
        # it without passing `can_view_project` at all.
        response.headers["Cache-Control"] = "private, no-store"
    return response
