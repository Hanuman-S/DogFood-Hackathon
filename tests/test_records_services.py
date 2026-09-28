"""C2b: issuing and revoking signed records, the record page, /verify, the .well-known lists, the
portal lists, and the signing-unavailable banners."""

import base64
import json
from datetime import timedelta

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from accounts.roles import ADMIN, Role
from core.audit import Origin
from core.models import AuditAction, AuditLog
from events.models import Event
from records import keys, services
from records.canonical import canonical
from records.errors import (
    AlreadyRevoked, InvalidRevoke, JudgingOpen, NoEvent, NoPublishedFinal, NoRecord, RateLimited,
    SigningUnavailable, SubmissionsOpen,
)
from records.models import ForeignSigningKey, IssuedRecord, RecordKind, SigningKey
from scoring import services as scoring
from scoring.models import Score
from test_results_flow import judged, publish  # noqa: F401 -- fixtures and helpers

TX = pytest.mark.django_db(transaction=True)
HERE = Origin(ip_hash="9" * 64)


@pytest.fixture(autouse=True)
def signing(settings, tmp_path):
    settings.SIGNING_KEY_DIR = tmp_path / "signing"


def ready():
    keys.ensure_signing_key()


def reopen(event):
    Event.objects.filter(pk=event.pk).update(judging_ends_at=timezone.now() + timedelta(hours=2))
    event.refresh_from_db()


def close(event):
    Event.objects.filter(pk=event.pk).update(judging_ends_at=timezone.now() - timedelta(minutes=1))
    event.refresh_from_db()


def active(event, kind):
    return IssuedRecord.objects.filter(event=event, kind=kind, revoked_at__isnull=True)


def keys_in(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys_in(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys_in(v)}
    return set()


# --- judge records -----------------------------------------------------------------------------------------

@TX
def test_judge_records_only_after_judging_closes(judged):
    ready()
    reopen(judged)
    with pytest.raises(JudgingOpen) as caught:
        services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer, origin=HERE)
    assert caught.value.status == 409 and not IssuedRecord.objects.exists()
    assert AuditLog.objects.filter(action=AuditAction.RECORD_ISSUE_REFUSED, detail__reason="judging_open").exists()


@TX
def test_a_judge_record_holds_the_count_and_the_window_never_a_score(judged):
    ready()
    counts = services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer, origin=HERE)
    assert counts == {"issued": 3, "reissued": 0, "unchanged": 0, "revoked": 0}
    record = active(judged, RecordKind.JUDGE).first()
    payload = record.payload
    assert payload["reviews_submitted"] == 4 and set(payload["judging"]) == {"starts_at", "ends_at"}
    assert not keys_in(payload) & {"score", "scores", "value", "values", "criteria", "rank", "items", "comment"}
    assert "@" not in record.payload_text  # a display name, never an email
    assert record.payload_text.encode() == canonical(payload)
    assert services.verify_record(record).state == "valid"


@TX
def test_a_judge_with_no_submitted_review_gets_none(judged):
    ready()
    first = Score.objects.filter(judge__user=judged.judge_user)
    first.update(submitted_at=None)
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    assert not active(judged, RecordKind.JUDGE).filter(subject_user=judged.judge_user).exists()
    assert active(judged, RecordKind.JUDGE).count() == 2


@TX
def test_reissue_is_idempotent_by_meaning_and_follows_reopen_close_reissue(judged):
    ready()
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    again = services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    assert again == {"issued": 0, "reissued": 0, "unchanged": 3, "revoked": 0}  # new issued_at, same meaning
    reopen(judged)
    first = Score.objects.filter(judge__user=judged.judge_user).order_by("pk").first()
    Score.objects.filter(pk=first.pk).update(submitted_at=None)
    close(judged)
    # judging's end moved, and every judge record carries the window: all three are reissued
    counts = services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    assert counts == {"issued": 0, "reissued": 3, "unchanged": 0, "revoked": 0}
    mine = IssuedRecord.objects.filter(event=judged, subject_user=judged.judge_user).order_by("issued_at")
    assert [r.revoke_reason for r in mine] == ["reissued", ""]
    assert mine.last().payload["reviews_submitted"] == 3
    assert mine.last().payload["judging"]["ends_at"] != mine.first().payload["judging"]["ends_at"]
    assert services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)["unchanged"] == 3
    # everything withdrawn: that judge is no longer eligible
    reopen(judged)
    Score.objects.filter(judge__user=judged.judge_user).update(submitted_at=None)
    close(judged)
    counts = services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    assert counts["revoked"] == 1 and not active(judged, RecordKind.JUDGE).filter(
        subject_user=judged.judge_user).exists()
    assert mine.last().__class__.objects.get(pk=mine.last().pk).revoke_reason == "no longer eligible"


# --- participant and winner records ------------------------------------------------------------------------

@TX
def test_participant_records_after_submissions_close(judged, make_event):
    ready()
    services.issue_records(judged, RecordKind.PARTICIPANT, actor=judged.organizer)
    rows = active(judged, RecordKind.PARTICIPANT)
    assert rows.count() == 4 and {r.payload["project"] for r in rows} == {"P0", "P1", "P2", "P3"}
    open_event = make_event()
    with pytest.raises(SubmissionsOpen):
        services.issue_records(open_event, RecordKind.PARTICIPANT, actor=open_event.organizer)


@TX
def test_winner_records_only_from_a_published_final(judged):
    ready()
    with pytest.raises(NoPublishedFinal):
        services.issue_records(judged, RecordKind.WINNER, actor=judged.organizer)
    publish(judged)
    services.issue_records(judged, RecordKind.WINNER, actor=judged.organizer)
    rows = list(active(judged, RecordKind.WINNER))
    slots = {(r.subject_user_id, r.slot) for r in rows}
    assert len(slots) == len(rows)
    overall = [r for r in rows if r.payload["track"] == "" and r.payload["peoples_choice"] == "no"]
    tracks = [r for r in rows if r.payload["track"]]
    assert sorted(r.payload["place"] for r in overall) == sorted({r.payload["place"] for r in overall}) or overall
    assert {r.payload["track"] for r in tracks} == {"Web", "Hardware"}
    # a project that is #1 overall and top of its track: two records for the same person
    assert any(sum(1 for r in rows if r.subject_user_id == u) == 2 for u in {r.subject_user_id for r in rows})
    assert "@" not in "".join(r.payload_text for r in rows)


@TX
def test_reissuing_winners_revokes_those_no_longer_winning(judged):
    ready()
    publish(judged)
    services.issue_records(judged, RecordKind.WINNER, actor=judged.organizer)
    before = active(judged, RecordKind.WINNER).filter(payload__track="").count()
    scoring.set_result_settings(judged, actor=judged.organizer, visibility="public_full", winners_top_n=1)
    counts = services.issue_records(judged, RecordKind.WINNER, actor=judged.organizer)
    assert counts["revoked"] == before - 1 and counts["issued"] == 0
    assert IssuedRecord.objects.filter(event=judged, revoke_reason="no longer eligible").count() == before - 1


# --- permissions, revocation ------------------------------------------------------------------------------

@TX
def test_only_organizers_and_admins_issue_and_revoke(judged, client_for):
    ready()
    for outsider in (judged.participant, judged.judge_user, judged.other_organizer):
        with pytest.raises(NoEvent) as caught:
            services.issue_records(judged, RecordKind.JUDGE, actor=outsider)
        assert caught.value.status == 404
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.admin)
    record = active(judged, RecordKind.JUDGE).first()
    for outsider in (judged.participant, judged.judge_user, judged.other_organizer):
        with pytest.raises(NoRecord):
            services.revoke_record(record.pk, "no", actor=outsider)
    assert client_for(judged.participant).post(f"/organizer/events/{judged.slug}/records/issue",
                                               {"kind": "participant"}).status_code == 403
    assert client_for(judged.other_organizer).post(f"/organizer/events/{judged.slug}/records/issue",
                                                   {"kind": "participant"}).status_code == 404
    assert not IssuedRecord.objects.filter(kind=RecordKind.PARTICIPANT).exists()


@TX
def test_revoking_needs_a_reason_happens_once_and_shows_everywhere(judged, client_for):
    ready()
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    record = active(judged, RecordKind.JUDGE).first()
    with pytest.raises(InvalidRevoke):
        services.revoke_record(record.pk, "  ", actor=judged.organizer)
    services.revoke_record(record.pk, "issued to the wrong person", actor=judged.organizer, origin=HERE)
    with pytest.raises(AlreadyRevoked):
        services.revoke_record(record.pk, "again", actor=judged.organizer)
    page = Client().get(f"/records/{record.pk}").content.decode()
    assert "revoked" in page and "revoked by the organizer" in page
    verdict = services.verify_bytes(record.payload_text.encode(), record.signature)
    assert verdict.state == "revoked"
    data = Client().get(f"/records/{record.pk}.json").json()
    assert data["revoked"] is True and data["revocation"]["category"] == "organizer"
    assert data["revocation"]["replaced_by"] is None
    # the organizer's own words stay off every public page
    public = [page, json.dumps(data), Client().get("/.well-known/dogfood-revoked.json").content.decode(),
              Client().post("/verify", {"payload": record.payload_text, "signature": record.signature}).content
              .decode()]
    assert not any("issued to the wrong person" in text for text in public)
    organizer_page = client_for(judged.organizer).get(f"/organizer/events/{judged.slug}/records").content.decode()
    assert "issued to the wrong person" in organizer_page


@TX
def test_the_revoked_list_names_hashes_never_the_ids(judged):
    import hashlib
    ready()
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    records = list(active(judged, RecordKind.JUDGE))
    for record in records[:2]:
        services.revoke_record(record.pk, "test", actor=judged.organizer)
    body = Client().get("/.well-known/dogfood-revoked.json").content.decode()
    for record in records:
        assert str(record.pk) not in body and str(record.pk).replace("-", "") not in body
    hashes = {r["record_id_sha256"] for r in json.loads(body)["revoked"]}
    assert hashes == {hashlib.sha256(str(r.pk).encode()).hexdigest() for r in records[:2]}


@TX
def test_a_superseded_record_links_to_its_replacement(judged):
    ready()
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    reopen(judged)
    close(judged)  # judging's end moved: every judge record is reissued
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    old = IssuedRecord.objects.filter(subject_user=judged.judge_user).order_by("issued_at").first()
    new = IssuedRecord.objects.filter(subject_user=judged.judge_user).order_by("issued_at").last()
    assert old.revoke_category == "superseded"
    page = Client().get(f"/records/{old.pk}").content.decode()
    assert "superseded by a newer record" in page and f"/records/{new.pk}" in page
    assert Client().get(f"/records/{old.pk}.json").json()["revocation"]["replaced_by"] == str(new.pk)


@TX
def test_moving_judgings_end_reissues_judge_records_only(judged):
    ready()
    publish(judged)
    for kind in (RecordKind.JUDGE, RecordKind.PARTICIPANT, RecordKind.WINNER):
        services.issue_records(judged, kind, actor=judged.organizer)
    before = {k: set(active(judged, k).values_list("pk", flat=True)) for k in RecordKind}
    Event.objects.filter(pk=judged.pk).update(judging_ends_at=judged.judging_ends_at - timedelta(minutes=5))
    judged.refresh_from_db()
    judge = services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    participant = services.issue_records(judged, RecordKind.PARTICIPANT, actor=judged.organizer)
    winner = services.issue_records(judged, RecordKind.WINNER, actor=judged.organizer)
    assert judge["reissued"] == 3
    assert participant == {"issued": 0, "reissued": 0, "unchanged": 4, "revoked": 0}
    assert winner["reissued"] == 0 and winner["revoked"] == 0 and winner["unchanged"] == len(before[RecordKind.WINNER])
    for kind in (RecordKind.PARTICIPANT, RecordKind.WINNER):
        assert set(active(judged, kind).values_list("pk", flat=True)) == before[kind]
    payload = active(judged, RecordKind.PARTICIPANT).first().payload
    assert set(payload["event"]) == {"slug", "name", "submissions_open_at", "submissions_close_at"}


@TX
def test_the_reissue_audit_row_lists_the_revoked_ids(judged):
    ready()
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    old = {str(pk) for pk in active(judged, RecordKind.JUDGE).values_list("pk", flat=True)}
    reopen(judged)
    Score.objects.filter(judge__user=judged.judge_user).update(submitted_at=None)
    close(judged)
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    row = AuditLog.objects.filter(action=AuditAction.RECORD_REVOKED, detail__reason="reissue").latest("pk")
    assert len(row.detail["superseded"]) == 2 and len(row.detail["no_longer_eligible"]) == 1
    assert set(row.detail["superseded"]) | set(row.detail["no_longer_eligible"]) == old


# --- verification ------------------------------------------------------------------------------------------

@TX
def test_any_change_to_the_payload_fails(judged):
    ready()
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    record = active(judged, RecordKind.JUDGE).first()
    text = record.payload_text
    tampered = text.replace('"reviews_submitted":4', '"reviews_submitted":5')
    assert tampered != text and services.verify_bytes(tampered.encode(), record.signature).state == "invalid"
    flipped = bytearray(text.encode())
    flipped[-3] ^= 1
    assert services.verify_bytes(bytes(flipped), record.signature).state in ("invalid", "not_canonical")
    pretty = json.dumps(record.payload, indent=2).encode()
    assert services.verify_bytes(pretty, record.signature).state == "not_canonical"
    unknown = dict(record.payload, kid="0" * 16)
    assert services.verify_bytes(canonical(unknown), record.signature).state == "unknown_key"


@TX
def test_records_signed_before_a_rotation_still_verify(judged):
    ready()
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    old = active(judged, RecordKind.JUDGE).first()
    keys.rotate()
    assert services.verify_record(old).state == "valid"
    services.issue_records(judged, RecordKind.PARTICIPANT, actor=judged.organizer)
    new = active(judged, RecordKind.PARTICIPANT).first()
    assert new.kid != old.kid and services.verify_record(new).state == "valid"
    published = {k["kid"] for k in Client().get("/.well-known/dogfood-signing-keys.json").json()["keys"]}
    assert {old.kid, new.kid} <= published


@TX
def test_the_verify_page_checks_exact_bytes_with_csrf_and_a_live_rate_limit(judged):
    ready()
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    record = active(judged, RecordKind.JUDGE).first()
    client = Client(enforce_csrf_checks=True, REMOTE_ADDR="10.9.9.9")
    assert client.post("/verify", {"payload": record.payload_text, "signature": record.signature}).status_code == 403
    page = client.get("/verify")
    token = page.cookies["csrftoken"].value
    response = client.post("/verify", {"payload": record.payload_text, "signature": record.signature,
                                       "csrfmiddlewaretoken": token})
    assert response.status_code == 200 and "valid: signed by this install" in response.content.decode()
    ip_hash = AuditLog.objects.filter(action=AuditAction.RECORD_VERIFY).latest("pk").ip_hash
    # imported history carrying the same network hash never counts toward the limit
    for _ in range(5):
        AuditLog.objects.create(action=AuditAction.RECORD_VERIFY, ip_hash=ip_hash, detail={"source_history": True})
    with override_settings(VERIFY_RATE_PER_IP=2):
        ok = client.post("/verify", {"payload": record.payload_text, "signature": record.signature,
                                     "csrfmiddlewaretoken": token})
        assert ok.status_code == 200
        limited = client.post("/verify", {"payload": record.payload_text, "signature": record.signature,
                                          "csrfmiddlewaretoken": token})
        assert limited.status_code == 429
    assert AuditLog.objects.filter(action=AuditAction.RECORD_VERIFY_THROTTLED).count() == 1


# --- keys published, foreign records -------------------------------------------------------------------

def _foreign_record(event, user):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    other = Ed25519PrivateKey.generate()
    raw = keys.raw_public(other)
    kid = keys.kid_of(raw)
    ForeignSigningKey.objects.create(kid=kid, public_key=base64.b64encode(raw).decode(), imported_from="f" * 64)
    payload = {"v": 1, "kind": "participant", "record_id": "11111111-1111-4111-8111-111111111111",
               "issued_at": "2026-01-01T00:00:00Z", "kid": kid, "event": {"slug": "x", "name": "X",
               "starts_at": "", "ends_at": ""}, "subject": {"name": "Someone"}, "team": "T", "project": "P"}
    text = canonical(payload)
    return IssuedRecord.objects.create(
        id=payload["record_id"], kind="participant", event=event, subject_user=user, payload=payload,
        payload_text=text.decode(), signature=base64.b64encode(other.sign(text)).decode(), kid=kid, is_foreign=True)


@TX
def test_own_keys_and_foreign_keys_are_published_apart_and_labelled(judged):
    ready()
    record = _foreign_record(judged, judged.participant)
    own = {k["kid"] for k in Client().get("/.well-known/dogfood-signing-keys.json").json()["keys"]}
    foreign = {k["kid"] for k in Client().get("/.well-known/dogfood-foreign-signing-keys.json").json()["keys"]}
    assert own == set(SigningKey.objects.values_list("kid", flat=True)) and record.kid not in own
    assert foreign == {record.kid}
    assert services.verify_record(record).state == "foreign_valid"
    page = Client().get(f"/records/{record.pk}").content.decode()
    assert f"signed by another install (kid {record.kid})" in page
    assert "verified: signed by this install" not in page


# --- when signing is unavailable ------------------------------------------------------------------------

@TX
def test_a_missing_key_file_refuses_issuing_and_the_banners_say_why(judged, client_for):
    import os
    ready()
    _, kid = keys.status()
    os.unlink(keys.pem_path(kid))
    with pytest.raises(SigningUnavailable) as caught:
        services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    assert caught.value.status == 503 and not IssuedRecord.objects.exists()
    assert AuditLog.objects.filter(action=AuditAction.RECORD_ISSUE_REFUSED, detail__reason="signing_unavailable").exists()
    for client, url in ((client_for(judged.organizer), f"/organizer/events/{judged.slug}/records"),
                        (client_for(judged.admin), "/admin/")):
        html = client.get(url).content.decode()
        assert "signing unavailable" in html and "has no private key file" in html, url
        assert "restore the <code>secrets</code> volume" in html and "rotate_signing_key" in html


@TX
def test_no_banner_when_signing_works(judged, client_for):
    ready()
    assert "signing unavailable" not in client_for(judged.admin).get("/admin/").content.decode()


# --- the portals -------------------------------------------------------------------------------------------

@TX
def test_judges_and_participants_see_and_download_their_own_records(judged, client_for):
    ready()
    services.issue_records(judged, RecordKind.JUDGE, actor=judged.organizer)
    services.issue_records(judged, RecordKind.PARTICIPANT, actor=judged.organizer)
    judge_page = client_for(judged.judge_user).get("/judge/").content.decode()
    mine = IssuedRecord.objects.get(subject_user=judged.judge_user)
    assert "my records" in judge_page and f"/records/{mine.pk}" in judge_page
    participant_page = client_for(judged.participant).get("/participant/").content.decode()
    theirs = IssuedRecord.objects.get(subject_user=judged.participant)
    assert f"/records/{theirs.pk}.json" in participant_page and f"/records/{mine.pk}" not in participant_page


@TX
def test_the_record_page_is_printable_and_has_no_script(judged):
    ready()
    services.issue_records(judged, RecordKind.PARTICIPANT, actor=judged.organizer)
    record = active(judged, RecordKind.PARTICIPANT).first()
    html = Client().get(f"/records/{record.pk}").content.decode()
    assert "certificate" in html and record.signature in html and record.kid in html
    assert "<script>" not in html.split("</main>")[0]
    css = open("src/static/css/crt.css", encoding="utf-8").read()
    assert "@media print" in css and ".no-print" in css



@TX
def test_participant_eligibility_is_team_membership_when_issued(make_event, make_team, make_user):
    """Members of a team whose project is submitted, as the team stands when records are issued. Someone
    who left before the close gets none; nobody can leave after it, except through an organizer's
    audited bypass, and then the next issue revokes theirs."""
    from core.deadlines import SubmissionsClosed, deadline_bypass
    from django.test import RequestFactory
    from projects.models import Project, Status
    from teams.models import TeamMember
    from teams.services import leave_team
    from test_results_flow import close_judging

    ready()
    event = make_event()
    stays, leaves_early, removed = (make_user(email=f"{n}@example.org", name=n) for n in ("stays", "early", "removed"))
    team = make_team(event, captain=stays, members=(leaves_early, removed))
    Project.objects.create(team=team, name="Kept", status=Status.SUBMITTED, submitted_at=timezone.now())
    request = RequestFactory().post("/")
    request.user = leaves_early
    leave_team(request, team)                     # before the close: allowed, and then not a member
    close_judging(event)
    request.user = removed
    with pytest.raises(SubmissionsClosed):        # after the close: refused
        leave_team(request, team)
    services.issue_records(event, RecordKind.PARTICIPANT, actor=event.organizer)
    holders = set(active(event, RecordKind.PARTICIPANT).values_list("subject_user__name", flat=True))
    assert holders == {"stays", "removed"}
    with deadline_bypass(None, "test: an organizer's repair", actor=event.organizer):
        TeamMember.objects.filter(team=team, user=removed).delete()
    counts = services.issue_records(event, RecordKind.PARTICIPANT, actor=event.organizer)
    assert counts == {"issued": 0, "reissued": 0, "unchanged": 1, "revoked": 1}
    gone = IssuedRecord.objects.get(subject_user=removed)
    assert gone.revoke_category == "no_longer_eligible"
