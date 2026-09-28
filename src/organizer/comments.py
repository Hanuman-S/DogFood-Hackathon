"""The organizer's comment moderation page: every comment on the event's projects (hidden and deleted
ones included), hide with a reason, restore, and the event's comments switch. Two gates on every view
(`portal_required("organizer")`, then `get_managed_event`); the rules are projects/comments.py's."""

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts.guards import portal_required
from core import audit
from events.services import get_managed_event
from projects import comments
from projects.comment_errors import CommentError

PAGE_SIZE = 50


@never_cache
@portal_required("organizer")
def comments_page(request, slug):
    event = get_managed_event(request.user, slug)
    page = Paginator(comments.moderation_list(event), PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "organizer/comments.html", {"event": event, "page": page})


def _moderate(request, slug, comment_id, action, done):
    event = get_managed_event(request.user, slug)
    try:
        action(comment_id, request.POST.get("reason", ""), actor=request.user, origin=audit.origin_of(request))
    except CommentError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"comment #{comment_id} {done}.")
    return redirect("organizer:comments", slug=event.slug)


@require_POST
@portal_required("organizer")
def comment_hide(request, slug, comment_id):
    return _moderate(request, slug, comment_id, comments.hide_comment, "hidden")


@require_POST
@portal_required("organizer")
def comment_restore(request, slug, comment_id):
    return _moderate(request, slug, comment_id, comments.restore_comment, "restored")


@require_POST
@portal_required("organizer")
def comments_toggle(request, slug):
    event = get_managed_event(request.user, slug)
    enabled = request.POST.get("enabled") == "1"
    try:
        comments.set_comments_enabled(event, enabled, actor=request.user, origin=audit.origin_of(request))
    except CommentError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "comments are on." if enabled else "comments are off. existing ones stay readable.")
    return redirect("organizer:comments", slug=event.slug)
