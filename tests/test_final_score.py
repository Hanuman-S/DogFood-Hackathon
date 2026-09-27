"""The final score (T3 stage 5): judges + community by percentile rank, the weights and their lock,
frozen vote tallies, the combined results page, and People's Choice.

Tests that compute a snapshot use django_db(transaction=True) (compute_snapshot refuses to run
inside another transaction)."""

import random
import threading
from datetime import timedelta
from fractions import Fraction

import pytest
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, connection, transaction
from django.test import Client
from django.utils import timezone

from core.models import AuditAction, AuditLog
from scoring import results, services
from scoring.engine.combine import combine, mid_rank_percentiles
from scoring.errors import ConcurrentFinal, InvalidWeights, NoVoteForCommunityWeight, WeightsLocked
from scoring.models import EventScoringConfig, ResultSnapshot, ResultVisibility, SnapshotKind
from test_results_flow import fake_snapshot, judged  # noqa: F401 -- fixtures
from voting import services as voting
from voting.errors import VotingOpen
from voting.models import Ballot, BallotLine, Method, TallyImmutable, VoteTallySnapshot, VotingConfig

TX = pytest.mark.django_db(transaction=True)


# --- the combination, pure ------------------------------------------------------------------------------

def test_mid_rank_percentiles():
    assert mid_rank_percentiles({"a": 1, "b": 2, "c": 2, "d": 3}) == {
        "a": Fraction(0), "b": Fraction(1, 2), "c": Fraction(1, 2), "d": Fraction(1)}
    assert mid_rank_percentiles({"only": 5.0}) == {"only": Fraction(1, 2)}
    assert mid_rank_percentiles({"a": 1.0, "b": 1.0 + 1e-12}) == {"a": Fraction(1, 2), "b": Fraction(1, 2)}


def test_zero_vote_projects_share_the_bottom_mid_rank():
    rows = {r.project_id: r for r in combine({"a": 3, "b": 2, "c": 1}, {"a": 9.0}, 50, 50)}
    assert rows["b"].vote_pct == rows["c"].vote_pct == Fraction(1, 4)  # mid-rank 1.5 of 3
    assert rows["a"].vote_pct == 1


def test_weights_must_be_whole_numbers_summing_to_100():
    for jw, cw in ((60, 30), (101, -1), (50.0, 50)):
        with pytest.raises((ValueError, TypeError)):
            combine({"a": 1}, {}, jw, cw)


def test_exact_combined_tie_shares_first_place_and_no_float_tie_break():
    rows = combine({"a": 5, "b": 1}, {"a": 0, "b": 7}, 50, 50)
    assert {r.project_id: r.final for r in rows} == {"a": Fraction(1, 2), "b": Fraction(1, 2)}
    assert [r.final_rank for r in rows] == [1, 1]


def test_community_weight_zero_gives_exactly_the_judged_ranking():
    rng = random.Random(7)
    for _ in range(50):
        n = rng.randint(1, 30)
        scores = {f"p{i}": round(rng.uniform(1, 5), rng.choice((0, 1, 3))) for i in range(n)}  # with ties
        votes = {f"p{i}": rng.choice((0, 1, 2.5, 4)) for i in range(n)}
        combined = {r.project_id: r.final_rank for r in combine(scores, votes, 100, 0)}
        judged = {pid: 1 + sum(1 for w in scores.values() if round(w, 9) > round(s, 9)) for pid, s in scores.items()}
        assert combined == judged


def test_combination_is_deterministic():
    scores, votes = {"a": 3.2, "b": 3.2, "c": 1.0}, {"a": 1.0, "c": 2.0}
    assert combine(scores, votes, 70, 30) == combine(dict(reversed(scores.items())), votes, 70, 30)


# --- the weights ----------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_weights_are_validated_audited_and_lock_when_judging_opens(make_event, make_user):
    now = timezone.now()
    event = make_event()  # submissions open; judging later
    with pytest.raises(InvalidWeights):
        services.set_final_weights(event, actor=event.organizer, judge_weight=70, community_weight=20)
    with pytest.raises(InvalidWeights):
        services.set_final_weights(event, actor=event.organizer, judge_weight=110, community_weight=-10)
    with pytest.raises(PermissionDenied):
        services.set_final_weights(event, actor=make_user(), judge_weight=80, community_weight=20)
    services.set_final_weights(event, actor=event.organizer, judge_weight=80, community_weight=20)
    assert services.final_weights(event) == (80, 20)
    assert AuditLog.objects.get(action=AuditAction.WEIGHTS_CHANGED).detail["after"] == {"judge": 80, "community": 20}
    type(event).objects.filter(pk=event.pk).update(
        submissions_close_at=now - timedelta(hours=2), judging_starts_at=now - timedelta(hours=1),
        submissions_open_at=now - timedelta(days=2), starts_at=now - timedelta(days=3))
    event.refresh_from_db()
    with pytest.raises(WeightsLocked) as caught:
        services.set_final_weights(event, actor=event.organizer, judge_weight=100, community_weight=0)
    assert (caught.value.status, caught.value.code) == (409, "weights_locked")
    assert AuditLog.objects.filter(action=AuditAction.WEIGHTS_REFUSED).count() == 4


@pytest.mark.django_db
def test_weights_lock_when_voting_opens_before_judging(make_event):
    now = timezone.now()
    event = make_event(submissions_open_at=now - timedelta(days=2), submissions_close_at=now - timedelta(hours=2),
                       judging_starts_at=now + timedelta(days=1))
    VotingConfig.objects.create(event=event, opens_at=now - timedelta(hours=1), closes_at=now + timedelta(days=2),
                                ballot_secret="s" * 64)
    assert services.weights_locked(event)
    with pytest.raises(WeightsLocked):
        services.set_final_weights(event, actor=event.organizer, judge_weight=80, community_weight=20)


@pytest.mark.django_db
def test_trigger_backs_the_weights_lock_and_the_bypass_is_audited(judged):
    EventScoringConfig.objects.create(event=judged)  # default 100/0 is always allowed
    with pytest.raises(DatabaseError, match="dogfood_weights_locked"), transaction.atomic():
        EventScoringConfig.objects.filter(event=judged).update(judge_weight=80, community_weight=20)
    with services.weights_bypass("test: seed weights"):
        EventScoringConfig.objects.filter(event=judged).update(judge_weight=80, community_weight=20)
    assert services.final_weights(judged) == (80, 20)
    assert AuditLog.objects.filter(action=AuditAction.WEIGHTS_BYPASSED).exists()
    with pytest.raises(DatabaseError, match="dogfood_weights_locked"), transaction.atomic():
        EventScoringConfig.objects.filter(event=judged).update(judge_weight=50, community_weight=50)


# --- tallies and finals ------------------------------------------------------------------------------------------

def add_vote(event, closes_in=timedelta(minutes=-30), method=Method.QUADRATIC):
    now = timezone.now()
    return VotingConfig.objects.create(
        event=event, opens_at=event.submissions_close_at, closes_at=now + closes_in, method=method,
        credit_budget=16 if method == Method.QUADRATIC else 1, ballot_secret="s" * 64)


def add_ballots(event, ballots):
    """Ballots written straight in (through the audited bypass: the window may be over)."""
    from projects.models import Project, Status

    now = timezone.now()
    projects = list(Project.objects.filter(event=event, status=Status.SUBMITTED).order_by("pk"))
    made = []
    with voting.voting_bypass("test: seed ballots"):
        for i, lines in enumerate(ballots):
            ballot = Ballot.objects.create(event=event, voter_cookie=f"voter-{Ballot.objects.count()}-{i}",
                                           created_at=now, updated_at=now)
            for position, project in enumerate(projects):
                BallotLine.objects.create(ballot=ballot, project=project, credits=lines.get(project.pk, 0),
                                          shown_position=position)
            made.append(ballot)
    return made


def set_weights(event, judge, community):
    with services.weights_bypass("test: weights after judging opened"):
        EventScoringConfig.objects.update_or_create(event=event, defaults={
            "judge_weight": judge, "community_weight": community})


@TX
def test_final_freezes_a_tally_and_community_weight_zero_is_exactly_m2(judged):
    add_vote(judged)
    p = judged.projects_list
    add_ballots(judged, [{p[3].pk: 16}, {p[3].pk: 9, p[2].pk: 4}])
    snapshot = services.compute_snapshot(judged, "final", actor=judged.organizer)
    tally = snapshot.vote_tally
    assert tally is not None and tally.previous is None and not tally.changed_since_previous
    assert {r["project_id"]: r["influence"] for r in tally.rows}[str(p[3].pk)] == pytest.approx(7.0)
    assert snapshot.final_weights == {"judge": 100, "community": 0}
    # community weight 0: the combined ranking is the M2 ranking, exactly
    m2_rows, _ = results.build_rows(judged, snapshot)
    assert not results.is_combined(snapshot)
    m2 = {str(r.project.pk): r.display_rank for r in m2_rows}
    assert {c["project_id"]: c["final_rank"] for c in snapshot.combined} == m2
    assert AuditLog.objects.get(action=AuditAction.TALLY_FROZEN).detail["tally"] == tally.pk


@TX
def test_each_final_freezes_a_new_tally_recording_changes_since_the_previous(judged):
    add_vote(judged)
    p = judged.projects_list
    ballots = add_ballots(judged, [{p[1].pk: 16}, {p[2].pk: 4}])
    first = services.compute_snapshot(judged, "final", actor=judged.organizer).vote_tally
    second = services.compute_snapshot(judged, "final", actor=judged.organizer).vote_tally
    assert second.pk != first.pk and second.previous_id == first.pk and not second.changed_since_previous
    voting.void_ballot(judged, ballots[0].pk, actor=judged.organizer, reason="sock puppet")
    third_snapshot = services.compute_snapshot(judged, "final", actor=judged.organizer)
    third = third_snapshot.vote_tally
    assert third.changed_since_previous and third.voided_since_previous == [ballots[0].pk]
    assert third_snapshot.diagnostics["vote_tally"]["changed_since_previous_tally"] is True
    voting.restore_ballot(judged, ballots[0].pk, actor=judged.organizer, reason="genuine")
    fourth = services.compute_snapshot(judged, "final", actor=judged.organizer).vote_tally
    assert fourth.restored_since_previous == [ballots[0].pk]
    assert first.rows == second.rows and third.rows != second.rows and fourth.rows == first.rows
    assert VoteTallySnapshot.objects.filter(event=judged).count() == 4


@TX
def test_preview_uses_the_live_tally_and_freezes_nothing(judged):
    add_vote(judged, closes_in=timedelta(days=1))
    add_ballots(judged, [{judged.projects_list[1].pk: 16}])
    snapshot = services.compute_snapshot(judged, "preview", actor=judged.organizer)
    assert snapshot.vote_tally is None and snapshot.diagnostics["live_tally"]
    assert snapshot.combined is not None
    assert not VoteTallySnapshot.objects.exists()


@TX
def test_community_weight_needs_a_closed_vote(judged):
    set_weights(judged, 80, 20)
    with pytest.raises(NoVoteForCommunityWeight):
        services.compute_snapshot(judged, "final", actor=judged.organizer)
    add_vote(judged, closes_in=timedelta(days=1))
    with pytest.raises(VotingOpen) as caught:
        services.compute_snapshot(judged, "final", actor=judged.organizer)
    assert (caught.value.status, caught.value.code) == (409, "voting_open")
    assert not ResultSnapshot.objects.filter(kind=SnapshotKind.FINAL).exists()
    assert not VoteTallySnapshot.objects.exists()
    assert AuditLog.objects.filter(action=AuditAction.SNAPSHOT_REFUSED).count() == 2


@TX
def test_community_weight_zero_final_while_voting_is_open_has_no_community_part(judged):
    add_vote(judged, closes_in=timedelta(days=1))
    snapshot = services.compute_snapshot(judged, "final", actor=judged.organizer)
    assert snapshot.vote_tally is None and snapshot.combined is None
    assert "no community part" in snapshot.diagnostics["no_tally_reason"]


@TX
def test_a_tally_is_immutable(judged):
    add_vote(judged)
    add_ballots(judged, [{judged.projects_list[1].pk: 4}])
    tally = services.compute_snapshot(judged, "final", actor=judged.organizer).vote_tally
    tally.rows = []
    with pytest.raises(TallyImmutable):
        tally.save()
    with pytest.raises(DatabaseError, match="dogfood_tally_immutable"), transaction.atomic():
        VoteTallySnapshot.objects.filter(pk=tally.pk).update(rows=[])


@TX
def test_concurrent_finals_never_record_a_stale_previous_tally(judged):
    add_vote(judged)
    add_ballots(judged, [{judged.projects_list[1].pk: 4}])
    barrier, outcomes = threading.Barrier(2), []

    def run():
        try:
            barrier.wait()
            outcomes.append(services.compute_snapshot(judged, "final", actor=judged.organizer).vote_tally_id)
        except ConcurrentFinal:
            outcomes.append("retry")
        finally:
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    tallies = list(VoteTallySnapshot.objects.filter(event=judged).order_by("id"))
    assert 1 <= len(tallies) <= 2 and len(outcomes) == 2
    assert tallies[0].previous_id is None
    if len(tallies) == 2:
        assert tallies[1].previous_id == tallies[0].pk  # the second saw the first: never both "first"
    else:
        assert "retry" in outcomes


# --- the page ------------------------------------------------------------------------------------------------------

@TX
def test_combined_page_shows_both_components_and_peoples_choice(judged):
    set_weights(judged, 80, 20)
    add_vote(judged)
    p = judged.projects_list
    add_ballots(judged, [{p[0].pk: 16}, {p[0].pk: 9, p[1].pk: 4}])
    snapshot = services.compute_snapshot(judged, "final", actor=judged.organizer)
    assert results.is_combined(snapshot) and snapshot.final_weights == {"judge": 80, "community": 20}
    services.set_result_settings(judged, actor=judged.organizer, visibility="public_full", winners_top_n=3)
    services.publish_results(judged, snapshot.pk, actor=judged.organizer)
    body = Client().get(f"/events/{judged.slug}/results").content.decode()
    for text in ("80% judges + 20% community", "judged rank", "vote pct", "people's choice", "no standard error"):
        assert text in body, text
    page = results.results_page(judged, None)
    assert [c.project.pk for c in page.peoples_choice] == [p[0].pk, p[1].pk]
    rows, _ = results.build_rows(judged, snapshot)
    combined_ranks = {c["project_id"]: c["final_rank"] for c in snapshot.combined}
    assert [r.display_rank for r in rows] == sorted(combined_ranks.values())


@TX
def test_peoples_choice_follows_the_result_visibility(judged, client_for):
    add_vote(judged)
    p = judged.projects_list
    add_ballots(judged, [{p[0].pk: 16}, {p[1].pk: 9}, {p[2].pk: 4}])
    snapshot = services.compute_snapshot(judged, "final", actor=judged.organizer)
    services.set_result_settings(judged, actor=judged.organizer, visibility="public_winners", winners_top_n=1)
    services.publish_results(judged, snapshot.pk, actor=judged.organizer)
    page = results.results_page(judged, None)
    assert [c.project.pk for c in page.peoples_choice] == [p[0].pk]  # winners only: top N by influence
    services.set_result_settings(judged, actor=judged.organizer, visibility="public_full", winners_top_n=1)
    assert [c.project.pk for c in results.results_page(judged, None).peoples_choice] == [p[0].pk, p[1].pk, p[2].pk]
    services.set_result_settings(judged, actor=judged.organizer, visibility="private", winners_top_n=1)
    assert Client().get(f"/events/{judged.slug}/results").status_code == 404
    assert "people's choice" in client_for(judged.organizer).get(f"/events/{judged.slug}/results").content.decode()


@pytest.mark.django_db
def test_first_place_is_tied_only_on_an_exact_combined_tie(judged):
    p = judged.projects_list
    snapshot = fake_snapshot(judged, [4.0, 3.9, 3.0, 2.0], tie_groups=[1, 1, 2, 3])
    snapshot.final_weights = {"judge": 80, "community": 20}
    snapshot.combined = [
        {"project_id": str(p[0].pk), "final": 0.9, "final_rank": 1, "judge_pct": 1, "vote_pct": 0.5, "influence": 2},
        {"project_id": str(p[1].pk), "final": 0.8, "final_rank": 2, "judge_pct": 0.66, "vote_pct": 1, "influence": 3},
        {"project_id": str(p[2].pk), "final": 0.3, "final_rank": 3, "judge_pct": 0.33, "vote_pct": 0, "influence": 0},
        {"project_id": str(p[3].pk), "final": 0.1, "final_rank": 4, "judge_pct": 0, "vote_pct": 0, "influence": 0},
    ]
    rows, _ = results.build_rows(judged, snapshot)
    assert rows[0].tie_size == 2  # the judged tie group is still shown beside the judged part
    assert not results.no_separable_winner(rows, combined=True)
    assert results.no_separable_winner(rows, combined=False)
    snapshot.combined[1]["final_rank"] = 1
    rows, _ = results.build_rows(judged, snapshot)
    assert results.no_separable_winner(rows, combined=True)


@TX
def test_winners_csv_includes_peoples_choice(judged, client_for):
    add_vote(judged)
    add_ballots(judged, [{judged.projects_list[2].pk: 16}])
    snapshot = services.compute_snapshot(judged, "final", actor=judged.organizer)
    services.publish_results(judged, snapshot.pk, actor=judged.organizer)
    body = client_for(judged.organizer).get(f"/organizer/events/{judged.slug}/results/winners.csv").content.decode("utf-8-sig")
    assert "people's choice" in body and judged.projects_list[2].name in body


@pytest.mark.django_db
def test_weights_page_is_organizer_only(judged, client_for, make_event):
    url = f"/organizer/events/{judged.slug}/results/weights"
    assert client_for(judged.participant).post(url, {"judge_weight": 80, "community_weight": 20}).status_code == 403
    assert client_for(make_event().organizer).post(url, {"judge_weight": 80, "community_weight": 20}).status_code == 404
    response = client_for(judged.organizer).post(url, {"judge_weight": 80, "community_weight": 20})
    assert response.status_code == 409  # judging has opened (and closed) on this event
    assert services.final_weights(judged) == (100, 0)


# --- the combined population, the publish rule, results.csv -----------------------------------------------------

def add_unreviewed_project(event, make_team):
    """A submitted project nobody reviewed: the engine leaves it unranked."""
    from core.deadlines import deadline_bypass
    from projects.models import Project, Status

    with deadline_bypass(None, "test: a late unreviewed project"):
        return Project.objects.create(team=make_team(event, name="Unreviewed"), event=event, name="PX",
                                      status=Status.SUBMITTED, submitted_at=timezone.now())


@TX
def test_combination_covers_only_engine_ranked_projects(judged, make_team):
    set_weights(judged, 50, 50)
    add_vote(judged)
    extra = add_unreviewed_project(judged, make_team)
    p = judged.projects_list
    add_ballots(judged, [{extra.pk: 16}, {p[0].pk: 4}])
    snapshot = services.compute_snapshot(judged, "final", actor=judged.organizer)
    ranked_ids = {e["project_id"] for e in snapshot.result["projects"] if e.get("score") is not None}
    assert {c["project_id"] for c in snapshot.combined} == ranked_ids and str(extra.pk) not in ranked_ids
    # both percentiles over exactly the ranked set: its top vote is 1, bottom 0, with 4 ranked projects
    pcts = {c["project_id"]: c["vote_pct"] for c in snapshot.combined}
    assert pcts[str(p[0].pk)] == 1 and min(pcts.values()) == pytest.approx(1 / 3)  # three zero-vote ties at 1/3
    rows, unranked = results.build_rows(judged, snapshot)
    assert [r.project.pk for r in unranked] == [extra.pk]
    assert extra.pk not in {r.project.pk for r in rows}
    choice = results.peoples_choice(judged, snapshot)
    assert choice[0].project.pk == extra.pk and choice[0].rank == 1  # People's Choice may include it


@pytest.mark.django_db
def test_judged_percentile_uses_the_same_rounded_equality_as_the_shared_ranks(judged):
    from scoring.engine.combine import mid_rank_percentiles

    near = 4.0 + 1e-12  # equal after rounding to 9 decimals
    snapshot = fake_snapshot(judged, [4.0, near, 3.0, 2.0])
    rows, _ = results.build_rows(judged, snapshot)
    assert [r.display_rank for r in rows] == [1, 1, 3, 4]
    pct = mid_rank_percentiles({str(r.project.pk): r.score for r in rows}, 9)
    ids = [str(r.project.pk) for r in rows]
    assert pct[ids[0]] == pct[ids[1]]
    shared = {}
    for r in rows:
        shared.setdefault(r.display_rank, set()).add(pct[str(r.project.pk)])
    assert all(len(v) == 1 for v in shared.values())  # one percentile per shared rank, and vice versa
    assert len(set(pct.values())) == len(shared)


@TX
def test_publishing_a_final_that_predates_the_vote_close_is_refused(judged):
    from scoring.errors import FinalPredatesVoteClose

    config = add_vote(judged, closes_in=timedelta(days=1))
    early = services.compute_snapshot(judged, "final", actor=judged.organizer)
    assert early.vote_tally is None
    with voting.voting_bypass("test: close the vote"):
        VotingConfig.objects.filter(pk=config.pk).update(closes_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(FinalPredatesVoteClose) as caught:
        services.publish_results(judged, early.pk, actor=judged.organizer)
    assert (caught.value.status, caught.value.code) == (409, "final_predates_vote_close")
    assert AuditLog.objects.filter(action=AuditAction.RESULTS_PUBLISH_REFUSED,
                                   detail__reason="final_predates_vote_close").exists()
    fresh = services.compute_snapshot(judged, "final", actor=judged.organizer)
    services.publish_results(judged, fresh.pk, actor=judged.organizer)


@TX
def test_results_csv_for_the_latest_final(judged, make_team, client_for, make_event):
    import csv
    import io

    set_weights(judged, 80, 20)
    add_vote(judged)
    extra = add_unreviewed_project(judged, make_team)
    p = judged.projects_list
    add_ballots(judged, [{p[1].pk: 16}, {extra.pk: 4}])
    snapshot = services.compute_snapshot(judged, "final", actor=judged.organizer)
    url = f"/organizer/events/{judged.slug}/results/results.csv"
    assert client_for(judged.participant).get(url).status_code == 403
    assert client_for(make_event().organizer).get(url).status_code == 404
    response = client_for(judged.organizer).get(url)
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert list(rows[0]) == results.RESULTS_HEADER
    by_name = {r["project"]: r for r in rows}
    combined = {c["project_id"]: c for c in snapshot.combined}
    top = by_name[p[1].name]
    assert top["people's choice position"] == "1" and top["weights"] == "80/20"
    assert int(top["final rank"]) == combined[str(p[1].pk)]["final_rank"]
    assert float(top["vote percentile"]) == pytest.approx(combined[str(p[1].pk)]["vote_pct"])
    assert by_name["PX"]["final rank"] == "" and by_name["PX"]["people's choice position"] == "2"
    assert AuditLog.objects.filter(action=AuditAction.RESULTS_EXPORTED).count() == 1


@TX
def test_results_csv_judges_only_has_percentiles_and_no_vote_columns(judged, client_for):
    import csv
    import io

    services.compute_snapshot(judged, "final", actor=judged.organizer)
    body = client_for(judged.organizer).get(f"/organizer/events/{judged.slug}/results/results.csv").content
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    assert all(r["judged percentile"] != "" and r["vote percentile"] == "" for r in rows)
    assert {r["weights"] for r in rows} == {"100/0"}
