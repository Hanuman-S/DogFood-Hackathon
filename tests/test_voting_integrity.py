"""Voting anti-abuse (T3 stage 4): rate limits (429, audited), IPs stored only as keyed hashes,
integrity flags (zero-credit ballots ignored), voiding, and the organizer's integrity page."""

import json
import re
from datetime import timedelta

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from accounts.models import User, UserSession
from accounts.roles import ADMIN, Role
from core.models import AuditAction, AuditLog
from core.net import hash_ip
from test_voting import backdate, cast, credits_of, old_user, shift_voting, vote_event  # noqa: F401 -- fixtures
from voting import integrity, services
from voting.errors import AlreadyVoided, InvalidVoid, NoSuchBallot, RateLimited
from voting.models import Ballot, BallotLine, VotingConfig

IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6 = re.compile(r"\b(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{1,4}\b", re.I)


def cast_from(event, user, lines, ip):
    return services.cast(event, services.Voter(user), hash_ip(ip), lines, user)


# --- IPs are stored only as hashes ------------------------------------------------------------------------

@pytest.mark.django_db
def test_no_ip_address_is_stored_anywhere(vote_event, client_for):
    ip = "203.0.113.77"
    client = client_for(vote_event.outsider, ip=ip)
    client.post(f"/participant/events/{vote_event.slug}/vote/cast", {f"p_{vote_event.projects_list[1].pk}": "4"})
    Client(REMOTE_ADDR=ip).post("/login", {"email": vote_event.outsider.email, "password": "wrong"})
    session = UserSession.objects.filter(user=vote_event.outsider).first()
    client.post(f"/account/sessions/{session.pk}/revoke")
    dumps = []
    for row in AuditLog.objects.all():
        dumps.append(json.dumps([row.ip_hash, row.subject, row.user_agent, row.detail], default=str))
    for row in UserSession.objects.all():
        dumps.append(json.dumps([row.ip_hash, row.user_agent], default=str))
    for row in Ballot.objects.all():
        dumps.append(json.dumps([row.ip_hash, row.created_ip_hash], default=str))
    everything = "\n".join(dumps)
    assert ip not in everything
    assert not IPV4.search(everything) and not IPV6.search(everything)
    assert AuditLog.objects.filter(ip_hash=hash_ip(ip)).exists()
    assert Ballot.objects.get().created_ip_hash == hash_ip(ip)


@pytest.mark.django_db
def test_login_throttle_still_counts_per_ip_hash(make_user):
    user = make_user()
    client = Client(REMOTE_ADDR="198.51.100.9")
    for _ in range(5):
        client.post("/login", {"email": user.email, "password": "wrong"})
    assert client.post("/login", {"email": user.email, "password": "wrong"}).status_code == 429
    assert AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED, ip_hash=hash_ip("198.51.100.9")).count() == 5


# --- rate limits ---------------------------------------------------------------------------------------------

@pytest.mark.django_db
@override_settings(VOTE_RATE_PER_VOTER=3, VOTE_RATE_PER_IP=100)
def test_per_voter_limit_is_429_and_audited(vote_event):
    p = vote_event.projects_list[1]
    for n in range(3):
        cast_from(vote_event, vote_event.outsider, {p.pk: n + 1}, "10.0.0.1")
    with pytest.raises(RateLimited) as caught:
        cast_from(vote_event, vote_event.outsider, {p.pk: 9}, "10.0.0.2")
    assert (caught.value.status, caught.value.code) == (429, "rate_limited")
    assert credits_of(vote_event, vote_event.outsider) == {p.pk: 3}
    entry = AuditLog.objects.get(action=AuditAction.VOTE_THROTTLED)
    assert entry.detail["limit"] == "actor"
    # refused writes count too, so hammering with bad ballots is limited the same way
    with pytest.raises(RateLimited):
        cast_from(vote_event, vote_event.outsider, {p.pk: 999}, "10.0.0.3")


@pytest.mark.django_db
@override_settings(VOTE_RATE_PER_VOTER=100, VOTE_RATE_PER_IP=3)
def test_per_ip_limit_across_voters(vote_event, old_user):
    p = vote_event.projects_list[1]
    for _ in range(3):
        cast_from(vote_event, old_user(), {p.pk: 1}, "10.9.9.9")
    with pytest.raises(RateLimited):
        cast_from(vote_event, old_user(), {p.pk: 1}, "10.9.9.9")
    cast_from(vote_event, old_user(), {p.pk: 1}, "10.9.9.10")  # another network is fine
    assert AuditLog.objects.get(action=AuditAction.VOTE_THROTTLED).detail["limit"] == "ip"


@pytest.mark.django_db
@override_settings(VOTE_RATE_PER_VOTER=2)
def test_api_returns_429_on_a_burst(vote_event, client_for):
    client = client_for(vote_event.outsider)
    url = f"/api/events/{vote_event.slug}/ballot"
    body = json.dumps({"lines": {str(vote_event.projects_list[1].pk): 1}})
    statuses = [client.post(url, body, content_type="application/json").status_code for _ in range(4)]
    assert statuses == [200, 200, 429, 429]
    assert client.post(url, body, content_type="application/json").json()["error"] == "rate_limited"


@pytest.mark.django_db
@override_settings(VOTE_RATE_PER_VOTER=1)
def test_late_write_is_409_even_when_rate_limited(vote_event):
    from voting.errors import VotingClosed

    p = vote_event.projects_list[1]
    cast_from(vote_event, vote_event.outsider, {p.pk: 1}, "10.0.0.1")
    shift_voting(vote_event, closes_at=-timedelta(seconds=1))
    with pytest.raises(VotingClosed):
        cast_from(vote_event, vote_event.outsider, {p.pk: 2}, "10.0.0.1")


# --- flags ------------------------------------------------------------------------------------------------------

def kinds(event):
    return sorted(f.kind for f in integrity.flags(event))


@pytest.mark.django_db
def test_ip_burst_is_flagged_but_empty_ballots_are_not(vote_event, old_user):
    p = vote_event.projects_list
    for _ in range(3):  # three opened-but-empty ballots from one network: identical, and ignored
        services.open_ballot(vote_event, services.Voter(old_user()), ip_hash=hash_ip("10.7.7.7"))
    assert kinds(vote_event) == []
    assert integrity.counts(vote_event)["empty"] == 3
    for i in range(3):
        cast_from(vote_event, old_user(), {p[1 + i].pk: 4}, "10.8.8.8")
    flags = integrity.flags(vote_event)
    assert [f.kind for f in flags] == ["ip_burst"] and len(flags[0].ballots) == 3


@pytest.mark.django_db
def test_identical_spread_ballots_are_flagged_single_project_ones_are_not(vote_event, old_user):
    p = vote_event.projects_list
    for i in range(3):
        cast_from(vote_event, old_user(), {p[1].pk: 16}, f"10.1.{i}.1")  # all on one project: common, not flagged
    assert kinds(vote_event) == []
    cast_from(vote_event, old_user(), {p[1].pk: 9, p[2].pk: 4}, "10.2.0.1")
    cast_from(vote_event, old_user(), {p[1].pk: 9, p[2].pk: 4}, "10.2.0.2")
    assert kinds(vote_event) == ["identical_ballots"]


@pytest.mark.django_db
def test_new_account_voting_at_once_is_flagged(vote_event, make_user):
    VotingConfig.objects.filter(pk=vote_event.config.pk).update(accounts_before_open_only=False)
    newcomer = make_user()  # joined now
    cast_from(vote_event, newcomer, {vote_event.projects_list[1].pk: 4}, "10.3.0.1")
    assert kinds(vote_event) == ["new_account"]


@pytest.mark.django_db
def test_voided_ballots_are_not_flagged(vote_event, old_user):
    p = vote_event.projects_list
    ballots = [cast_from(vote_event, old_user(), {p[1].pk: 9, p[2].pk: 4}, f"10.4.0.{i}") for i in range(2)]
    services.void_ballot(vote_event, ballots[0].pk, actor=vote_event.organizer, reason="duplicate voter")
    assert kinds(vote_event) == []


# --- voiding ------------------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_void_with_reason_leaves_the_tally_and_is_audited(vote_event):
    p = vote_event.projects_list[1]
    ballot = cast_from(vote_event, vote_event.outsider, {p.pk: 16}, "10.0.0.1")
    services.void_ballot(vote_event, ballot.pk, actor=vote_event.organizer, reason="sock puppet")
    ballot.refresh_from_db()
    assert ballot.voided_by == vote_event.organizer and ballot.void_reason == "sock puppet"
    assert all(r.influence == 0 for r in services.tally(vote_event, vote_event.organizer))
    entry = AuditLog.objects.get(action=AuditAction.BALLOT_VOIDED)
    assert entry.detail["credits"] == {str(p.pk): 16} and entry.detail["reason"] == "sock puppet"
    with pytest.raises(AlreadyVoided):
        services.void_ballot(vote_event, ballot.pk, actor=vote_event.organizer, reason="again")
    with pytest.raises(InvalidVoid):
        services.void_ballot(vote_event, ballot.pk, actor=vote_event.organizer, reason=" ")
    with pytest.raises(NoSuchBallot):
        services.void_ballot(vote_event, 999999, actor=vote_event.organizer, reason="missing")
    assert AuditLog.objects.filter(action=AuditAction.BALLOT_VOID_REFUSED).count() == 3
    assert Ballot.objects.filter(pk=ballot.pk).exists()  # kept


@pytest.mark.django_db
def test_voiding_works_after_voting_closes(vote_event):
    ballot = cast_from(vote_event, vote_event.outsider, {vote_event.projects_list[1].pk: 4}, "10.0.0.1")
    shift_voting(vote_event, closes_at=-timedelta(seconds=1))
    services.void_ballot(vote_event, ballot.pk, actor=vote_event.organizer, reason="found later")
    assert Ballot.objects.get(pk=ballot.pk).voided_at is not None


@pytest.mark.django_db
def test_only_this_events_organizers_void(vote_event, client_for, make_event):
    from django.core.exceptions import PermissionDenied

    ballot = cast_from(vote_event, vote_event.outsider, {vote_event.projects_list[1].pk: 4}, "10.0.0.1")
    url = f"/organizer/events/{vote_event.slug}/voting/ballots/{ballot.pk}/void"
    assert client_for(vote_event.voter).post(url, {"reason": "nope"}).status_code == 403
    assert client_for(vote_event.judge).post(url, {"reason": "nope"}).status_code == 403
    assert client_for(make_event().organizer).post(url, {"reason": "nope"}).status_code == 404
    with pytest.raises(PermissionDenied):
        services.void_ballot(vote_event, ballot.pk, actor=vote_event.judge, reason="nope")
    assert Ballot.objects.get(pk=ballot.pk).voided_at is None
    assert client_for(vote_event.organizer).post(url, {"reason": "checked"}).status_code == 302
    assert Ballot.objects.get(pk=ballot.pk).voided_at is not None


# --- the page -------------------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_integrity_page_shows_counts_flags_trail_and_positions(vote_event, client_for, old_user):
    p = vote_event.projects_list
    services.open_ballot(vote_event, services.Voter(old_user()), ip_hash=hash_ip("10.5.0.1"))
    for i in range(3):
        cast_from(vote_event, old_user(), {p[1].pk: 9, p[2].pk: 4}, "10.6.0.1")
    url = f"/organizer/events/{vote_event.slug}/voting/integrity"
    page = client_for(vote_event.organizer).get(url).content.decode()
    for text in ("ip_burst", "identical_ballots", "opened, nothing placed", "credits by position shown",
                 "Cast a vote", "audit trail"):
        assert text in page, text
    assert client_for(vote_event.voter).get(url).status_code == 403
    assert client_for(vote_event.judge).get(url).status_code == 403
    assert client_for(old_user(role=ADMIN)).get(url).status_code == 200


@pytest.mark.django_db
def test_position_bias_is_the_average_credits_per_shown_position(vote_event, old_user):
    p = vote_event.projects_list
    ballot = cast_from(vote_event, old_user(), {p[1].pk: 9, p[2].pk: 4}, "10.0.0.1")
    services.open_ballot(vote_event, services.Voter(old_user()), ip_hash="")  # empty: left out
    by_position = {line.shown_position + 1: line.credits for line in ballot.lines.all()}
    assert integrity.position_bias(vote_event) == [(pos, float(c), 1) for pos, c in sorted(by_position.items())]


# --- restoring ------------------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_void_then_restore_returns_the_tally_to_its_prior_value(vote_event, client_for, make_event):
    from voting.errors import NotVoided

    p = vote_event.projects_list
    ballot = cast_from(vote_event, vote_event.outsider, {p[1].pk: 9, p[2].pk: 7}, "10.0.0.1")
    cast_from(vote_event, vote_event.voter, {p[1].pk: 4}, "10.0.0.2")

    def snapshot():
        return [(r.project.pk, r.influence, r.ballots, r.credits) for r in services.tally(vote_event, vote_event.organizer)]

    before = snapshot()
    services.void_ballot(vote_event, ballot.pk, actor=vote_event.organizer, reason="looked odd")
    assert snapshot() != before
    url = f"/organizer/events/{vote_event.slug}/voting/ballots/{ballot.pk}/restore"
    assert client_for(vote_event.voter).post(url, {"reason": "nope"}).status_code == 403
    assert client_for(make_event().organizer).post(url, {"reason": "nope"}).status_code == 404
    organizer = client_for(vote_event.organizer)
    assert organizer.post(url, {"reason": "x"}).status_code == 302  # too short: refused, still voided
    assert Ballot.objects.get(pk=ballot.pk).voided_at is not None
    assert organizer.post(url, {"reason": "checked, genuine"}).status_code == 302
    assert snapshot() == before
    restored = Ballot.objects.get(pk=ballot.pk)
    assert restored.voided_at is None and restored.voided_by is None and restored.void_reason == ""
    entry = AuditLog.objects.get(action=AuditAction.BALLOT_RESTORED)
    assert entry.detail["undid"]["void_reason"] == "looked odd" and entry.detail["reason"] == "checked, genuine"
    with pytest.raises(NotVoided):
        services.restore_ballot(vote_event, ballot.pk, actor=vote_event.organizer, reason="again")
    assert AuditLog.objects.filter(action=AuditAction.BALLOT_RESTORE_REFUSED).count() == 2
    assert "checked, genuine" in organizer.get(f"/organizer/events/{vote_event.slug}/voting/integrity").content.decode()


@pytest.mark.django_db
def test_restore_works_after_voting_closes(vote_event):
    ballot = cast_from(vote_event, vote_event.outsider, {vote_event.projects_list[1].pk: 4}, "10.0.0.1")
    services.void_ballot(vote_event, ballot.pk, actor=vote_event.organizer, reason="looked odd")
    shift_voting(vote_event, closes_at=-timedelta(seconds=1))
    services.restore_ballot(vote_event, ballot.pk, actor=vote_event.organizer, reason="genuine after all")
    assert Ballot.objects.get(pk=ballot.pk).voided_at is None
