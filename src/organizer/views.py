"""The organizer portal: create events and configure them.

Every view is gated twice: `portal_required("organizer")` lets in anyone who organizes some
event (or may create one), then `get_managed_event` narrows that to the organizers *of this
event* and platform admins (404 for anyone else, so slugs cannot be probed).
"""

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts.guards import portal_required, refuse
from events import services
from core import deadlines
from imports.models import FixtureRef
from projects.gallery import possible_duplicates
from accounts.roles import Role
from events.forms import (
    AddJudgeForm, AddOrganizerForm, EventForm, ExtendDeadlineForm, ExtendJudgingForm, PrizeForm, QuestionForm, TeamExtensionForm, TrackForm,
)
from events.models import CustomQuestion, Event, JudgeInvite, Prize, Track
from projects.models import Project, Status
from core.judging import judging_window
from scoring.services import rubric_locked, with_shares
from teams.models import Team, TeamExtension
from organizer.export import sheet_states

PARTS = {
    "track": (Track, TrackForm, "tracks"),
    "prize": (Prize, PrizeForm, "prizes"),
    "question": (CustomQuestion, QuestionForm, "questions"),
}


@never_cache
@portal_required("organizer")
def home(request):
    events = (
        Event.objects.managed_by(request.user)
        .annotate(
            team_count=Count("teams", distinct=True),
            submitted_count=Count("projects", filter=Q(projects__status=Status.SUBMITTED), distinct=True),
            draft_count=Count("projects", filter=Q(projects__status=Status.DRAFT), distinct=True),
        )
    )
    return render(request, "organizer/home.html", {"events": events})


@never_cache
@portal_required("organizer")
def event_create(request):
    # Organizing one event (as a co-organizer) does not mean you may start new ones.
    if not (request.user.is_platform_admin or request.user.can_create_events):
        return refuse(request, "event_create", "this account may not create events; ask a platform admin.")
    form = EventForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        event = services.create_event(request, form)
        messages.success(request, f"event created. it is unpublished until you publish it.")
        return redirect("organizer:event", slug=event.slug)
    return render(
        request, "organizer/event_form.html", {"form": form},
        status=400 if request.method == "POST" else 200,
    )


def _control(request, event, status=200, **forms):
    context = {
        "event": event,
        "settings_form": forms.get("settings_form") or EventForm(instance=event),
        "track_form": forms.get("track_form") or TrackForm(event=event),
        "prize_form": forms.get("prize_form") or PrizeForm(event=event),
        "question_form": forms.get("question_form") or QuestionForm(event=event),
        "organizer_form": forms.get("organizer_form") or AddOrganizerForm(),
        "judge_form": forms.get("judge_form") or AddJudgeForm(event=event),
        "extend_form": forms.get("extend_form") or ExtendDeadlineForm(),
        "judging_form": forms.get("judging_form") or ExtendJudgingForm(),
        "judging_window": judging_window(event),
        "extension_form": forms.get("extension_form") or TeamExtensionForm(event=event),
        "extensions": TeamExtension.objects.filter(team__event=event).select_related("team", "granted_by"),
        "window": deadlines.window(event),
        "export_sheets": sheet_states(event, deadlines.db_now()),
        "duplicates": possible_duplicates(event),
        "import_duplicates": FixtureRef.objects.filter(
            kind=FixtureRef.Kind.PROJECT, object_id__in=event.projects.values("pk")
        ).exclude(duplicate_of=""),
        "tracks": event.tracks.annotate(n=Count("projects")),
        "prizes": event.prizes.select_related("track"),
        "questions": event.questions.annotate(n=Count("answers")),
        "organizer_links": event.memberships.filter(role=Role.ORGANIZER).select_related("user"),
        "criteria": with_shares(event.criteria.all()),
        "rubric_locked": rubric_locked(event),
        "publish_blockers": services.publish_blockers(event),
        "judge_invites": [
            (invite, services.judge_invite_state(invite))
            for invite in event.judge_invites.filter(role=Role.JUDGE).select_related("created_by", "accepted_by")
            .prefetch_related("tracks")[:20]
        ],
        "organizer_invites": [
            (invite, services.judge_invite_state(invite))
            for invite in event.judge_invites.filter(role=Role.ORGANIZER)
            .select_related("created_by", "accepted_by")[:20]
        ],
        "judges": event.memberships.filter(role=Role.JUDGE).select_related("user")
        .prefetch_related("judge_tracks__track"),
        "teams": Team.objects.filter(event=event).annotate(n=Count("members")).select_related("project"),
        "projects": Project.objects.filter(event=event).select_related("team", "track"),
    }
    return render(request, "organizer/event_control.html", context, status=status)


@never_cache
@portal_required("organizer")
def event_control(request, slug):
    event = services.get_managed_event(request.user, slug)
    if request.method == "POST":
        form = EventForm(request.POST, instance=event)
        if not form.is_valid():
            event.refresh_from_db()
            return _control(request, event, status=400, settings_form=form)
        event = services.update_event(request, event, form)
        messages.success(request, "event settings saved.")
        return redirect("organizer:event", slug=event.slug)
    return _control(request, event)


@require_POST
@portal_required("organizer")
def event_publish(request, slug):
    event = services.get_managed_event(request.user, slug)
    publish = request.POST.get("publish") == "1"
    try:
        services.set_published(request, event, publish)
    except services.EventRuleError as error:
        messages.error(request, str(error))
        return redirect("organizer:event", slug=event.slug)
    messages.success(request, "event published: it is now public." if publish else "event unpublished: only organizers can see it.")
    return redirect("organizer:event", slug=event.slug)


# --- tracks, prizes, questions ----------------------------------------------------------


def _part_form(kind, event, *args, **kwargs):
    _, form_class, _ = PARTS[kind]
    if kind == "question":
        instance = kwargs.get("instance")
        kwargs["kind_locked"] = bool(instance and services.question_kind_locked(instance))
    return form_class(*args, event=event, **kwargs)


@require_POST
@portal_required("organizer")
def part_add(request, slug, kind):
    event = services.get_managed_event(request.user, slug)
    form = _part_form(kind, event, request.POST)
    if not form.is_valid():
        return _control(request, event, status=400, **{f"{kind}_form": form})
    services.save_part(request, event, form, kind)
    messages.success(request, f"{kind} added.")
    return redirect(f"/organizer/events/{event.slug}/#{PARTS[kind][2]}")


@never_cache
@portal_required("organizer")
def part_edit(request, slug, kind, part_id):
    event = services.get_managed_event(request.user, slug)
    model, _, anchor = PARTS[kind]
    part = get_object_or_404(model, pk=part_id, event=event)
    form = _part_form(kind, event, request.POST or None, instance=part)
    if request.method == "POST" and form.is_valid():
        services.save_part(request, event, form, kind)
        messages.success(request, f"{kind} saved.")
        return redirect(f"/organizer/events/{event.slug}/#{anchor}")
    return render(
        request, "organizer/part_form.html",
        {"event": event, "form": form, "kind": kind, "part": part},
        status=400 if request.method == "POST" else 200,
    )


@require_POST
@portal_required("organizer")
def part_visibility(request, slug, kind, part_id):
    event = services.get_managed_event(request.user, slug)
    model, _, anchor = PARTS[kind]
    if kind == "prize":
        return redirect(f"/organizer/events/{event.slug}/#{anchor}")
    part = get_object_or_404(model, pk=part_id, event=event)
    services.set_part_hidden(request, event, part, kind, request.POST.get("hidden") == "1")
    return redirect(f"/organizer/events/{event.slug}/#{anchor}")


@require_POST
@portal_required("organizer")
def part_delete(request, slug, kind, part_id):
    event = services.get_managed_event(request.user, slug)
    model, _, anchor = PARTS[kind]
    part = get_object_or_404(model, pk=part_id, event=event)
    try:
        services.delete_part(request, event, part, kind)
        messages.success(request, f"{kind} deleted.")
    except services.EventRuleError as error:
        messages.error(request, str(error))
    return redirect(f"/organizer/events/{event.slug}/#{anchor}")


# --- judge invite links -----------------------------------------------------------------


@never_cache
@require_POST
@portal_required("organizer")
def judge_invite_create(request, slug):
    """Create a one-time judge link and show it -- once. Rendered, not redirected, because the
    raw token is not stored anywhere and so could not be shown after a redirect."""
    event = services.get_managed_event(request.user, slug)
    form = AddJudgeForm(request.POST, event=event)
    if not form.is_valid():
        return _control(request, event, status=400, judge_form=form)
    try:
        invite, raw = services.create_judge_invite(
            request, event, form.cleaned_data["email"], form.cleaned_data["tracks"]
        )
    except services.EventRuleError as error:
        form.add_error("email", str(error))
        return _control(request, event, status=400, judge_form=form)
    link = request.build_absolute_uri(reverse("judge_invite", args=[raw]))
    return render(request, "organizer/judge_invite_created.html", {
        "event": event, "invite": invite, "link": link,
    })


@require_POST
@portal_required("organizer")
def judge_invite_revoke(request, slug, invite_id):
    event = services.get_managed_event(request.user, slug)
    invite = get_object_or_404(JudgeInvite, pk=invite_id, event=event)
    try:
        services.revoke_judge_invite(request, event, invite)
        messages.success(request, f"invite for {invite.email or 'an open link'} revoked: the link no longer works.")
    except services.EventRuleError as error:
        messages.error(request, str(error))
    return redirect(f"/organizer/events/{event.slug}/#{'judges' if invite.role == Role.JUDGE else 'organizers'}")


# --- co-organizers ----------------------------------------------------------------------


@require_POST
@portal_required("organizer")
def organizer_add(request, slug):
    event = services.get_managed_event(request.user, slug)
    form = AddOrganizerForm(request.POST)
    if form.is_valid() and not form.cleaned_data["email"]:
        form.add_error("email", "add needs the email of an existing account. to invite someone "
                                "without it, use invite by link.")
    if not form.is_valid():
        return _control(request, event, status=400, organizer_form=form)
    try:
        services.add_organizer(request, event, form.cleaned_data["email"])
        messages.success(request, "co-organizer added.")
    except services.EventRuleError as error:
        form.add_error("email", str(error))
        return _control(request, event, status=400, organizer_form=form)
    return redirect(f"/organizer/events/{event.slug}/#organizers")


@require_POST
@portal_required("organizer")
def organizer_invite_create(request, slug):
    """A one-time co-organizer link, shown once (rendered, not redirected: see judge_invite_create)."""
    event = services.get_managed_event(request.user, slug)
    form = AddOrganizerForm(request.POST)
    if not form.is_valid():
        return _control(request, event, status=400, organizer_form=form)
    try:
        invite, raw = services.create_organizer_invite(request, event, form.cleaned_data["email"])
    except services.EventRuleError as error:
        form.add_error("email", str(error))
        return _control(request, event, status=400, organizer_form=form)
    link = request.build_absolute_uri(reverse("organizer_invite", args=[raw]))
    return render(request, "organizer/judge_invite_created.html", {
        "event": event, "invite": invite, "link": link,
    })


@require_POST
@portal_required("organizer")
def organizer_remove(request, slug, link_id):
    event = services.get_managed_event(request.user, slug)
    link = get_object_or_404(event.memberships, pk=link_id, role=Role.ORGANIZER)
    try:
        services.remove_organizer(request, event, link)
        messages.success(request, "co-organizer removed.")
    except services.EventRuleError as error:
        messages.error(request, str(error))
    if link.user_id == request.user.pk and not services.can_manage(request.user, event):
        return redirect("organizer:home")
    return redirect(f"/organizer/events/{event.slug}/#organizers")


# --- judges -----------------------------------------------------------------------------------


@require_POST
@portal_required("organizer")
def judge_add(request, slug):
    event = services.get_managed_event(request.user, slug)
    form = AddJudgeForm(request.POST, event=event)
    if form.is_valid() and not form.cleaned_data["email"]:
        form.add_error("email", "add judge needs the email of an existing account. to invite someone "
                                "without it, use invite by link.")
    if not form.is_valid():
        return _control(request, event, status=400, judge_form=form)
    try:
        services.add_judge(request, event, form.cleaned_data["email"], form.cleaned_data["tracks"])
        messages.success(request, "judge added.")
    except services.EventRuleError as error:
        form.add_error("email", str(error))
        return _control(request, event, status=400, judge_form=form)
    return redirect(f"/organizer/events/{event.slug}/#judges")


@require_POST
@portal_required("organizer")
def judge_remove(request, slug, membership_id):
    event = services.get_managed_event(request.user, slug)
    membership = get_object_or_404(event.memberships, pk=membership_id, role=Role.JUDGE)
    services.remove_judge(request, event, membership)
    messages.success(request, "judge removed.")
    return redirect(f"/organizer/events/{event.slug}/#judges")


# --- deadline ---------------------------------------------------------------------------------


@require_POST
@portal_required("organizer")
def deadline_extend(request, slug):
    event = services.get_managed_event(request.user, slug)
    form = ExtendDeadlineForm(request.POST)
    if form.is_valid():
        try:
            services.extend_deadline(request, event, form.cleaned_data["new_close"], form.cleaned_data["reason"])
            messages.success(request, "deadline extended for every team.")
            return redirect(f"/organizer/events/{event.slug}/#deadline")
        except services.EventRuleError as error:
            form.add_error("new_close", str(error))
    return _control(request, event, status=400, extend_form=form)


@require_POST
@portal_required("organizer")
def judging_extend(request, slug):
    event = services.get_managed_event(request.user, slug)
    form = ExtendJudgingForm(request.POST)
    if form.is_valid():
        try:
            services.extend_judging(request, event, form.cleaned_data["new_end"], form.cleaned_data["reason"])
        except services.EventRuleError as error:
            form.add_error("new_end", str(error))
        else:
            messages.success(request, "judging extended.")
            return redirect(f"/organizer/events/{event.slug}/#judging")
    event.refresh_from_db()
    return _control(request, event, status=400, judging_form=form)


@require_POST
@portal_required("organizer")
def extension_grant(request, slug):
    event = services.get_managed_event(request.user, slug)
    form = TeamExtensionForm(request.POST, event=event)
    if form.is_valid():
        try:
            services.grant_extension(
                request, event, form.cleaned_data["team"], form.cleaned_data["until"], form.cleaned_data["reason"]
            )
            messages.success(request, f"extension granted to {form.cleaned_data['team'].name}.")
            return redirect(f"/organizer/events/{event.slug}/#deadline")
        except services.EventRuleError as error:
            form.add_error("until", str(error))
    return _control(request, event, status=400, extension_form=form)


@require_POST
@portal_required("organizer")
def extension_revoke(request, slug, extension_id):
    event = services.get_managed_event(request.user, slug)
    extension = get_object_or_404(TeamExtension, pk=extension_id, team__event=event)
    services.revoke_extension(request, event, extension)
    messages.success(request, "extension revoked.")
    return redirect(f"/organizer/events/{event.slug}/#deadline")
