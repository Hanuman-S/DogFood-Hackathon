"""The sample event bundle on the import page: a real bundle, imported through the same validation
as an upload, downloadable by the same people, and free of secrets."""

import json
import re
import zipfile

import pytest

from accounts.roles import Role
from events.models import Event
from organizer.bundle import SAMPLE_BUNDLE
from voting.models import Ballot

pytestmark = pytest.mark.django_db


def test_the_sample_is_a_bundle_with_the_whole_demo_and_no_secrets():
    with zipfile.ZipFile(SAMPLE_BUNDLE) as z:
        assert set(z.namelist()) == {"event.json", "manifest.json"}
        manifest = json.loads(z.read("manifest.json"))
        text = z.read("event.json").decode()
    assert manifest["format"] == "dogfood-event-bundle"
    body = json.loads(text)
    assert len(body["projects"]) == 5 and len(body["ballots"]) == 10 and len(body["criteria"]) == 3
    assert all(email.endswith("@dogfood.local") for email in re.findall(r"[\w.+-]+@[\w.-]+", text))
    for word in ("ip_hash", "password", "token", "secret"):
        assert word not in text.lower()


def test_importing_the_sample_makes_a_new_unpublished_event_of_mine(make_user, client_for):
    organizer = make_user(role=Role.ORGANIZER)  # may create events
    before = set(Event.objects.values_list("pk", flat=True))
    response = client_for(organizer).post("/organizer/events/import", {"sample": "1"})
    assert response.status_code == 302
    event = Event.objects.exclude(pk__in=before).get()
    assert response["Location"] == f"/organizer/events/{event.slug}/"
    assert not event.is_published and event.projects.count() == 5
    assert Ballot.objects.filter(event=event).count() == 10
    assert event.memberships.filter(user=organizer, role=Role.ORGANIZER).exists()


def test_the_sample_is_downloadable_by_those_who_may_import_it(make_user, client_for):
    response = client_for(make_user(role=Role.ORGANIZER)).get("/organizer/events/import/sample.zip")
    assert response.status_code == 200 and response["Content-Type"] == "application/zip"
    assert b"".join(response.streaming_content) == SAMPLE_BUNDLE.read_bytes()


def test_a_participant_can_neither_download_nor_import_the_sample(make_user, client_for):
    client = client_for(make_user(role=Role.PARTICIPANT))
    assert client.get("/organizer/events/import/sample.zip").status_code == 403
    assert client.post("/organizer/events/import", {"sample": "1"}).status_code == 403
    assert not Event.objects.exists()


def test_the_import_page_asks_before_importing_the_sample(make_user, client_for):
    page = client_for(make_user(role=Role.ORGANIZER)).get("/organizer/events/import").content.decode()
    assert 'name="sample" value="1"' in page and "permanent event" in page


def test_the_event_page_puts_the_bundle_where_it_is_seen(make_event, client_for):
    event = make_event()
    page = client_for(event.organizer).get(f"/organizer/events/{event.slug}/").content.decode()
    box = page[page.index('class="frame frame--feature" id="bundle"'):]
    assert f'class="btn" href="/organizer/events/{event.slug}/bundle"' in box[:box.index("</section>")]
    # First in its tab, and a tile on the overview.
    tab = page[page.index('id="tab-export"'):]
    assert tab.index('id="bundle"') < tab.index('id="export"')
    assert '>event bundle</a><span class="faint">download the whole event as one zip' in page
