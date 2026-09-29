"""Import validation (C1b): every way a bundle is refused before anything is written. Each refusal is
a 400 with a stable code (403 for an account that may not import at all), audited, and changes no
other table. The import's write path is C1c.

The bundles are real exports of the seeded demo (built once for this module, then the database is
flushed): Sample Hack 2026 (closed vote, no ballots) and Dogfood Archive 2026 (open vote with ten
ballots, one project image). Each test tampers with a copy."""

import hashlib
import io
import json
import os
import struct
import zipfile
from io import StringIO

import pytest
from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from PIL import Image

from accounts.roles import ADMIN, Role
from core.models import AuditAction, AuditLog
from imports import bundle, bundle_validate
from imports.bundle_validate import ImportForbidden

TOKENS = {"admin": "t-admin", "organizer": "t-org", "judge_a": "t-ja", "judge_b": "t-jb", "participant": "t-p"}


@pytest.fixture(scope="module")
def bundles(django_db_setup, django_db_blocker, tmp_path_factory):
    from core.deadlines import deadline_bypass
    from django.core.files.base import ContentFile
    from events.models import Event
    from imports.fixtures import import_file
    from projects.images import clean_image
    from projects.models import Project
    from scoring import services as scoring
    from accounts.models import User

    media = tmp_path_factory.mktemp("media")
    out = {}
    with django_db_blocker.unblock(), override_settings(DEMO_MODE=True, DEMO_TOKENS=TOKENS, MEDIA_ROOT=str(media)):
        try:
            call_command("seed_demo", stdout=StringIO())
            import_file()
            call_command("seed_demo", "--votes", stdout=StringIO())
            organizer = User.objects.get(email="organizer@dogfood.local")
            scoring.compute_snapshot(Event.objects.get(slug="sample-hack-2026"), "final", actor=organizer)
            project = Project.objects.filter(event__slug="dogfood-archive-2026").order_by("pk").first()
            buffer = io.BytesIO()
            Image.new("RGB", (40, 30), (10, 200, 90)).save(buffer, "PNG")
            with deadline_bypass(None, "test image"):
                project.thumbnail.save("t.png", clean_image(ContentFile(buffer.getvalue(), name="t.png")), save=True)
            for slug in ("sample-hack-2026", "dogfood-archive-2026"):
                path = bundle.export_event(Event.objects.get(slug=slug), actor=None)
                with zipfile.ZipFile(path) as z:
                    out[slug] = {i.filename: z.read(i.filename) for i in z.infolist()}
                os.unlink(path)
            # The same event after a judge the final names was removed -- LEGACY / PRE-GUARD data,
            # made with explicit deletes (the service now refuses): the real export writes a
            # missing-memberships#n placeholder, which the import must accept.
            from events.models import EventMembership
            from scoring.models import ResultSnapshot
            from test_bundle_export import legacy_remove_judge
            fixture = Event.objects.get(slug="sample-hack-2026")
            final = ResultSnapshot.objects.filter(event=fixture, kind="final").latest("pk")
            legacy_remove_judge(EventMembership.objects.get(pk=int(final.result["judges"][0]["judge_id"])))
            path = bundle.export_event(fixture, actor=None)
            with zipfile.ZipFile(path) as z:
                out["missing-judge"] = {i.filename: z.read(i.filename) for i in z.infolist()}
            os.unlink(path)
        finally:
            call_command("flush", interactive=False, verbosity=0)
    return out


def rezip(files, *, fix_manifest=True, infos=()):
    """Zip `files` (name -> bytes), recomputing the manifest's sha256 list unless told not to.
    `infos`: extra (ZipInfo, bytes) written as they are."""
    files = dict(files)
    if fix_manifest:
        manifest = json.loads(files["manifest.json"])
        manifest["files"] = {n: hashlib.sha256(d).hexdigest() for n, d in files.items() if n != "manifest.json"}
        files["manifest.json"] = bundle.encode(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            z.writestr(name, files[name])
        for info, data in infos:
            z.writestr(info, data)
    return buffer.getvalue()


def edit_body(files, change):
    files = dict(files)
    body = json.loads(files["event.json"])
    change(body)
    files["event.json"] = bundle.encode(body)
    return files


@pytest.fixture
def importer(make_user):
    return make_user(role=ADMIN)


@pytest.fixture
def check(tmp_path, importer):
    def run(data, actor=None):
        path = tmp_path / "bundle.zip"
        path.write_bytes(data)
        return bundle_validate.validate(str(path), actor=actor or importer)
    return run


def counts():
    return {m._meta.label: m.objects.count() for m in apps.get_models() if m._meta.label != "core.AuditLog"}


def refused(check, data, code):
    before, audits = counts(), AuditLog.objects.count()
    with pytest.raises(bundle.BundleError) as caught:
        check(data)
    assert caught.value.code == code, caught.value.detail
    assert caught.value.status == 400
    assert counts() == before, "a refused bundle wrote something"
    assert AuditLog.objects.count() == audits + 1
    entry = AuditLog.objects.latest("pk")
    assert entry.action == AuditAction.EVENT_IMPORT_REFUSED and entry.detail["reason"] == code
    return caught.value


# --- a good bundle ---------------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_real_export_validates(bundles, check):
    result = check(rezip(bundles["sample-hack-2026"], fix_manifest=False))
    assert result.body["event"]["slug"] == "sample-hack-2026" and len(result.body["projects"]) == 40
    assert result.manifest["format"] == "dogfood-event-bundle" and len(result.sha256) == 64


@pytest.mark.django_db
def test_an_account_that_may_create_events_may_import(bundles, check, make_user):
    creator = make_user(role=Role.ORGANIZER)  # can_create_events, not an admin
    assert check(rezip(bundles["sample-hack-2026"]), actor=creator).body


@pytest.mark.django_db
@pytest.mark.parametrize("role", [Role.JUDGE, Role.PARTICIPANT])
def test_a_judge_or_participant_may_not_import_403(bundles, check, make_user, role):
    someone = make_user(role=role)
    before = counts()
    with pytest.raises(ImportForbidden) as caught:
        check(rezip(bundles["sample-hack-2026"]), actor=someone)
    assert (caught.value.status, caught.value.code) == (403, "forbidden")
    assert counts() == before
    assert AuditLog.objects.filter(action=AuditAction.EVENT_IMPORT_REFUSED, detail__reason="forbidden").exists()


@pytest.mark.django_db
def test_images_go_through_the_reencoding_pipeline(bundles, check):
    closed = edit_body(bundles["dogfood-archive-2026"], lambda b: b["voting_config"].update(
        closes_at="2020-01-02T00:00:00+00:00", opens_at="2020-01-01T00:00:00+00:00"))
    result = check(rezip(closed))
    # Every picture in the bundle is re-encoded: this test's own 40x30 thumbnail, and the demo
    # seed's other four thumbnails and ten screenshots (960x540).
    sizes = sorted(Image.open(io.BytesIO(cleaned.read())).size for cleaned in result.images.values())
    assert sizes == [(40, 30)] + [(960, 540)] * 14


# --- the live vote -----------------------------------------------------------------------------------

@pytest.mark.django_db
def test_an_open_vote_with_ballots_is_refused(bundles, check):
    error = refused(check, rezip(bundles["dogfood-archive-2026"]), "voting_in_progress")
    assert "End voting now" in error.detail and "export the event again" in error.detail


@pytest.mark.django_db
def test_an_open_vote_with_no_ballots_imports(bundles, check):
    def drop_ballots(body):
        body["ballots"], body["ballot_lines"] = [], []
    assert check(rezip(edit_body(bundles["dogfood-archive-2026"], drop_ballots))).body["voting_config"]


@pytest.mark.django_db
def test_a_vote_that_has_not_opened_imports(bundles, check):
    def later(body):
        body["ballots"], body["ballot_lines"] = [], []
        body["voting_config"].update(opens_at="2099-01-01T00:00:00+00:00", closes_at="2099-01-02T00:00:00+00:00")
    assert check(rezip(edit_body(bundles["dogfood-archive-2026"], later))).body


# --- the manifest ------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_tampered_checksum_is_refused(bundles, check):
    files = edit_body(bundles["sample-hack-2026"], lambda b: b["event"].update(name="Tampered"))
    refused(check, rezip(files, fix_manifest=False), "checksum_mismatch")


@pytest.mark.django_db
@pytest.mark.parametrize("key,value,code", [("version", 2, "unsupported_version"),
                                            ("format", "something-else", "unknown_format")])
def test_an_unknown_version_or_format_is_refused(bundles, check, key, value, code):
    files = dict(bundles["sample-hack-2026"])
    manifest = json.loads(files["manifest.json"])
    manifest[key] = value
    files["manifest.json"] = bundle.encode(manifest)
    refused(check, rezip(files, fix_manifest=False), code)


@pytest.mark.django_db
def test_an_unlisted_extra_file_is_refused(bundles, check):
    data = b"\x89PNG not really"
    files = dict(bundles["sample-hack-2026"])
    extra = zipfile.ZipInfo(f"media/{hashlib.sha256(data).hexdigest()}.png")
    refused(check, rezip(files, fix_manifest=False, infos=[(extra, data)]), "files_mismatch")


@pytest.mark.django_db
def test_a_listed_file_nobody_refers_to_is_refused(bundles, check):
    buffer = io.BytesIO()
    Image.new("RGB", (5, 5)).save(buffer, "PNG")
    data = buffer.getvalue()
    files = dict(bundles["sample-hack-2026"], **{f"media/{hashlib.sha256(data).hexdigest()}.png": data})
    refused(check, rezip(files), "invalid_bundle")


@pytest.mark.django_db
def test_a_bad_image_is_refused(bundles, check):
    data = b"this is not an image at all"
    name = f"media/{hashlib.sha256(data).hexdigest()}.png"
    files = edit_body(bundles["sample-hack-2026"], lambda b: b["projects"][0].update(thumbnail=name))
    files[name] = data
    refused(check, rezip(files), "bad_image")


@pytest.mark.django_db
def test_a_media_file_not_named_after_its_hash_is_refused(bundles, check):
    buffer = io.BytesIO()
    Image.new("RGB", (5, 5)).save(buffer, "PNG")
    name = "media/" + "0" * 64 + ".png"
    files = edit_body(bundles["sample-hack-2026"], lambda b: b["projects"][0].update(thumbnail=name))
    files[name] = buffer.getvalue()
    refused(check, rezip(files), "checksum_mismatch")


# --- paths inside the zip ---------------------------------------------------------------------------

def _info(name, mode=None):
    info = zipfile.ZipInfo(name)
    if mode is not None:
        info.external_attr = mode << 16
    return info


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["../evil.png", "media/../../etc/passwd", "/abs/event.json", "C:/x.png",
                                  "media\\x.png", "dir/"])
def test_path_traversal_absolute_and_odd_paths_are_refused(bundles, check, name):
    refused(check, rezip(bundles["sample-hack-2026"], fix_manifest=False, infos=[(_info(name), b"x")]), "bad_path")


@pytest.mark.django_db
def test_a_symlink_is_refused(bundles, check):
    link = _info("media/" + "a" * 64 + ".png", mode=0o120777)
    refused(check, rezip(bundles["sample-hack-2026"], fix_manifest=False, infos=[(link, b"/etc/passwd")]), "bad_path")


@pytest.mark.django_db
def test_a_non_nfc_name_is_refused(bundles, check):
    refused(check, rezip(bundles["sample-hack-2026"], fix_manifest=False,
                         infos=[(_info("media/e\u0301.png"), b"x")]), "bad_path")


@pytest.mark.django_db
def test_an_unexpected_file_is_refused(bundles, check):
    refused(check, rezip(bundles["sample-hack-2026"], fix_manifest=False, infos=[(_info("notes.txt"), b"x")]),
            "unexpected_file")


@pytest.mark.django_db
def test_not_a_zip_is_refused(check):
    refused(check, b"PK but not really a zip file", "not_a_zip")


# --- sizes, bombs, lies -----------------------------------------------------------------------------

@pytest.mark.django_db
def test_an_oversized_entry_is_refused_before_it_is_read(bundles, check):
    with override_settings(BUNDLE_MAX_EVENT_JSON_BYTES=1000):
        refused(check, rezip(bundles["sample-hack-2026"]), "entry_too_large")


@pytest.mark.django_db
def test_an_oversized_total_or_zip_is_refused(bundles, check):
    with override_settings(BUNDLE_MAX_TOTAL_BYTES=1000):
        refused(check, rezip(bundles["sample-hack-2026"]), "bundle_too_large")
    with override_settings(BUNDLE_MAX_BYTES=1000):
        refused(check, rezip(bundles["sample-hack-2026"]), "bundle_too_large")


@pytest.mark.django_db
def test_a_zip_bomb_is_refused_from_its_declared_sizes(bundles, check):
    files = dict(bundles["sample-hack-2026"])
    files["event.json"] = files["event.json"] + b" " * (20 * 1024 * 1024)  # still valid JSON, ~1000:1
    refused(check, rezip(files), "zip_bomb")


@pytest.mark.django_db
def test_a_zip_that_lies_about_a_size_is_refused(bundles, check):
    data = bytearray(rezip(bundles["sample-hack-2026"]))
    name = b"event.json"
    at = data.find(b"PK\x01\x02")  # the central directory
    while at != -1:
        name_len = struct.unpack("<H", data[at + 28:at + 30])[0]
        if data[at + 46:at + 46 + name_len] == name:
            data[at + 24:at + 28] = struct.pack("<I", 100)  # declare 100 bytes uncompressed
            break
        at = data.find(b"PK\x01\x02", at + 4)
    assert at != -1
    before, audits = counts(), AuditLog.objects.count()
    with pytest.raises(bundle.BundleError) as caught:
        check(bytes(data))
    # The zip library notices the lie as a bad CRC (or the chunked read as an overrun): either way,
    # refused before anything is used, and nothing written but the audit row.
    assert caught.value.code in ("corrupt_zip", "zip_bomb"), caught.value.detail
    assert counts() == before and AuditLog.objects.count() == audits + 1


# --- the shape of event.json ------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize("label,change", [
    ("an unknown key", lambda b: b["projects"][0].update(secret_extra="x")),
    ("a missing key", lambda b: b["projects"][0].pop("name")),
    ("a reference to nothing", lambda b: b["scores"][0].update(project="projects#999")),
    ("a reference to the wrong section", lambda b: b["scores"][0].update(project="teams#1")),
    ("a wrong type", lambda b: b["projects"][0].update(name=42)),
    ("a bad choice", lambda b: b["projects"][0].update(status="published")),
    ("a bad datetime", lambda b: b["event"].update(starts_at="yesterday")),
    ("a naive datetime", lambda b: b["event"].update(starts_at="2026-01-01T00:00:00")),
    ("rows out of order", lambda b: b["projects"].reverse()),
    ("an extra top-level key", lambda b: b.update(extra=[])),
    ("a bad email", lambda b: b["users"][0].update(email="not-an-email")),
    ("a duplicate email", lambda b: b["users"][1].update(email=b["users"][0]["email"].upper())),
    ("an over-long text", lambda b: b["projects"][0].update(name="x" * 121)),
    ("a NUL in text", lambda b: b["projects"][0].update(tagline="a\x00b")),
    ("an id in an undeclared JSON place", lambda b: b["result_snapshots"][0]["result"].update(sneaky_id="3")),
    ("a JSON id naming nothing", lambda b: b["result_snapshots"][0]["result"]["projects"][0].update(
        project_id="projects#999")),
    ("a bad decimal", lambda b: b["score_items"][0].update(value="1e999")),
    ("a secret column", lambda b: b["teams"][0].update(invite_token="abc")),
])
def test_a_malformed_event_json_is_refused(bundles, check, label, change):
    refused(check, rezip(edit_body(bundles["sample-hack-2026"], change)), "invalid_bundle")


@pytest.mark.django_db
def test_invalid_json_is_refused(bundles, check):
    files = dict(bundles["sample-hack-2026"], **{"event.json": b"{not json"})
    refused(check, rezip(files), "invalid_bundle")


# --- the upload ------------------------------------------------------------------------------------------

def test_an_upload_over_the_cap_stops_while_copying_and_leaves_nothing(tmp_path):
    import tempfile
    before = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("dogfood-import-")}
    upload = SimpleUploadedFile("b.zip", b"x" * 5000)
    with pytest.raises(bundle.BundleError) as caught:
        bundle_validate.save_upload(upload, limit=1000)
    assert caught.value.code == "bundle_too_large"
    after = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("dogfood-import-")}
    assert after == before
    path = bundle_validate.save_upload(SimpleUploadedFile("b.zip", b"y" * 500), limit=1000)
    assert open(path, "rb").read() == b"y" * 500
    os.unlink(path)


# --- placeholders for deleted rows: the export never writes a bundle the import refuses ------------------

@pytest.mark.django_db
def test_a_real_export_with_a_removed_judge_passes(bundles, check):
    files = bundles["missing-judge"]
    assert b"missing-memberships#1" in files["event.json"]
    result = check(rezip(files, fix_manifest=False))
    judges = [j["judge_id"] for j in result.body["result_snapshots"][-1]["result"]["judges"]]
    assert "missing-memberships#1" in judges


@pytest.mark.django_db
def test_missing_project_judge_and_track_placeholders_all_pass(bundles, check):
    def placeholders(body):
        snapshot = body["result_snapshots"][-1]
        row = snapshot["result"]["projects"][0]
        row["project_id"], row["track_id"] = "missing-projects#1", "missing-tracks#1"
        snapshot["result"]["judges"][0]["judge_id"] = "missing-memberships#1"
        snapshot["comparison"]["rows"][0]["project_id"] = "missing-projects#1"
        snapshot["result"]["coverage"]["reviews_by_judge"]["missing-memberships#2"] = 1
        snapshot["result"]["excluded"].append({"kind": "review", "id": "missing-memberships#1:missing-projects#1",
                                               "reason": "by flat judge missing-memberships#1"})
    assert check(rezip(edit_body(bundles["sample-hack-2026"], placeholders))).body


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["missing-ballots#1", "missing-projects#0", "missing-projects#x",
                                   "missing-memberships#1"])
def test_a_placeholder_must_name_a_deletable_namespace_correctly(bundles, check, value):
    """Ballots cannot be deleted, so there is no missing ballot; a placeholder is numbered from 1; and a
    project location cannot hold a judge's placeholder."""
    def bad(body):
        body["result_snapshots"][-1]["result"]["projects"][0]["project_id"] = value
    refused(check, rezip(edit_body(bundles["sample-hack-2026"], bad)), "invalid_bundle")


# --- the schema accepts exactly the keys the export writes ------------------------------------------------

@pytest.mark.django_db
def test_the_schema_expects_exactly_the_keys_the_export_writes(bundles):
    from imports.bundle_schema import EXTRA_KEYS, expected_fields

    for slug in ("sample-hack-2026", "dogfood-archive-2026"):
        body = json.loads(bundles[slug]["event.json"])
        for section in bundle.SECTIONS:
            rows = [body[section.name]] if section.one else body[section.name]
            wanted = {n for n, _ in expected_fields(section)} | EXTRA_KEYS.get(section.name, set())
            wanted |= set() if section.one else {"id"}
            for row in rows:
                if row is not None:
                    assert set(row) == wanted, section.name
        for user in body["users"]:
            assert set(user) == {"id", "email", "name"}


@pytest.mark.django_db
@pytest.mark.parametrize("label,change", [
    ("a user marked superuser", lambda b: b["users"][0].update(is_superuser=True)),
    ("a user marked staff", lambda b: b["users"][0].update(is_staff=True)),
    ("a user marked active", lambda b: b["users"][0].update(is_active=True)),
    ("a user marked platform admin", lambda b: b["users"][0].update(is_platform_admin=True)),
    ("a user with a password", lambda b: b["users"][0].update(password="md5$x$y")),
    ("a membership with an unknown role", lambda b: b["memberships"][0].update(role="superadmin")),
    ("a membership with its generated side", lambda b: b["memberships"][0].update(side="staff")),
])
def test_keys_the_export_never_writes_are_refused(bundles, check, label, change):
    refused(check, rezip(edit_body(bundles["sample-hack-2026"], change)), "invalid_bundle")
