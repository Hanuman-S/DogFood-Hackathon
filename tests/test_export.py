"""CSV export: every stage, the right people only, one consistent read, nothing secret."""

import csv
import io
import zipfile
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from accounts.models import ApiToken
from accounts.roles import Role
from core.models import AuditAction, AuditLog
from events.models import Event, EventMembership, JudgeInvite
from organizer.export import SHEET_NAMES, SHEETS, cell
from projects.models import Project, Status
from scoring import services as scoring
from scoring.models import Assignment, AssignmentSource, Criterion, ResultSnapshot, Score, ScoreItem, SnapshotKind
from teams.models import TeamExtension

pytestmark = pytest.mark.django_db(transaction=True)  # the export refuses to run inside a transaction


def rows_of(body: bytes):
    text = body.decode("utf-8")
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:])))


def sheet(client, event, name):
    response = client.get(f"/api/export.csv?event={event.slug}&sheet={name}")
    assert response.status_code == 200, response.content[:300]
    assert response["Content-Type"].startswith("text/csv")
    return rows_of(response.content)


def as_dicts(rows):
    header, *body = rows
    return [dict(zip(header, r)) for r in body]


@pytest.fixture
def judged(make_event, make_team, make_user):
    """An event in judging: 3 criteria, 3 submitted projects, 1 draft project, 2 judges.
    Judge A submitted every review; judge B submitted one and has one draft."""
    event = make_event()
    criteria = [Criterion.objects.create(event=event, key=k, label=k.title(), weight=w, order=i)
                for i, (k, w) in enumerate((("functionality", 2), ("quality", 1), ("innovation", 1)))]
    now = timezone.now()
    projects = [Project.objects.create(team=make_team(event, name=f"Team {i}"), event=event, name=f"P{i}",
                                       status=Status.SUBMITTED, submitted_at=now) for i in range(3)]
    Project.objects.create(team=make_team(event, name="Late Team"), event=event, name="Still Drafting")
    judges = {n: EventMembership.objects.create(event=event, user=make_user(name=f"Judge {n}", email=f"j{n.lower()}@x.org"),
                                                role=Role.JUDGE) for n in ("A", "B")}
    for judge in judges.values():
        for p in projects:
            Assignment.objects.create(judge=judge, project=p, source=AssignmentSource.MANUAL)

    def review(judge, project, values, submitted=True):
        score = Score.objects.create(judge=judge, project=project, comment=f"{judge.user.name} on {project.name}",
                                     submitted_at=now if submitted else None)
        for c, v in zip(criteria, values):
            ScoreItem.objects.create(score=score, criterion=c, value=v)

    for i, p in enumerate(projects):
        review(judges["A"], p, (1 + i, 2 + i, 3))
    review(judges["B"], projects[0], (4, 4, 4))
    review(judges["B"], projects[1], (5, 1, 2), submitted=False)  # a draft: never exported
    Event.objects.filter(pk=event.pk).update(
        starts_at=now - timedelta(days=3, hours=1), submissions_open_at=now - timedelta(days=3),
        submissions_close_at=now - timedelta(days=1), judging_starts_at=now - timedelta(hours=12),
        judging_ends_at=now + timedelta(days=2),
    )
    event.refresh_from_db()
    event.judges_by_name, event.projects_list, event.criteria_list = judges, projects, criteria
    return event


def end_judging(event):
    Event.objects.filter(pk=event.pk).update(judging_ends_at=timezone.now() - timedelta(minutes=5))
    event.refresh_from_db()
    return event


# --- every stage --------------------------------------------------------------------------------


SETUP = {"event", "tracks", "prizes", "questions", "rubric", "judges", "judge_invites", "audit"}
STAGES = [  # (dates that put the event in a stage, the sheets open then)
    ({"submissions_open_at": timezone.now() + timedelta(days=1)}, SETUP),
    ({}, SETUP | {"teams", "members", "projects"}),
    ({"submissions_open_at": timezone.now() - timedelta(days=2), "submissions_close_at": timezone.now() - timedelta(hours=2),
      "judging_starts_at": timezone.now() + timedelta(hours=2)}, SETUP | {"teams", "members", "projects", "assignments", "assignment_rounds"}),
    ({"submissions_open_at": timezone.now() - timedelta(days=2), "submissions_close_at": timezone.now() - timedelta(hours=2),
      "judging_starts_at": timezone.now() - timedelta(hours=1)}, set(SHEET_NAMES) - {"results"}),
    ({"submissions_open_at": timezone.now() - timedelta(days=3), "submissions_close_at": timezone.now() - timedelta(days=2),
      "judging_starts_at": timezone.now() - timedelta(days=1), "judging_ends_at": timezone.now() - timedelta(hours=1)}, set(SHEET_NAMES)),
]


@pytest.mark.parametrize("dates, open_sheets", STAGES, ids=["upcoming", "open", "closed", "judging", "finished"])
def test_each_sheet_unlocks_when_its_stage_starts(make_event, client_for, dates, open_sheets):
    event = make_event(**dates)
    client = client_for(event.organizer)
    for name in SHEET_NAMES:
        response = client.get(f"/api/export.csv?event={event.slug}&sheet={name}")
        if name in open_sheets:
            assert response.status_code == 200 and rows_of(response.content)[0], name
        else:
            assert response.status_code == 409, name
            assert response.json()["error"] == "stage_not_open" and response.json()["opens_at"]
    archive = zipfile.ZipFile(io.BytesIO(client.get(f"/api/export.zip?event={event.slug}").content))
    assert {n.split("-", 2)[-1].removesuffix(".csv") for n in archive.namelist() if n.endswith(".csv")} == open_sheets
    readme = archive.read("README.txt").decode()
    assert ("has not started yet" in readme) == (open_sheets != set(SHEET_NAMES))
    page = client.get(f"/organizer/events/{event.slug}/").content.decode()
    for name in SHEET_NAMES:
        assert (f"sheet={name}\"" in page) == (name in open_sheets), name


def test_a_brand_new_event_exports_its_setup_with_headers(make_event, client_for):
    event = make_event(published=False, submissions_open_at=timezone.now() + timedelta(days=1))
    response = client_for(event.organizer).get(f"/api/export.zip?event={event.slug}")
    assert response.status_code == 200 and response["Content-Type"] == "application/zip"
    assert f'filename="{event.slug}-export-' in response["Content-Disposition"]
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    assert len(names) == len(SETUP) + 1 and "README.txt" in names
    for name in names:
        if name.endswith(".csv"):
            rows = rows_of(archive.read(name))
            assert rows and len(rows[0]) >= 2, name  # a header row at least
    readme = archive.read("README.txt").decode()
    assert "one consistent database snapshot" in readme and "results open when judging ends" in readme
    event_sheet = dict(rows_of(archive.read([n for n in names if n.endswith("-event.csv")][0]))[1:])
    assert event_sheet["published"] == "no" and event_sheet["projects submitted"] == "0"


def test_during_judging_reviews_are_the_submitted_ones_only(judged, client_for):
    client = client_for(judged.organizer)
    reviews = as_dicts(sheet(client, judged, "reviews"))
    assert len(reviews) == 4  # 3 by A, 1 by B; B's draft is not there
    assert not any(r["judge"] == "Judge B" and r["project"] == "P1" for r in reviews)
    b = next(r for r in reviews if r["judge"] == "Judge B")
    assert (b["Functionality (functionality, 1-5)"], b["Quality (quality, 1-5)"], b["comment"]) == ("4", "4", "Judge B on P0")
    assert b["weighted rating"] == "4"
    a0 = next(r for r in reviews if r["judge"] == "Judge A" and r["project"] == "P0")
    assert a0["weighted rating"] == "1.75"  # (2*1 + 1*2 + 1*3) / 4
    judges = {r["judge"]: r for r in as_dicts(sheet(client, judged, "judges"))}
    assert (judges["Judge B"]["submitted"], judges["Judge B"]["drafts"], judges["Judge B"]["not started"]) == ("1", "1", "1")
    assert (judges["Judge A"]["submitted"], judges["Judge A"]["assigned"]) == ("3", "3")


def test_the_draft_values_appear_nowhere(judged, client_for):
    body = client_for(judged.organizer).get(f"/api/export.zip?event={judged.slug}").content
    archive = zipfile.ZipFile(io.BytesIO(body))
    everything = "".join(archive.read(n).decode("utf-8") for n in archive.namelist())
    assert "Judge B on P1" not in everything  # the draft's comment


def test_projects_show_progress_status_and_answers(judged, client_for):
    projects = {r["project"]: r for r in as_dicts(sheet(client_for(judged.organizer), judged, "projects"))}
    assert projects["Still Drafting"]["status"] == "draft"
    assert (projects["P0"]["judges assigned"], projects["P0"]["reviews submitted"]) == ("2", "2")
    assert projects["P1"]["reviews submitted"] == "1"
    assert projects["P0"]["rank"] == projects["P0"]["score"] == ""  # judging is still open
    judges = as_dicts(sheet(client_for(judged.organizer), judged, "judges"))
    assert all(j["lean (from results)"] == "" for j in judges)
    end_judging(judged)
    projects = {r["project"]: r for r in as_dicts(sheet(client_for(judged.organizer), judged, "projects"))}
    assert projects["P0"]["rank"] and projects["P0"]["score"]  # results are open now


def test_results_are_computed_for_the_export_until_a_final_exists(judged, client_for):
    end_judging(judged)
    results = as_dicts(sheet(client_for(judged.organizer), judged, "results"))
    assert len(results) == 3 and all(r["source"].startswith("computed for this export") for r in results)
    assert min(int(r["rank"]) for r in results) == 1 and {r["project"] for r in results} == {"P0", "P1", "P2"}
    assert not ResultSnapshot.objects.exists()  # the export computed it, it saved nothing


@pytest.mark.django_db(transaction=True)
def test_results_use_the_final_snapshot_once_there_is_one(judged, client_for):
    end_judging(judged)
    final = scoring.compute_snapshot(judged, SnapshotKind.FINAL, actor=judged.organizer)
    results = as_dicts(sheet(client_for(judged.organizer), judged, "results"))
    assert results and all(r["source"].startswith(f"final snapshot #{final.pk}") for r in results)
    ranked = {r["project_id"]: r["rank"] for r in final.result["projects"]}
    by_name = {p.name: str(ranked[str(p.pk)]) for p in judged.projects_list}
    assert {r["project"]: r["rank"] for r in results} == by_name


def test_the_rubric_sheet_has_shares_and_level_descriptions(judged, client_for):
    client = client_for(judged.organizer)
    c = judged.criteria_list[0]
    c.level_descriptions = {"2": "barely runs", "5": "polished"}
    c.save()
    rubric = {r["key"]: r for r in as_dicts(sheet(client, judged, "rubric"))}
    assert rubric["functionality"]["share of score (%)"] == "50"
    assert (rubric["functionality"]["level 2"], rubric["functionality"]["level 1"]) == ("barely runs", "")


def test_extensions_teams_members_and_invites(judged, client_for, make_user):
    team = judged.projects_list[0].team
    TeamExtension.objects.create(team=team, until=timezone.now() + timedelta(hours=1), reason="wifi", granted_by=judged.organizer)
    JudgeInvite.objects.create(event=judged, email="", digest="secretdigest" * 5, created_by=judged.organizer,
                               expires_at=timezone.now() + timedelta(days=1))
    client = client_for(judged.organizer)
    teams = {r["team"]: r for r in as_dicts(sheet(client, judged, "teams"))}
    assert teams[team.name]["extension reason"] == "wifi"
    members = as_dicts(sheet(client, judged, "members"))
    assert any(m["team"] == team.name and m["captain"] == "yes" for m in members)
    invites = as_dicts(sheet(client, judged, "judge_invites"))
    assert invites[0]["for"].startswith("open link") and invites[0]["state"] == "pending"


def test_no_secret_is_exported(judged, client_for):
    team = judged.projects_list[0].team
    JudgeInvite.objects.create(event=judged, email="z@x.org", digest="d" * 64, created_by=judged.organizer,
                               expires_at=timezone.now() + timedelta(days=1))
    body = client_for(judged.organizer).get(f"/api/export.zip?event={judged.slug}").content
    archive = zipfile.ZipFile(io.BytesIO(body))
    everything = "".join(archive.read(n).decode("utf-8") for n in archive.namelist())
    assert team.invite_token not in everything and "d" * 64 not in everything
    assert "pbkdf2" not in everything and "argon2" not in everything


def test_a_formula_in_a_name_is_shown_not_run(make_event, make_team, client_for):
    event = make_event()
    Project.objects.create(team=make_team(event), event=event, name='=HYPERLINK("http://x","click")')
    projects = as_dicts(sheet(client_for(event.organizer), event, "projects"))
    assert projects[0]["project"] == "'=HYPERLINK(\"http://x\",\"click\")"
    assert (cell(-0.25), cell(-1)) == ("-0.25", "-1")  # numbers are numbers, not escaped text
    assert cell("-1") == "'-1"  # text that looks like a formula is escaped


def test_the_audit_sheet_has_this_events_rows_only(judged, make_event, client_for):
    other = make_event()
    AuditLog.objects.create(action=AuditAction.PROJECT_SUBMITTED, subject="P0", detail={"event": judged.slug})
    AuditLog.objects.create(action=AuditAction.PROJECT_SUBMITTED, subject="P0", detail={"event": other.slug})
    audit = as_dicts(sheet(client_for(judged.organizer), judged, "audit"))
    submitted = [r for r in audit if r["action code"] == "project_submitted"]
    assert len(submitted) == 1 and judged.slug in submitted[0]["detail"]


# --- who may export ---------------------------------------------------------------------------


def test_only_the_events_organizers_and_admins_may_export(judged, client_for, make_user, make_event):
    url = f"/api/export.zip?event={judged.slug}"
    elsewhere = make_event().organizer
    assert client_for(elsewhere).get(url).status_code == 404  # organizes another event: no probing
    assert client_for(judged.judges_by_name["A"].user).get(url).status_code == 403
    assert client_for(judged.projects_list[0].team.captain).get(url).status_code == 403
    anonymous = Client().get(url)
    assert anonymous.status_code == 401 and anonymous["WWW-Authenticate"] == "Bearer"
    admin = make_user()
    admin.is_platform_admin = True
    admin.save()
    assert client_for(admin).get(url).status_code == 200
    assert AuditLog.objects.filter(action=AuditAction.ACCESS_DENIED, subject="api:export").count() == 3


def test_a_judge_cannot_read_scores_through_the_csv_route(judged, client_for):
    response = client_for(judged.judges_by_name["A"].user).get(f"/api/export.csv?event={judged.slug}&sheet=reviews")
    assert response.status_code == 403 and b"Judge B" not in response.content


def test_every_download_is_audited(judged, client_for):
    client = client_for(judged.organizer)
    client.get(f"/api/export.zip?event={judged.slug}")
    sheet(client, judged, "reviews")
    rows = AuditLog.objects.filter(action=AuditAction.EXPORT_DOWNLOADED).order_by("pk")
    assert [(r.detail["format"], r.actor) for r in rows] == [("zip", judged.organizer), ("csv", judged.organizer)]
    assert rows[1].detail["sheet"] == "reviews" and rows[1].detail["rows"] == 4


# --- the checker's route ------------------------------------------------------------------------


def test_the_default_is_projects_across_the_callers_events_only(judged, make_event, make_team, client_for):
    mine = make_event(organizer=judged.organizer)
    Project.objects.create(team=make_team(mine), event=mine, name="Mine Too")
    theirs = make_event()
    Project.objects.create(team=make_team(theirs), event=theirs, name="Not Mine")
    response = client_for(judged.organizer).get("/api/export.csv")
    assert response.status_code == 200
    rows = as_dicts(rows_of(response.content))
    names = {r["project"] for r in rows}
    assert {"P0", "P1", "P2", "Still Drafting", "Mine Too"} <= names and "Not Mine" not in names
    assert {r["event"] for r in rows} == {judged.slug, mine.slug}


def test_the_checkers_request_shape(judged):
    """Bearer token, GET, first line has a comma (the acceptance checker's pass rule)."""
    _, raw = ApiToken.issue(judged.organizer, "checker")
    response = Client().get("/api/export.csv", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert response.status_code == 200
    assert "," in response.content.decode("utf-8").splitlines()[0]


def test_bad_requests_say_what_is_allowed(judged, client_for):
    client = client_for(judged.organizer)
    unknown = client.get(f"/api/export.csv?event={judged.slug}&sheet=secrets")
    assert unknown.status_code == 400 and "reviews" in unknown.json()["sheets"]
    assert client.get("/api/export.csv?sheet=reviews").json()["error"] == "event_required"
    assert client.get("/api/export.zip").json()["error"] == "event_required"


def test_the_event_page_offers_the_export(judged, client_for):
    page = client_for(judged.organizer).get(f"/organizer/events/{judged.slug}/").content.decode()
    assert f"/api/export.zip?event={judged.slug}" in page
    assert f"/api/export.csv?event={judged.slug}&amp;sheet=reviews" in page
