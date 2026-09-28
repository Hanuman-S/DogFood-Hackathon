"""Posting and deleting comments from the project page. The rules are projects/comments.py's; these
views only take the actor and origin off the request and turn a refusal into a message.

No login decorator on purpose: an anonymous POST still reaches the service, so the attempt is audited
(in its own bucket) exactly as it is through the JSON API, and then goes to the login page."""

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from core import audit
from projects import comments
from projects.comment_errors import CommentError, LoginRequired


def _back(project_id):
    return redirect(reverse("public:project", args=[project_id]) + "#comments")


@require_POST
def comment_post(request, project_id):
    try:
        comments.post_comment(project_id, request.POST.get("body", ""), author=request.user,
                              origin=audit.origin_of(request))
    except LoginRequired:
        target = reverse("public:project", args=[project_id])
        return redirect(f"{reverse('accounts:login')}?{urlencode({'next': target})}")
    except CommentError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "comment posted.")
    return _back(project_id)


@require_POST
def comment_delete(request, comment_id):
    try:
        comment = comments.delete_own_comment(comment_id, actor=request.user, origin=audit.origin_of(request))
    except LoginRequired:
        return redirect(reverse("accounts:login"))
    except CommentError as error:
        messages.error(request, str(error))
        return redirect("public:gallery")
    messages.success(request, "comment deleted.")
    return _back(comment.project_id)
