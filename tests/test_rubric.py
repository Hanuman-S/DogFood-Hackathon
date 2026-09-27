"""The rubric: weights are percentages adding up to exactly 100, the whole rubric saves at once,
and it locks at the submission close -- except for wording, which stays editable."""

import importlib
from datetime import timedelta
from decimal import Decimal

import pytest
from django.apps import apps
from django.test import Client
from django.utils import timezone

from accounts.roles import Role
from core.models import AuditAction, AuditLog
from events.models import EventMembership
from projects.models import Project, Status
from scoring import services
from scoring.models import Criterion, Score, ScoreItem
from scoring.services import RubricError, RubricLocked, RubricRow

pytestmark = pytest.mark.django_db

D = Decimal


def row(label, weight, id=None, key="", lo=1, hi=5, delete=False, order=1):
    return RubricRow(id=id, key=key, label=label, weight=D(str(weight)), min_value=lo,
                     max_value=hi, order=order, delete=delete)


@pytest.fixture
def open_event(make_event):
    return make_event()


@pytest.fixture
def closed_event(make_event):
    now = timezone.now()
    return make_event(
        submissions_open_at=now - timedelta(days=5),
        submissions_close_at=now - timedelta(hours=1), judging_ends_at=now + timedelta(days=5),
    )


class FakeRequest:
    """What audit.record reads from a request, for service calls outside a view."""

    def __init__(self, user):
        self.user = user
        self.META = {"REMOTE_ADDR": "10.0.0.1"}
        self.headers = {}


def req(event):
    return FakeRequest(event.organizer)


# --- weight arithmetic -----------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 2, 3, 6, 7, 11])
def test_split_equally_always_adds_up_to_exactly_100(n):
    weights = services.split_equally(n)
    assert len(weights) == n and sum(weights) == D("100")
    assert max(weights) - min(weights) <= D("0.001")


def test_three_criteria_split_as_33_334_33_333_33_333():
    assert [str(w) for w in services.split_equally(3)] == ["33.334", "33.333", "33.333"]


@pytest.mark.parametrize("given, expected", [
    ([1, 1, 1], ["33.334", "33.333", "33.333"]),
    ([2, 1, 1], ["50.000", "25.000", "25.000"]),
    ([0, 0], ["50.000", "50.000"]),
    ([1, 2], ["33.333", "66.667"]),
])
def test_old_relative_weights_convert_keeping_their_proportions(given, expected):
    assert [str(w) for w in services.as_percentages(given)] == expected


# --- saving -----------------------------------------------------------------------------------------


def test_a_rubric_that_adds_up_to_100_saves(open_event):
    services.save_rubric(req(open_event), open_event, [row("Functionality", 50), row("Quality", "30.5"), row("Innovation", "19.5")])
    saved = {c.key: c.weight for c in open_event.criteria.all()}
    assert saved == {"functionality": D("50"), "quality": D("30.5"), "innovation": D("19.5")}
    assert AuditLog.objects.filter(action=AuditAction.CRITERION_ADDED).count() == 3


@pytest.mark.parametrize("rows, message", [
    ([row("A", 50), row("B", "49.999")], "add up to 99.999%"),
    ([row("A", 100), row("B", 1)], "add up to 101%"),
    ([row("A", 110), row("B", -10)], "cannot be negative"),
    ([row("A", 50), row("A", 50)], "share the key"),
    ([row("A", 100, lo=5, hi=5)], "scale"),
    ([row("A", 100, lo=0, hi=11)], "scale"),
    ([row("A", "99.9995"), row("B", "0.0005")], "three decimal places"),
    ([row("A", 100, delete=True)], "at least one criterion"),
    ([], "at least one criterion"),
])
def test_a_rubric_that_breaks_a_rule_is_refused_whole(open_event, rows, message):
    with pytest.raises(RubricError, match=message):
        services.save_rubric(req(open_event), open_event, rows)
    assert not open_event.criteria.exists()


def test_editing_reweights_renames_removes_and_audits_old_and_new(open_event):
    services.use_standard_rubric(req(open_event), open_event)
    f, q, i = open_event.criteria.order_by("order")
    services.save_rubric(req(open_event), open_event, [
        row("Functionality", 60, id=f.pk, key="functionality"),
        row("Code quality", 40, id=q.pk, key="quality"),
        row("Innovation", 0, id=i.pk, key="innovation", delete=True),
    ])
    assert {c.key: (c.label, c.weight) for c in open_event.criteria.all()} == {
        "functionality": ("Functionality", D("60")), "quality": ("Code quality", D("40")),
    }
    updated = AuditLog.objects.filter(action=AuditAction.CRITERION_UPDATED, detail__key="functionality").get()
    assert updated.detail["changed"]["weight"] == ["33.334", "60.000"]
    assert AuditLog.objects.filter(action=AuditAction.CRITERION_REMOVED, detail__key="innovation").exists()


def test_two_criteria_can_swap_keys(open_event):
    services.save_rubric(req(open_event), open_event, [row("A", 50, key="a"), row("B", 50, key="b")])
    a, b = open_event.criteria.order_by("order")
    services.save_rubric(req(open_event), open_event, [row("A", 50, id=a.pk, key="b"), row("B", 50, id=b.pk, key="a")])
    assert dict(open_event.criteria.values_list("label", "key")) == {"A": "b", "B": "a"}


def test_a_criterion_from_another_event_cannot_be_edited_through_this_one(open_event, make_event):
    other = make_event()
    services.use_standard_rubric(req(other), other)
    foreign = other.criteria.first()
    with pytest.raises(RubricError, match="changed while you were editing"):
        services.save_rubric(req(open_event), open_event, [row("Hijack", 100, id=foreign.pk)])
    foreign.refresh_from_db()
    assert foreign.label == "Functionality"


def _score(event, criterion, value, make_team, make_user):
    project = Project.objects.create(team=make_team(event), name="P", status=Status.SUBMITTED, submitted_at=timezone.now())
    judge = EventMembership.objects.create(event=event, user=make_user(), role=Role.JUDGE)
    score = Score.objects.create(judge=judge, project=project)
    ScoreItem.objects.create(score=score, criterion=criterion, value=value)


def test_scored_criteria_are_never_rewritten(open_event, make_team, make_user):
    services.save_rubric(req(open_event), open_event, [row("A", 50, key="a"), row("B", 50, key="b")])
    a, b = open_event.criteria.order_by("order")
    _score(open_event, a, 5, make_team, make_user)
    with pytest.raises(RubricError, match="already has scores"):
        services.save_rubric(req(open_event), open_event, [row("A", 50, id=a.pk, key="a", delete=True), row("B", 100, id=b.pk, key="b")])
    with pytest.raises(RubricError, match="outside 1-4"):
        services.save_rubric(req(open_event), open_event, [row("A", 50, id=a.pk, key="a", hi=4), row("B", 50, id=b.pk, key="b")])
    assert open_event.criteria.count() == 2


def test_the_standard_rubric_is_three_equal_criteria_with_level_descriptions(open_event):
    services.use_standard_rubric(req(open_event), open_event)
    criteria = list(open_event.criteria.order_by("order"))
    assert [c.key for c in criteria] == ["functionality", "quality", "innovation"]
    assert sum(c.weight for c in criteria) == 100
    assert all(sorted(c.level_descriptions) == ["1", "2", "3", "4", "5"] for c in criteria)
    with pytest.raises(RubricError, match="already has a rubric"):
        services.use_standard_rubric(req(open_event), open_event)


# --- the lock ---------------------------------------------------------------------------------------


def test_the_rubric_locks_at_the_submission_close(open_event, closed_event):
    assert not services.rubric_locked(open_event)
    assert services.rubric_locked(closed_event)


def test_a_locked_rubric_refuses_every_ranking_change_and_logs_it(closed_event):
    with pytest.raises(RubricLocked):
        services.save_rubric(req(closed_event), closed_event, [row("A", 100)])
    with pytest.raises(RubricLocked):
        services.use_standard_rubric(req(closed_event), closed_event)
    assert not closed_event.criteria.exists()
    assert AuditLog.objects.filter(action=AuditAction.RUBRIC_CHANGE_REFUSED).count() == 2


def test_wording_stays_editable_after_the_lock_and_is_logged(closed_event):
    criterion = Criterion.objects.create(event=closed_event, key="quality", label="Qualty", weight=100)
    services.update_criterion_text(
        req(closed_event), criterion, label="Quality", description="Readable code.",
        level_descriptions={"1": "unreadable", "3": "", "5": "idiomatic"},
    )
    criterion.refresh_from_db()
    assert criterion.label == "Quality" and criterion.weight == 100
    assert criterion.level_descriptions == {"1": "unreadable", "5": "idiomatic"}
    entry = AuditLog.objects.get(action=AuditAction.CRITERION_UPDATED)
    assert entry.detail["while_locked"] is True and "label" in entry.detail["changed"]


def test_a_level_outside_the_scale_is_refused(open_event):
    criterion = Criterion.objects.create(event=open_event, key="a", label="A", weight=100)
    with pytest.raises(RubricError, match="not a level"):
        services.update_criterion_text(req(open_event), criterion, label="A", description="", level_descriptions={"9": "x"})


# --- the one-time conversion ---------------------------------------------------------------------


def test_the_migration_turns_old_relative_weights_into_percentages(open_event, make_event):
    Criterion.objects.bulk_create([
        Criterion(event=open_event, key=k, label=k, weight=1, order=n) for n, k in enumerate("abc", 1)
    ])
    already = make_event()
    Criterion.objects.bulk_create([
        Criterion(event=already, key="x", label="x", weight=D("70"), order=1),
        Criterion(event=already, key="y", label="y", weight=D("30"), order=2),
    ])
    migration = importlib.import_module("scoring.migrations.0003_weights_are_percentages")
    migration.convert_weights(apps, None)
    assert [str(c.weight) for c in open_event.criteria.order_by("order")] == ["33.334", "33.333", "33.333"]
    assert [c.weight for c in already.criteria.order_by("order")] == [D("70"), D("30")]


# --- the pages --------------------------------------------------------------------------------------


def formset_post(rows, action="save", initial=0):
    data = {
        "rubric-TOTAL_FORMS": str(len(rows)), "rubric-INITIAL_FORMS": str(initial),
        "rubric-MIN_NUM_FORMS": "0", "rubric-MAX_NUM_FORMS": "20", "action": action,
    }
    for i, r in enumerate(rows):
        for field, value in r.items():
            data[f"rubric-{i}-{field}"] = value
    return data


# The empty "add a criterion" row, as a browser submits it: only the scale's initial values.
BLANK_ROW = {"min_value": "1", "max_value": "5"}


def line(label, weight, **extra):
    return {"label": label, "weight": str(weight), "min_value": "1", "max_value": "5", **extra}


def test_only_this_events_organizers_reach_the_rubric(open_event, client_for, make_user):
    url = f"/organizer/events/{open_event.slug}/rubric"
    assert client_for(open_event.organizer).get(url).status_code == 200
    assert client_for(make_user(role=Role.ORGANIZER)).get(url).status_code == 404
    assert client_for(make_user(role=Role.JUDGE)).get(url).status_code == 403
    assert Client().get(url).status_code == 302


def test_saving_through_the_page(open_event, client_for):
    client = client_for(open_event.organizer)
    url = f"/organizer/events/{open_event.slug}/rubric"
    response = client.post(url, formset_post([line("Functionality", 60), line("Quality", 40), BLANK_ROW]))
    assert response.status_code == 302
    assert sorted(open_event.criteria.values_list("weight", flat=True)) == [D("40"), D("60")]


def test_a_total_that_is_not_100_is_shown_back_and_nothing_saves(open_event, client_for):
    response = client_for(open_event.organizer).post(
        f"/organizer/events/{open_event.slug}/rubric", formset_post([line("A", 60), line("B", 30)])
    )
    assert response.status_code == 400
    assert b"add up to 90%" in response.content
    assert not open_event.criteria.exists()


def test_split_equally_fills_the_weights_in_without_saving(open_event, client_for):
    response = client_for(open_event.organizer).post(
        f"/organizer/events/{open_event.slug}/rubric", formset_post([line("A", 0), line("B", 0), line("C", 0)], action="split")
    )
    assert response.status_code == 200
    assert b'value="33.334"' in response.content and response.content.count(b'value="33.333"') == 2
    assert not open_event.criteria.exists()


def test_a_locked_rubric_page_is_read_only_and_refuses_a_forged_post(closed_event, client_for):
    Criterion.objects.create(event=closed_event, key="a", label="A", weight=100)
    client = client_for(closed_event.organizer)
    url = f"/organizer/events/{closed_event.slug}/rubric"
    page = client.get(url)
    assert b"locked" in page.content and b"save rubric" not in page.content
    response = client.post(url, formset_post([line("A", 100)]))
    assert response.status_code == 409
    assert closed_event.criteria.get().weight == 100


def test_level_descriptions_can_be_edited_while_locked(closed_event, client_for):
    criterion = Criterion.objects.create(event=closed_event, key="a", label="A", weight=100)
    response = client_for(closed_event.organizer).post(
        f"/organizer/events/{closed_event.slug}/rubric/{criterion.pk}",
        {"label": "A", "description": "", "level_1": "poor", "level_5": "great"},
    )
    assert response.status_code == 302
    criterion.refresh_from_db()
    assert criterion.level_descriptions == {"1": "poor", "5": "great"}


def test_the_event_page_summarises_the_rubric(open_event, client_for):
    services.use_standard_rubric(req(open_event), open_event)
    page = client_for(open_event.organizer).get(f"/organizer/events/{open_event.slug}/")
    assert b"33.334%" in page.content and b"edit rubric" in page.content
