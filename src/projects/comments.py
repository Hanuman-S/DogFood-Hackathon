"""Comments on projects in the public gallery: every write, and the one read rule.

Which projects: exactly the ones `/projects` shows an anonymous visitor
(`projects.gallery.visible_projects()`, which takes no viewer). A draft, a project of an unpublished
event, or no project at all is the same 404, whoever asks -- a team member cannot comment on their
own draft.

Who: anyone reads; a logged-in account posts; the author deletes their own (soft); an organizer of
the event or a platform admin hides one (a reason is required) and restores it. Hidden and deleted
comments are shown to nobody but organizers and admins. There is no editing.

Order of checks on a post, like a vote write: no project 404, comments off 409, rate limit 429,
then the body 400, then (under a lock on the author) the duplicate 409. Every refusal is audited,
outside any transaction, before it is raised. Every row carries subject=event.slug, so an event's
comment actions sit on its audit trail.

Services never take the request: the view passes the actor and an `audit.Origin`.
"""

from django.conf import settings
from django.core.paginator import Paginator
from django.db import transaction

from accounts.models import User
from accounts.roles import is_organizer_of
from core import audit, ratelimit
from core.deadlines import db_now
from core.models import AuditAction

from .comment_errors import (
    CommentsDisabled, DuplicateComment, InvalidComment, InvalidModeration, LoginRequired, NoComment,
    NoEvent, NotCommentable, RateLimited,
)
from .gallery import visible_projects
from .models import COMMENT_MAX_LENGTH, Comment, Project

PAGE_SIZE = 20
HIDE_REASON_MAX = 300

# Every attempt at a post leaves one of these rows, so they are what the rate limit counts. The
# throttle's own rows are not counted: being refused does not extend the refusal.
COMMENT_WRITE_ACTIONS = (AuditAction.COMMENT_POSTED, AuditAction.COMMENT_REFUSED)


def _public_project(project_id):
    """The project if it is in the anonymous gallery, else None."""
    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        return None
    # The gallery's own query, minus the tag prefetch nothing here reads.
    return visible_projects().prefetch_related(None).filter(pk=project_id).first()


def _slug_of(project_id):
    """The event slug to file an audit row under, for any project id (even a draft's); "" if none."""
    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        return ""
    return Project.objects.filter(pk=project_id).values_list("event__slug", flat=True).first() or ""


def post_comment(project_id, body, *, author, origin=None) -> Comment:
    if author is None or not author.is_authenticated:
        audit.record(AuditAction.COMMENT_REFUSED, origin=origin, subject=_slug_of(project_id),
                     project=str(project_id), reason="not logged in")
        raise LoginRequired("Log in to comment.")

    def refuse(error, why, action=AuditAction.COMMENT_REFUSED, subject=None, **detail):
        audit.record(action, origin=origin, actor=author, subject=subject if subject is not None else _slug_of(project_id),
                     project=str(project_id), reason=why, **detail)
        raise error

    project = _public_project(project_id)
    if project is None:
        refuse(NotCommentable("No such project in the gallery."), "no project")
    event = project.event
    if not event.comments_enabled:
        refuse(CommentsDisabled("Comments are turned off for this event."), "comments disabled", subject=event.slug)

    now = db_now()
    which = ratelimit.exceeded(
        COMMENT_WRITE_ACTIONS, now=now,
        per_actor=ratelimit.Limit(settings.COMMENT_RATE_PER_USER, settings.COMMENT_RATE_WINDOW),
        actor_filter={"actor": author},
        per_ip=ratelimit.Limit(settings.COMMENT_RATE_PER_IP, settings.COMMENT_RATE_WINDOW),
        ip_hash=origin.ip_hash if origin is not None else "",
    )
    if which:
        refuse(RateLimited("Too many comments in a short time. Try again in a few minutes."), "rate limit",
               action=AuditAction.COMMENT_THROTTLED, subject=event.slug, limit=which)

    text = (body or "").strip() if isinstance(body, str) or body is None else None
    if not text:
        refuse(InvalidComment("Write something first."), "empty", subject=event.slug)
    if len(text) > COMMENT_MAX_LENGTH:
        refuse(InvalidComment(f"A comment is at most {COMMENT_MAX_LENGTH} characters."), "too long", subject=event.slug)

    duplicate = False
    with transaction.atomic():
        # One account's posts are serialised, so two identical posts at once cannot both pass the
        # duplicate check.
        User.objects.select_for_update().filter(pk=author.pk).first()
        duplicate = Comment.objects.filter(
            author=author, body=text, created_at__gte=now - settings.COMMENT_DUPLICATE_WINDOW,
        ).exists()
        if not duplicate:
            comment = Comment.objects.create(project=project, author=author, body=text, created_at=now)
    if duplicate:
        refuse(DuplicateComment("You posted this same comment a moment ago."), "duplicate", subject=event.slug)
    audit.record(AuditAction.COMMENT_POSTED, origin=origin, actor=author, subject=event.slug,
                 project=str(project.pk), comment=comment.pk)
    return comment


def _moderation_refusal(origin, actor, comment_id, subject, why):
    audit.record(AuditAction.COMMENT_MODERATION_REFUSED, origin=origin, actor=actor, subject=subject,
                 comment=str(comment_id), reason=why)


def _comment_id(comment_id):
    try:
        return int(comment_id)
    except (TypeError, ValueError):
        return None


def delete_own_comment(comment_id, *, actor, origin=None) -> Comment:
    """Soft delete by the author. Anyone else gets the same 404 as for a missing comment. Deleting
    an already deleted comment changes nothing (and is not an error)."""
    if actor is None or not actor.is_authenticated:
        raise LoginRequired("Log in first.")
    pk = _comment_id(comment_id)
    problem, already = None, False
    with transaction.atomic():
        comment = (Comment.objects.select_for_update().select_related("project__event")
                   .filter(pk=pk).first() if pk is not None else None)
        if comment is None or comment.author_id != actor.pk:
            problem = "not the author" if comment is not None else "no such comment"
        elif comment.deleted_at is not None:
            already = True
        else:
            comment.deleted_at = db_now()
            comment.save(update_fields=["deleted_at"])
    if problem:
        subject = comment.project.event.slug if comment is not None else ""
        _moderation_refusal(origin, actor, comment_id, subject, f"delete: {problem}")
        raise NoComment("No such comment.")
    if not already:
        audit.record(AuditAction.COMMENT_DELETED, origin=origin, actor=actor, subject=comment.project.event.slug,
                     project=str(comment.project_id), comment=comment.pk)
    return comment


def _moderate(comment_id, reason, *, actor, origin, hide):
    verb = "hide" if hide else "restore"
    if actor is None or not actor.is_authenticated:
        raise LoginRequired("Log in first.")
    pk = _comment_id(comment_id)
    # Existence and permission first, with the same 404 for both: another event's organizer learns
    # nothing about this event's comments.
    target = Comment.objects.select_related("project__event").filter(pk=pk).first() if pk is not None else None
    if target is None or not is_organizer_of(actor, target.project.event):
        subject = target.project.event.slug if target is not None else ""
        _moderation_refusal(origin, actor, comment_id, subject,
                            f"{verb}: " + ("not an organizer of the event" if target is not None else "no such comment"))
        raise NoComment("No such comment.")
    slug = target.project.event.slug
    reason = (reason or "").strip() if isinstance(reason, str) or reason is None else ""
    if not reason or len(reason) > HIDE_REASON_MAX:
        _moderation_refusal(origin, actor, comment_id, slug, f"{verb}: invalid reason")
        raise InvalidModeration(f"Give a reason (1 to {HIDE_REASON_MAX} characters); it is kept in the audit log.")
    problem, undid = None, None
    with transaction.atomic():
        comment = Comment.objects.select_for_update().get(pk=target.pk)
        if hide and comment.hidden_at is not None:
            problem = "already hidden"
        elif not hide and comment.hidden_at is None:
            problem = "not hidden"
        elif hide:
            comment.hidden_at, comment.hidden_by, comment.hide_reason = db_now(), actor, reason
            comment.save(update_fields=["hidden_at", "hidden_by", "hide_reason"])
        else:
            undid = {"hidden_at": comment.hidden_at.isoformat(), "hidden_by": comment.hidden_by.email,
                     "hide_reason": comment.hide_reason}
            comment.hidden_at, comment.hidden_by, comment.hide_reason = None, None, ""
            comment.save(update_fields=["hidden_at", "hidden_by", "hide_reason"])
    if problem:
        _moderation_refusal(origin, actor, comment_id, slug, f"{verb}: {problem}")
        raise InvalidModeration(f"This comment is {problem}.")
    detail = {"project": str(comment.project_id), "comment": comment.pk, "reason": reason}
    if undid:
        detail["undid"] = undid
    audit.record(AuditAction.COMMENT_HIDDEN if hide else AuditAction.COMMENT_RESTORED, origin=origin, actor=actor,
                 subject=slug, **detail)
    return comment


def hide_comment(comment_id, reason, *, actor, origin=None) -> Comment:
    return _moderate(comment_id, reason, actor=actor, origin=origin, hide=True)


def restore_comment(comment_id, reason, *, actor, origin=None) -> Comment:
    """Unhide. The hide's who, when and why stay in the audit log (and in this row's `undid`)."""
    return _moderate(comment_id, reason, actor=actor, origin=origin, hide=False)


def set_comments_enabled(event, enabled, *, actor, origin=None):
    if actor is None or not is_organizer_of(actor, event):
        audit.record(AuditAction.COMMENT_MODERATION_REFUSED, origin=origin, actor=actor, subject=event.slug,
                     reason="toggle: not an organizer of the event")
        raise NoEvent("No such event.")
    enabled = bool(enabled)
    if event.comments_enabled == enabled:
        return event
    type(event).objects.filter(pk=event.pk).update(comments_enabled=enabled)
    event.comments_enabled = enabled
    audit.record(AuditAction.COMMENTS_TOGGLED, origin=origin, actor=actor, subject=event.slug, enabled=enabled)
    return event


# --- reading ---------------------------------------------------------------------------------------

def can_moderate(viewer, event):
    return viewer is not None and viewer.is_authenticated and is_organizer_of(viewer, event)


def comments_for(project_id, viewer=None, page=1):
    """(project, page of comments, moderator?) -- newest first, PAGE_SIZE per page.

    Visitors, participants and judges: projects in the anonymous gallery only, and only comments
    that are neither hidden nor deleted. Organizers of the event and admins: any project of their
    event, every comment. A fixed number of queries whatever the page size (authors are joined).
    An invalid page number falls back rather than failing."""
    pk = _comment_id(project_id)
    project = None
    if pk is not None:
        project = Project.objects.select_related("event").filter(pk=pk).first()
    moderator = project is not None and can_moderate(viewer, project.event)
    if project is None or (not moderator and _public_project(pk) is None):
        raise NotCommentable("No such project in the gallery.")
    comments = Comment.objects.filter(project=project).select_related("author")
    if moderator:
        comments = comments.select_related("hidden_by")
    else:
        comments = comments.filter(hidden_at__isnull=True, deleted_at__isnull=True)
    return project, Paginator(comments.order_by("-created_at", "-id"), PAGE_SIZE).get_page(page), moderator


def moderation_list(event):
    """Every comment on the event's projects, newest first, for the organizer page."""
    return (Comment.objects.filter(project__event=event).select_related("author", "hidden_by", "project")
            .order_by("-created_at", "-id"))


def comment_actions():
    """The audit actions that describe comments (for the integrity trail)."""
    return (AuditAction.COMMENT_POSTED, AuditAction.COMMENT_REFUSED, AuditAction.COMMENT_THROTTLED,
            AuditAction.COMMENT_DELETED, AuditAction.COMMENT_HIDDEN, AuditAction.COMMENT_RESTORED,
            AuditAction.COMMENT_MODERATION_REFUSED, AuditAction.COMMENTS_TOGGLED)

