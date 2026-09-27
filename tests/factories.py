"""Small builders for test data.

Deliberately plain functions rather than a factory library: there is one more dependency to pin
and explain otherwise, and these objects have enough interlocking constraints (denormalized
`event` on TeamMember, the participant/staff exclusion, one-captain-per-team) that being explicit
about what is created is a feature.

Each builder creates the *complete* valid shape. `make_participant` registers the event membership
as well as the team row, because a TeamMember without an EventMembership is a state the service
layer never produces and testing against it would prove nothing.
"""

from __future__ import annotations

import datetime as dt
import itertools

from accounts.models import User
from core import clock
from events.models import CustomQuestion, Event, EventMembership, Prize, QuestionKind, Role, Track
from projects.models import Project, ProjectStatus
from projects.search import rebuild_search_vector
from teams.models import DEFAULT_INVITE_DAYS, Team, TeamInvite

_counter = itertools.count(1)

PASSWORD = "a-decent-test-password-42"


def unique(prefix: str = "x") -> str:
    return f"{prefix}{next(_counter)}"


# --------------------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------------------


def make_user(email: str | None = None, *, password: str = PASSWORD, **kwargs) -> User:
    email = email or f"{unique('user')}@example.test"
    kwargs.setdefault("display_name", email.split("@")[0])
    return User.objects.create_user(email=email, password=password, **kwargs)


def make_admin(email: str | None = None, **kwargs) -> User:
    return make_user(email or f"{unique('admin')}@example.test", is_platform_admin=True, is_staff=True, is_superuser=True, **kwargs)


# --------------------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------------------


def make_event(
    *,
    name: str | None = None,
    open_window: bool = True,
    gallery_public: bool = True,
    max_team_size: int = 4,
    created_by: User | None = None,
    **kwargs,
) -> Event:
    """An event with a coherent timeline.

    `open_window=False` produces an event that closed a week ago, which is the state the imported
    fixture event is permanently in.
    """
    name = name or f"Event {unique()}"
    now = clock.now()

    if open_window:
        starts_at = now - dt.timedelta(days=1)
        closes_at = now + dt.timedelta(days=7)
    else:
        starts_at = now - dt.timedelta(days=14)
        closes_at = now - dt.timedelta(days=7)

    defaults = {
        "starts_at": starts_at,
        "submissions_open_at": starts_at,
        "submissions_close_at": closes_at,
        "judging_ends_at": closes_at + dt.timedelta(days=7),
        "gallery_public": gallery_public,
        "max_team_size": max_team_size,
        "created_by": created_by,
    }
    defaults.update(kwargs)
    return Event.objects.create(name=name, slug=Event.build_slug(name), **defaults)


def make_track(event: Event, name: str | None = None, **kwargs) -> Track:
    return Track.objects.create(event=event, name=name or f"Track {unique()}", **kwargs)


def make_prize(event: Event, name: str | None = None, **kwargs) -> Prize:
    return Prize.objects.create(event=event, name=name or f"Prize {unique()}", **kwargs)


def make_question(
    event: Event,
    *,
    kind: str = QuestionKind.SHORT_TEXT,
    required: bool = False,
    prompt: str | None = None,
    choices: list | None = None,
    **kwargs,
) -> CustomQuestion:
    return CustomQuestion.objects.create(
        event=event,
        prompt=prompt or f"Question {unique()}?",
        kind=kind,
        required=required,
        choices=choices or ([] if kind != QuestionKind.CHOICE else ["one", "two"]),
        **kwargs,
    )


# --------------------------------------------------------------------------------------
# roles
# --------------------------------------------------------------------------------------


def make_membership(user: User, event: Event, role: str) -> EventMembership:
    return EventMembership.objects.create(user=user, event=event, role=role)


def make_organizer(event: Event, user: User | None = None) -> User:
    user = user or make_user(f"{unique('organizer')}@example.test", can_create_events=True)
    make_membership(user, event, Role.ORGANIZER)
    return user


def make_judge(event: Event, user: User | None = None, tracks: list[Track] | None = None) -> User:
    from events.models import JudgeTrack

    user = user or make_user(f"{unique('judge')}@example.test")
    membership = make_membership(user, event, Role.JUDGE)
    for track in tracks or []:
        JudgeTrack.objects.create(membership=membership, track=track)
    return user


# --------------------------------------------------------------------------------------
# teams and projects
# --------------------------------------------------------------------------------------


def make_team(event: Event, *, name: str | None = None, captain: User | None = None) -> Team:
    """A team with a captain who is also a registered participant of the event."""
    from teams.models import TeamMember

    captain = captain or make_user(f"{unique('captain')}@example.test")
    team = Team.objects.create(
        event=event, name=name or f"Team {unique()}", created_by=captain
    )
    make_membership(captain, event, Role.PARTICIPANT)
    TeamMember.objects.create(team=team, user=captain, event=event, is_captain=True)
    return team


def add_team_member(team: Team, user: User | None = None):
    """Add a non-captain member, registering them as a participant too."""
    from teams.models import TeamMember

    user = user or make_user(f"{unique('member')}@example.test")
    make_membership(user, team.event, Role.PARTICIPANT)
    return TeamMember.objects.create(team=team, user=user, event=team.event, is_captain=False)


def make_invite(team: Team, *, expires_in_days: int = DEFAULT_INVITE_DAYS, max_uses=None, revoked=False):
    """Returns `(invite, plaintext)`."""
    plaintext = TeamInvite.generate_plaintext()
    invite = TeamInvite.objects.create(
        team=team,
        token_hash=TeamInvite.hash_token(plaintext),
        created_by=team.captain().user if team.captain() else None,
        expires_at=clock.now() + dt.timedelta(days=expires_in_days),
        max_uses=max_uses,
        revoked_at=clock.now() if revoked else None,
    )
    return invite, plaintext


def make_project(
    team: Team,
    *,
    status: str = ProjectStatus.DRAFT,
    name: str | None = None,
    track: Track | None = None,
    submitted_at=None,
    index: bool = True,
    **kwargs,
) -> Project:
    """A project on `team`, draft by default.

    The `submitted_at` / `status` pair is kept coherent because a database check constraint
    requires it: a submitted project has a timestamp, a draft has none.
    """
    if status == ProjectStatus.SUBMITTED and submitted_at is None:
        submitted_at = clock.now()
    if status == ProjectStatus.DRAFT:
        submitted_at = None

    project = Project.objects.create(
        event=team.event,
        team=team,
        track=track,
        name=name or f"Project {unique()}",
        status=status,
        submitted_at=submitted_at,
        **kwargs,
    )
    if index:
        rebuild_search_vector(project)
    return project


def make_submitted_project(team: Team, **kwargs) -> Project:
    kwargs.setdefault("tagline", "A one-line summary.")
    kwargs.setdefault("description", "Some **markdown** description.")
    kwargs.setdefault("repo_url", "https://example.org/repo")
    kwargs.setdefault("track", make_track(team.event))
    return make_project(team, status=ProjectStatus.SUBMITTED, **kwargs)
