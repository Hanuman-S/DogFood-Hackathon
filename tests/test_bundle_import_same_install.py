"""Importing an event bundle (C1c, second half): into the install it came from (the same bundle twice),
by an event creator (placeholder accounts), with a changed slug (the cross-validation seed pinned), and
with ids of rows deleted before the export (missing-*), plus the integrity page for imported ballots.

The seeded demo stays in the database for this whole module (the source event must exist for a
same-install import); each test imports inside its own transaction, rolled back after it, and the
module flushes the database when it is done."""

import io
import json
import os
import zipfile
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from accounts.models import User
from accounts.roles import Role
from core import audit
from core.models import AuditAction, AuditLog
from events.models import Event, EventMembership
from imports import bundle, bundle_import
from imports.models import FixtureRef
from projects.models import Project, Status
from scoring.models import EventScoringConfig

TOKENS = {"admin": "t-admin", "organizer": "t-org", "judge_a": "t-ja", "judge_b": "t-jb", "participant": "t-p"}
ARCHIVE, FIXTURE = "dogfood-archive-2026", "sample-hack-2026"


@pytest.fixture(scope="module")
def world(django_db_setup, django_db_blocker, tmp_path_factory):
    from events.models import EventMembership as Membership
    from imports.fixtures import import_file
    from scoring import services as scoring
    from scoring.models import ResultSnapshot
    from test_bundle_export import legacy_remove_judge
    from voting import services as voting
    from voting.models import Ballot

    media = tmp_path_factory.mktemp("media")
    out = {}
    with django_db_blocker.unblock(), override_settings(DEMO_MODE=True, DEMO_TOKENS=TOKENS, MEDIA_ROOT=str(media)):
        call_command("seed_demo", stdout=StringIO())
        import_file()
        call_command("seed_demo", "--votes", stdout=StringIO())
        organizer = User.objects.get(email="organizer@dogfood.local")
        fixture, archive = Event.objects.get(slug=FIXTURE), Event.objects.get(slug=ARCHIVE)
        final = scoring.compute_snapshot(fixture, "final", actor=organizer)
        scoring.publish_results(fixture, final.pk, actor=organizer)
        cluster = Ballot.objects.filter(event=archive, voter_user__email__startswith="voter.cluster").first()
        voting.void_ballot(archive, cluster.pk, actor=organizer, reason="cluster")
        voting.end_voting_now(archive, actor=organizer)
        for slug in (FIXTURE, ARCHIVE):
            path = bundle.export_event(Event.objects.get(slug=slug), actor=None)
            out[slug] = open(path, "rb").read()
            os.unlink(path)
        # LEGACY / PRE-GUARD data: a judge the published final names, removed with their reviews.
        legacy_remove_judge(Membership.objects.get(pk=int(final.result["judges"][0]["judge_id"])))
        path = bundle.export_event(fixture, actor=None)
        out["missing"] = open(path, "rb").read()
        os.unlink(path)
        assert ResultSnapshot.objects.filter(event=fixture).exists()
    yield out
    with django_db_blocker.unblock():
        call_command("flush", interactive=False, verbosity=0)


@pytest.fixture
def media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")


def run_import(data, tmp_path, actor):
    path = tmp_path / "b.zip"
    path.write_bytes(data)
    return bundle_import.import_event(str(path), actor=actor)


def admin(email="importing.admin@example.org"):
    return User.objects.create_user(email, None, name="Importing Admin", is_platform_admin=True)


# --- the same bundle twice, on the install it came from ------------------------------------------------------

@pytest.mark.django_db
def test_the_same_bundle_twice_gives_two_new_events_with_their_own_refs(world, media, tmp_path):
    importer = admin()
    first = run_import(world[FIXTURE], tmp_path, importer)
    second = run_import(world[FIXTURE], tmp_path, importer)
    assert (first.slug, second.slug) == (f"{FIXTURE}-2", f"{FIXTURE}-3")
    sources = {e.slug: set(FixtureRef.objects.filter(event=e).values_list("source", flat=True))
               for e in (first, second)}
    assert sources[first.slug] != sources[second.slug]
    assert all(len(s) == 1 and next(iter(s)).endswith(slug) for slug, s in sources.items())


@pytest.mark.django_db
def test_every_fixture_ref_lookup_still_resolves_to_exactly_one_row(world, media, tmp_path, client_for):
    """Every lookup in the code (FixtureRef readers), after the same bundle was imported twice next to
    its source. `.get()` raises MultipleObjectsReturned where a lookup would be ambiguous."""
    from imports.fixtures import SOURCE, Importer
    from scoring.services import build_input, display_labels

    importer = admin()
    copies = [run_import(world[FIXTURE], tmp_path, importer) for _ in range(2)]
    events = [Event.objects.get(slug=FIXTURE), *copies]

    # judge/api.py: kind judge + external id (install-wide): the bundle never carries judge refs
    assert FixtureRef.objects.get(kind=FixtureRef.Kind.JUDGE, external_id="jdg_02")
    # seed_demo.py: kind event + external id
    assert FixtureRef.objects.get(kind=FixtureRef.Kind.EVENT, external_id="evt_01").event.slug == FIXTURE
    # imports/fixtures.py Importer.ref: source + kind + external id
    assert Importer({}).ref("project", "prj_07").event.slug == FIXTURE
    assert FixtureRef.objects.get(source=SOURCE, kind="project", external_id="prj_41").duplicate_of
    for event in events:
        # scoring/services.py build_input and display_labels: by event
        assert FixtureRef.objects.get(event=event, kind="project", external_id="prj_07")
        assert FixtureRef.objects.get(event=event, kind="project", external_id="prj_41").duplicate_of == "prj_07"
        inp = build_input(event)
        assert list(inp.duplicates) == ["dup:prj_41"]
        labels = display_labels(event, inp)
        assert sum("(prj_07)" in name for name in labels["projects"].values()) == 1
        # organizer/views.py: kind project + object_id in the event's projects
        own = FixtureRef.objects.filter(kind="project", object_id__in=event.projects.values("pk"))
        assert own.count() == 41 and set(own.values_list("event", flat=True)) == {event.pk}
    # the fixture importer run again finds its own refs and creates nothing
    before = (Event.objects.count(), Project.objects.count())
    from imports.fixtures import import_file
    import_file()
    assert (Event.objects.count(), Project.objects.count()) == before
    # the judge API resolves a fixture judge id without ambiguity
    organizer = User.objects.get(email="organizer@dogfood.local")
    organizer.set_password("correct-horse-battery")
    organizer.save()
    response = client_for(organizer).get("/api/judge/scores?judge=jdg_02")
    assert response.status_code == 200


@pytest.mark.django_db
def test_the_gallery_still_shows_each_project_once(world, media, tmp_path):
    before = Client().get("/api/projects?event=" + FIXTURE).json()["count"]
    total_before = Client().get("/api/projects").json()["count"]
    importer = admin()
    run_import(world[FIXTURE], tmp_path, importer)
    run_import(world[FIXTURE], tmp_path, importer)
    assert Client().get("/api/projects?event=" + FIXTURE).json()["count"] == before == 40
    assert Client().get("/api/projects").json()["count"] == total_before  # imports arrive unpublished
    listing = Client().get("/api/projects").json()
    assert listing["pages"] == 1  # 45 projects: one page, so every project is in this one listing
    ids = [p["id"] for p in listing["results"]]
    assert len(ids) == len(set(ids)) == total_before


# --- the cross-validation seed -------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_changed_slug_pins_the_source_seed_and_the_audit_row_records_it(world, media, tmp_path):
    from scoring.engine.config import seed_for

    event = run_import(world[FIXTURE], tmp_path, admin())
    assert event.slug != FIXTURE
    config = EventScoringConfig.objects.get(event=event)
    assert config.overrides["cv_seed"] == seed_for(FIXTURE)
    entry = AuditLog.objects.get(action=AuditAction.EVENT_IMPORTED, subject=event.slug)
    assert entry.detail["cv_seed_pinned"] == seed_for(FIXTURE)


# --- an event creator's import: placeholder accounts ----------------------------------------------------

@pytest.mark.django_db
def test_an_event_creator_gets_placeholders_and_attaches_no_existing_account(world, media, tmp_path):
    creator = User.objects.get(email="organizer@dogfood.local")  # may create events; not an admin
    assert creator.can_create_events and not creator.is_platform_admin
    existing = set(User.objects.values_list("pk", flat=True))
    event = run_import(world[FIXTURE], tmp_path, creator)
    members = EventMembership.objects.filter(event=event)
    attached = set(members.values_list("user", flat=True))
    assert attached & existing == {creator.pk}  # only the importer, as the new event's organizer
    assert members.filter(user=creator).get().role == Role.ORGANIZER
    placeholders = User.objects.exclude(pk__in=existing)
    assert placeholders.exists() and all(u.email.endswith("@import.invalid") for u in placeholders)
    assert not any(u.has_usable_password() for u in placeholders)
    assert not placeholders.filter(is_platform_admin=True).exists()
    body = json.loads(zipfile.ZipFile(io.BytesIO(world[FIXTURE])).read("event.json"))
    assert placeholders.count() == len(body["users"])
    assert set(placeholders.values_list("name", flat=True)) <= {u["name"] for u in body["users"]}
    entry = AuditLog.objects.get(action=AuditAction.EVENT_IMPORTED, subject=event.slug)
    assert entry.detail["placeholder_accounts"] is True


# --- ids of rows deleted before the export ----------------------------------------------------------------

@pytest.mark.django_db
def test_an_imported_event_with_missing_ids_renders_its_pages(world, media, tmp_path, client_for):
    assert b"missing-memberships#1" in zipfile.ZipFile(io.BytesIO(world["missing"])).read("event.json")
    importer = admin()
    importer.set_password("correct-horse-battery")
    importer.save()
    event = run_import(world["missing"], tmp_path, importer)
    client = client_for(importer)
    for url in (f"/organizer/events/{event.slug}/results", f"/organizer/events/{event.slug}/voting/integrity",
                f"/events/{event.slug}/results", f"/organizer/events/{event.slug}/"):
        response = client.get(url)
        assert response.status_code == 200, url


@pytest.mark.django_db
def test_a_missing_project_in_a_published_final_still_renders(world, media, tmp_path, client_for):
    from test_bundle_import_validation import edit_body, rezip

    files = {i.filename: zipfile.ZipFile(io.BytesIO(world[FIXTURE])).read(i.filename)
             for i in zipfile.ZipFile(io.BytesIO(world[FIXTURE])).infolist()}

    def missing_project(body):
        final = body["result_snapshots"][-1]
        final["result"]["projects"][0]["project_id"] = "missing-projects#1"
        final["comparison"]["rows"][0]["project_id"] = "missing-projects#1"
    importer = admin()
    importer.set_password("correct-horse-battery")
    importer.save()
    event = run_import(rezip(edit_body(files, missing_project)), tmp_path, importer)
    client = client_for(importer)
    assert client.get(f"/organizer/events/{event.slug}/results").status_code == 200
    assert client.get(f"/events/{event.slug}/results").status_code == 200


# --- the integrity page for imported ballots -------------------------------------------------------------

@pytest.mark.django_db
def test_imported_ballots_say_ip_clustering_is_unavailable(world, media, tmp_path, client_for):
    importer = admin()
    importer.set_password("correct-horse-battery")
    importer.save()
    event = run_import(world[ARCHIVE], tmp_path, importer)
    client = client_for(importer)
    html = client.get(f"/organizer/events/{event.slug}/voting/integrity").content.decode()
    assert "IP-based clustering unavailable: ballots were imported" in html
    assert "nothing flagged" not in html
    source = client.get(f"/organizer/events/{ARCHIVE}/voting/integrity").content.decode()
    assert "IP-based clustering unavailable" not in source  # the source's own ballots have hashes


# --- the audit write path ----------------------------------------------------------------------------------

@pytest.mark.django_db
def test_audit_record_refuses_a_source_history_key():
    with pytest.raises(ValueError):
        audit.record(AuditAction.COMMENT_POSTED, subject="x", source_history=True)
    with pytest.raises(ValueError):
        audit.record(AuditAction.COMMENT_POSTED, subject="x", source_history=False)
    assert not AuditLog.objects.filter(subject="x").exists()
