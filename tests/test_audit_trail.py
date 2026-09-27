"""The audit trail, and in particular that refusals are actually recorded.

The brief requires an entry for "every refused write due to the deadline". That is easy to get
subtly wrong: if the guard writes its audit row inside a transaction that then raises, the row is
rolled back along with the write it was describing, and the trail silently contains nothing. The
first version of the service layer had exactly that bug -- every service was decorated
`@transaction.atomic`, so every refusal recorded nothing.

These tests exist so it cannot come back. They assert the row is *in the database* after the
exception, which is the only thing that matters.
"""

from __future__ import annotations

import datetime as dt

import pytest

from core import clock
from core.errors import PortalError, SubmissionsClosed
from core.models import AuditAction, AuditLog
from events import services as event_services
from events.models import Role
from teams import services as team_services
from tests.factories import (
    add_team_member,
    make_event,
    make_invite,
    make_judge,
    make_organizer,
    make_question,
    make_submitted_project,
    make_team,
    make_track,
    make_user,
)


@pytest.fixture
def closed_event(db):
    return make_event(name="Closed Event", open_window=False)


# --------------------------------------------------------------------------------------
# deadline refusals survive
# --------------------------------------------------------------------------------------


def test_a_deadline_refused_team_creation_is_recorded(closed_event):
    user = make_user()

    with pytest.raises(SubmissionsClosed):
        team_services.create_team(actor=user, event=closed_event, name="Too Late")

    entry = AuditLog.objects.get(action=AuditAction.REFUSED_DEADLINE)
    assert entry.actor == user
    assert entry.event == closed_event
    assert entry.metadata["attempted"] == "create_team"
    assert entry.metadata["reason"] == "submissions_closed"
    # The instant is recorded, so an organizer can see how late the attempt was.
    assert entry.metadata["closed_at"] == clock.iso(closed_event.submissions_close_at)


def test_a_deadline_refused_join_is_recorded(closed_event):
    team = make_team(closed_event)
    invite, plaintext = make_invite(team)

    with pytest.raises(SubmissionsClosed):
        team_services.join_via_invite(actor=make_user(), plaintext=plaintext)

    entry = AuditLog.objects.get(action=AuditAction.REFUSED_DEADLINE)
    assert entry.metadata["attempted"] == "join_team"


def test_a_deadline_refused_leave_is_recorded(closed_event):
    team = make_team(closed_event)
    captain = team.captain().user

    with pytest.raises(SubmissionsClosed):
        team_services.leave_team(actor=captain, team=team)

    assert AuditLog.objects.filter(
        action=AuditAction.REFUSED_DEADLINE, metadata__attempted="leave_team"
    ).exists()


def test_a_deadline_refused_invite_creation_is_recorded(closed_event):
    team = make_team(closed_event)

    with pytest.raises(SubmissionsClosed):
        team_services.create_invite(actor=team.captain().user, team=team)

    assert AuditLog.objects.filter(
        action=AuditAction.REFUSED_DEADLINE, metadata__attempted="create_invite"
    ).exists()


def test_a_deadline_refused_invite_revocation_is_recorded(closed_event):
    team = make_team(closed_event)
    invite, _ = make_invite(team)

    with pytest.raises(SubmissionsClosed):
        team_services.revoke_invite(actor=team.captain().user, invite=invite)

    assert AuditLog.objects.filter(
        action=AuditAction.REFUSED_DEADLINE, metadata__attempted="revoke_invite"
    ).exists()


def test_a_deadline_refused_captaincy_transfer_is_recorded(closed_event):
    team = make_team(closed_event)
    member = add_team_member(team).user

    with pytest.raises(SubmissionsClosed):
        team_services.transfer_captaincy(
            actor=team.captain().user, team=team, new_captain=member
        )

    assert AuditLog.objects.filter(
        action=AuditAction.REFUSED_DEADLINE, metadata__attempted="transfer_captaincy"
    ).exists()


@pytest.mark.parametrize(
    "path",
    ["create_team", "create_invite", "revoke_invite", "join_team", "leave_team", "transfer_captaincy"],
)
def test_every_guarded_team_path_records_its_own_refusal(closed_event, path):
    """One parameterized sweep, so adding a guarded path without its audit entry fails here."""
    team = make_team(closed_event)
    captain = team.captain().user
    member = add_team_member(team).user
    invite, plaintext = make_invite(team)

    calls = {
        "create_team": lambda: team_services.create_team(
            actor=make_user(), event=closed_event, name="X"
        ),
        "create_invite": lambda: team_services.create_invite(actor=captain, team=team),
        "revoke_invite": lambda: team_services.revoke_invite(actor=captain, invite=invite),
        "join_team": lambda: team_services.join_via_invite(
            actor=make_user(), plaintext=plaintext
        ),
        "leave_team": lambda: team_services.leave_team(actor=member, team=team),
        "transfer_captaincy": lambda: team_services.transfer_captaincy(
            actor=captain, team=team, new_captain=member
        ),
    }

    with pytest.raises(SubmissionsClosed):
        calls[path]()

    assert AuditLog.objects.filter(
        action=AuditAction.REFUSED_DEADLINE, metadata__attempted=path
    ).exists(), f"{path} refused without recording it"


# --------------------------------------------------------------------------------------
# the same, for every project write path
# --------------------------------------------------------------------------------------

PROJECT_WRITE_PATHS = [
    "create_project",
    "update_project",
    "submit_project",
    "set_tags",
    "save_answers",
    "add_image",
    "remove_image",
    "set_thumbnail",
]


@pytest.mark.parametrize("path", PROJECT_WRITE_PATHS)
def test_every_guarded_project_path_records_its_own_refusal(closed_event, path):
    """One sweep over every public write function in `projects.services`.

    The list is exhaustive on purpose: adding a guarded path without its audit entry fails here,
    and so does adding a *public* write function that forgets to guard at all -- which is the more
    dangerous mistake, since it would accept a submission after the deadline.
    """
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    from projects import services as project_services

    team = make_team(closed_event)
    member = team.captain().user
    # Built directly rather than through the service layer: the event is already closed, so no
    # participant write path could produce this state. This is the same reason the fixture
    # importer bypasses the guard.
    project = make_submitted_project(team, name="Already There")
    question = make_question(closed_event, prompt="Anything?")

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, format="PNG")
    upload = SimpleUploadedFile("x.png", buffer.getvalue(), "image/png")

    existing_image = project.images.create(image="projects/none.png", order=0)

    calls = {
        "create_project": lambda: project_services.create_project(
            actor=member, event=closed_event, team=make_team(closed_event), name="Too Late"
        ),
        "update_project": lambda: project_services.update_project(
            actor=member, project=project, tagline="edited late"
        ),
        "submit_project": lambda: project_services.submit_project(actor=member, project=project),
        "set_tags": lambda: project_services.set_tags(
            actor=member, project=project, tags="late"
        ),
        "save_answers": lambda: project_services.save_answers(
            actor=member, project=project, answers={question.pk: "late"}
        ),
        "add_image": lambda: project_services.add_image(
            actor=member, project=project, upload=upload
        ),
        "remove_image": lambda: project_services.remove_image(actor=member, image=existing_image),
        "set_thumbnail": lambda: project_services.set_thumbnail(
            actor=member, project=project, upload=upload
        ),
    }

    with pytest.raises(SubmissionsClosed):
        calls[path]()

    assert AuditLog.objects.filter(
        action=AuditAction.REFUSED_DEADLINE, metadata__attempted=path
    ).exists(), f"{path} refused without recording it"


def test_the_project_sweep_covers_every_public_write_function():
    """Guards the list above against drift.

    A new public write in `projects.services` that nobody adds here would otherwise go untested
    for the one property that matters most about it.
    """
    import inspect

    from projects import services as project_services

    # Functions that write but are not participant write paths, with why each is exempt.
    exempt = {
        "guard_project_create",  # the guards themselves; covered via create_project
        "normalize_tag",
        "parse_tags",
        "render_markdown",
        "missing_to_submit",
        "can_submit",
        "verify_image",  # pure validation, writes nothing
    }

    public_writes = {
        name
        for name, obj in vars(project_services).items()
        if not name.startswith("_")
        and inspect.isfunction(obj)
        and obj.__module__ == project_services.__name__
        and name not in exempt
    }

    assert public_writes == set(PROJECT_WRITE_PATHS), (
        "projects.services gained or lost a public write function; add it to "
        "PROJECT_WRITE_PATHS (and make sure it guards itself) or to the exempt list above"
    )


# --------------------------------------------------------------------------------------
# permission refusals survive
# --------------------------------------------------------------------------------------


def test_a_permission_refusal_is_recorded(db):
    event = make_event()
    outsider = make_user()

    with pytest.raises(PortalError):
        event_services.update_event(actor=outsider, event=event, name="Hijacked")

    entry = AuditLog.objects.get(action=AuditAction.REFUSED_PERMISSION)
    assert entry.actor == outsider
    assert entry.metadata["attempted"] == "update_event"
    # And the event was not changed.
    event.refresh_from_db()
    assert event.name != "Hijacked"


def test_a_refused_event_creation_is_recorded(db):
    ungranted = make_user()
    with pytest.raises(PortalError):
        event_services.create_event(
            actor=ungranted,
            name="Unauthorized Event",
            starts_at=clock.now(),
            submissions_open_at=clock.now(),
            submissions_close_at=clock.now() + dt.timedelta(days=1),
        )

    assert AuditLog.objects.filter(
        action=AuditAction.REFUSED_PERMISSION, metadata__attempted="create_event"
    ).exists()


def test_a_non_captain_trying_to_invite_is_recorded(db):
    event = make_event()
    team = make_team(event)
    member = add_team_member(team).user

    with pytest.raises(PortalError):
        team_services.create_invite(actor=member, team=team)

    assert AuditLog.objects.filter(
        action=AuditAction.REFUSED_PERMISSION, metadata__attempted="create_invite"
    ).exists()


# --------------------------------------------------------------------------------------
# successful actions are recorded too
# --------------------------------------------------------------------------------------


def test_a_date_change_records_old_and_new_values(db):
    """Moving a deadline is the most consequential edit an organizer can make, so the trail has to
    show what it was before."""
    event = make_event()
    organizer = make_organizer(event)
    original = event.submissions_close_at
    extended = original + dt.timedelta(days=3)

    event_services.update_event(
        actor=organizer,
        event=event,
        submissions_close_at=extended,
        judging_ends_at=extended + dt.timedelta(days=7),
    )

    entry = AuditLog.objects.get(action=AuditAction.EVENT_DATES_CHANGED)
    change = entry.metadata["changes"]["submissions_close_at"]
    assert change["from"] == clock.iso(original)
    assert change["to"] == clock.iso(extended)


def test_extending_a_closed_deadline_is_allowed_and_recorded(closed_event):
    """The explicit escape hatch: a power cut ate the last hour, so the organizer extends."""
    organizer = make_organizer(closed_event)
    new_close = clock.now() + dt.timedelta(days=1)

    event_services.update_event(
        actor=organizer,
        event=closed_event,
        submissions_close_at=new_close,
        judging_ends_at=new_close + dt.timedelta(days=7),
    )

    closed_event.refresh_from_db()
    assert closed_event.submissions_open is True
    assert AuditLog.objects.filter(action=AuditAction.EVENT_DATES_CHANGED).exists()

    # And now a participant can act again, through the ordinary guarded path.
    team_services.create_team(actor=make_user(), event=closed_event, name="Back In Time")


def test_a_non_date_edit_is_recorded_separately_from_a_date_edit(db):
    event = make_event()
    organizer = make_organizer(event)

    event_services.update_event(actor=organizer, event=event, name="Renamed Event")

    assert AuditLog.objects.filter(action=AuditAction.EVENT_UPDATED).exists()
    assert not AuditLog.objects.filter(action=AuditAction.EVENT_DATES_CHANGED).exists()


def test_an_unchanged_update_records_nothing(db):
    """Saving a form without editing anything should not add noise to the trail."""
    event = make_event()
    organizer = make_organizer(event)

    event_services.update_event(actor=organizer, event=event, name=event.name)

    assert not AuditLog.objects.filter(
        action__in=[AuditAction.EVENT_UPDATED, AuditAction.EVENT_DATES_CHANGED]
    ).exists()


def test_the_audit_log_survives_deletion_of_its_target(db):
    """Targets are stored as loose (type, id) strings so a row outlives what it describes.

    A foreign key would either cascade the evidence away or block the deletion.
    """
    event = make_event()
    team = make_team(event)
    captain = team.captain().user

    team_services.leave_team(actor=captain, team=team)  # deletes the team

    entry = AuditLog.objects.get(action=AuditAction.TEAM_DELETED)
    assert entry.metadata["team"] == team.name
    assert entry.event == event


def test_audit_rows_carry_the_client_ip_when_a_request_is_present(rf, db):
    event = make_event()
    request = rf.post("/", REMOTE_ADDR="198.51.100.7")
    request.user = make_user()

    with pytest.raises(PortalError):
        event_services.update_event(actor=request.user, event=event, name="No", request=request)

    entry = AuditLog.objects.get(action=AuditAction.REFUSED_PERMISSION)
    assert entry.metadata["ip"] == "198.51.100.7"
