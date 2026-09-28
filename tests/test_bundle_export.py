"""The event bundle export (C1a): what goes in, what never does, the id guard, and who may download.

The world is the real demo: seed_demo (the archive, with its open quadratic vote, ten ballots and a
flagged cluster), the organizers' fixture file (Sample Hack 2026, with its folded duplicate) and its
closed vote, and a final result computed on it. On top: the secrets a bundle must never carry (API
tokens, a session, a judge invite, a voter link, IP hashes)."""

import io
import json
import os
import tempfile
import zipfile
from io import StringIO

import pytest
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import Client, override_settings
from PIL import Image

from accounts.models import ApiToken, User, UserSession
from conftest import PASSWORD
from accounts.roles import ADMIN, Role
from core.audit import Origin
from core.deadlines import deadline_bypass
from core.keys import derived_key
from core.models import AuditAction, AuditLog
from events.models import Event, JudgeInvite
from imports import bundle, bundle_ids
from imports.models import FixtureRef
from projects import comments
from projects.images import clean_image
from projects.models import Project
from scoring import services as scoring
from scoring.models import ResultSnapshot
from teams.models import Team
from voting import services as voting
from voting.models import Ballot, VoterLink, VotingConfig

TOKENS = {"admin": "t-admin", "organizer": "t-org", "judge_a": "t-ja", "judge_b": "t-jb", "participant": "t-p"}
ARCHIVE, FIXTURE = "dogfood-archive-2026", "sample-hack-2026"


def _png():
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (120, 60, 200)).save(buffer, "PNG")
    return ContentFile(buffer.getvalue(), name="shot.png")


@pytest.fixture
def world(transactional_db, tmp_path, client_for):
    from imports.fixtures import import_file

    with override_settings(DEMO_MODE=True, DEMO_TOKENS=TOKENS, MEDIA_ROOT=str(tmp_path)):
        call_command("seed_demo", stdout=StringIO())
        import_file()
        call_command("seed_demo", "--votes", stdout=StringIO())
        archive, fixture = Event.objects.get(slug=ARCHIVE), Event.objects.get(slug=FIXTURE)
        organizer = User.objects.get(email="organizer@dogfood.local")
        for demo in User.objects.filter(email__in=["organizer@dogfood.local", "judge.a@dogfood.local",
                                                    "participant@dogfood.local"]):
            demo.set_password(PASSWORD)  # conftest's client_for logs in with the suite's password
            demo.save(update_fields=["password"])
        scoring.compute_snapshot(fixture, "final", actor=organizer)

        # A thumbnail and a gallery image on an archive project (closed: through the audited bypass).
        project = Project.objects.filter(event=archive).order_by("pk").first()
        with deadline_bypass(None, "test image"):
            project.thumbnail.save("t.png", clean_image(_png()), save=True)
        # A comment, a voided ballot, a session, a judge invite, a voter link: all with secrets.
        comments.post_comment(project.pk, "A comment that travels", author=organizer,
                              origin=Origin(ip_hash="c0ffee" * 10 + "abcd"))
        ballot = Ballot.objects.filter(event=archive, voter_user__email__startswith="voter.cluster").first()
        voting.void_ballot(archive, ballot.pk, actor=organizer, reason="cluster", origin=Origin(ip_hash="e" * 64))
        client_for(organizer)  # a real login: a session row and a UserSession with an ip hash
        JudgeInvite.objects.create(event=archive, role=Role.JUDGE, email="future.judge@example.org",
                                   digest="invite-digest-" + "9" * 50, created_by=organizer,
                                   expires_at=archive.judging_ends_at)
        VoterLink.objects.create(event=archive, email="allowlisted@example.org", nonce="voter-link-nonce-xyz",
                                 token_digest="voter-link-digest-" + "8" * 46, created_by=organizer,
                                 created_at=archive.judging_starts_at)
        yield {"archive": archive, "fixture": fixture, "organizer": organizer, "project": project}


def read(path):
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info.filename) for info in archive.infolist()}


def export(event, actor=None):
    path = bundle.export_event(event, actor=actor)
    try:
        return read(path)
    finally:
        os.unlink(path)


def body_of(files):
    return json.loads(files["event.json"])


def everything(files):
    return b"\n".join(files.values())


def leftovers():
    return [name for name in os.listdir(tempfile.gettempdir()) if name.startswith("dogfood-bundle-")]


# --- the format --------------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_manifest_lists_every_other_file_with_its_sha256(world):
    import hashlib
    files = export(world["archive"])
    manifest = json.loads(files["manifest.json"])
    assert manifest["format"] == "dogfood-event-bundle" and manifest["version"] == 1
    assert manifest["source_event"] == ARCHIVE and manifest["generator"]
    others = {name: data for name, data in files.items() if name != "manifest.json"}
    assert set(manifest["files"]) == set(others)
    assert all(hashlib.sha256(data).hexdigest() == manifest["files"][name] for name, data in others.items())
    media = [name for name in others if name.startswith("media/")]
    assert len(media) == 1 and media[0] == f"media/{hashlib.sha256(others[media[0]]).hexdigest()}.png"
    assert body_of(files)["projects"][0]["thumbnail"] == media[0]


@pytest.mark.django_db(transaction=True)
def test_every_section_is_there_and_references_are_bundle_ids(world):
    body = body_of(export(world["fixture"]))
    for section in bundle.SECTIONS:
        assert section.name in body, section.name
    assert body["event"]["slug"] == FIXTURE
    assert len(body["projects"]) == 40 and len(body["scores"]) == 123
    assert body["projects"][0]["id"] == "projects#1"  # numbered 1..n, never a source primary key
    assert {s["project"] for s in body["scores"]} <= {p["id"] for p in body["projects"]}
    assert all(r["object"].split("#")[0] in ("projects", "scores") for r in body["fixture_refs"])
    snapshot = body["result_snapshots"][-1]
    ids = {p["id"] for p in body["projects"]}
    assert {p["project_id"] for p in snapshot["result"]["projects"]} <= ids
    assert snapshot["vote_tally"] == body["tally_snapshots"][-1]["id"]
    assert body["publications"] == [] or body["publications"][0]["snapshot"].startswith("result_snapshots#")


@pytest.mark.django_db(transaction=True)
def test_strings_round_trip_exactly(world):
    event = world["archive"]
    tricky = 'Line one\nquote " backslash \\ emoji \U0001F680 accents éè NUL-free <b>tag</b>  '
    Event.objects.filter(pk=event.pk).update(tagline=tricky[:200])
    assert body_of(export(event))["event"]["tagline"] == tricky[:200]


# --- never exported ---------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_no_secret_is_in_any_file_of_the_bundle(world):
    event = world["archive"]
    files = export(event)
    blob = everything(files)
    secrets = {
        "password hashes": [u.password for u in User.objects.all() if u.password],
        "api token digests": list(ApiToken.objects.values_list("digest", flat=True)),
        "team invite tokens": list(Team.objects.values_list("invite_token", flat=True)),
        "judge invite digests": list(JudgeInvite.objects.values_list("digest", flat=True)),
        "voter link nonces": list(VoterLink.objects.values_list("nonce", flat=True)),
        "voter link digests": list(VoterLink.objects.values_list("token_digest", flat=True)),
        "ballot secret": list(VotingConfig.objects.values_list("ballot_secret", flat=True)),
        "open link nonce": [n for n in VotingConfig.objects.values_list("open_link_nonce", flat=True) if n],
        "audit ip hashes": [h for h in AuditLog.objects.values_list("ip_hash", flat=True) if h],
        "ballot ip hashes": [h for pair in Ballot.objects.values_list("ip_hash", "created_ip_hash") for h in pair if h],
        "session ip hashes": [h for h in UserSession.objects.values_list("ip_hash", flat=True) if h],
        "session keys": list(UserSession.objects.values_list("session_key", flat=True)),
        "secret key": [settings.SECRET_KEY],
        "derived keys": [derived_key(p).hex() for p in ("ip-hash", "voter-links", "open-link")],
        "audit user agents": [a for a in AuditLog.objects.values_list("user_agent", flat=True) if len(a) > 8],
    }
    for what, values in secrets.items():
        assert values or what in ("open link nonce", "audit user agents"), f"the test world has no {what}"
        for value in values:
            assert value.encode() not in blob, f"{what} found in the bundle"
    keys = set()

    def collect(value):
        if isinstance(value, dict):
            keys.update(value)
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(body_of(files))
    forbidden = {"password", "ballot_secret", "open_link_nonce", "invite_token", "ip_hash", "created_ip_hash",
                 "user_agent", "digest", "token_digest", "nonce", "token_prefix", "session_key", "voter_user",
                 "voter_link", "voter_cookie"}
    assert not keys & forbidden


@pytest.mark.django_db(transaction=True)
def test_voters_are_pseudonyms_and_voter_only_accounts_are_absent(world):
    event = world["archive"]
    files = export(event)
    body = body_of(files)
    voter_emails = set(Ballot.objects.filter(event=event, voter_user__isnull=False)
                       .values_list("voter_user__email", flat=True))
    staff_or_team = {u["email"] for u in body["users"]}
    voter_only = {e for e in voter_emails if e.startswith("voter.")}
    assert len(voter_only) == 10
    for email in voter_only:
        assert email.encode() not in everything(files), email
    assert not voter_only & staff_or_team
    assert "allowlisted@example.org".encode() not in everything(files)

    ballots = {b["voter"] for b in body["ballots"]}
    assert len(ballots) == len(body["ballots"]) and all(v.startswith("v_") and len(v) == 18 for v in ballots)
    cast = [a for a in body["audit"] if a["action"] in ("vote_cast", "vote_changed", "ballot_opened")]
    assert cast and {a["actor_email"] for a in cast} <= ballots  # the same pseudonym as the ballot
    assert all(a["actor"] is None for a in cast)
    voided = [a for a in body["audit"] if a["action"] == "ballot_voided"]
    assert voided and voided[0]["detail"]["voter"] in ballots


@pytest.mark.django_db(transaction=True)
def test_pseudonyms_are_stable_within_an_export_and_differ_between_exports(world):
    first = {b["voter"] for b in body_of(export(world["archive"]))["ballots"]}
    second = {b["voter"] for b in body_of(export(world["archive"]))["ballots"]}
    assert len(first) == len(second) == 10 and not first & second


@pytest.mark.django_db(transaction=True)
def test_fixture_refs_are_selected_by_kind_and_object_id(world):
    """A ref of another kind whose object_id happens to equal one of the event's project ids is not
    the project's ref, and must not travel with it."""
    fixture = world["fixture"]
    project = Project.objects.filter(event=fixture).order_by("pk").first()
    user = User.objects.create_user("same-number@example.org", None, name="Same Number")
    FixtureRef.objects.create(source="elsewhere", kind="user", external_id="usr_x", object_id=project.pk)
    FixtureRef.objects.create(source="elsewhere", kind="judge", external_id="jdg_x", object_id=project.pk)
    body = body_of(export(fixture))
    assert {r["kind"] for r in body["fixture_refs"]} == {"project", "score"}
    assert not [r for r in body["fixture_refs"] if r["external_id"] in ("usr_x", "jdg_x")]
    assert user.email not in json.dumps(body)


# --- the id guard ------------------------------------------------------------------------------------

def _mapper(namespace, value, typ):
    return f"{namespace}#{value}"


def test_a_plain_id_is_remapped():
    out = bundle_ids.rewrite({"projects": [{"project_id": "25", "track_id": None}]}, bundle_ids.RESULT, _mapper, "t")
    assert out == {"projects": [{"project_id": "projects#25", "track_id": None}]}


def test_a_composite_review_id_remaps_both_halves():
    value = {"excluded": [{"kind": "review", "id": "16:33", "reason": "x"}]}
    assert bundle_ids.rewrite(value, bundle_ids.RESULT, _mapper, "t")["excluded"][0]["id"] == "memberships#16:projects#33"


def test_an_external_id_survives_byte_for_byte():
    value = {"excluded": [{"kind": "review", "id": "33:dup:prj_41",
                           "reason": "review of duplicate submission dup:prj_41 (kept: 33)"},
                          {"kind": "project", "id": "dup:prj_41", "reason": "duplicate submission of 33; x"}]}
    out = bundle_ids.rewrite(value, bundle_ids.RESULT, _mapper, "t")["excluded"]
    assert out[0]["id"] == "memberships#33:dup:prj_41" and out[1]["id"] == "dup:prj_41"
    assert out[0]["reason"] == "review of duplicate submission dup:prj_41 (kept: projects#33)"
    rubric = bundle_ids.TABLE[("scoring.ResultSnapshot", "rubric")]
    assert bundle_ids.rewrite({"criteria": [{"id": "impact"}]}, rubric, _mapper, "t") == {"criteria": [{"id": "impact"}]}


def test_ids_as_dict_keys_are_remapped_and_their_values_kept():
    out = bundle_ids.rewrite({"coverage": {"reviews_by_judge": {"16": 3}}}, bundle_ids.RESULT, _mapper, "t")
    assert out == {"coverage": {"reviews_by_judge": {"memberships#16": 3}}}


@pytest.mark.parametrize("value", [
    {"foo_id": "3"}, {"ids": [1, 2]}, {"project": "4"}, {"judge": "5"}, {"ballot": 6}, {"nested": {"owner_ids": [7]}},
    {"coverage": {"extra": {"12": 1}}}, {"12": "a digit key at the top"},
])
def test_an_undeclared_id_location_fails(value):
    with pytest.raises(bundle_ids.UnknownIdField):
        bundle_ids.rewrite(value, bundle_ids.RESULT, _mapper, "t")


def _inject(event, **fields):
    """A new snapshot row (immutable rows are inserted, never updated) with odd JSON."""
    base = ResultSnapshot.objects.filter(event=event).order_by("-pk").first()
    values = {f.attname: getattr(base, f.attname) for f in ResultSnapshot._meta.concrete_fields if not f.primary_key}
    values.update(fields)
    return ResultSnapshot.objects.create(**values)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("field,value", [
    ("result", {"foo_id": "3"}),
    ("result", {"coverage": {"reviews_by_judge": {}, "mystery": {"12": 1}}}),
])
def test_an_unknown_id_field_in_a_snapshot_fails_the_export(world, field, value):
    fixture = world["fixture"]
    _inject(fixture, **{field: value})
    before = leftovers()
    with pytest.raises(bundle.BundleError) as refused:
        bundle.export_event(fixture, actor=world["organizer"])
    assert refused.value.code == "unknown_id_field"
    assert leftovers() == before
    entry = AuditLog.objects.filter(action=AuditAction.EVENT_EXPORT_REFUSED).latest("pk")
    assert entry.detail["reason"] == "unknown_id_field" and not AuditLog.objects.filter(
        action=AuditAction.EVENT_EXPORTED).exists()


@pytest.mark.django_db(transaction=True)
def test_a_bundle_the_import_could_not_take_is_refused(world):
    before = leftovers()
    with override_settings(BUNDLE_MAX_EVENT_JSON_BYTES=1000):
        with pytest.raises(bundle.BundleError) as refused:
            bundle.export_event(world["fixture"], actor=world["organizer"])
    assert refused.value.code == "bundle_too_large" and leftovers() == before
    assert AuditLog.objects.filter(action=AuditAction.EVENT_EXPORT_REFUSED, detail__reason="bundle_too_large").exists()


@pytest.mark.django_db(transaction=True)
def test_the_export_refuses_to_run_inside_a_transaction(world):
    from django.db import transaction
    with transaction.atomic(), pytest.raises(bundle.ExportInsideTransaction):
        bundle.export_event(world["archive"], actor=None)


# --- who may download ---------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_organizers_and_admins_download_from_the_page_and_the_api(world, client_for, make_user):
    event = world["archive"]
    for client in (client_for(world["organizer"]), client_for(make_user(role=ADMIN))):
        for url in (f"/organizer/events/{ARCHIVE}/bundle", f"/api/events/{ARCHIVE}/bundle"):
            response = client.get(url)
            assert response.status_code == 200 and response["Content-Type"] == "application/zip", url
            data = b"".join(response.streaming_content)
            assert zipfile.ZipFile(io.BytesIO(data)).read("manifest.json")
    assert AuditLog.objects.filter(action=AuditAction.EVENT_EXPORTED, subject=event.slug).count() == 4
    response = Client().get(f"/api/events/{ARCHIVE}/bundle", HTTP_AUTHORIZATION=f"Bearer {TOKENS['organizer']}")
    assert response.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_judges_participants_and_other_organizers_cannot_download(world, client_for, make_event, make_user):
    judge = User.objects.get(email="judge.a@dogfood.local")
    participant = User.objects.get(email="participant@dogfood.local")
    other_organizer = make_event().organizer
    for who, user, wanted in (("judge", judge, (403, 404)), ("participant", participant, (403, 404)),
                              ("other organizer", other_organizer, (404,))):
        client = client_for(user)
        for url in (f"/organizer/events/{ARCHIVE}/bundle", f"/api/events/{ARCHIVE}/bundle"):
            assert client.get(url).status_code in wanted, (who, url)
    assert Client().get(f"/api/events/{ARCHIVE}/bundle").status_code == 401
    assert not AuditLog.objects.filter(action=AuditAction.EVENT_EXPORTED).exists()


@pytest.mark.django_db(transaction=True)
def test_the_service_refuses_a_non_organizer_itself(world, make_user):
    with pytest.raises(bundle.NoEvent):
        bundle.export_event(world["archive"], actor=make_user(role=Role.PARTICIPANT))
    assert AuditLog.objects.filter(action=AuditAction.EVENT_EXPORT_REFUSED, detail__reason="no_event").exists()


@pytest.mark.django_db(transaction=True)
def test_the_command_writes_the_same_bundle(world, tmp_path):
    target = tmp_path / "archive.zip"
    call_command("export_event", ARCHIVE, str(target), stdout=StringIO())
    files = read(target)
    assert set(json.loads(files["manifest.json"])["files"]) == set(files) - {"manifest.json"}
    entry = AuditLog.objects.filter(action=AuditAction.EVENT_EXPORTED).latest("pk")
    assert entry.actor is None and entry.subject == ARCHIVE


# --- audit detail keys, rows deleted after a snapshot ------------------------------------------------

@pytest.mark.parametrize("key", ["description", "recipient", "skip", "zip", "tipped", "tokenizer", "agent",
                                 "user", "prefixed_by", "hashes"])
def test_audit_keys_that_merely_contain_a_word_survive(key):
    assert not bundle.scrubbed_key(key)
    assert bundle._scrub({key: 1}) == {key: 1}


@pytest.mark.parametrize("key", ["ip", "ip_hash", "created_ip_hash", "token_prefix", "voter_token", "digest",
                                 "token_digest", "secret", "ballot_secret", "open_link_nonce", "password",
                                 "user_agent", "IP_Hash"])
def test_audit_keys_naming_a_secret_or_an_address_are_stripped_at_any_depth(key):
    assert bundle.scrubbed_key(key)
    detail = {"description": "kept", "nested": [{key: "x", "recipient": "kept"}], key: "x"}
    assert bundle._scrub(detail) == {"description": "kept", "nested": [{"recipient": "kept"}]}


@pytest.mark.django_db(transaction=True)
def test_a_judge_removed_after_the_final_becomes_a_missing_row_not_a_failure(world):
    from events.models import EventMembership

    fixture = world["fixture"]
    final = ResultSnapshot.objects.filter(event=fixture, kind="final").latest("pk")
    judge_pk = int(final.result["judges"][0]["judge_id"])
    legacy_remove_judge(EventMembership.objects.get(pk=judge_pk))
    body = body_of(export(fixture, actor=world["organizer"]))
    snapshot = body["result_snapshots"][-1]
    judges = [j["judge_id"] for j in snapshot["result"]["judges"]]
    assert "missing-memberships#1" in judges
    assert all(j.startswith(("memberships#", "missing-memberships#")) for j in judges)
    entry = AuditLog.objects.filter(action=AuditAction.EVENT_EXPORTED).latest("pk")
    assert entry.detail["missing"] == {"memberships": 1}


@pytest.mark.django_db(transaction=True)
def test_an_unknown_ballot_or_tally_is_a_dangling_id(world):
    fixture = world["fixture"]
    base = ResultSnapshot.objects.filter(event=fixture).latest("pk")
    _inject(fixture, diagnostics={**base.diagnostics, "vote_tally": {"id": 999999, "previous": None,
                                                                    "voided_since_previous": [],
                                                                    "restored_since_previous": []}})
    with pytest.raises(bundle.BundleError) as refused:
        bundle.export_event(fixture, actor=world["organizer"])
    assert refused.value.code == "dangling_id"
    assert AuditLog.objects.filter(action=AuditAction.EVENT_EXPORT_REFUSED, detail__reason="dangling_id").exists()


def legacy_remove_judge(membership):
    """LEGACY / PRE-GUARD DATA. Before events.services.remove_judge refused judges with submitted
    reviews (and Score.judge became PROTECT), removing a judge deleted their reviews with them. Data
    from then can exist, so the export must still write missing-memberships#n for such a judge. This
    reproduces that state with explicit deletes; the service itself now refuses."""
    from scoring.models import Assignment, Score, ScoreItem
    ScoreItem.objects.filter(score__judge=membership).delete()
    Score.objects.filter(judge=membership).delete()
    Assignment.objects.filter(judge=membership).delete()
    membership.delete()
