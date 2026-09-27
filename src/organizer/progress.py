"""Judging progress: /organizer/events/<slug>/progress.

Who has not started, who is part-way, which projects are short of reviews, and how each track is
doing. The tables refresh themselves (app.js fetches `?partial=1` every 30 seconds); the page
works without JavaScript too. Organizers of the event only.
"""

from collections import defaultdict
from urllib.parse import quote

from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts.guards import portal_required
from accounts.roles import Role
from core import audit
from core.deadlines import db_now
from core.judging import judging_window
from core.models import AuditAction
from events.models import EventMembership
from events.services import get_managed_event
from projects.models import Project, Status
from scoring.models import Assignment, AssignmentStatus, Score
from scoring.services import review_target

JUDGE_ORDER = {"not started": 0, "in progress": 1, "no assignments": 2, "done": 3}


def progress_data(event):
    """Everything the dashboard shows, computed in a handful of queries."""
    target = review_target(event)
    judges = list(
        EventMembership.objects.filter(event=event, role=Role.JUDGE)
        .select_related("user").prefetch_related("judge_tracks__track")
    )
    assigned = defaultdict(set)  # judge -> projects
    for judge_id, project_id in Assignment.objects.filter(
        project__event=event, status=AssignmentStatus.ASSIGNED
    ).values_list("judge_id", "project_id"):
        assigned[judge_id].add(project_id)
    scores = list(Score.objects.filter(project__event=event).values_list(
        "judge_id", "project_id", "submitted_at", "updated_at"))
    submitted = defaultdict(set)
    drafts = defaultdict(set)
    last_active = {}
    for judge_id, project_id, submitted_at, updated_at in scores:
        (submitted if submitted_at else drafts)[judge_id].add(project_id)
        if updated_at and (judge_id not in last_active or updated_at > last_active[judge_id]):
            last_active[judge_id] = updated_at

    judge_rows = []
    for j in judges:
        mine = assigned[j.pk]
        done = len(mine & submitted[j.pk])
        started = len(mine & (submitted[j.pk] | drafts[j.pk]))
        if not mine:
            state = "no assignments"
        elif done == len(mine):
            state = "done"
        elif started == 0:
            state = "not started"
        else:
            state = "in progress"
        judge_rows.append({
            "judge": j, "assigned": len(mine), "submitted": done,
            "drafts": len(mine & drafts[j.pk]), "unstarted": len(mine) - started,
            "state": state, "last_active": last_active.get(j.pk),
            "tracks": [jt.track.name for jt in j.judge_tracks.all()],
        })
    judge_rows.sort(key=lambda r: (JUDGE_ORDER[r["state"]], -r["unstarted"], r["judge"].user.name.lower()))

    projects = list(Project.objects.filter(event=event, status=Status.SUBMITTED).select_related("track"))
    reviews_in = defaultdict(int)
    for judge_id, project_id, submitted_at, _ in scores:
        if submitted_at:
            reviews_in[project_id] += 1
    reviewers = defaultdict(int)
    for judge_id, projects_of in assigned.items():
        for p in projects_of:
            reviewers[p] += 1
    def project_state(done, assigned):
        if done >= target:
            return "done"
        if assigned >= target:
            return "waiting"  # enough judges assigned; their reviews are not all in yet
        return "short"  # needs more judges: assign on the assignment page

    project_rows = [
        {"project": p, "in": reviews_in[p.pk], "assigned": reviewers[p.pk],
         "short": max(0, target - reviewers[p.pk]), "state": project_state(reviews_in[p.pk], reviewers[p.pk])}
        for p in projects
    ]
    order = {"short": 0, "waiting": 1, "done": 2}
    project_rows.sort(key=lambda r: (order[r["state"]], r["in"], r["project"].name.lower()))

    tracks = defaultdict(lambda: {"projects": 0, "in": 0, "needed": 0, "judges": set()})
    judge_tracks = {j.pk: {jt.track_id for jt in j.judge_tracks.all()} for j in judges}
    for p in projects:
        t = tracks[p.track.name if p.track else "no track"]
        t["projects"] += 1
        t["in"] += min(reviews_in[p.pk], target)
        t["needed"] += target
        t["judges"] |= {j.pk for j in judges if not judge_tracks[j.pk] or p.track_id in judge_tracks[j.pk]}
    track_rows = [
        {"name": name, "projects": t["projects"], "in": t["in"], "needed": t["needed"],
         "judges": len(t["judges"]), "percent": round(100 * t["in"] / t["needed"]) if t["needed"] else 0}
        for name, t in sorted(tracks.items())
    ]

    total_needed = target * len(projects)
    total_in = sum(min(reviews_in[p.pk], target) for p in projects)
    return {
        "target": target,
        "judge_rows": judge_rows,
        "project_rows": project_rows,
        "track_rows": track_rows,
        "not_started": sum(1 for r in judge_rows if r["state"] == "not started"),
        "short_projects": sum(1 for r in project_rows if r["in"] < target),
        "total_in": total_in,
        "total_needed": total_needed,
        "percent": round(100 * total_in / total_needed) if total_needed else 0,
        "now": db_now(),
    }


@never_cache
@portal_required("organizer")
def progress(request, slug):
    event = get_managed_event(request.user, slug)
    context = {"event": event, "window": judging_window(event), **progress_data(event)}
    if request.GET.get("partial") == "1":
        return render(request, "organizer/_progress_tables.html", context)
    return render(request, "organizer/progress.html", context)


@never_cache
@require_POST
@portal_required("organizer")
def nudge(request, slug, membership_id):
    """Log a reminder, then show it ready to send from the organizer's own mail.

    The portal sends no email itself (it runs offline), so the reminder goes from the organizer's
    mailbox: the page gives a mail link and the text to copy. (A redirect straight to `mailto:`
    would be blocked by the `form-action 'self'` security policy.) The audit log keeps who was
    nudged, when, and by whom. The dashboard does not show it: the portal cannot know whether
    the mail was actually sent.
    """
    event = get_managed_event(request.user, slug)
    judge = get_object_or_404(
        EventMembership.objects.select_related("user"), pk=membership_id, event=event, role=Role.JUDGE
    )
    audit.record(
        AuditAction.JUDGE_NUDGED, request=request, subject=event.slug,
        judge_id=judge.pk, email=judge.user.email,
    )
    portal = request.build_absolute_uri("/judge/")
    subject = f"Reminder: your reviews for {event.name}"
    body = (
        f"Hi {judge.user.get_short_name()},\n\n"
        f"You have reviews waiting for {event.name}. Judging closes "
        f"{event.judging_ends_at:%Y-%m-%d %H:%M} UTC.\n\n"
        f"Your projects: {portal}\n\nThank you!\n{request.user.name}"
    )
    return render(request, "organizer/nudge.html", {
        "event": event, "judge": judge, "subject": subject, "body": body,
        "mailto": f"mailto:{quote(judge.user.email)}?subject={quote(subject)}&body={quote(body)}",
    })
