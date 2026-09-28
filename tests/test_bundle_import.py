"""Importing an event bundle (C1c, first half): the admin write path and the fresh-install round trip.

The source world is the seeded demo, built once for this module and exported, then the database is
flushed (TRUNCATE: no row triggers fire) -- a fresh install:
* Sample Hack 2026: 40 projects, 123 reviews, its folded duplicate (fixture refs), a closed vote, a
  published final;
* Dogfood Archive 2026: five projects (one with a thumbnail), assignments, comments, and its quadratic
  vote with ten ballots (one voided here) -- ended early so the bundle can move (voting_in_progress
  otherwise), and a preview.
Each test imports into that empty install with every sequence moved to 10000, so a new primary key
never equals its source one by accident."""

import io
import json
import os
import zipfile
from io import StringIO

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import IntegrityError, connection
from django.test import override_settings
from PIL import Image

from accounts.models import User
from accounts.roles import ADMIN, Role
from core.models import AuditAction, AuditLog
from events.models import Event, EventMembership
from imports import bundle, bundle_import
from imports.models import FixtureRef
from projects.models import Project
from scoring.models import Publication, ResultSnapshot, Score
from teams.models import Team
from voting.models import Ballot, VoteTallySnapshot, VotingConfig

TOKENS = {"admin": "t-admin", "organizer": "t-org", "judge_a": "t-ja", "judge_b": "t-jb", "participant": "t-p"}
ARCHIVE, FIXTURE = "dogfood-archive-2026", "sample-hack-2026"


def ranking(event):
    from scoring import results
    snapshot = Publication.objects.filter(event=event, unpublished_at__isnull=True).get().snapshot
    rows, _ = results.build_rows(event, snapshot)
    return [(r.project.name, r.display_rank, r.tie_group) for r in rows]


@pytest.fixture(scope="module")
def source(django_db_setup, django_db_blocker, tmp_path_factory):
    from core.deadlines import deadline_bypass
    from django.core.files.base import ContentFile
    from imports.fixtures import import_file
    from projects.images import clean_image
    from scoring import services as scoring
    from voting import services as voting

    media = tmp_path_factory.mktemp("media")
    out = {"media": str(media)}
    with django_db_blocker.unblock(), override_settings(DEMO_MODE=True, DEMO_TOKENS=TOKENS, MEDIA_ROOT=str(media)):
        try:
            call_command("seed_demo", stdout=StringIO())
            import_file()
            call_command("seed_demo", "--votes", stdout=StringIO())
            organizer = User.objects.get(email="organizer@dogfood.local")
            fixture, archive = Event.objects.get(slug=FIXTURE), Event.objects.get(slug=ARCHIVE)
            final = scoring.compute_snapshot(fixture, "final", actor=organizer)
            scoring.publish_results(fixture, final.pk, actor=organizer)
            project = Project.objects.filter(event=archive).order_by("pk").first()
            buffer = io.BytesIO()
            Image.new("RGB", (48, 32), (200, 40, 90)).save(buffer, "PNG")
            with deadline_bypass(None, "test image"):
                project.thumbnail.save("t.png", clean_image(ContentFile(buffer.getvalue(), name="t.png")), save=True)
            cluster = Ballot.objects.filter(event=archive, voter_user__email__startswith="voter.cluster").first()
            voting.void_ballot(archive, cluster.pk, actor=organizer, reason="cluster")
            voting.end_voting_now(archive, actor=organizer)
            scoring.compute_snapshot(archive, "preview", actor=organizer)
            out["ranking"] = ranking(fixture)
            out["pks"] = {"event": fixture.pk, "projects": set(Project.objects.filter(event=fixture)
                                                                .values_list("pk", flat=True))}
            out["ballot_secret"] = VotingConfig.objects.get(event=archive).ballot_secret
            out["invite_tokens"] = set(Team.objects.values_list("invite_token", flat=True))
            out["organizer"] = {"email": organizer.email, "name": organizer.name}
            for slug in (FIXTURE, ARCHIVE):
                path = bundle.export_event(Event.objects.get(slug=slug), actor=None)
                with open(path, "rb") as fh:
                    out[slug] = fh.read()
                os.unlink(path)
        finally:
            call_command("flush", interactive=False, verbosity=0)
    return out


@pytest.fixture
def install(source, transactional_db, tmp_path, settings):
    """A fresh install: empty, every sequence at 10000, media in a new directory."""
    settings.MEDIA_ROOT = str(tmp_path / "media")
    with connection.cursor() as cursor:
        for model in apps.get_models():
            if model._meta.pk.get_internal_type() in ("AutoField", "BigAutoField"):
                cursor.execute("SELECT setval(pg_get_serial_sequence(%s, %s), 10000)",
                               [model._meta.db_table, model._meta.pk.column])
    return tmp_path


def admin_like_source_organizer(source):
    """The importer: an admin whose email and name are the source organizer's, so the round trip adds
    no membership and no account (anything else is tested separately)."""
    return User.objects.create_user(source["organizer"]["email"], None, name=source["organizer"]["name"],
                                    is_platform_admin=True)


def import_bytes(data, tmp_path, actor):
    path = tmp_path / "in.zip"
    path.write_bytes(data)
    return bundle_import.import_event(str(path), actor=actor)


def files_of(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {i.filename: z.read(i.filename) for i in z.infolist()}


def export_bytes(event):
    path = bundle.export_event(event, actor=None)
    try:
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(path)


IMPORT_MADE = {AuditAction.EVENT_IMPORTED, AuditAction.DEADLINE_BYPASSED, AuditAction.VOTING_BYPASSED,
               AuditAction.WEIGHTS_BYPASSED}


def normalised(data):
    """event.json of a bundle with what an import legitimately changes made neutral:
    * `event.is_published` (an import always arrives unpublished);
    * media paths and bytes (images are re-encoded): each path becomes media#<n>, first-seen order;
    * voter pseudonyms (salted per export): each becomes voter#<n>, first-seen order;
    * fixture ref sources (dogfood-fixtures -> bundle:<sha>:<slug>);
    * audit rows the import itself wrote, and the source_history mark on the imported ones."""
    files = files_of(data)
    body = json.loads(files["event.json"])
    body["event"]["is_published"] = None
    for ref in body["fixture_refs"]:
        ref["source"] = None
    body["audit"] = [row for row in body["audit"]
                     if not (row["action"] in IMPORT_MADE and (row["action"] == AuditAction.EVENT_IMPORTED
                             or str(row["detail"].get("reason", "")).startswith("event import")))]
    for n, row in enumerate(body["audit"], start=1):
        row["id"] = f"audit#{n}"
        row["detail"].pop("source_history", None)
    text = json.dumps(body, sort_keys=True, ensure_ascii=False)
    for pattern, label in ((r"media/[0-9a-f]{64}\.(?:jpg|png|webp)", "media"), (r"v_[0-9a-f]{16}", "voter")):
        import re
        seen = {}
        text = re.sub(pattern, lambda m: seen.setdefault(m.group(0), f"{label}#{len(seen) + 1}"), text)
    return json.loads(text), {n: Image.open(io.BytesIO(d)).size for n, d in files.items() if n.startswith("media/")}


# --- the fresh-install round trip -------------------------------------------------------------------------

@pytest.mark.parametrize("slug", [FIXTURE, ARCHIVE])
def test_export_import_export_gives_the_same_bundle(source, install, slug):
    importer = admin_like_source_organizer(source)
    event = import_bytes(source[slug], install, importer)
    assert event.slug == slug  # a fresh install: no clash, the slug is kept
    first, first_images = normalised(source[slug])
    second, second_images = normalised(export_bytes(event))
    assert first == second
    assert sorted(first_images.values()) == sorted(second_images.values())


def test_new_primary_keys_differ_from_the_source(source, install):
    event = import_bytes(source[FIXTURE], install, admin_like_source_organizer(source))
    assert event.pk != source["pks"]["event"] and event.pk > 10000
    new = set(Project.objects.filter(event=event).values_list("pk", flat=True))
    assert len(new) == 40 and not new & source["pks"]["projects"]


def test_the_imported_results_show_the_same_projects_ranks_and_ties(source, install):
    event = import_bytes(source[FIXTURE], install, admin_like_source_organizer(source))
    assert ranking(event) == source["ranking"]
    assert ResultSnapshot.objects.filter(event=event).exclude(imported_from=None).count() == \
        ResultSnapshot.objects.filter(event=event).count() > 0
    assert not ResultSnapshot.objects.filter(event=event).exclude(imported_from=event_sha(event)).exists()


def event_sha(event):
    return AuditLog.objects.get(action=AuditAction.EVENT_IMPORTED, subject=event.slug).detail["sha256"]


# --- what the import decides ---------------------------------------------------------------------------------

def test_an_imported_event_arrives_unpublished_and_the_audit_row_says_what_it_was(source, install):
    event = import_bytes(source[FIXTURE], install, admin_like_source_organizer(source))
    assert event.is_published is False
    entry = AuditLog.objects.get(action=AuditAction.EVENT_IMPORTED)
    assert entry.detail["source_published"] is True and entry.detail["source_event"] == FIXTURE
    assert entry.subject == event.slug and len(entry.detail["sha256"]) == 64


def test_secrets_are_made_new(source, install):
    event = import_bytes(source[ARCHIVE], install, admin_like_source_organizer(source))
    config = VotingConfig.objects.get(event=event)
    assert len(config.ballot_secret) == 64 and config.ballot_secret != source["ballot_secret"]
    assert config.open_link_nonce
    tokens = set(Team.objects.filter(event=event).values_list("invite_token", flat=True))
    assert len(tokens) == 5 and not tokens & source["invite_tokens"]


def test_ballots_arrive_as_pseudonymous_voters_with_no_address(source, install):
    event = import_bytes(source[ARCHIVE], install, admin_like_source_organizer(source))
    ballots = Ballot.objects.filter(event=event)
    assert ballots.count() == 10 and ballots.filter(voided_at__isnull=False).count() == 1
    assert not ballots.filter(voter_user__isnull=False).exists()
    assert not ballots.filter(voter_link__isnull=False).exists()
    assert all(b.voter_cookie.startswith("v_") for b in ballots)
    assert not ballots.exclude(ip_hash="").exists() and not ballots.exclude(created_ip_hash="").exists()


def test_missing_accounts_are_created_without_a_usable_password(source, install):
    import_bytes(source[FIXTURE], install, admin_like_source_organizer(source))
    created = User.objects.exclude(email=source["organizer"]["email"])
    assert created.count() > 100 and not any(u.has_usable_password() for u in created)
    assert not created.filter(is_platform_admin=True).exists()
    assert not created.filter(can_create_events=True).exists()


def test_an_admin_who_is_not_in_the_bundle_becomes_its_organizer(source, install, make_user):
    admin = make_user(role=ADMIN, email="someone.else@example.org")
    event = import_bytes(source[FIXTURE], install, admin)
    assert EventMembership.objects.filter(event=event, user=admin, role=Role.ORGANIZER).exists()


def test_fixture_refs_are_filed_under_this_import(source, install):
    from scoring.services import build_input

    event = import_bytes(source[FIXTURE], install, admin_like_source_organizer(source))
    refs = FixtureRef.objects.filter(event=event)
    assert refs.count() == 164
    assert set(refs.values_list("source", flat=True)) == {f"bundle:{event_sha(event)}:{event.slug}"}
    assert list(build_input(event).duplicates) == ["dup:prj_41"]  # the folded duplicate survives the move


def test_the_imported_audit_rows_are_labelled_source_history(source, install):
    event = import_bytes(source[FIXTURE], install, admin_like_source_organizer(source))
    history = AuditLog.objects.filter(detail__source_history=True)
    assert history.exists() and not history.exclude(ip_hash="").exists()
    assert not history.filter(subject=FIXTURE).exclude(subject=event.slug).exists()


# --- who, and all or nothing -----------------------------------------------------------------------------------

def test_a_participant_of_the_event_cannot_import_it_as_its_organizer(source, install):
    body = json.loads(files_of(source[FIXTURE])["event.json"])
    participant_user = next(m["user"] for m in body["memberships"] if m["role"] == "participant")
    email = next(u["email"] for u in body["users"] if u["id"] == participant_user)
    admin = User.objects.create_user(email, None, name="Competitor", is_platform_admin=True)
    with pytest.raises(bundle.BundleError) as caught:
        import_bytes(source[FIXTURE], install, admin)
    assert (caught.value.status, caught.value.code) == (400, "importer_is_competitor")
    assert not Event.objects.exists()


def test_a_kept_slug_pins_no_seed(source, install):
    """The fresh-install round trip keeps the slug, so M2 derives the same seed by itself: no override."""
    from scoring.models import EventScoringConfig

    event = import_bytes(source[FIXTURE], install, admin_like_source_organizer(source))
    config = EventScoringConfig.objects.filter(event=event).first()
    assert config is None or "cv_seed" not in config.overrides
    assert AuditLog.objects.get(action=AuditAction.EVENT_IMPORTED).detail["cv_seed_pinned"] is None


def test_a_recompute_under_a_changed_slug_ranks_as_the_source_did(source, install):
    """Import twice: the second copy's slug gets a suffix, its seed is pinned, and a new preview ranks
    every project exactly as the source's published final did."""
    from scoring import results
    from scoring import services as scoring

    importer = admin_like_source_organizer(source)
    import_bytes(source[FIXTURE], install, importer)
    copy = import_bytes(source[FIXTURE], install, importer)
    assert copy.slug == f"{FIXTURE}-2"
    preview = scoring.compute_snapshot(copy, "preview", actor=importer)
    rows, _ = results.build_rows(copy, preview)
    assert [(r.project.name, r.display_rank, r.tie_group) for r in rows] == source["ranking"]


def row_counts():
    return {m._meta.label: m.objects.count() for m in apps.get_models()}


def _media_files(root):
    return [name for _, _, names in os.walk(root) for name in names] if os.path.isdir(root) else []


def test_a_failure_after_the_reviews_are_in_rolls_everything_back(source, install, monkeypatch):
    """Scores go in long before publications; a database error there must leave no trace but one audit
    row: an event with reviews could never be deleted afterwards."""
    importer = admin_like_source_organizer(source)
    before = row_counts()
    reached = {}

    def refuse(self, *args, **kwargs):
        reached["scores"] = Score.objects.count()
        raise IntegrityError("forced: a later section fails")
    monkeypatch.setattr(Publication, "save", refuse)
    with pytest.raises(bundle.BundleError) as caught:
        import_bytes(source[FIXTURE], install, importer)
    monkeypatch.undo()
    assert reached["scores"] == 123  # the failure really came after the reviews were written
    assert caught.value.code == "import_conflict" and caught.value.status == 400
    after = row_counts()
    assert after.pop("core.AuditLog") == before.pop("core.AuditLog") + 1
    assert after == before
    assert AuditLog.objects.get().action == AuditAction.EVENT_IMPORT_REFUSED


def test_a_failed_import_removes_the_images_it_stored(source, install, monkeypatch):
    importer = admin_like_source_organizer(source)

    def refuse(self, *args, **kwargs):
        raise IntegrityError("forced")
    monkeypatch.setattr(ResultSnapshot, "save", refuse)
    with pytest.raises(bundle.BundleError):
        import_bytes(source[ARCHIVE], install, importer)
    assert _media_files(os.path.join(install, "media")) == []
    assert not Project.objects.exists()


# --- the three ways in ------------------------------------------------------------------------------------

def test_the_page_the_api_and_the_command(source, install, client_for, tmp_path):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test import Client

    from conftest import PASSWORD

    importer = admin_like_source_organizer(source)
    importer.set_password(PASSWORD)
    importer.save()
    client = client_for(importer)
    assert client.get("/organizer/events/import").status_code == 200
    response = client.post("/organizer/events/import", {"bundle": SimpleUploadedFile("b.zip", source[FIXTURE])})
    assert response.status_code == 302 and response["Location"].endswith(f"/organizer/events/{FIXTURE}/")
    response = client.post("/api/bundles", {"bundle": SimpleUploadedFile("b.zip", source[FIXTURE])})
    assert response.status_code == 201 and response.json()["slug"] == f"{FIXTURE}-2"
    assert response.json()["published"] is False
    refused = client.post("/api/bundles", {})
    assert (refused.status_code, refused.json()["error"]) == (400, "no_file")
    assert AuditLog.objects.filter(action=AuditAction.EVENT_IMPORT_REFUSED, detail__reason="no_file").exists()
    target = tmp_path / "archive.zip"
    target.write_bytes(source[ARCHIVE])
    out = StringIO()
    call_command("import_event", str(target), "--as", importer.email, stdout=out)
    assert f"imported as {ARCHIVE}" in out.getvalue()
    assert Client().post("/api/bundles", {}).status_code == 401


def test_the_commands_answer_bad_paths_with_an_error_and_leave_nothing(source, install, tmp_path):
    import tempfile
    from django.core.management.base import CommandError

    importer = admin_like_source_organizer(source)
    with pytest.raises(CommandError, match="No file"):
        call_command("import_event", str(tmp_path / "absent.zip"), "--as", importer.email)
    event = import_bytes(source[FIXTURE], install, importer)
    before = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("dogfood-bundle-")}
    with pytest.raises(CommandError, match="does not exist"):
        call_command("export_event", event.slug, str(tmp_path / "no-such-folder" / "x.zip"))
    folder = tmp_path / "a-folder.zip"
    folder.mkdir()
    with pytest.raises(CommandError, match="is a folder"):
        call_command("export_event", event.slug, str(folder))
    locked = tmp_path / "read-only"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(CommandError, match="Could not write"):
            call_command("export_event", event.slug, str(locked / "x.zip"))
    finally:
        locked.chmod(0o700)
    after = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("dogfood-bundle-")}
    assert after == before  # the temporary bundle is removed whatever happens
