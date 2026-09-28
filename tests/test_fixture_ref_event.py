"""FixtureRef.event: every event-scoped ref names its event (a database constraint), the backfill fills
it on a database that already has refs, the fixture importer sets it, and scoring finds an event's
refs by it -- whatever their source -- and never another event's."""

from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from accounts.roles import Role
from events.models import EventMembership, Track
from imports.models import FixtureRef
from projects.models import Project, Status
from scoring.models import Criterion, Score

BEFORE = [("imports", "0002_fixture_judge_refs")]
AFTER = [("imports", "0005_fixture_ref_event_required")]


@pytest.mark.django_db(transaction=True)
def test_the_backfill_fills_every_event_scoped_ref_on_a_database_with_rows(make_event, make_team, make_user):
    event = make_event()
    track = Track.objects.create(event=event, name="Hardware")
    team = make_team(event)
    project = Project.objects.create(team=team, name="P", status=Status.SUBMITTED, submitted_at=timezone.now())
    judge = EventMembership.objects.create(user=make_user(role=Role.JUDGE), event=event, role=Role.JUDGE)
    criterion = Criterion.objects.create(event=event, key="impact", label="Impact", weight=1)
    score = Score.objects.create(judge=judge, project=project, submitted_at=timezone.now())
    user = make_user()
    objects = {"event": event.pk, "track": track.pk, "team": team.pk, "project": project.pk,
               "judge": judge.pk, "score": score.pk, "user": user.pk}
    assert criterion.pk

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps.get_model("imports", "FixtureRef")
    for kind, object_id in objects.items():
        old.objects.create(source="dogfood-fixtures", kind=kind, external_id=f"x_{kind}", object_id=object_id)
    old.objects.create(source="dogfood-fixtures", kind="project", external_id="gone", object_id=987654)

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(AFTER)
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())

    refs = {r.kind: r for r in FixtureRef.objects.all()}
    for kind in ("event", "track", "team", "project", "judge", "score"):
        assert refs[kind].event_id == event.pk, kind
    assert refs["user"].event_id is None
    assert not FixtureRef.objects.filter(external_id="gone").exists()  # its object is gone


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["event", "track", "team", "project", "score", "judge"])
def test_an_event_scoped_ref_without_an_event_is_refused_by_the_database(kind):
    with pytest.raises(IntegrityError), transaction.atomic():
        FixtureRef.objects.create(kind=kind, external_id="x", object_id=1)


@pytest.mark.django_db
def test_a_user_ref_needs_no_event():
    assert FixtureRef.objects.create(kind="user", external_id="u", object_id=1).event_id is None


@pytest.mark.django_db
def test_the_fixture_importer_sets_the_event_of_every_ref():
    from imports.fixtures import import_file

    import_file()
    refs = FixtureRef.objects.all()
    assert refs.filter(kind="user").exists() and not refs.filter(kind="user", event__isnull=False).exists()
    scoped = refs.exclude(kind="user")
    assert scoped.exists() and not scoped.filter(event__isnull=True).exists()
    assert scoped.values("event").distinct().count() == 1
    project_ref = scoped.filter(kind="project").first()
    assert Project.objects.get(pk=project_ref.object_id).event_id == project_ref.event_id


@pytest.mark.django_db
def test_build_input_reads_its_own_events_refs_whatever_their_source(make_event, make_team):
    from scoring.services import build_input

    event, other = make_event(), make_event()
    Criterion.objects.create(event=event, key="impact", label="Impact", weight=1)
    kept = Project.objects.create(team=make_team(event), name="Kept", status=Status.SUBMITTED,
                                  submitted_at=timezone.now() - timedelta(minutes=1))
    # Another event's duplicate ref that happens to name this project's id: not ours, never read.
    FixtureRef.objects.create(source="dogfood-fixtures", kind="project", external_id="prj_other",
                              object_id=kept.pk, duplicate_of="prj_x", event=other)
    assert build_input(event).duplicates == {}
    # Our own duplicate ref, from a bundle's source: read.
    FixtureRef.objects.create(source="bundle:abc:copy", kind="project", external_id="prj_dup",
                              object_id=kept.pk, duplicate_of="prj_kept", event=event)
    assert build_input(event).duplicates == {"dup:prj_dup": str(kept.pk)}
