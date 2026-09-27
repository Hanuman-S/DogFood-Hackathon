"""Assigning judges: /organizer/events/<slug>/assignments.

Gated like every organizer page (`portal_required`, then `get_managed_event`: 404 for anyone who
does not manage this event). Every write goes through `scoring.services`.
"""

from collections import defaultdict

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts.guards import portal_required
from accounts.roles import Role
from core.judging import judging_window
from events.models import EventMembership
from events.services import get_managed_event
from projects.models import Project
from scoring import services
from scoring.assignment import Board, capacity_warnings
from scoring.models import Assignment, AssignmentStatus, Score


class AssignmentRunForm(forms.Form):
    target = forms.IntegerField(
        min_value=1, max_value=20, label="reviews per project",
        widget=forms.NumberInput(attrs={"min": 1, "max": 20}),
    )
    max_load = forms.IntegerField(
        min_value=1, required=False, label="most projects per judge",
        widget=forms.NumberInput(attrs={"min": 1, "placeholder": "empty = no cap"}),
        help_text="empty: no cap; load is still balanced",
    )
    seed = forms.IntegerField(
        min_value=0, required=False, label="seed",
        widget=forms.NumberInput(attrs={"min": 0, "placeholder": "empty = a fresh random seed"}),
        help_text="only to reproduce an earlier round exactly",
    )


def review_state(score):
    if score is None:
        return "not started"
    return "submitted" if score.submitted_at else "draft"


def _target_locked(event):
    """Reviews per project is fixed once judging has started (after a first round exists)."""
    from core.deadlines import db_now

    return db_now() >= event.judging_starts_at and event.assignment_rounds.exists()


def _run_form(event, data=None):
    form = AssignmentRunForm(data, initial={"target": services.review_target(event)})
    if _target_locked(event):
        form.fields["target"].disabled = True
        form.fields["target"].help_text = "fixed once judging has started"
    else:
        form.fields["target"].help_text = "can change until judging starts; lowering it releases the extra reviews"
    return form


def _page(request, event, form=None, status=200, partial=False):
    target = services.review_target(event)
    board = Board(event)
    scores = {
        (s.judge_id, s.project_id): s
        for s in Score.objects.filter(project__event=event).only("judge_id", "project_id", "submitted_at")
    }
    assignments = defaultdict(list)
    for a in (Assignment.objects.filter(project__event=event, status=AssignmentStatus.ASSIGNED)
              .select_related("judge__user").order_by("judge__user__name")):
        a.review = review_state(scores.get((a.judge_id, a.project_id)))
        assignments[a.project_id].append(a)
    judges = {j.pk: j for j in board.judges}
    rows = []
    for project in board.projects:
        live = assignments[project.pk]
        # Judges who may still take this project by hand: they cover its track, are not on it
        # already, and did not decline it. One you withdrew from it is listed (marked), but an
        # automatic round will not give it back to them -- so "fillable" counts only the others.
        candidates = [
            {"judge": judges[j], "load": board.load.get(j, 0),
             "withdrawn": (j, project.pk) in board.withdrawn}
            for j in board.eligible(project, by_hand=True)
        ]
        candidates.sort(key=lambda c: (c["withdrawn"], c["load"], c["judge"].user.name.lower()))
        automatic = [c for c in candidates if not c["withdrawn"]]
        needed = max(0, target - len(live))
        submitted = sum(1 for a in live if a.review == "submitted")
        if needed == 0:
            state = "done" if submitted >= target else "assigned"
        else:
            state = "fillable" if automatic else "stuck"
        rows.append({
            "project": project,
            "assignments": live,
            "assigned": len(live),
            "submitted": submitted,
            "needed": needed,
            "candidates": candidates,
            "state": state,
        })
    order = {"stuck": 0, "fillable": 1, "assigned": 2, "done": 3}
    rows.sort(key=lambda r: (order[r["state"]], r["project"].track.name if r["project"].track else "",
                             r["project"].name.lower()))
    by_project = {r["project"].pk: r for r in rows}
    islands = board.islands(board.active)
    declined = []
    for a in (Assignment.objects.filter(project__event=event, status=AssignmentStatus.DECLINED)
              .select_related("judge__user", "project").order_by("-status_changed_at")):
        row = by_project.get(a.project_id)
        if row is None or row["needed"] == 0:
            next_step = "covered"
        elif row["state"] == "fillable":
            next_step = "assign"
        else:
            next_step = "no_judge"
        declined.append((a, next_step))
    template = "organizer/_assignment_board.html" if partial else "organizer/assignments.html"
    return render(request, template, {
        "event": event,
        "form": form or _run_form(event),
        "target": target,
        "rows": rows,
        "short_count": sum(1 for r in rows if r["needed"]),
        "stuck_count": sum(1 for r in rows if r["state"] == "stuck"),
        "reviews_in": sum(min(r["submitted"], target) for r in rows),
        "reviews_wanted": target * len(rows),
        "judges": board.judges,
        "warnings": capacity_warnings(board, target, None),
        "islands": len(islands),
        "rounds": event.assignment_rounds.select_related("created_by")[:10],
        "declined": declined,
        "open_declines": sum(1 for _, step in declined if step != "covered"),
        "window": judging_window(event),
    }, status=status)


@never_cache
@portal_required("organizer")
def assignments(request, slug):
    event = get_managed_event(request.user, slug)
    if request.method != "POST":
        return _page(request, event, partial=request.GET.get("partial") == "1")
    form = _run_form(event, request.POST)
    if not form.is_valid():
        return _page(request, event, form, status=400)
    reshuffle = request.POST.get("action") == "reshuffle"
    try:
        if reshuffle:
            released, round_ = services.reshuffle_unstarted(
                request, event, target=form.cleaned_data["target"],
                max_load=form.cleaned_data["max_load"], seed=form.cleaned_data["seed"],
            )
        else:
            round_ = services.run_assignment(
                request, event, target=form.cleaned_data["target"],
                max_load=form.cleaned_data["max_load"], seed=form.cleaned_data["seed"],
            )
    except services.AssignmentError as error:
        form.add_error(None, str(error))
        return _page(request, event, form, status=409)
    summary = round_.summary
    short = sum(summary.get("short", {}).values())
    if summary.get("released"):
        messages.info(request, f"reviews per project lowered to {round_.target_reviews}: "
                               f"{summary['released']} extra review{'s' if summary['released'] != 1 else ''} released.")
    if not reshuffle and summary["added"] == 0 and not summary.get("released"):
        if short:
            messages.warning(request, f"nothing could be added: {short} review{'s are' if short != 1 else ' is'} "
                                      "still missing and no other judge may take "
                                      f"{'them' if short != 1 else 'it'}. add or invite a judge, or add one by hand.")
        else:
            messages.info(request, "nothing to add: every project already has its reviewers. to draw the "
                                   "unstarted ones again at random, use reshuffle.")
        return redirect("organizer:assignments", slug=event.slug)
    messages.success(
        request,
        (f"reshuffled: {released} unstarted review{'s' if released != 1 else ''} released and drawn again. " if reshuffle else "")
        + f"round {round_.pk}: {summary['added']} review{'s' if summary['added'] != 1 else ''} assigned"
        + (f", {summary['bridges']} of them to link separate groups" if summary.get("bridges") else "")
        + (f"; {short} still missing (see warnings)" if short else "; every project reaches the target")
        + f". seed {round_.seed}.",
    )
    for warning in summary.get("warnings", []):
        messages.warning(request, warning)
    return redirect("organizer:assignments", slug=event.slug)


def _judge(event, pk):
    return get_object_or_404(EventMembership.objects.select_related("user"), pk=pk, event=event, role=Role.JUDGE)


def _back(event, anchor=""):
    return redirect(f"/organizer/events/{event.slug}/assignments{('#' + anchor) if anchor else ''}")


@require_POST
@portal_required("organizer")
def assignment_add(request, slug):
    event = get_managed_event(request.user, slug)
    project = get_object_or_404(Project, pk=request.POST.get("project") or 0, event=event)
    try:
        judge = _judge(event, request.POST.get("judge") or 0)
        services.add_assignment(request, event, judge, project)
        messages.success(request, f"{judge.user.name} now reviews {project.name}.")
    except services.AssignmentError as error:
        messages.error(request, str(error))
    return _back(event, f"project-{project.pk}")


@require_POST
@portal_required("organizer")
def assignment_withdraw(request, slug, assignment_id):
    event = get_managed_event(request.user, slug)
    assignment = get_object_or_404(
        Assignment.objects.select_related("judge__user", "project"), pk=assignment_id, project__event=event
    )
    try:
        services.withdraw_assignment(request, event, assignment)
        messages.success(request, f"{assignment.project.name} taken back from {assignment.judge.user.name}.")
    except services.AssignmentError as error:
        messages.error(request, str(error))
    return _back(event, f"project-{assignment.project_id}")


@require_POST
@portal_required("organizer")
def judge_reassign(request, slug, membership_id):
    """A stalled judge: give everything they have not started to other judges."""
    event = get_managed_event(request.user, slug)
    judge = _judge(event, membership_id)
    try:
        count, round_ = services.reassign_unstarted(request, event, judge)
    except services.AssignmentError as error:
        messages.error(request, str(error))
    else:
        messages.success(
            request,
            f"{count} unstarted review{'s' if count != 1 else ''} taken from {judge.user.name}; "
            f"round {round_.pk} assigned {round_.summary['added']} to other judges.",
        )
    # Back to whichever of this event's pages the button was on (assignments or progress);
    # never anywhere else.
    back = f"/organizer/events/{event.slug}/"
    target = request.POST.get("next") or ""
    return redirect(target if target.startswith(back) and "//" not in target[1:] else back + "assignments")
