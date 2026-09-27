"""Serves uploaded project images, applying the project's own visibility rules.

Uploads are not published as static files: a draft's screenshots are only for its team and
the event's organizers, and a guessed URL must not change that.
"""

import mimetypes

from django.db.models import Q
from django.http import FileResponse, Http404

from projects.models import Project, ProjectImage
from projects.services import can_view


def serve(request, name):
    path = f"projects/{name}"
    image = None
    project = Project.objects.filter(thumbnail=path).select_related("team", "event").first()
    if project is None:
        image = ProjectImage.objects.filter(image=path).select_related("project__team", "project__event").first()
        project = image.project if image else None
    if project is None or not can_view(request.user, project):
        raise Http404("No such file.")
    field = project.thumbnail if project.thumbnail.name == path else image.image
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    response = FileResponse(field.open("rb"), content_type=content_type)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=3600" if not project.is_submitted else "public, max-age=3600"
    return response
