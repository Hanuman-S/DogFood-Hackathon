"""Event views: public detail pages, and the organizer's management screens.

Thin, as everywhere: resolve the object through a permission-scoped queryset, call a service,
render. The scoped lookups are what produce 404 rather than 403 for an event a caller may not know
about.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from core import permissions as perms
from core.errors import PortalError
from events import services
from events.forms import CustomQuestionForm, EventForm, MembershipForm, PrizeForm, TrackForm
from events.models import CustomQuestion, Event, EventMembership, Prize, Role, Track


def _visible_event(request, slug: str) -> Event:
    """Fetch an event the caller may see, or 404.

    Scoped through `visible_events`, so a non-public event a caller has no part in is
    indistinguishable from one that does not exist.
    """
    return get_object_or_404(perms.visible_events(request.user), slug=slug)


def _manageable_event(request, slug: str) -> Event:
    """Fetch an event the caller may administer, or 404.

    404 rather than 403 on purpose: replying "forbidden" to
    `/events/someone-elses-event/manage` confirms the event exists and that you found its
    management URL.
    """
    return get_object_or_404(perms.manageable_events(request.user), slug=slug)


# --------------------------------------------------------------------------------------
# public
# --------------------------------------------------------------------------------------


def event_list(request):
    events = perms.visible_events(request.user).order_by("-submissions_close_at")
    return render(
        request,
        "events/event_list.html",
        {"events": events, "perms_can_create": perms.can_create_event(request.user)},
    )


def event_detail(request, slug: str):
    event = _visible_event(request, slug)

    my_team = None
    if request.user.is_authenticated:
        from teams.models import TeamMember

        membership = (
            TeamMember.objects.filter(event=event, user=request.user)
            .select_related("team")
            .first()
        )
        my_team = membership.team if membership else None

    return render(
        request,
        "events/event_detail.html",
        {
            "event": event,
            "tracks": event.tracks.all(),
            "prizes": event.prizes.select_related("track"),
            "questions": event.questions.all(),
            "my_team": my_team,
            "my_roles": sorted(perms.roles_in(request.user, event)),
            # Drives what the page *offers*. Never the enforcement -- the service layer checks
            # again, and `tests/test_deadlines_http.py` proves a hidden button is not a guard.
            "can_manage": perms.can_manage_event(request.user, event),
            "can_register": perms.can_register_for_event(request.user, event),
            "submissions_open": event.submissions_open,
        },
    )


@login_required
@require_POST
def register(request, slug: str):
    event = _visible_event(request, slug)
    try:
        services.register_participant(user=request.user, event=event, request=request)
    except PortalError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, f"You are registered for {event.name}.")
    return redirect(f"/events/{event.slug}")


# --------------------------------------------------------------------------------------
# create / edit
# --------------------------------------------------------------------------------------


@login_required
@require_http_methods(["GET", "POST"])
def event_create(request):
    if not perms.can_create_event(request.user):
        # 403 rather than 404: the ability to create events is not a secret, and telling someone
        # they lack the grant is actionable -- they can ask an admin for it.
        return render(request, "403.html", status=403)

    form = EventForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            event = services.create_event(
                actor=request.user, request=request, **form.cleaned_data
            )
        except PortalError as error:
            messages.error(request, error.message)
        else:
            messages.success(request, f"Created {event.name}.")
            return redirect(f"/events/{event.slug}/manage")

    return render(request, "events/event_form.html", {"form": form, "event": None})


@login_required
@require_http_methods(["GET", "POST"])
def event_edit(request, slug: str):
    event = _manageable_event(request, slug)
    form = EventForm(request.POST or None, instance=event)

    if request.method == "POST" and form.is_valid():
        try:
            services.update_event(
                actor=request.user, event=event, request=request, **form.cleaned_data
            )
        except PortalError as error:
            messages.error(request, error.message)
        else:
            messages.success(request, "Event updated. Any date change has been recorded.")
            return redirect(f"/events/{event.slug}/manage")

    return render(request, "events/event_form.html", {"form": form, "event": event})


# --------------------------------------------------------------------------------------
# organizer dashboard
# --------------------------------------------------------------------------------------


@login_required
def dashboard(request, slug: str):
    event = _manageable_event(request, slug)

    from projects.models import Project
    from teams.models import Team

    projects = (
        Project.objects.filter(event=event)
        .select_related("team", "track", "duplicate_of")
        .order_by("status", "-submitted_at", "name")
    )

    return render(
        request,
        "events/dashboard.html",
        {
            "event": event,
            "counts": services.dashboard_counts(event),
            # Every project, drafts included. This is the one place drafts are listed for someone
            # who is not on the team.
            "projects": projects,
            "duplicates": projects.filter(duplicate_of__isnull=False),
            "teams": Team.objects.filter(event=event).prefetch_related("members__user"),
            "memberships": EventMembership.objects.filter(event=event)
            .exclude(role=Role.PARTICIPANT)
            .select_related("user")
            .prefetch_related("judge_tracks__track"),
            "tracks": event.tracks.all(),
            "prizes": event.prizes.select_related("track"),
            "questions": event.questions.all(),
            "membership_form": MembershipForm(event=event),
            "recent_audit": event.audit_entries.select_related("actor")[:25],
        },
    )


# --------------------------------------------------------------------------------------
# tracks, prizes, questions
# --------------------------------------------------------------------------------------


@login_required
@require_http_methods(["GET", "POST"])
def track_form(request, slug: str, track_id: int | None = None):
    event = _manageable_event(request, slug)
    track = get_object_or_404(Track, pk=track_id, event=event) if track_id else None

    form = TrackForm(request.POST or None, instance=track)
    if request.method == "POST" and form.is_valid():
        try:
            services.save_track(
                actor=request.user, event=event, track=track, request=request, **form.cleaned_data
            )
        except PortalError as error:
            messages.error(request, error.message)
        else:
            messages.success(request, "Track saved.")
            return redirect(f"/events/{event.slug}/manage")

    return render(
        request, "events/simple_form.html", {"form": form, "event": event, "title": "Track"}
    )


@login_required
@require_POST
def track_delete(request, slug: str, track_id: int):
    event = _manageable_event(request, slug)
    track = get_object_or_404(Track, pk=track_id, event=event)
    try:
        services.delete_track(actor=request.user, track=track, request=request)
    except PortalError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, "Track deleted.")
    return redirect(f"/events/{event.slug}/manage")


@login_required
@require_http_methods(["GET", "POST"])
def prize_form(request, slug: str, prize_id: int | None = None):
    event = _manageable_event(request, slug)
    prize = get_object_or_404(Prize, pk=prize_id, event=event) if prize_id else None

    form = PrizeForm(request.POST or None, instance=prize, event=event)
    if request.method == "POST" and form.is_valid():
        try:
            services.save_prize(
                actor=request.user, event=event, prize=prize, request=request, **form.cleaned_data
            )
        except PortalError as error:
            messages.error(request, error.message)
        else:
            messages.success(request, "Prize saved.")
            return redirect(f"/events/{event.slug}/manage")

    return render(
        request, "events/simple_form.html", {"form": form, "event": event, "title": "Prize"}
    )


@login_required
@require_POST
def prize_delete(request, slug: str, prize_id: int):
    event = _manageable_event(request, slug)
    prize = get_object_or_404(Prize, pk=prize_id, event=event)
    try:
        services.delete_prize(actor=request.user, prize=prize, request=request)
    except PortalError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, "Prize deleted.")
    return redirect(f"/events/{event.slug}/manage")


@login_required
@require_http_methods(["GET", "POST"])
def question_form(request, slug: str, question_id: int | None = None):
    event = _manageable_event(request, slug)
    question = (
        get_object_or_404(CustomQuestion, pk=question_id, event=event) if question_id else None
    )

    form = CustomQuestionForm(request.POST or None, instance=question)
    if request.method == "POST" and form.is_valid():
        data = {k: v for k, v in form.cleaned_data.items() if k != "choices_text"}
        try:
            services.save_question(
                actor=request.user, event=event, question=question, request=request, **data
            )
        except PortalError as error:
            messages.error(request, error.message)
        else:
            messages.success(request, "Question saved.")
            return redirect(f"/events/{event.slug}/manage")

    return render(
        request,
        "events/simple_form.html",
        {"form": form, "event": event, "title": "Custom question"},
    )


@login_required
@require_POST
def question_delete(request, slug: str, question_id: int):
    event = _manageable_event(request, slug)
    question = get_object_or_404(CustomQuestion, pk=question_id, event=event)
    try:
        services.delete_question(actor=request.user, question=question, request=request)
    except PortalError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, "Question deleted.")
    return redirect(f"/events/{event.slug}/manage")


# --------------------------------------------------------------------------------------
# memberships
# --------------------------------------------------------------------------------------


@login_required
@require_POST
def membership_add(request, slug: str):
    event = _manageable_event(request, slug)
    form = MembershipForm(request.POST, event=event)

    if not form.is_valid():
        messages.error(request, "Check the email address and role.")
        return redirect(f"/events/{event.slug}/manage")

    try:
        services.add_membership(
            actor=request.user,
            event=event,
            email=form.cleaned_data["email"],
            role=form.cleaned_data["role"],
            track_ids=[t.pk for t in form.cleaned_data["tracks"]],
            request=request,
        )
    except PortalError as error:
        messages.error(request, error.message)
    else:
        messages.success(
            request, f"Added {form.cleaned_data['email']} as {form.cleaned_data['role']}."
        )
    return redirect(f"/events/{event.slug}/manage")


@login_required
@require_POST
def membership_remove(request, slug: str, membership_id: int):
    event = _manageable_event(request, slug)
    membership = get_object_or_404(EventMembership, pk=membership_id, event=event)
    try:
        services.remove_membership(actor=request.user, membership=membership, request=request)
    except PortalError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, "Membership removed.")
    return redirect(f"/events/{event.slug}/manage")
