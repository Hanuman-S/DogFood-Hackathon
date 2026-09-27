"""Community voting, core (T3 stage 2): the config rules, casting and changing a vote, the window
(service first, trigger backstop), who may vote, the per-ballot order, and hidden tallies."""

import json
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction
from django.test import Client
from django.utils import timezone

from accounts.models import User
from accounts.roles import ADMIN, Role
from core.models import AuditAction, AuditLog
from events.models import Event, EventMembership
from events.services import EventRuleError, end_judging_now
from projects.models import Project, Status
from teams.models import TeamExtension
from voting import services
from voting.errors import (AccountTooNew, InvalidBallot, InvalidVotingConfig, OverBudget,
                           OwnProject, StaffCannotVote, VotingClosed, VotingConfigLocked, VotingNotOpen, VotingOpen)
from voting.models import Ballot, BallotLine, Method, VotingConfig

TX = pytest.mark.django_db(transaction=True)


def backdate(user, days=30):
    User.objects.filter(pk=user.pk).update(date_joined=timezone.now() - timedelta(days=days))
    user.refresh_from_db()
    return user


@pytest.fixture
def old_user(make_user):
    def make(role=Role.PARTICIPANT, **kwargs):
        return backdate(make_user(role=role, **kwargs))
    return make


@pytest.fixture
def vote_event(make_event, make_team, old_user):
    """Submissions closed 2 days ago, judging open, voting open since an hour ago (quadratic, 16
    credits, accounts before opening only). Six submitted projects; `voter` is on the first team."""
    event = make_event()
    backdate(event.organizer)
    now = timezone.now()
    projects = []
    for i in range(6):
        team = make_team(event, captain=old_user(), name=f"Team {i}")
        projects.append(Project.objects.create(team=team, event=event, name=f"P{i}", status=Status.SUBMITTED,
                                               submitted_at=now))
    Event.objects.filter(pk=event.pk).update(
        starts_at=now - timedelta(days=6), submissions_open_at=now - timedelta(days=5),
        submissions_close_at=now - timedelta(days=2), judging_starts_at=now - timedelta(days=1),
        judging_ends_at=now + timedelta(days=5))
    event.refresh_from_db()
    event.config = VotingConfig.objects.create(
        event=event, opens_at=now - timedelta(hours=1), closes_at=now + timedelta(days=2),
        method=Method.QUADRATIC, credit_budget=16, ballot_secret="s" * 64, accounts_before_open_only=True)
    event.projects_list = projects
    event.voter = projects[0].team.captain
    event.judge = old_user(role=Role.JUDGE)
    EventMembership.objects.create(user=event.judge, event=event, role=Role.JUDGE)
    event.outsider = old_user()  # any account, on no team of this event
    return event


def voter(user):
    return services.Voter(user)


def cast(event, user, lines):
    return services.cast(event, voter(user), "h" * 64, lines, user)


def credits_of(event, user):
    ballot = Ballot.objects.get(event=event, voter_user=user)
    return {line.project_id: line.credits for line in ballot.lines.all() if line.credits}


def shift_voting(event, **deltas):
    """Move the window in a test. Once voting has opened the config row is guarded by the trigger,
    so this goes through the audited bypass, the same way a real repair would."""
    now = timezone.now()
    with services.voting_bypass("test: move the voting window"):
        VotingConfig.objects.filter(event=event).update(**{k: now + v for k, v in deltas.items()})
    event.config.refresh_from_db()


# --- casting ------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_cast_change_and_tally_with_sqrt_influence(vote_event):
    p = vote_event.projects_list
    cast(vote_event, vote_event.outsider, {p[1].pk: 9, p[2].pk: 4})
    cast(vote_event, vote_event.voter, {p[1].pk: 16})
    rows = {r.project.pk: r for r in services.tally(vote_event, vote_event.organizer)}
    assert rows[p[1].pk].influence == pytest.approx(3 + 4)
    assert rows[p[2].pk].influence == pytest.approx(2)
    assert (rows[p[1].pk].ballots, rows[p[1].pk].credits) == (2, 25)
    # a change replaces the whole ballot and is audited with the previous values
    cast(vote_event, vote_event.outsider, {p[3].pk: 1})
    assert credits_of(vote_event, vote_event.outsider) == {p[3].pk: 1}
    changed = AuditLog.objects.get(action=AuditAction.VOTE_CHANGED)
    assert changed.detail["before"] == {str(p[1].pk): 9, str(p[2].pk): 4}
    assert changed.detail["after"] == {str(p[3].pk): 1}
    assert AuditLog.objects.filter(action=AuditAction.VOTE_CAST).count() == 2


@pytest.mark.django_db
def test_one_person_one_vote_is_a_budget_of_one(vote_event):
    VotingConfig.objects.filter(pk=vote_event.config.pk).update(method=Method.ONE_PERSON_ONE_VOTE, credit_budget=1)
    p = vote_event.projects_list
    cast(vote_event, vote_event.outsider, {p[1].pk: 1})
    with pytest.raises(OverBudget):
        cast(vote_event, vote_event.outsider, {p[1].pk: 1, p[2].pk: 1})
    cast(vote_event, vote_event.outsider, {p[2].pk: 1})  # switching is a replacement
    assert credits_of(vote_event, vote_event.outsider) == {p[2].pk: 1}


@pytest.mark.django_db
def test_over_budget_and_bad_lines_are_400_and_audited(vote_event):
    p = vote_event.projects_list
    for lines, error in [({p[1].pk: 9, p[2].pk: 8}, OverBudget), ({p[1].pk: 17}, OverBudget),
                         ({p[1].pk: -1}, InvalidBallot), ({p[1].pk: "x"}, InvalidBallot),
                         ({"abc": 1}, InvalidBallot), ({999999: 1}, InvalidBallot), ([1, 2], InvalidBallot)]:
        with pytest.raises(error) as caught:
            cast(vote_event, vote_event.outsider, lines)
        assert caught.value.status == 400
    assert AuditLog.objects.filter(action=AuditAction.VOTE_REFUSED).count() == 7
    assert not credits_of_any(vote_event)


def credits_of_any(event):
    return BallotLine.objects.filter(ballot__event=event, credits__gt=0).exists()


@pytest.mark.django_db
def test_own_project_refused_and_not_on_the_ballot(vote_event):
    own = vote_event.projects_list[0]
    with pytest.raises(OwnProject) as caught:
        cast(vote_event, vote_event.voter, {own.pk: 1})
    assert caught.value.status == 403
    ballot = services.open_ballot(vote_event, voter(vote_event.voter))
    assert own.pk not in set(ballot.lines.values_list("project_id", flat=True))
    assert ballot.lines.count() == 5


@pytest.mark.django_db
def test_judges_organizers_and_admins_cannot_vote(vote_event, old_user):
    p = vote_event.projects_list[1]
    for user in (vote_event.judge, vote_event.organizer, old_user(role=ADMIN)):
        with pytest.raises(StaffCannotVote) as caught:
            cast(vote_event, user, {p.pk: 1})
        assert caught.value.status == 403
    assert not Ballot.objects.exists()


@pytest.mark.django_db
def test_accounts_created_after_voting_opened_are_refused_when_the_option_is_on(vote_event, make_user):
    newcomer = make_user()
    with pytest.raises(AccountTooNew):
        cast(vote_event, newcomer, {vote_event.projects_list[1].pk: 1})
    VotingConfig.objects.filter(pk=vote_event.config.pk).update(accounts_before_open_only=False)
    cast(vote_event, newcomer, {vote_event.projects_list[1].pk: 1})


# --- the window ------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_window_is_checked_first_and_audited(vote_event):
    p = vote_event.projects_list[1]
    shift_voting(vote_event, opens_at=timedelta(hours=1), closes_at=timedelta(days=1))
    with pytest.raises(VotingNotOpen) as caught:
        cast(vote_event, vote_event.judge, {p.pk: 99})  # a judge, over budget: still "not open"
    assert (caught.value.status, caught.value.code) == (409, "voting_not_open")
    shift_voting(vote_event, opens_at=-timedelta(days=1), closes_at=-timedelta(seconds=1))
    with pytest.raises(VotingClosed) as caught:
        cast(vote_event, vote_event.judge, {p.pk: 99})
    assert (caught.value.status, caught.value.code) == (409, "voting_closed")
    assert AuditLog.objects.filter(action=AuditAction.VOTE_LATE_REFUSED).count() == 2


@pytest.mark.django_db
def test_trigger_refuses_writes_outside_the_window_but_allows_voiding(vote_event):
    p = vote_event.projects_list[1]
    ballot = cast(vote_event, vote_event.outsider, {p.pk: 4})
    shift_voting(vote_event, opens_at=-timedelta(days=1), closes_at=-timedelta(seconds=1))
    with pytest.raises(DatabaseError, match="dogfood_voting_closed"), transaction.atomic():
        BallotLine.objects.filter(ballot=ballot).update(credits=9)
    with pytest.raises(DatabaseError, match="dogfood_voting_closed"), transaction.atomic():
        Ballot.objects.filter(pk=ballot.pk).update(ip_hash="changed", voided_at=timezone.now(),
                                                   voided_by=vote_event.organizer)
    # voiding alone is allowed after the close
    Ballot.objects.filter(pk=ballot.pk).update(voided_at=timezone.now(), voided_by=vote_event.organizer,
                                               void_reason="test")
    assert Ballot.objects.get(pk=ballot.pk).voided_at is not None
    assert services.tally(vote_event, vote_event.organizer)[0].influence == 0


@pytest.mark.django_db
def test_voided_ballots_are_left_out_of_the_tally(vote_event):
    p = vote_event.projects_list[1]
    ballot = cast(vote_event, vote_event.outsider, {p.pk: 16})
    Ballot.objects.filter(pk=ballot.pk).update(voided_at=timezone.now(), voided_by=vote_event.organizer)
    assert all(r.influence == 0 for r in services.tally(vote_event, vote_event.organizer))


# --- order --------------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_ballot_order_is_per_ballot_stable_and_stored(vote_event, client_for):
    config = vote_event.config
    ids = list(range(1, 21))
    assert services.ballot_order(config, 1, ids) == services.ballot_order(config, 1, ids)
    assert services.ballot_order(config, 1, ids) != services.ballot_order(config, 2, ids)
    other_event_secret = VotingConfig(ballot_secret="t" * 64)
    assert services.ballot_order(config, 1, ids) != services.ballot_order(other_event_secret, 1, ids)

    client = client_for(vote_event.outsider)
    url = f"/participant/events/{vote_event.slug}/vote"
    assert client.get(url).status_code == 200
    assert not Ballot.objects.exists()  # a GET never creates a ballot
    assert client.post(url + "/open").status_code == 302
    ballot = Ballot.objects.get()
    stored = list(ballot.lines.order_by("shown_position").values_list("project_id", flat=True))
    assert stored == services.ballot_order(vote_event.config, ballot.pk, sorted(stored))
    import re

    def shown(): return [int(x) for x in re.findall(r'name="p_(\d+)"', client.get(url).content.decode())]
    assert shown() == stored and shown() == stored  # the page shows the stored order, on every reload


# --- config rules --------------------------------------------------------------------------------------------

def configure(event, **overrides):
    now = timezone.now()
    values = dict(opens_at=event.submissions_close_at, closes_at=now + timedelta(days=3), access_mode="authenticated",
                  method="quadratic", credit_budget=16, accounts_before_open_only=True)
    values.update(overrides)
    return services.set_voting_config(event, actor=event.organizer, **values)


@pytest.mark.django_db
def test_config_rules(make_event):
    now = timezone.now()
    event = make_event()  # submissions close in 2 days
    with pytest.raises(InvalidVotingConfig):
        configure(event, opens_at=event.submissions_close_at - timedelta(minutes=1))
    with pytest.raises(InvalidVotingConfig):
        configure(event, closes_at=event.submissions_close_at)
    with pytest.raises(InvalidVotingConfig):
        configure(event, access_mode="carrier_pigeon")
    with pytest.raises(InvalidVotingConfig):
        configure(event, credit_budget=0)
    config = configure(event, method="one_person_one_vote", credit_budget=16)
    assert config.budget == 1 and config.credit_budget == 1
    assert AuditLog.objects.filter(action=AuditAction.VOTING_CONFIG_REFUSED).count() == 4
    # overlapping judging is fine
    configure(event, closes_at=event.judging_ends_at + timedelta(days=1))
    # a team extension past voting's opening is refused
    with pytest.raises(EventRuleError):
        from events.services import grant_extension
        grant_extension(None, event, _team(event), event.submissions_close_at + timedelta(minutes=30), "late")
    assert not TeamExtension.objects.exists()
    with pytest.raises(PermissionDenied):
        services.set_voting_config(event, actor=make_event().organizer, opens_at=event.submissions_close_at,
                                   closes_at=now + timedelta(days=3), access_mode="authenticated",
                                   method="quadratic")


def _team(event):
    from teams.models import Team
    return Team.objects.create(event=event, name="Late team", captain=event.organizer)


@pytest.mark.django_db
def test_extension_counts_toward_the_earliest_opening(make_event, make_team):
    event = make_event()
    team = make_team(event)
    until = event.submissions_close_at + timedelta(minutes=30)
    TeamExtension.objects.create(team=team, until=until, reason="upload failed")
    with pytest.raises(InvalidVotingConfig):
        configure(event, opens_at=event.submissions_close_at)
    assert configure(event, opens_at=until).opens_at == until


@pytest.mark.django_db
def test_extending_the_deadline_past_voting_is_refused(make_event, client_for):
    from events.services import extend_deadline

    event = make_event()
    configure(event)
    with pytest.raises(EventRuleError):
        extend_deadline(None, event, event.submissions_close_at + timedelta(hours=1), "more time")
    assert AuditLog.objects.filter(action=AuditAction.EVENT_CHANGE_REFUSED).exists()


@pytest.mark.django_db
def test_once_open_only_the_close_can_change_and_once_closed_nothing(vote_event):
    event, config = vote_event, vote_event.config
    base = dict(opens_at=config.opens_at, closes_at=config.closes_at, access_mode="authenticated",
                method="quadratic", credit_budget=16, accounts_before_open_only=True)
    with pytest.raises(VotingConfigLocked):
        services.set_voting_config(event, actor=event.organizer, **{**base, "credit_budget": 25})
    with pytest.raises(VotingConfigLocked):
        services.set_voting_config(event, actor=event.organizer, **{**base, "method": "one_person_one_vote"})
    later = config.closes_at + timedelta(days=1)
    assert services.set_voting_config(event, actor=event.organizer, **{**base, "closes_at": later}).closes_at == later
    shift_voting(event, closes_at=-timedelta(seconds=1))
    with pytest.raises(VotingConfigLocked):
        services.set_voting_config(event, actor=event.organizer, **{**base, "closes_at": later})


@pytest.mark.django_db
def test_end_voting_now(vote_event, client_for, make_user):
    url = f"/organizer/events/{vote_event.slug}/voting/end"
    before = vote_event.config.closes_at
    assert client_for(make_user()).post(url).status_code == 403
    assert client_for(vote_event.judge).post(url).status_code == 403
    VotingConfig.objects.get(pk=vote_event.config.pk)
    assert VotingConfig.objects.get(pk=vote_event.config.pk).closes_at == before
    assert client_for(vote_event.organizer).post(url).status_code == 302
    config = VotingConfig.objects.get(pk=vote_event.config.pk)
    assert config.closes_at < before and config.original_closes_at == before
    with pytest.raises(VotingConfigLocked):
        services.end_voting_now(vote_event, actor=vote_event.organizer)
    with pytest.raises(VotingClosed):
        cast(vote_event, vote_event.outsider, {vote_event.projects_list[1].pk: 1})
    assert AuditLog.objects.filter(action=AuditAction.VOTING_ENDED_EARLY).count() == 1
    assert AuditLog.objects.filter(action=AuditAction.VOTING_END_REFUSED).count() == 1


@pytest.mark.django_db
def test_remove_voting_only_before_it_opens(make_event, vote_event):
    event = make_event()
    configure(event)
    services.remove_voting(event, actor=event.organizer)
    assert not VotingConfig.objects.filter(event=event).exists()
    with pytest.raises(VotingConfigLocked):
        services.remove_voting(vote_event, actor=vote_event.organizer)


# --- publishing final results waits for the vote ---------------------------------------------------------------

@TX
def test_publishing_final_results_is_refused_while_voting_is_open(vote_event):
    from scoring import services as scoring
    from scoring.models import Criterion

    Criterion.objects.create(event=vote_event, key="q", label="q", weight=1)
    end_judging_now(vote_event, actor=vote_event.organizer)
    snapshot = scoring.compute_snapshot(vote_event, "final", actor=vote_event.organizer)
    with pytest.raises(VotingOpen) as caught:
        scoring.publish_results(vote_event, snapshot.pk, actor=vote_event.organizer)
    assert (caught.value.status, caught.value.code) == (409, "voting_open")
    assert AuditLog.objects.get(action=AuditAction.RESULTS_PUBLISH_REFUSED).detail["reason"] == "voting_open"
    services.end_voting_now(vote_event, actor=vote_event.organizer)
    scoring.publish_results(vote_event, snapshot.pk, actor=vote_event.organizer)


# --- hidden tallies ------------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_tally_is_hidden_from_everyone_but_organizers_and_admins(vote_event, client_for, make_event, old_user):
    cast(vote_event, vote_event.outsider, {vote_event.projects_list[1].pk: 16})
    urls = [f"/api/events/{vote_event.slug}/votes/tally", f"/organizer/events/{vote_event.slug}/voting/tally.csv",
            f"/organizer/events/{vote_event.slug}/voting"]
    expected = {
        "participant": (client_for(vote_event.voter), 403),
        "judge": (client_for(vote_event.judge), 403),
        "other organizer": (client_for(make_event().organizer), 404),
        "organizer": (client_for(vote_event.organizer), 200),
        "admin": (client_for(old_user(role=ADMIN)), 200),
    }
    for url in urls:
        anonymous = Client().get(url)
        assert anonymous.status_code in (302, 401), url
        for who, (client, status) in expected.items():
            assert client.get(url).status_code == status, (who, url)
    body = client_for(vote_event.organizer).get(urls[0]).json()
    assert body["projects"][0]["influence"] == 4.0
    with pytest.raises(PermissionDenied):
        services.tally(vote_event, vote_event.voter)


@pytest.mark.django_db
def test_gallery_and_project_pages_never_show_votes(vote_event):
    p = vote_event.projects_list[1]
    cast(vote_event, vote_event.outsider, {p.pk: 16})
    for url in ("/projects", f"/projects/{p.pk}", f"/api/projects/{p.pk}", f"/api/projects?event={vote_event.slug}",
                f"/events/{vote_event.slug}"):
        body = Client().get(url).content.decode().lower()
        assert "influence" not in body and "ballot" not in body and "credits" not in body, url


# --- the API and the page --------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_api_cast_and_error_codes(vote_event, client_for, old_user):
    url = f"/api/events/{vote_event.slug}/ballot"
    p = vote_event.projects_list
    client = client_for(vote_event.voter)

    def post(c, lines):
        return c.post(url, json.dumps({"lines": lines}), content_type="application/json")

    assert client.get(url).json()["ballot"] is None
    ok = post(client, {str(p[1].pk): 4, str(p[2].pk): 12})
    assert ok.status_code == 200 and ok.json()["ballot"]["spent"] == 16
    assert post(client, {str(p[1].pk): 17}).json()["error"] == "over_budget"
    assert post(client, {str(p[0].pk): 1}).status_code == 403
    assert post(client_for(vote_event.judge), {str(p[1].pk): 1}).json()["error"] == "staff_cannot_vote"
    assert Client().get(url).status_code == 401
    assert post(client_for(old_user(role=ADMIN)), {str(p[1].pk): 1}).status_code == 403
    shift_voting(vote_event, closes_at=-timedelta(seconds=1))
    late = post(client, {str(p[1].pk): 1})
    assert (late.status_code, late.json()["error"]) == (409, "voting_closed")


@pytest.mark.django_db
def test_voting_page_casts_through_the_form(vote_event, client_for):
    p = vote_event.projects_list
    client = client_for(vote_event.outsider)
    url = f"/participant/events/{vote_event.slug}/vote"
    client.post(url + "/open")
    assert client.post(url + "/cast", {f"p_{p[1].pk}": "9", f"p_{p[2].pk}": "7"}).status_code == 302
    assert credits_of(vote_event, vote_event.outsider) == {p[1].pk: 9, p[2].pk: 7}
    page = client.get(url).content.decode()
    assert "16 of 16 credits placed" in page


# --- concurrency and the database admin -------------------------------------------------------------------------

@TX
def test_concurrent_first_casts_make_one_ballot_within_budget(vote_event):
    """Two tabs cast the same voter's first ballot at the same instant: one creates the ballot, the
    other hits the unique constraint, retries on the now-existing row (locked), and replaces it.
    Exactly one ballot, and its credits are one of the two casts -- never their sum."""
    import threading

    from django.db import connection

    p = vote_event.projects_list
    casts = [{p[1].pk: 16}, {p[2].pk: 16}]
    barrier, errors = threading.Barrier(2), []

    def run(lines):
        try:
            barrier.wait()
            cast(vote_event, vote_event.outsider, lines)
        except Exception as error:  # noqa: BLE001 -- reported below
            errors.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(lines,)) for lines in casts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert Ballot.objects.filter(event=vote_event, voter_user=vote_event.outsider).count() == 1
    assert credits_of(vote_event, vote_event.outsider) in casts
    assert sum(credits_of(vote_event, vote_event.outsider).values()) <= 16


@pytest.mark.django_db
def test_database_admin_refuses_to_delete_a_voter_or_a_ballot(vote_event, client_for, old_user):
    """Ballot.voter_user is PROTECT: the database admin shows its "protected objects" page instead of
    deleting (or crashing), on the single delete and the bulk action. Ballots have no delete there."""
    ballot = cast(vote_event, vote_event.outsider, {vote_event.projects_list[1].pk: 4})
    admin = client_for(old_user(role=ADMIN))
    url = f"/admin/db/accounts/user/{vote_event.outsider.pk}/delete/"
    page = admin.get(url)
    assert page.status_code == 200 and "cannot delete user" in page.content.decode().lower()
    assert admin.post(url, {"post": "yes"}).status_code == 200
    bulk = admin.post("/admin/db/accounts/user/", {"action": "delete_selected", "_selected_action": [vote_event.outsider.pk],
                                                   "post": "yes"})
    assert bulk.status_code in (200, 302)
    assert User.objects.filter(pk=vote_event.outsider.pk).exists()
    assert admin.get(f"/admin/db/voting/ballot/{ballot.pk}/delete/").status_code == 403
    assert admin.get(f"/admin/db/voting/ballot/{ballot.pk}/change/").status_code == 200
    assert Ballot.objects.filter(pk=ballot.pk).exists()


@pytest.mark.django_db
def test_end_voting_now_is_the_one_way_to_close_at_once(vote_event):
    """Editing the close while voting is open only moves it to a future time; "end voting now" is
    the one exception, and it closes at the database clock's current instant."""
    config = vote_event.config
    base = dict(opens_at=config.opens_at, access_mode="authenticated", method="quadratic", credit_budget=16,
                accounts_before_open_only=True)
    with pytest.raises(VotingConfigLocked):
        services.set_voting_config(vote_event, actor=vote_event.organizer,
                                   closes_at=timezone.now() - timedelta(seconds=1), **base)
    ended = services.end_voting_now(vote_event, actor=vote_event.organizer)
    assert ended.closes_at <= timezone.now() + timedelta(seconds=1)
    assert services.state(ended, timezone.now() + timedelta(seconds=1)) == "closed"



# --- votes cannot be deleted once voting has opened ------------------------------------------------------------

def tally_snapshot(event):
    return [(r.project.pk, r.influence, r.ballots) for r in services.tally(event, event.organizer)]


@pytest.mark.django_db
def test_leave_and_delete_team_with_voted_project_is_refused_and_tally_unchanged(vote_event, client_for):
    """The last member leaving deletes the team (and its project). After voting opens that is refused
    -- first by the submission deadline (409), and under the deadline bypass by PROTECT -- and the
    tally does not move."""
    from django.db.models import ProtectedError

    from core.deadlines import deadline_bypass

    team = vote_event.projects_list[0].team
    cast(vote_event, vote_event.outsider, {vote_event.projects_list[0].pk: 16})
    before = tally_snapshot(vote_event)
    client = client_for(vote_event.voter)
    client.post(f"/participant/teams/{team.pk}/leave", {"confirm": "yes"})
    assert team.__class__.objects.filter(pk=team.pk).exists()
    with pytest.raises(ProtectedError), deadline_bypass(None, "test: delete a team with votes"):
        team.delete()
    assert team.__class__.objects.filter(pk=team.pk).exists()
    assert tally_snapshot(vote_event) == before


@pytest.mark.django_db
def test_deleting_a_project_or_event_with_votes_is_refused(vote_event):
    from django.db.models import ProtectedError

    from core.deadlines import deadline_bypass

    project = vote_event.projects_list[1]
    cast(vote_event, vote_event.outsider, {project.pk: 4})
    before = tally_snapshot(vote_event)
    with pytest.raises(ProtectedError), deadline_bypass(None, "test: delete a project with votes"):
        project.delete()
    with pytest.raises(ProtectedError):
        Event.objects.get(pk=vote_event.pk).delete()
    assert Project.objects.filter(pk=project.pk).exists()
    assert tally_snapshot(vote_event) == before


@pytest.mark.django_db
def test_raw_sql_delete_of_votes_is_refused_after_voting_opens(vote_event):
    from django.db import connection

    ballot = cast(vote_event, vote_event.outsider, {vote_event.projects_list[1].pk: 4})
    line = ballot.lines.get(credits=4)
    for sql, arg in (("DELETE FROM voting_ballotline WHERE id = %s", line.pk),
                     ("DELETE FROM voting_ballot WHERE id = %s", ballot.pk),
                     ("DELETE FROM voting_votingconfig WHERE id = %s", vote_event.config.pk),
                     ("UPDATE voting_votingconfig SET opens_at = now() + interval '1 day' WHERE id = %s",
                      vote_event.config.pk)):
        with pytest.raises(DatabaseError, match="dogfood_voting_closed"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sql, [arg])
    shift_voting(vote_event, closes_at=-timedelta(seconds=1))  # after the close too
    with pytest.raises(DatabaseError, match="dogfood_voting_closed"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM voting_ballotline WHERE id = %s", [line.pk])
    assert BallotLine.objects.filter(pk=line.pk).exists()


@pytest.mark.django_db
def test_voting_bypass_is_the_audited_way_past_the_trigger(vote_event):
    from django.db import connection

    ballot = cast(vote_event, vote_event.outsider, {vote_event.projects_list[1].pk: 4})
    with services.voting_bypass("test: repair", actor=vote_event.organizer):
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM voting_ballotline WHERE ballot_id = %s", [ballot.pk])
    assert not BallotLine.objects.filter(ballot=ballot).exists()
    entry = AuditLog.objects.filter(action=AuditAction.VOTING_BYPASSED, detail__reason="test: repair").get()
    assert entry.actor == vote_event.organizer


def test_every_foreign_key_touching_the_voting_tables_is_as_documented():
    from django.apps import apps

    found = {}
    for model in apps.get_models():
        for f in model._meta.concrete_fields:
            if f.is_relation and "voting" in (model._meta.app_label, f.related_model._meta.app_label):
                found[f"{model._meta.label}.{f.name}"] = f.remote_field.on_delete.__name__
    assert found == {
        "voting.VotingConfig.event": "CASCADE", "voting.VotingConfig.created_by": "SET_NULL",
        "voting.VotingConfig.updated_by": "SET_NULL",
        "voting.VoterLink.event": "CASCADE", "voting.VoterLink.created_by": "SET_NULL",
        "voting.VoterLink.revoked_by": "SET_NULL",
        "voting.Ballot.event": "PROTECT", "voting.Ballot.voter_user": "PROTECT",
        "voting.Ballot.voter_link": "PROTECT", "voting.Ballot.voided_by": "PROTECT",
        "voting.BallotLine.ballot": "CASCADE", "voting.BallotLine.project": "PROTECT",
        # Stage 5: frozen tallies, and the result that used one
        "voting.VoteTallySnapshot.event": "PROTECT", "voting.VoteTallySnapshot.created_by": "PROTECT",
        "voting.VoteTallySnapshot.previous": "PROTECT", "scoring.ResultSnapshot.vote_tally": "PROTECT",
    }


@pytest.mark.django_db
def test_voting_bypass_does_not_leak_past_its_block_inside_a_transaction(vote_event):
    from django.db import connection

    ballot = cast(vote_event, vote_event.outsider, {vote_event.projects_list[1].pk: 4})
    with transaction.atomic():
        with services.voting_bypass("test: nested"):
            pass
        with pytest.raises(DatabaseError, match="dogfood_voting_closed"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM voting_ballot WHERE id = %s", [ballot.pk])
