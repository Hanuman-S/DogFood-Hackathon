"""Voting access modes (T3 stage 3): email-gated links and the open link, including a reused,
revoked or foreign token, and the account rules still applying to an allowlisted email."""

import csv
import io
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from accounts.roles import Role
from core.models import AuditAction, AuditLog
from events.models import EventMembership
from test_voting import backdate, old_user, vote_event  # noqa: F401 -- fixtures
from voting import links, services
from voting.errors import InvalidAllowlist, WrongAccessMode
from voting.models import AccessMode, Ballot, BallotLine, VoterLink, VotingConfig


def set_mode(event, mode):
    VotingConfig.objects.filter(event=event).update(access_mode=mode, open_link_nonce=links.new_nonce())
    event.config.refresh_from_db()


def credits(ballot):
    return {line.project_id: line.credits for line in ballot.lines.all() if line.credits}


@pytest.fixture
def gated(vote_event):
    set_mode(vote_event, AccessMode.EMAIL_GATED)
    return vote_event


def token_of(event, email):
    return links.link_token(VoterLink.objects.get(event=event, email=email))


def link_url(event, token):
    return f"/events/{event.slug}/vote/{token}"


# --- email_gated ----------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_allowlist_from_paste_and_csv(gated):
    created, reissued, unchanged, rejected = services.add_voter_links(
        gated, actor=gated.organizer, text="A@example.org, b@example.org; not-an-email\nA@example.org",
        csv_bytes="name,email\nCee,c@example.org\n".encode())
    assert created == ["a@example.org", "b@example.org", "c@example.org"]
    assert rejected == ["not-an-email"] and not reissued and not unchanged
    again = services.add_voter_links(gated, actor=gated.organizer, text="a@example.org")
    assert again[2] == ["a@example.org"]
    with pytest.raises(InvalidAllowlist):
        services.add_voter_links(gated, actor=gated.organizer, text="nothing here")
    assert AuditLog.objects.filter(action=AuditAction.VOTER_LINKS_ADDED).count() == 2


@pytest.mark.django_db
def test_link_votes_without_login_and_reuse_is_the_same_ballot(gated):
    services.add_voter_links(gated, actor=gated.organizer, text="guest@example.org")
    url = link_url(gated, token_of(gated, "guest@example.org"))
    p = gated.projects_list
    visitor = Client()
    assert visitor.get(url).status_code == 200
    assert not Ballot.objects.exists()  # a GET writes nothing
    assert visitor.post(url + "/cast", {f"p_{p[1].pk}": "9"}).status_code == 302
    # the same link again, from another browser: the same ballot, changed
    other = Client()
    assert other.post(url + "/cast", {f"p_{p[2].pk}": "4"}).status_code == 302
    ballot = Ballot.objects.get()
    assert ballot.voter_link.email == "guest@example.org" and ballot.voter_user is None
    assert credits(ballot) == {p[2].pk: 4}
    assert AuditLog.objects.filter(action=AuditAction.VOTE_CHANGED).count() == 1


@pytest.mark.django_db
def test_revoked_link_is_refused_and_reissue_makes_a_new_link(gated):
    services.add_voter_links(gated, actor=gated.organizer, text="guest@example.org")
    old = token_of(gated, "guest@example.org")
    link = VoterLink.objects.get()
    services.revoke_voter_link(gated, link.pk, actor=gated.organizer)
    p = gated.projects_list
    page = Client().get(link_url(gated, old))
    assert page.status_code == 200 and "revoked" in page.content.decode()
    Client().post(link_url(gated, old) + "/cast", {f"p_{p[1].pk}": "1"})
    assert not Ballot.objects.exists()
    assert AuditLog.objects.get(action=AuditAction.VOTE_REFUSED, detail__reason="link_revoked")
    services.add_voter_links(gated, actor=gated.organizer, text="guest@example.org")
    new = token_of(gated, "guest@example.org")
    assert new != old
    assert Client().get(link_url(gated, old)).status_code == 404
    assert Client().get(link_url(gated, new)).status_code == 200


@pytest.mark.django_db
def test_foreign_and_unknown_tokens_are_404(gated, make_event):
    other = make_event()
    VotingConfig.objects.create(event=other, opens_at=gated.config.opens_at, closes_at=gated.config.closes_at,
                                access_mode=AccessMode.EMAIL_GATED, ballot_secret="x" * 64)
    services.add_voter_links(other, actor=other.organizer, text="guest@example.org")
    foreign = token_of(other, "guest@example.org")
    for token in (foreign, "0" * 64, "short", "x" * 500):
        assert Client().get(link_url(gated, token)).status_code == 404, token[:10]
        assert Client().post(link_url(gated, token) + "/cast", {}).status_code == 404
    assert not Ballot.objects.exists()


@pytest.mark.django_db
def test_allowlisted_email_of_an_account_keeps_the_account_rules(gated):
    judge_email, member_email = gated.judge.email, gated.voter.email
    services.add_voter_links(gated, actor=gated.organizer, text=f"{judge_email}\n{member_email}")
    p = gated.projects_list
    Client().post(link_url(gated, token_of(gated, judge_email)) + "/cast", {f"p_{p[1].pk}": "1"})
    assert not Ballot.objects.filter(voter_link__email=judge_email).exists()
    member = Client()
    member.post(link_url(gated, token_of(gated, member_email)) + "/open")
    ballot = Ballot.objects.get(voter_link__email=member_email)
    assert p[0].pk not in set(ballot.lines.values_list("project_id", flat=True))  # their own project
    member.post(link_url(gated, token_of(gated, member_email)) + "/cast", {f"p_{p[0].pk}": "1"})
    assert credits(ballot) == {}
    reasons = set(AuditLog.objects.filter(action=AuditAction.VOTE_REFUSED).values_list("detail__reason", flat=True))
    assert {"staff_cannot_vote"} <= reasons


@pytest.mark.django_db
def test_logged_in_voting_is_refused_when_the_vote_is_by_link(gated, client_for):
    with pytest.raises(WrongAccessMode):
        services.cast(gated, services.Voter(gated.outsider), "", {gated.projects_list[1].pk: 1}, gated.outsider)
    page = client_for(gated.outsider).get(f"/participant/events/{gated.slug}/vote").content.decode()
    assert "personal link" in page


@pytest.mark.django_db
def test_voter_links_csv_is_organizer_only_and_escaped(gated, client_for):
    services.add_voter_links(gated, actor=gated.organizer, text="guest@example.org")
    VoterLink.objects.filter(email="guest@example.org").update(email="=cmd@example.org")
    url = f"/organizer/events/{gated.slug}/voting/voter-links.csv"
    assert client_for(gated.voter).get(url).status_code == 403
    assert client_for(gated.judge).get(url).status_code == 403
    response = client_for(gated.organizer).get(url)
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == services.VOTER_LINKS_HEADER
    assert rows[1][0] == "'=cmd@example.org"
    token = links.link_token(VoterLink.objects.get())
    assert rows[1][1].endswith(f"/events/{gated.slug}/vote/{token}")
    assert AuditLog.objects.filter(action=AuditAction.VOTER_LINKS_EXPORTED).count() == 1


@pytest.mark.django_db
def test_only_this_events_organizers_manage_links(gated, client_for, make_event):
    other = client_for(make_event().organizer)
    assert other.post(f"/organizer/events/{gated.slug}/voting/links", {"emails": "x@example.org"}).status_code == 404
    assert client_for(gated.voter).post(f"/organizer/events/{gated.slug}/voting/links",
                                        {"emails": "x@example.org"}).status_code == 403
    assert not VoterLink.objects.exists()


# --- open_link ---------------------------------------------------------------------------------------------

@pytest.fixture
def open_event(vote_event):
    set_mode(vote_event, AccessMode.OPEN_LINK)
    return vote_event


@pytest.mark.django_db
def test_open_link_cookie_identifies_the_voter(open_event):
    url = link_url(open_event, links.open_token(open_event.config))
    p = open_event.projects_list
    browser = Client()
    page = browser.get(url)
    assert page.status_code == 200 and "weakest" in page.content.decode()
    assert links.cookie_name(open_event) in page.cookies
    assert not Ballot.objects.exists()  # the cookie is not a database write
    browser.post(url + "/cast", {f"p_{p[1].pk}": "4"})
    browser.post(url + "/cast", {f"p_{p[2].pk}": "9"})
    ballot = Ballot.objects.get()
    assert ballot.voter_cookie and ballot.voter_user is None and credits(ballot) == {p[2].pk: 9}
    # another browser is another voter -- the mode's known weakness
    other = Client()
    other.get(url)
    other.post(url + "/cast", {f"p_{p[1].pk}": "1"})
    assert Ballot.objects.count() == 2


@pytest.mark.django_db
def test_open_link_needs_a_valid_cookie_and_rejects_tampering(open_event):
    url = link_url(open_event, links.open_token(open_event.config))
    p = open_event.projects_list
    Client().post(url + "/cast", {f"p_{p[1].pk}": "1"})  # no cookie at all
    tampered = Client()
    tampered.cookies[links.cookie_name(open_event)] = "forged-value"
    tampered.post(url + "/cast", {f"p_{p[1].pk}": "1"})
    assert not Ballot.objects.exists()


@pytest.mark.django_db
def test_rotated_open_link_stops_the_old_one(open_event):
    old = links.open_token(open_event.config)
    services.rotate_open_link(open_event, actor=open_event.organizer)
    open_event.config.refresh_from_db()
    assert Client().get(link_url(open_event, old)).status_code == 404
    assert Client().get(link_url(open_event, links.open_token(open_event.config))).status_code == 200
    assert AuditLog.objects.filter(action=AuditAction.OPEN_LINK_ROTATED).count() == 1


@pytest.mark.django_db
def test_open_link_still_refuses_a_logged_in_judge(open_event, client_for):
    url = link_url(open_event, links.open_token(open_event.config))
    judge = client_for(open_event.judge)
    judge.get(url)
    judge.post(url + "/cast", {f"p_{open_event.projects_list[1].pk}": "1"})
    assert not Ballot.objects.exists()


@pytest.mark.django_db
def test_setting_up_open_link_mode_makes_the_link(make_event):
    event = make_event()
    config = services.set_voting_config(
        event, actor=event.organizer, opens_at=event.submissions_close_at,
        closes_at=event.submissions_close_at + timedelta(days=2), access_mode="open_link", method="quadratic")
    assert config.open_link_nonce


@pytest.mark.django_db
def test_allowlist_is_the_gate_for_link_voters_not_account_age(gated, make_user):
    """An account created after voting opened is refused when logged in (the option is on) but may
    vote with its allowlisted link: for link voters only the staff and own-team rules apply."""
    from voting.errors import AccountTooNew

    newcomer = make_user(email="newcomer@example.org")  # date_joined = now > opens_at
    assert newcomer.date_joined >= gated.config.opens_at and gated.config.accounts_before_open_only
    assert isinstance(services.ineligibility(gated, gated.config, newcomer), AccountTooNew)  # logged in
    services.add_voter_links(gated, actor=gated.organizer, text="newcomer@example.org")
    url = link_url(gated, token_of(gated, "newcomer@example.org"))
    p = gated.projects_list
    assert Client().post(url + "/cast", {f"p_{p[1].pk}": "4"}).status_code == 302
    ballot = Ballot.objects.get(voter_link__email="newcomer@example.org")
    assert credits(ballot) == {p[1].pk: 4}
