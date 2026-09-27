"""The results flow (T2 completion): compute from the organizer page, publish / unpublish, who sees
/events/<slug>/results in each visibility mode, the tie display, and winners.csv.

Tests that compute a snapshot use django_db(transaction=True): compute_snapshot refuses to run
inside another transaction, and that check is not weakened for tests.
"""

import csv
import io
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.utils import timezone

from accounts.roles import ADMIN, Role
from core.models import AuditAction, AuditLog
from events.models import Event, EventMembership, Track
from projects.models import Project, Status
from scoring import results, services
from scoring.errors import AlreadyPublished, NotFinal, NotLatestFinal, NotPublished, InvalidResultSettings
from scoring.models import (Criterion, EventResultSettings, Publication, ResultSnapshot, ResultVisibility, Score,
                            ScoreItem, SnapshotKind)

TX = pytest.mark.django_db(transaction=True)
MODES = [ResultVisibility.PUBLIC_FULL, ResultVisibility.PUBLIC_WINNERS, ResultVisibility.PRIVATE]


def close_judging(event):
    """Move the whole timeline into the past (events_event has no deadline trigger), so judging
    has closed by the real database clock."""
    now = timezone.now()
    Event.objects.filter(pk=event.pk).update(
        starts_at=now - timedelta(days=6), submissions_open_at=now - timedelta(days=5),
        submissions_close_at=now - timedelta(days=3), judging_starts_at=now - timedelta(days=2),
        judging_ends_at=now - timedelta(hours=1), results_at=None,
    )
    event.refresh_from_db()


@pytest.fixture
def judged(make_event, make_user, make_team):
    """An event with 2 tracks, 4 submitted projects (team members have emails), 3 judges who each
    reviewed every project, then its judging closed. Also: a participant on one team, a judge of the
    event, a platform admin, and an organizer of another event."""
    event = make_event()
    tracks = [Track.objects.create(event=event, name=n, order=i) for i, n in enumerate(("Web", "Hardware"))]
    criteria = [Criterion.objects.create(event=event, key=k, label=k, weight=1, order=i)
                for i, k in enumerate(("functionality", "quality", "innovation"))]
    now = timezone.now()
    projects = []
    for i in range(4):
        member = make_user(email=f"member{i}@example.org", name=f"Member {i}")
        team = make_team(event, captain=member, name=f"Team {i}")
        projects.append(Project.objects.create(team=team, event=event, name=f"P{i}", track=tracks[i % 2],
                                               status=Status.SUBMITTED, submitted_at=now))
    judges = []
    for _ in range(3):
        judges.append(EventMembership.objects.create(user=make_user(role=Role.JUDGE), event=event, role=Role.JUDGE))
    for j, judge in enumerate(judges):
        for i, project in enumerate(projects):
            score = Score.objects.create(judge=judge, project=project, submitted_at=now)
            for c, criterion in enumerate(criteria):
                ScoreItem.objects.create(score=score, criterion=criterion, value=1 + (i + j + c) % 5)
    close_judging(event)
    event.projects_list = projects
    event.participant = projects[0].team.captain
    event.judge_user = judges[0].user
    event.admin = make_user(role=ADMIN)
    event.other_organizer = make_event().organizer
    return event


def final(event):
    return services.compute_snapshot(event, "final", actor=event.organizer)


def publish(event, visibility=ResultVisibility.PUBLIC_FULL):
    services.set_result_settings(event, actor=event.organizer, visibility=visibility, winners_top_n=3)
    snapshot = services.latest_final(event) or final(event)
    return services.publish_results(event, snapshot.pk, actor=event.organizer)


def viewers(event, client_for):
    return {
        "visitor": Client(),
        "participant": client_for(event.participant),
        "judge": client_for(event.judge_user),
        "organizer": client_for(event.organizer),
        "admin": client_for(event.admin),
        "other organizer": client_for(event.other_organizer),
    }


# --- who sees the public page ------------------------------------------------------------------------

STAFF = {"organizer", "admin"}


@TX
@pytest.mark.parametrize("mode", MODES)
def test_visibility_when_published(judged, client_for, mode):
    publish(judged, mode)
    url = f"/events/{judged.slug}/results"
    for who, client in viewers(judged, client_for).items():
        response = client.get(url)
        if mode == ResultVisibility.PRIVATE and who not in STAFF:
            assert response.status_code == 404, who
            continue
        assert response.status_code == 200, who
        body = response.content.decode()
        assert "winners" in body
        sees_full = who in STAFF or mode == ResultVisibility.PUBLIC_FULL
        assert ("full ranking" in body) is sees_full, who
        assert ("preview for organizers" in body) is (who in STAFF and mode != ResultVisibility.PUBLIC_FULL), who


@TX
@pytest.mark.parametrize("mode", MODES)
def test_not_published_is_404_except_for_staff(judged, client_for, mode):
    services.set_result_settings(judged, actor=judged.organizer, visibility=mode, winners_top_n=3)
    final(judged)
    url = f"/events/{judged.slug}/results"
    for who, client in viewers(judged, client_for).items():
        response = client.get(url)
        if who in STAFF:
            assert response.status_code == 200, who
            body = response.content.decode()
            assert "preview for organizers" in body and "full ranking" in body
        else:
            assert response.status_code == 404, who


@TX
def test_unpublish_hides_the_page_again_and_republish_is_a_new_row(judged, client_for):
    first = publish(judged)
    visitor = Client()
    url = f"/events/{judged.slug}/results"
    assert visitor.get(url).status_code == 200
    services.unpublish_results(judged, actor=judged.organizer)
    assert visitor.get(url).status_code == 404
    assert client_for(judged.organizer).get(url).status_code == 200
    second = services.publish_results(judged, services.latest_final(judged).pk, actor=judged.organizer)
    assert second.pk != first.pk
    first.refresh_from_db()
    assert first.unpublished_at is not None and first.unpublished_by == judged.organizer
    assert visitor.get(url).status_code == 200


@TX
def test_unpublished_event_hides_results_even_when_published(judged):
    publish(judged)
    Event.objects.filter(pk=judged.pk).update(is_published=False)
    assert Client().get(f"/events/{judged.slug}/results").status_code == 404


@TX
def test_public_page_shows_no_judge(judged):
    publish(judged)
    body = Client().get(f"/events/{judged.slug}/results").content.decode()
    for membership in EventMembership.objects.filter(event=judged, role=Role.JUDGE).select_related("user"):
        assert membership.user.email not in body
    assert "judge 1" not in body.lower()


# --- publish / unpublish rules -----------------------------------------------------------------------

@TX
def test_publish_refusals_are_audited(judged):
    preview = services.compute_snapshot(judged, "preview", actor=judged.organizer)
    with pytest.raises(NotFinal) as caught:
        services.publish_results(judged, preview.pk, actor=judged.organizer)
    assert caught.value.status == 400
    older = final(judged)
    newer = final(judged)
    with pytest.raises(NotLatestFinal) as caught:
        services.publish_results(judged, older.pk, actor=judged.organizer)
    assert caught.value.status == 409
    services.publish_results(judged, newer.pk, actor=judged.organizer)
    with pytest.raises(AlreadyPublished):
        services.publish_results(judged, newer.pk, actor=judged.organizer)
    with pytest.raises(PermissionDenied):
        services.publish_results(judged, newer.pk, actor=judged.participant)
    services.unpublish_results(judged, actor=judged.organizer)
    with pytest.raises(NotPublished):
        services.unpublish_results(judged, actor=judged.organizer)
    refused = AuditLog.objects.filter(action=AuditAction.RESULTS_PUBLISH_REFUSED)
    assert refused.count() == 5
    assert AuditLog.objects.filter(action=AuditAction.RESULTS_PUBLISHED).count() == 1
    assert AuditLog.objects.filter(action=AuditAction.RESULTS_UNPUBLISHED).count() == 1
    assert Publication.objects.filter(event=judged).count() == 1


@TX
def test_organizer_pages_compute_publish_unpublish(judged, client_for):
    client = client_for(judged.organizer)
    base = f"/organizer/events/{judged.slug}/results"
    assert client.get(base).status_code == 200
    assert client.post(base + "/compute", {"kind": "final"}).status_code == 302
    snapshot = ResultSnapshot.objects.get(event=judged)
    assert snapshot.kind == SnapshotKind.FINAL
    assert client.post(base + "/publish", {"snapshot": snapshot.pk}).status_code == 302
    assert services.active_publication(judged).snapshot_id == snapshot.pk
    assert client.post(base + "/unpublish").status_code == 302
    assert services.active_publication(judged) is None


@TX
def test_final_refused_on_the_page_while_judging_is_open(make_event, client_for):
    event = make_event()
    client = client_for(event.organizer)
    response = client.post(f"/organizer/events/{event.slug}/results/compute", {"kind": "final"}, follow=True)
    assert "judging" in response.content.decode().lower()
    assert not ResultSnapshot.objects.filter(event=event).exists()


@TX
def test_organizer_results_pages_are_for_this_events_organizers_only(judged, client_for):
    base = f"/organizer/events/{judged.slug}/results"
    for who in ("participant", "judge"):
        client = client_for(getattr(judged, {"participant": "participant", "judge": "judge_user"}[who]))
        assert client.get(base).status_code == 403, who
        assert client.post(base + "/compute", {"kind": "preview"}).status_code == 403, who
    other = client_for(judged.other_organizer)
    assert other.get(base).status_code == 404
    assert other.post(base + "/compute", {"kind": "preview"}).status_code == 404
    assert other.post(base + "/settings", {"visibility": "public_full", "winners_top_n": 3}).status_code == 404
    assert not ResultSnapshot.objects.filter(event=judged).exists()


# --- settings -------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_result_settings_default_private_and_changes_are_audited(judged, client_for):
    assert services.result_settings(judged).visibility == ResultVisibility.PRIVATE
    assert services.result_settings(judged).winners_top_n == 3
    client = client_for(judged.organizer)
    url = f"/organizer/events/{judged.slug}/results/settings"
    assert client.post(url, {"visibility": "everyone", "winners_top_n": 3}).status_code == 400
    assert client.post(url, {"visibility": "public_winners", "winners_top_n": 0}).status_code == 400
    assert client.post(url, {"visibility": "public_winners", "winners_top_n": 5}).status_code == 302
    row = EventResultSettings.objects.get(event=judged)
    assert (row.visibility, row.winners_top_n) == ("public_winners", 5)
    entry = AuditLog.objects.get(action=AuditAction.RESULT_SETTINGS_CHANGED)
    assert entry.detail["before"] == {"visibility": "private", "winners_top_n": 3}
    with pytest.raises(InvalidResultSettings):
        services.set_result_settings(judged, actor=judged.organizer, visibility="public_full", winners_top_n=True)


# --- winners.csv -----------------------------------------------------------------------------------------

@TX
@pytest.mark.parametrize("mode", MODES)
def test_winners_csv_for_organizers_in_every_mode(judged, client_for, mode):
    publish(judged, mode)
    response = client_for(judged.organizer).get(f"/organizer/events/{judged.slug}/results/winners.csv")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == results.WINNERS_HEADER
    emails = {r[6] for r in rows[1:]}
    assert emails and all(e.startswith("member") for e in emails)
    assert {r[0] for r in rows[1:]} >= {"overall", "top of Web", "top of Hardware"}
    assert AuditLog.objects.filter(action=AuditAction.WINNERS_EXPORTED).count() == 1


@TX
def test_winners_csv_is_organizer_only(judged, client_for):
    publish(judged)
    url = f"/organizer/events/{judged.slug}/results/winners.csv"
    assert Client().get(url).status_code in (302, 401)
    assert client_for(judged.participant).get(url).status_code == 403
    assert client_for(judged.judge_user).get(url).status_code == 403
    assert client_for(judged.other_organizer).get(url).status_code == 404
    assert client_for(judged.admin).get(url).status_code == 200
    assert AuditLog.objects.filter(action=AuditAction.WINNERS_EXPORTED).count() == 1


@pytest.mark.django_db
def test_winners_csv_needs_a_final(judged, client_for):
    response = client_for(judged.organizer).get(f"/organizer/events/{judged.slug}/results/winners.csv", follow=True)
    assert "compute final results first" in response.content.decode()
    assert not AuditLog.objects.filter(action=AuditAction.WINNERS_EXPORTED).exists()


# --- ties and winners, on a hand-made snapshot ---------------------------------------------------------

def fake_snapshot(event, scores, raw=None, tie_groups=None):
    projects = event.projects_list
    entries = []
    for i, (project, score) in enumerate(zip(projects, scores)):
        entries.append({"project_id": str(project.pk), "track_id": str(project.track_id), "component": 0,
                        "score": score, "se": 0.1, "rank": i + 1,
                        "tie_group": (tie_groups or list(range(1, len(scores) + 1)))[i], "n_reviews": 3})
    comparison = {"methods": ["m2", "raw_mean"], "rows": [
        {"project_id": str(p.pk), "scores": {"raw_mean": r}} for p, r in zip(projects, raw or scores)]}
    return ResultSnapshot(event=event, kind=SnapshotKind.FINAL, method="m2", method_version="1",
                          engine_config={"config": {"equal_decimals": 9}}, rubric={},
                          input_hash="x", result={"projects": entries, "components": [{}]}, comparison=comparison)


@pytest.mark.django_db
def test_exact_ties_share_a_display_rank_and_no_separable_winner(judged):
    rows, _ = results.build_rows(judged, fake_snapshot(judged, [4.0, 4.0, 3.0, 2.0]))
    assert [r.display_rank for r in rows] == [1, 1, 3, 4]
    assert results.no_separable_winner(rows)


@pytest.mark.django_db
def test_tie_group_at_the_top_means_no_separable_winner(judged):
    rows, _ = results.build_rows(judged, fake_snapshot(judged, [4.0, 3.9, 3.0, 2.0], tie_groups=[1, 1, 2, 3]))
    assert [r.display_rank for r in rows] == [1, 2, 3, 4]
    assert rows[0].tie_size == 2
    assert results.no_separable_winner(rows)
    rows, _ = results.build_rows(judged, fake_snapshot(judged, [4.0, 3.9, 3.0, 2.0]))
    assert not results.no_separable_winner(rows)


@pytest.mark.django_db
def test_winners_include_a_tie_on_the_cut_and_each_tracks_top(judged):
    # P0 Web 4, P1 Hardware 3, P2 Web 3, P3 Hardware 1; top 2 -> ranks 1, 2, 2
    rows, _ = results.build_rows(judged, fake_snapshot(judged, [4.0, 3.0, 3.0, 1.0], raw=[1.0, 2.0, 3.0, 4.0]))
    overall, tracks = results.winners(rows, 2)
    assert sorted(r.project.name for r in overall) == ["P0", "P1", "P2"]
    assert {t.track.name: [r.project.name for r in t.rows] for t in tracks} == {"Web": ["P0"], "Hardware": ["P1"]}
    assert {r.project.name: r.raw_rank for r in rows} == {"P0": 4, "P1": 3, "P2": 2, "P3": 1}


@pytest.mark.django_db
def test_unscored_projects_are_listed_unranked_and_foreign_ids_dropped(judged):
    snapshot = fake_snapshot(judged, [4.0, None, 3.0, 2.0])
    snapshot.result["projects"].append({"project_id": "dup:abc", "score": 5.0, "rank": 1, "tie_group": 9})
    rows, unranked = results.build_rows(judged, snapshot)
    assert [r.project.name for r in rows] == ["P0", "P2", "P3"]
    assert [r.project.name for r in unranked] == ["P1"]
