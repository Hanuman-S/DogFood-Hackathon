"""History an event bundle brings in describes another install: it must never feed a decision here, and
an imported voter pseudonym must never work as a voter's credential."""

import os
from io import StringIO

import pytest
from django.core import signing
from django.core.management import call_command
from django.utils import timezone

from accounts.models import User
from core.audit import Origin
from core.models import AuditAction, AuditLog
from events.models import Event
from imports import bundle, bundle_import
from projects import comments
from projects.comment_errors import RateLimited
from projects.models import Project, Status
from voting import links

HERE = Origin(ip_hash="f" * 64)


@pytest.mark.django_db
def test_live_leaves_out_imported_rows_and_keeps_every_other_row():
    AuditLog.objects.create(action=AuditAction.COMMENT_POSTED, detail={"source_history": True})
    AuditLog.objects.create(action=AuditAction.COMMENT_POSTED, detail={})
    AuditLog.objects.create(action=AuditAction.COMMENT_POSTED, detail={"project": "3", "source": "x"})
    AuditLog.objects.create(action=AuditAction.COMMENT_POSTED)
    assert AuditLog.objects.count() == 4 and AuditLog.objects.live().count() == 3


@pytest.mark.django_db(transaction=True)
def test_fresh_imported_comment_rows_do_not_use_up_the_matched_users_limit(make_event, make_team, make_user,
                                                                          tmp_path, settings):
    """The source user posts their five comments; the event moves to a fresh install at once, so those
    audit rows are still inside the window. There, the same person (matched by email) still has all
    five of their own."""
    settings.MEDIA_ROOT = str(tmp_path / "media")
    event = make_event()
    Event.objects.filter(pk=event.pk).update(tagline="t", description="d")
    project = Project.objects.create(team=make_team(event), name="Lamp", status=Status.SUBMITTED,
                                     submitted_at=timezone.now())
    writer = make_user(email="writer@example.org", name="Writer")
    for i in range(5):
        comments.post_comment(project.pk, f"source comment {i}", author=writer, origin=HERE)
    with pytest.raises(RateLimited):
        comments.post_comment(project.pk, "one too many", author=writer, origin=HERE)
    path = bundle.export_event(event, actor=None)
    data = open(path, "rb").read()
    os.unlink(path)

    call_command("flush", interactive=False, verbosity=0)  # a fresh install
    admin = User.objects.create_user("admin@example.org", None, name="Admin", is_platform_admin=True)
    target = tmp_path / "b.zip"
    target.write_bytes(data)
    imported = bundle_import.import_event(str(target), actor=admin)
    history = AuditLog.objects.filter(action=AuditAction.COMMENT_POSTED, detail__source_history=True)
    assert history.count() == 5
    assert all(timezone.now() - row.created_at < timezone.timedelta(minutes=5) for row in history)

    Event.objects.filter(pk=imported.pk).update(is_published=True)  # imports arrive unpublished
    same_person = User.objects.get(email="writer@example.org")
    assert history.filter(actor=same_person).count() == 5  # the rows name them: only live() keeps them out
    new_project = Project.objects.get(event=imported)
    for i in range(5):
        comments.post_comment(new_project.pk, f"new comment {i}", author=same_person, origin=HERE)
    with pytest.raises(RateLimited):  # the limit itself still works, on this install's own rows
        comments.post_comment(new_project.pk, "one too many", author=same_person, origin=HERE)


@pytest.mark.django_db
def test_an_imported_pseudonym_presented_as_a_voter_cookie_names_no_one(make_event):
    event = make_event()
    pseudonym = "v_0123456789abcdef"
    forged = signing.dumps({"e": event.pk, "v": pseudonym}, salt=links.COOKIE_SALT)  # even with the key
    assert links.cookie_voter_id(event, forged) == ""
    real = links.new_cookie_value(event)
    voter_id = links.cookie_voter_id(event, real)
    assert len(voter_id) == 32 and all(c in "0123456789abcdef" for c in voter_id)


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["v_0123456789abcdef", "0" * 31, "0" * 33, "G" * 32, "0" * 32 + "\n", ""])
def test_only_the_issued_voter_id_form_is_accepted(make_event, value):
    event = make_event()
    cookie = signing.dumps({"e": event.pk, "v": value}, salt=links.COOKIE_SALT)
    assert links.cookie_voter_id(event, cookie) == ""
