"""The project lifecycle: draft, edit, submit, tags, answers, markdown.

The rules under test here are the ones a hackathon is actually judged on -- what a draft needs,
what a submission needs, what happens at the deadline, and who may author. Note that the fixture
dataset is present in every test (it is imported once per session), so assertions are scoped to
objects each test made rather than to global counts.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest

from core import clock
from core.errors import PermissionDenied, PortalError, SubmissionsClosed, ValidationFailed
from core.models import AuditAction, AuditLog
from events.models import QuestionKind
from events.services import save_question
from projects import services
from projects.models import Project, ProjectStatus, Tag
from tests.factories import (
    add_team_member,
    make_admin,
    make_event,
    make_organizer,
    make_project,
    make_question,
    make_submitted_project,
    make_team,
    make_track,
    make_user,
)


@pytest.fixture
def event(db):
    return make_event(name="Draft Test Event")


@pytest.fixture
def team(event):
    return make_team(event, name="Draftees")


@pytest.fixture
def member(team):
    return team.captain().user


# --------------------------------------------------------------------------------------
# drafts
# --------------------------------------------------------------------------------------


def test_a_draft_needs_only_a_name(event, team, member):
    project = services.create_project(actor=member, event=event, team=team, name="Half An Idea")

    assert project.status == ProjectStatus.DRAFT
    assert project.submitted_at is None
    # And it knows what it is still missing, without refusing to exist.
    assert "tagline" in services.missing_to_submit(project)


def test_a_draft_without_a_name_is_refused(event, team, member):
    with pytest.raises(ValidationFailed):
        services.create_project(actor=member, event=event, team=team, name="   ")


def test_a_draft_is_not_in_the_gallery(event, team, member):
    project = services.create_project(actor=member, event=event, team=team, name="Unseen")
    assert project.pk not in set(Project.objects.gallery_visible().values_list("pk", flat=True))


def test_creating_a_project_records_an_audit_entry(event, team, member):
    project = services.create_project(actor=member, event=event, team=team, name="Logged")
    entry = AuditLog.objects.get(action=AuditAction.PROJECT_CREATED, target_id=str(project.pk))
    assert entry.metadata["team"] == team.name


# --------------------------------------------------------------------------------------
# one active project per team
# --------------------------------------------------------------------------------------


def test_a_second_project_for_the_same_team_is_a_409_not_a_500(event, team, member):
    services.create_project(actor=member, event=event, team=team, name="First")

    with pytest.raises(services.ProjectExists) as caught:
        services.create_project(actor=member, event=event, team=team, name="Second")

    assert caught.value.status_code == 409
    assert caught.value.code == "project_exists"
    # The message names the project that is in the way, so the participant knows where to go.
    assert "First" in caught.value.message
    assert Project.objects.filter(team=team).count() == 1


def test_the_constraint_is_what_actually_enforces_it(event, team, member, monkeypatch):
    """With the courtesy pre-check disabled, the one-active-project rule must still refuse -- as a
    409, not a 500.

    This is the race: two requests both pass the pre-check, then one loses. Simulated by removing
    the pre-check rather than by interleaving two real transactions, because a test that depends on
    timing is a test that fails intermittently on a loaded machine.

    Two mechanisms catch it, and both are exercised: `full_clean()` runs `validate_constraints()`,
    which sees the existing row in Python, and the partial unique index catches the genuine race
    where neither request could see the other's row yet. The assertion is on the outcome both must
    produce.
    """
    services.create_project(actor=member, event=event, team=team, name="First")

    real_filter = Project.objects.filter

    def blind(*args, **kwargs):
        # Make only the courtesy pre-check find nothing, leaving the constraints to object.
        if kwargs.get("duplicate_of__isnull") is True:
            return Project.objects.none()
        return real_filter(*args, **kwargs)

    monkeypatch.setattr(Project.objects, "filter", blind)

    with pytest.raises(services.ProjectExists) as caught:
        services.create_project(actor=member, event=event, team=team, name="Second")

    assert caught.value.status_code == 409
    assert caught.value.code == "project_exists"
    monkeypatch.undo()
    assert Project.objects.filter(team=team).count() == 1


def test_an_integrity_error_from_the_index_becomes_409_not_500(event, team, member, monkeypatch):
    """The race path specifically: past every Python-side check, straight into the index.

    Simulated by making the insert raise the IntegrityError Postgres would raise, because the
    genuine interleaving cannot be produced deterministically from one connection.
    """
    from django.db import IntegrityError

    def raise_conflict(*args, **kwargs):
        raise IntegrityError(
            'duplicate key value violates unique constraint '
            '"project_one_active_per_team_per_event"'
        )

    monkeypatch.setattr(Project, "save", raise_conflict)

    with pytest.raises(services.ProjectExists) as caught:
        services.create_project(actor=member, event=event, team=team, name="Racer")

    assert caught.value.status_code == 409


def test_an_unrelated_integrity_error_is_not_disguised_as_a_conflict(event, team, member, monkeypatch):
    """Swallowing every IntegrityError would turn a real bug into a plausible-looking 409 that
    nobody investigates."""
    from django.db import IntegrityError

    def raise_something_else(*args, **kwargs):
        raise IntegrityError('null value in column "event_id" violates not-null constraint')

    monkeypatch.setattr(Project, "save", raise_something_else)

    with pytest.raises(IntegrityError):
        services.create_project(actor=member, event=event, team=team, name="Broken")


def test_a_field_too_long_is_a_400_not_a_500(event, team, member):
    """`Model.full_clean` raises Django's own ValidationError, which the API's exception handler
    knows nothing about -- untranslated, it is an unhandled exception and a 500."""
    project = services.create_project(actor=member, event=event, team=team, name="Long")

    with pytest.raises(ValidationFailed) as caught:
        services.update_project(actor=member, project=project, tagline="x" * 500)

    assert caught.value.status_code == 400


# --------------------------------------------------------------------------------------
# submitting
# --------------------------------------------------------------------------------------


def _complete(project, **extra):
    """Fill in everything a submission needs, straight on the model."""
    project.tagline = extra.get("tagline", "A one-line summary.")
    project.description = extra.get("description", "What it does and why.")
    project.track = extra.get("track") or make_track(project.event)
    project.repo_url = extra.get("repo_url", "https://example.org/repo")
    project.save()
    return project


def test_an_incomplete_project_cannot_be_submitted(event, team, member):
    project = services.create_project(actor=member, event=event, team=team, name="Bare")

    with pytest.raises(ValidationFailed) as caught:
        services.submit_project(actor=member, project=project)

    # The refusal lists what is missing rather than saying "invalid".
    for field in ("tagline", "description", "track", "repository URL"):
        assert field in caught.value.message
    project.refresh_from_db()
    assert project.status == ProjectStatus.DRAFT


def test_a_complete_project_submits(event, team, member):
    project = _complete(services.create_project(actor=member, event=event, team=team, name="Ready"))

    with clock.frozen_at(event.submissions_close_at - dt.timedelta(minutes=1)) as frozen:
        services.submit_project(actor=member, project=project)

    project.refresh_from_db()
    assert project.status == ProjectStatus.SUBMITTED
    # Stamped with the instant the guard approved, not a second reading of the clock.
    assert project.submitted_at == frozen
    assert AuditLog.objects.filter(action=AuditAction.PROJECT_SUBMITTED).exists()


def test_submitting_twice_does_not_move_the_submission_time(event, team, member):
    project = _complete(services.create_project(actor=member, event=event, team=team, name="Twice"))
    services.submit_project(actor=member, project=project)
    project.refresh_from_db()
    first = project.submitted_at

    services.submit_project(actor=member, project=project)

    project.refresh_from_db()
    assert project.submitted_at == first


def test_editing_after_submitting_keeps_the_status_and_the_timestamp(event, team, member):
    project = _complete(services.create_project(actor=member, event=event, team=team, name="Edited"))
    services.submit_project(actor=member, project=project)
    project.refresh_from_db()
    submitted_at = project.submitted_at

    services.update_project(actor=member, project=project, tagline="A better one-liner.")

    project.refresh_from_db()
    assert project.status == ProjectStatus.SUBMITTED
    assert project.submitted_at == submitted_at
    assert project.tagline == "A better one-liner."


def test_a_submitted_project_cannot_be_edited_into_an_incomplete_state(event, team, member):
    project = _complete(services.create_project(actor=member, event=event, team=team, name="Whole"))
    services.submit_project(actor=member, project=project)
    project.refresh_from_db()

    with pytest.raises(ValidationFailed) as caught:
        services.update_project(actor=member, project=project, tagline="")

    assert "incomplete" in caught.value.message
    project.refresh_from_db()
    assert project.tagline == "A one-line summary."  # the edit did not land


def test_a_draft_left_at_the_deadline_stays_a_draft_forever(event, team, member):
    project = _complete(services.create_project(actor=member, event=event, team=team, name="Late"))

    with clock.frozen_at(event.submissions_close_at):
        with pytest.raises(SubmissionsClosed):
            services.submit_project(actor=member, project=project)

    # And a moment later, still refused. There is no path that promotes it afterwards.
    with clock.frozen_at(event.submissions_close_at + dt.timedelta(hours=1)):
        with pytest.raises(SubmissionsClosed):
            services.submit_project(actor=member, project=project)

    project.refresh_from_db()
    assert project.status == ProjectStatus.DRAFT


# --------------------------------------------------------------------------------------
# the deadline, on every write path
# --------------------------------------------------------------------------------------


def test_the_close_instant_itself_is_refused(event, team, member):
    """The window is half-open: 18:00 means the last accepted write is at 17:59:59."""
    project = services.create_project(actor=member, event=event, team=team, name="Boundary")

    with clock.frozen_at(event.submissions_close_at - dt.timedelta(seconds=1)):
        services.update_project(actor=member, project=project, tagline="just in time")

    with clock.frozen_at(event.submissions_close_at):
        with pytest.raises(SubmissionsClosed):
            services.update_project(actor=member, project=project, tagline="too late")


def test_a_closed_event_refuses_creation_with_409(db):
    closed = make_event(name="Closed", open_window=False)
    team = make_team(closed)

    with pytest.raises(SubmissionsClosed) as caught:
        services.create_project(
            actor=team.captain().user, event=closed, team=team, name="Nope"
        )

    assert caught.value.status_code == 409
    assert caught.value.code == "submissions_closed"
    assert caught.value.extra["closed_at"] == clock.iso(closed.submissions_close_at)


# --------------------------------------------------------------------------------------
# who may author
# --------------------------------------------------------------------------------------


def test_another_teams_member_cannot_edit_this_project(event, team, member):
    project = services.create_project(actor=member, event=event, team=team, name="Ours")
    outsider = make_team(event, name="Someone Else").captain().user

    with pytest.raises(PermissionDenied):
        services.update_project(actor=outsider, project=project, name="Mine now")


def test_neither_an_organizer_nor_an_admin_may_author(event, team, member):
    """The permission matrix says no for both, and that is deliberate -- see
    `core.permissions.can_edit_project`. An admin who could rewrite a submission after the
    deadline would undermine the one guarantee this software exists to provide.
    """
    project = services.create_project(actor=member, event=event, team=team, name="Theirs")
    organizer = make_organizer(event)
    admin = make_admin()

    for actor in (organizer, admin):
        with pytest.raises(PermissionDenied):
            services.update_project(actor=actor, project=project, name="Rewritten")
        with pytest.raises(PermissionDenied):
            services.submit_project(actor=actor, project=project)

    project.refresh_from_db()
    assert project.name == "Theirs"


def test_a_new_member_of_the_team_may_edit(event, team, member):
    project = services.create_project(actor=member, event=event, team=team, name="Shared")
    joiner = add_team_member(team).user

    services.update_project(actor=joiner, project=project, tagline="added by the new member")

    project.refresh_from_db()
    assert project.tagline == "added by the new member"


# --------------------------------------------------------------------------------------
# tags
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("React", "react"),
        ("  django  ", "django"),
        ("MACHINE   LEARNING", "machine learning"),
        ("rust\tlang", "rust lang"),
    ],
)
def test_tags_are_normalized(raw, expected):
    assert services.normalize_tag(raw) == expected


def test_tags_are_deduplicated_after_normalization(event, team, member):
    project = services.create_project(actor=member, event=event, team=team, name="Tagged")

    services.set_tags(actor=member, project=project, tags="React, react,  REACT , django")

    assert sorted(project.project_tags.values_list("tag__name", flat=True)) == ["django", "react"]
    assert Tag.objects.filter(name="react").count() == 1


def test_setting_tags_replaces_rather_than_appends(event, team, member):
    project = services.create_project(actor=member, event=event, team=team, name="Retagged")
    services.set_tags(actor=member, project=project, tags="one, two")

    services.set_tags(actor=member, project=project, tags="two, three")

    assert sorted(project.project_tags.values_list("tag__name", flat=True)) == ["three", "two"]


def test_too_many_tags_is_refused(event, team, member):
    project = services.create_project(actor=member, event=event, team=team, name="Overtagged")
    too_many = [f"tag{i}" for i in range(services.MAX_PROJECT_TAGS + 1)]

    with pytest.raises(ValidationFailed):
        services.set_tags(actor=member, project=project, tags=too_many)

    assert project.project_tags.count() == 0


def test_tags_are_searchable(event, team, member):
    """Tags carry weight B in the search vector, so setting them must reindex."""
    from django.contrib.postgres.search import SearchQuery

    project = _complete(
        services.create_project(actor=member, event=event, team=team, name="Findable")
    )
    services.set_tags(actor=member, project=project, tags="elixir")

    hits = Project.objects.filter(pk=project.pk).filter(
        search_vector=SearchQuery("elixir", config="english")
    )
    assert hits.exists()


# --------------------------------------------------------------------------------------
# custom questions and answers
# --------------------------------------------------------------------------------------


def test_a_required_question_blocks_submission_until_answered(event, team, member):
    question = make_question(event, prompt="How did you hear about us?", required=True)
    project = _complete(services.create_project(actor=member, event=event, team=team, name="Asked"))

    with pytest.raises(ValidationFailed) as caught:
        services.submit_project(actor=member, project=project)
    assert "How did you hear about us?" in caught.value.message

    services.save_answers(actor=member, project=project, answers={question.pk: "A friend"})
    services.submit_project(actor=member, project=project)

    project.refresh_from_db()
    assert project.status == ProjectStatus.SUBMITTED


def test_an_optional_question_does_not_block_submission(event, team, member):
    make_question(event, prompt="Anything else?", required=False)
    project = _complete(services.create_project(actor=member, event=event, team=team, name="Free"))

    services.submit_project(actor=member, project=project)

    project.refresh_from_db()
    assert project.status == ProjectStatus.SUBMITTED


def test_a_required_question_added_after_submission_is_grandfathered(event, team, member):
    """An organizer adding a question mid-event changes what a *new* submission needs. It must not
    lock an already-complete team out of its own edit form."""
    project = _complete(
        services.create_project(actor=member, event=event, team=team, name="Grandfathered")
    )
    services.submit_project(actor=member, project=project)
    project.refresh_from_db()

    organizer = make_organizer(event)
    save_question(
        actor=organizer,
        event=event,
        prompt="A question invented later",
        kind=QuestionKind.SHORT_TEXT,
        required=True,
    )

    # Still editable...
    services.update_project(actor=member, project=project, tagline="still editable")
    project.refresh_from_db()
    assert project.tagline == "still editable"

    # ...and the new question is not counted against it.
    assert services.missing_to_submit(project, grandfather=True) == []
    # But it *is* counted for a project that has not submitted yet.
    assert services.missing_to_submit(project) != []


def test_an_answer_to_another_events_question_is_refused(event, team, member):
    other = make_event(name="Elsewhere")
    foreign = make_question(other, prompt="Not yours")
    project = services.create_project(actor=member, event=event, team=team, name="Confused")

    with pytest.raises(ValidationFailed):
        services.save_answers(actor=member, project=project, answers={foreign.pk: "hello"})


def test_a_choice_answer_must_be_one_of_the_choices(event, team, member):
    question = make_question(event, kind=QuestionKind.CHOICE, choices=["alpha", "beta"])
    project = services.create_project(actor=member, event=event, team=team, name="Choosy")

    with pytest.raises(ValidationFailed):
        services.save_answers(actor=member, project=project, answers={question.pk: "gamma"})

    services.save_answers(actor=member, project=project, answers={question.pk: "beta"})
    assert project.answers.get(question=question).value == "beta"


def test_a_url_answer_must_be_http(event, team, member):
    question = make_question(event, kind=QuestionKind.URL)
    project = services.create_project(actor=member, event=event, team=team, name="Linked")

    with pytest.raises(ValidationFailed):
        services.save_answers(
            actor=member, project=project, answers={question.pk: "javascript:alert(1)"}
        )


@pytest.mark.parametrize(
    "given,stored",
    [(True, "true"), (False, "false"), ("yes", "true"), ("no", "false"), ("", "false")],
)
def test_a_boolean_answer_is_stored_as_a_word(event, team, member, given, stored):
    question = make_question(event, kind=QuestionKind.BOOLEAN)
    project = services.create_project(actor=member, event=event, team=team, name="Boolean")

    services.save_answers(actor=member, project=project, answers={question.pk: given})

    assert project.answers.get(question=question).value == stored


def test_answers_are_updated_not_duplicated(event, team, member):
    question = make_question(event)
    project = services.create_project(actor=member, event=event, team=team, name="Answered")

    services.save_answers(actor=member, project=project, answers={question.pk: "first"})
    services.save_answers(actor=member, project=project, answers={question.pk: "second"})

    assert project.answers.count() == 1
    assert project.answers.get().value == "second"


# --------------------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "data:text/html,<script>x</script>", "file:///etc/passwd", "ftp://x/y"],
)
def test_only_http_urls_are_accepted(event, team, member, url):
    project = services.create_project(actor=member, event=event, team=team, name="Linky")

    with pytest.raises(ValidationFailed):
        services.update_project(actor=member, project=project, repo_url=url)


def test_a_track_from_another_event_is_refused(event, team, member):
    project = services.create_project(actor=member, event=event, team=team, name="Tracked")
    foreign_track = make_track(make_event(name="Other Event"))

    with pytest.raises(ValidationFailed):
        services.update_project(actor=member, project=project, track=foreign_track)


# --------------------------------------------------------------------------------------
# markdown
# --------------------------------------------------------------------------------------


def test_markdown_renders_ordinary_prose():
    html = services.render_markdown("# Title\n\nSome **bold** text.")
    assert "<h1>Title</h1>" in html
    assert "<strong>bold</strong>" in html


def _tags_and_attributes(html: str) -> tuple[set[str], str]:
    """The element names and the attribute text actually present in the rendered output.

    Needed because `markdown-it` with `html=False` *escapes* raw HTML rather than dropping it, so
    `&lt;div onclick="..."&gt;` is inert text that nonetheless contains the substring "onclick".
    Asserting on substrings would either fail on safe output or pass on unsafe output; asserting on
    real tags is the only check that means anything.
    """
    tags = set(re.findall(r"<\s*([a-zA-Z][a-zA-Z0-9]*)", html))
    attributes = " ".join(re.findall(r"<[^>]+>", html))
    return tags, attributes


@pytest.mark.parametrize(
    "dangerous",
    [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        '<a href="javascript:alert(1)">click</a>',
        '<div onclick="alert(1)">x</div>',
        '<iframe src="https://evil.test"></iframe>',
        "<style>body{display:none}</style>",
        '<svg><use href="#x"/></svg>',
    ],
)
def test_raw_html_in_a_description_never_becomes_live_markup(dangerous):
    html = services.render_markdown(dangerous)
    tags, attributes = _tags_and_attributes(html)

    # Whatever survives is inert text inside allowed elements, and no event handler or dangerous
    # scheme appears in any attribute.
    assert tags <= services.MARKDOWN_ALLOWED_TAGS
    assert "script" not in tags and "iframe" not in tags and "style" not in tags
    assert "onerror" not in attributes and "onclick" not in attributes
    assert "javascript:" not in attributes


def test_a_markdown_link_with_a_javascript_scheme_is_dropped():
    """The real risk, as opposed to raw HTML: markdown's own link syntax carrying a scheme that
    executes. nh3's url_schemes allow-list is what refuses it."""
    html = services.render_markdown("[click me](javascript:alert(1))")

    _tags, attributes = _tags_and_attributes(html)
    assert "javascript:" not in attributes
    # The link text survives; only the dangerous target is gone.
    assert "click me" in html


def test_a_markdown_link_with_a_data_scheme_is_dropped():
    html = services.render_markdown("[x](data:text/html;base64,PHNjcmlwdD4=)")

    _tags, attributes = _tags_and_attributes(html)
    assert "data:" not in attributes


def test_markdown_does_not_render_remote_images():
    """An <img> would fetch from an external host at view time, which the offline rule forbids and
    which leaks every reader's IP to whoever wrote the description."""
    html = services.render_markdown("![shot](https://evil.test/track.png)")

    tags, _attributes = _tags_and_attributes(html)
    assert "img" not in tags
    assert "evil.test" not in html


def test_markdown_links_get_rel_attributes():
    html = services.render_markdown("[docs](https://example.org/docs)")

    assert 'href="https://example.org/docs"' in html
    for token in ("noopener", "noreferrer", "nofollow"):
        assert token in html


def test_an_empty_description_renders_to_nothing():
    assert services.render_markdown("") == ""
    assert services.render_markdown("   \n ") == ""


# --------------------------------------------------------------------------------------
# a team rename must keep its project findable
# --------------------------------------------------------------------------------------


def test_renaming_a_team_reindexes_its_projects(event, team, member):
    """The team name carries weight B in the search vector. A rename that did not reindex would
    leave the project findable only under the old name -- silently, with no error."""
    from django.contrib.postgres.search import SearchQuery

    from teams import services as team_services

    project = make_submitted_project(team, name="Reindexed")

    team_services.rename_team(actor=member, team=team, name="Renamedaceae")

    def found_by(word):
        return (
            Project.objects.filter(pk=project.pk)
            .filter(search_vector=SearchQuery(word, config="english"))
            .exists()
        )

    assert found_by("Renamedaceae")
    assert not found_by("Draftees")
