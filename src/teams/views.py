"""Team views, including the `/invite/<token>` landing page."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from accounts.models import User
from core import permissions as perms
from core.errors import PortalError
from teams import services
from teams.models import DEFAULT_INVITE_DAYS, Team, TeamInvite


class TeamForm(forms.Form):
    name = forms.CharField(label="Team name", max_length=200)

    def clean_name(self):
        return self.cleaned_data["name"].strip()


class InviteForm(forms.Form):
    expires_in_days = forms.IntegerField(
        label="Expires in (days)", min_value=1, max_value=services.MAX_INVITE_DAYS,
        initial=DEFAULT_INVITE_DAYS,
    )
    max_uses = forms.IntegerField(
        label="Maximum uses", min_value=1, required=False,
        help_text="Leave blank for unlimited use until it expires.",
    )


def _visible_team(request, team_id: int) -> Team:
    """A team the caller may see, or 404. Members, the event's organizers, and admins."""
    return get_object_or_404(perms.visible_teams(request.user), pk=team_id)


# --------------------------------------------------------------------------------------
# create / view
# --------------------------------------------------------------------------------------


@login_required
@require_http_methods(["GET", "POST"])
def team_create(request, slug: str):
    event = get_object_or_404(perms.visible_events(request.user), slug=slug)
    form = TeamForm(request.POST or None)
    status = 200

    if request.method == "POST" and form.is_valid():
        try:
            team = services.create_team(
                actor=request.user, event=event, name=form.cleaned_data["name"], request=request
            )
        except PortalError as error:
            # The deadline refusal carries 409, and the response carries it too: a participant who
            # is told "closed" with a 200 has no way for a script or a screen reader to tell that
            # anything failed.
            form.add_error(None, error.message)
            status = error.status_code
        else:
            messages.success(request, f"Created “{team.name}”. You are its captain.")
            return redirect(reverse("team_detail", args=[team.pk]))

    return render(
        request,
        "teams/team_form.html",
        {"form": form, "event": event, "submissions_open": event.submissions_open},
        status=status,
    )


@login_required
def team_detail(request, team_id: int):
    team = _visible_team(request, team_id)
    invites = (
        team.invites.select_related("created_by").order_by("revoked_at", "-created_at")
        if perms.can_manage_team(request.user, team)
        else team.invites.none()
    )

    return render(
        request,
        "teams/team_detail.html",
        {
            "team": team,
            "event": team.event,
            "members": team.members.select_related("user").order_by("-is_captain", "user__email"),
            "project": team.projects.first(),
            "invites": invites,
            "invite_form": InviteForm(),
            "is_captain": perms.can_manage_team(request.user, team),
            "is_member": perms.is_team_member(request.user, team),
            "submissions_open": team.event.submissions_open,
        },
    )


# --------------------------------------------------------------------------------------
# invites
# --------------------------------------------------------------------------------------


@login_required
@require_POST
def invite_create(request, team_id: int):
    team = _visible_team(request, team_id)
    form = InviteForm(request.POST)

    if not form.is_valid():
        messages.error(request, "Check the expiry and usage limit.")
        return redirect(reverse("team_detail", args=[team.pk]))

    try:
        invite, plaintext = services.create_invite(
            actor=request.user,
            team=team,
            expires_in_days=form.cleaned_data["expires_in_days"],
            max_uses=form.cleaned_data.get("max_uses"),
            request=request,
        )
    except PortalError as error:
        messages.error(request, error.message)
        return redirect(reverse("team_detail", args=[team.pk]))

    # The link is shown once, on the next page load, via a one-shot message. Only the digest is
    # stored, so it cannot be redisplayed later -- which is why the page tells the captain to copy
    # it now.
    url = request.build_absolute_uri(reverse("invite_accept", args=[plaintext]))
    messages.success(request, f"Invite link created. Copy it now — it is not shown again:\n{url}")
    return redirect(reverse("team_detail", args=[team.pk]))


@login_required
@require_POST
def invite_revoke(request, team_id: int, invite_id: int):
    team = _visible_team(request, team_id)
    invite = get_object_or_404(TeamInvite, pk=invite_id, team=team)
    try:
        services.revoke_invite(actor=request.user, invite=invite, request=request)
    except PortalError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, "Invite link revoked.")
    return redirect(reverse("team_detail", args=[team.pk]))


@require_http_methods(["GET", "POST"])
def invite_accept(request, token: str):
    """The invite landing page, and the join action.

    Public on GET, because the whole point is that somebody who is not logged in can open the link.
    They see what they are being invited to, then sign in or sign up and come back — `?next=` on the
    login link is what brings them back here rather than to the home page.

    The GET explains any problem with the link using the same rules the POST enforces, so nobody
    clicks a button that was always going to fail.
    """
    invite = services.find_invite(token)
    refusal = services.describe_invite_refusal(
        invite=invite, user=request.user if request.user.is_authenticated else None
    )

    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect(f"/login?next={reverse('invite_accept', args=[token])}")
        try:
            team = services.join_via_invite(actor=request.user, plaintext=token, request=request)
        except PortalError as error:
            return render(
                request,
                "teams/invite.html",
                {"invite": invite, "refusal": error.message, "token": token},
                status=error.status_code,
            )
        messages.success(request, f"You have joined “{team.name}”.")
        return redirect(reverse("team_detail", args=[team.pk]))

    return render(
        request,
        "teams/invite.html",
        {
            "invite": invite,
            "refusal": refusal,
            "token": token,
            # A missing or unusable link is still a 200 page explaining why, not a 404: the visitor
            # needs to read the reason, and the link's existence is not a secret worth protecting
            # from whoever was sent it.
            "team": invite.team if invite else None,
            "event": invite.team.event if invite else None,
        },
    )


# --------------------------------------------------------------------------------------
# leaving and captaincy
# --------------------------------------------------------------------------------------


@login_required
@require_POST
def team_leave(request, team_id: int):
    team = _visible_team(request, team_id)
    event_slug = team.event.slug
    try:
        result = services.leave_team(actor=request.user, team=team, request=request)
    except PortalError as error:
        messages.error(request, error.message)
        return redirect(reverse("team_detail", args=[team.pk]))

    if result["team_deleted"]:
        messages.success(
            request,
            "You left the team. As its last member, the team and its draft project were deleted.",
        )
    else:
        messages.success(request, "You left the team.")
    return redirect(f"/events/{event_slug}")


@login_required
@require_POST
def captain_transfer(request, team_id: int):
    team = _visible_team(request, team_id)
    new_captain = get_object_or_404(User, pk=request.POST.get("user_id") or 0)
    try:
        services.transfer_captaincy(
            actor=request.user, team=team, new_captain=new_captain, request=request
        )
    except PortalError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, f"{new_captain.display_name} is now the captain.")
    return redirect(reverse("team_detail", args=[team.pk]))
