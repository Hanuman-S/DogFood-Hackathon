"""Projects: draft and submit, editing, tags, answers, images and who can see what."""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from PIL import Image

from core.markdown import render
from events.models import CustomQuestion, Track
from projects.models import Answer, Project, ProjectImage, Status

pytestmark = pytest.mark.django_db


def png_bytes(size=(64, 48), exif=False, trailing=b""):
    image = Image.new("RGB", size, (90, 70, 200))
    out = io.BytesIO()
    if exif:
        data = Image.Exif()
        data[0x010F] = "SpyCam"  # Make
        image.save(out, "JPEG", exif=data.tobytes())
    else:
        image.save(out, "PNG")
    return out.getvalue() + trailing


def complete_form(project, **overrides):
    data = {
        "name": project.name,
        "tagline": "One line",
        "description": "## About\n\nIt works.",
        "repo_url": "https://github.com/x/y",
        "demo_video_url": "",
        "live_url": "",
        "track": "",
        "tags": "",
    }
    data.update(overrides)
    return data


@pytest.fixture
def setup(make_event, make_team, client_for):
    event = make_event()
    team = make_team(event)
    project = Project.objects.create(team=team, name="Quiet Hours")
    return event, team, project, client_for(team.captain)


# --- starting ------------------------------------------------------------------------------


def test_starting_solo_creates_a_team_of_one(make_event, make_user, client_for):
    event = make_event()
    user = make_user(name="Ada")
    client = client_for(user)
    response = client.post(f"/participant/events/{event.slug}/project", {"name": "Solo thing"})
    project = Project.objects.get()
    assert response["Location"] == f"/participant/projects/{project.pk}/"
    assert project.team.captain == user and project.team.members.count() == 1
    assert project.status == Status.DRAFT


def test_one_project_per_team(setup):
    event, team, project, client = setup
    client.post(f"/participant/events/{event.slug}/project", {"name": "Second"})
    assert Project.objects.count() == 1


def test_non_members_cannot_open_the_editor(setup, make_user, client_for):
    event, team, project, client = setup
    assert client_for(make_user()).get(f"/participant/projects/{project.pk}/").status_code == 404


# --- editing and submitting ------------------------------------------------------------------


def test_a_draft_may_be_incomplete(setup):
    event, team, project, client = setup
    response = client.post(f"/participant/projects/{project.pk}/", {"name": "Renamed"})
    assert response.status_code == 302
    project.refresh_from_db()
    assert project.name == "Renamed" and project.last_edited_by == client.user


def test_submit_refuses_an_incomplete_project(setup):
    event, team, project, client = setup
    client.post(f"/participant/projects/{project.pk}/submit")
    project.refresh_from_db()
    assert project.status == Status.DRAFT


def test_submit_a_complete_project_and_withdraw_it(setup):
    event, team, project, client = setup
    client.post(f"/participant/projects/{project.pk}/", complete_form(project))
    client.post(f"/participant/projects/{project.pk}/submit")
    project.refresh_from_db()
    assert project.status == Status.SUBMITTED and project.submitted_at
    client.post(f"/participant/projects/{project.pk}/unsubmit")
    project.refresh_from_db()
    assert project.status == Status.DRAFT and project.submitted_at is None


def test_track_is_required_when_the_event_has_tracks(setup):
    event, team, project, client = setup
    track = Track.objects.create(event=event, name="Security")
    client.post(f"/participant/projects/{project.pk}/", complete_form(project))
    client.post(f"/participant/projects/{project.pk}/submit")
    project.refresh_from_db()
    assert project.status == Status.DRAFT
    client.post(f"/participant/projects/{project.pk}/", complete_form(project, track=track.pk))
    client.post(f"/participant/projects/{project.pk}/submit")
    project.refresh_from_db()
    assert project.status == Status.SUBMITTED


def test_a_track_from_another_event_is_refused(setup, make_event):
    event, team, project, client = setup
    foreign = Track.objects.create(event=make_event(), name="Elsewhere")
    response = client.post(f"/participant/projects/{project.pk}/", complete_form(project, track=foreign.pk))
    assert response.status_code == 400


def test_a_submitted_project_cannot_be_edited_into_an_incomplete_one(setup):
    event, team, project, client = setup
    client.post(f"/participant/projects/{project.pk}/", complete_form(project))
    client.post(f"/participant/projects/{project.pk}/submit")
    response = client.post(f"/participant/projects/{project.pk}/", complete_form(project, description=""))
    assert response.status_code == 400
    project.refresh_from_db()
    assert project.description


def test_every_team_member_can_edit(setup, make_user, client_for):
    event, team, project, client = setup
    from teams.models import TeamMember

    member = make_user()
    TeamMember.objects.create(team=team, user=member)
    client_for(member).post(f"/participant/projects/{project.pk}/", {"name": "By member"})
    project.refresh_from_db()
    assert project.name == "By member" and project.last_edited_by == member


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://example.org/x", "data:text/html,hi"])
def test_links_must_be_web_urls(setup, url):
    event, team, project, client = setup
    response = client.post(f"/participant/projects/{project.pk}/", complete_form(project, repo_url=url))
    assert response.status_code == 400


# --- tags and answers ------------------------------------------------------------------------


def test_tags_are_normalised(setup):
    event, team, project, client = setup
    client.post(f"/participant/projects/{project.pk}/", complete_form(project, tags="Python, django ,PYTHON,  Machine   Learning"))
    assert list(project.tags.values_list("name", flat=True)) == ["django", "machine learning", "python"]


def test_at_most_fifty_tags(setup):
    event, team, project, client = setup
    tags = ", ".join(f"t{i}" for i in range(51))
    assert client.post(f"/participant/projects/{project.pk}/", complete_form(project, tags=tags)).status_code == 400
    ok = ", ".join(f"t{i}" for i in range(50))
    assert client.post(f"/participant/projects/{project.pk}/", complete_form(project, tags=ok)).status_code == 302
    assert project.tags.count() == 50


def test_required_question_blocks_submission_until_answered(setup):
    event, team, project, client = setup
    question = CustomQuestion.objects.create(event=event, prompt="What did you cut?", kind="long", required=True)
    client.post(f"/participant/projects/{project.pk}/", complete_form(project))
    client.post(f"/participant/projects/{project.pk}/submit")
    project.refresh_from_db()
    assert project.status == Status.DRAFT
    client.post(f"/participant/projects/{project.pk}/", complete_form(project, **{f"q_{question.pk}": "Auth"}))
    client.post(f"/participant/projects/{project.pk}/submit")
    project.refresh_from_db()
    assert project.status == Status.SUBMITTED
    assert Answer.objects.get(project=project, question=question).value == "Auth"


def test_choice_answer_must_be_one_of_the_options(setup):
    event, team, project, client = setup
    question = CustomQuestion.objects.create(event=event, prompt="Pick", kind="choice", choices="a\nb")
    response = client.post(f"/participant/projects/{project.pk}/", complete_form(project, **{f"q_{question.pk}": "c"}))
    assert response.status_code == 400


# --- images ------------------------------------------------------------------------------------


def upload(client, project, content, name="shot.png", caption=""):
    return client.post(
        f"/participant/projects/{project.pk}/images",
        {"image": SimpleUploadedFile(name, content, content_type="image/png"), "caption": caption},
    )


@pytest.fixture(autouse=True)
def media_root(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path


def test_uploaded_images_are_reencoded_without_metadata_or_trailing_bytes(setup):
    event, team, project, client = setup
    upload(client, project, png_bytes(exif=True, trailing=b"<html><script>alert(1)</script>"), name="photo.jpg")
    stored = ProjectImage.objects.get(project=project)
    data = stored.image.read()
    assert b"<script>" not in data
    assert not Image.open(io.BytesIO(data)).getexif()
    assert stored.image.name.startswith("projects/") and "photo" not in stored.image.name


def test_non_images_are_refused_whatever_their_name(setup):
    event, team, project, client = setup
    upload(client, project, b"<html><script>alert(1)</script></html>", name="evil.png")
    assert not ProjectImage.objects.exists()


@override_settings(MAX_IMAGE_BYTES=1000)
def test_large_images_are_refused(setup):
    event, team, project, client = setup
    upload(client, project, png_bytes(size=(400, 400)) + b"\0" * 2000)
    assert not ProjectImage.objects.exists()


@override_settings(MAX_PROJECT_IMAGES=2)
def test_image_count_is_capped(setup):
    event, team, project, client = setup
    for _ in range(3):
        upload(client, project, png_bytes())
    assert project.images.count() == 2


def test_thumbnail_upload_and_removal(setup):
    event, team, project, client = setup
    data = complete_form(project)
    data["thumbnail_upload"] = SimpleUploadedFile("t.png", png_bytes(), content_type="image/png")
    client.post(f"/participant/projects/{project.pk}/", data)
    project.refresh_from_db()
    name = project.thumbnail.name
    assert name and project.thumbnail.storage.exists(name)
    client.post(f"/participant/projects/{project.pk}/", complete_form(project, remove_thumbnail="on"))
    project.refresh_from_db()
    assert not project.thumbnail and not project.thumbnail.storage.exists(name)


def test_draft_images_are_private_submitted_ones_public(setup, make_user, client_for):
    event, team, project, client = setup
    upload(client, project, png_bytes())
    url = "/media/" + ProjectImage.objects.get().image.name
    assert client.get(url).status_code == 200                       # team member
    assert client_for(event.organizer).get(url).status_code == 200   # organizer of the event
    assert Client().get(url).status_code == 404                      # stranger
    assert client_for(make_user()).get(url).status_code == 404       # another participant
    client.post(f"/participant/projects/{project.pk}/", complete_form(project))
    client.post(f"/participant/projects/{project.pk}/submit")
    assert Client().get(url).status_code == 200


# --- markdown ------------------------------------------------------------------------------------


def test_markdown_is_rendered_and_sanitised():
    html = str(render(
        "## Title\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1)) [ok](https://example.org)\n\n"
        "![img](https://evil.example/pixel.png)"
    ))
    assert "<h2>Title</h2>" in html
    assert "<script>" not in html
    assert "href=\"javascript" not in html  # left as inert text, never a link
    assert 'href="https://example.org"' in html and 'rel="nofollow noopener noreferrer"' in html
    assert "<img" not in html


# --- minimum team size -------------------------------------------------------------------------


def test_a_team_below_the_minimum_cannot_submit(setup, make_user):
    from teams.models import TeamMember

    event, team, project, client = setup
    event.min_team_size = 2
    event.save()
    client.post(f"/participant/projects/{project.pk}/", complete_form(project))
    client.post(f"/participant/projects/{project.pk}/submit")
    project.refresh_from_db()
    assert project.status == Status.DRAFT
    TeamMember.objects.create(team=team, user=make_user())
    client.post(f"/participant/projects/{project.pk}/submit")
    project.refresh_from_db()
    assert project.status == Status.SUBMITTED


def test_a_submitted_team_cannot_shrink_below_the_minimum(setup, make_user, client_for):
    from teams.models import TeamMember

    event, team, project, client = setup
    event.min_team_size = 2
    event.save()
    member = make_user()
    TeamMember.objects.create(team=team, user=member)
    client.post(f"/participant/projects/{project.pk}/", complete_form(project))
    client.post(f"/participant/projects/{project.pk}/submit")
    client_for(member).post(f"/participant/teams/{team.pk}/leave")
    assert team.members.filter(user=member).exists()
    row = team.members.get(user=member)
    client.post(f"/participant/teams/{team.pk}/members/{row.pk}/remove")
    assert team.members.filter(user=member).exists()
