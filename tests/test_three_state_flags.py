"""The two "changed since the previous one" flags are true / false / unknown (None). Unknown means the
previous final or tally was imported from a bundle: its input_hash was computed from the source
install's ids, so a comparison would be meaningless -- and a skipped comparison must never read as
"no changes", on the page or anywhere else."""

import pytest
from django.utils import timezone

from scoring import services
from scoring.models import ResultSnapshot
from test_final_score import TX, add_ballots, add_vote
from test_results_flow import judged  # noqa: F401 -- fixture
from voting.models import VoteTallySnapshot

IMPORTED = "b" * 64


def imported_copy(model, row, **changes):
    """An imported row like `row` (a new INSERT: both tables are immutable)."""
    values = {f.attname: getattr(row, f.attname) for f in model._meta.concrete_fields if not f.primary_key}
    values.update({"imported_from": IMPORTED, "created_at": timezone.now(), **changes})
    return model.objects.create(**values)


def results_page(client_for, event):
    return client_for(event.organizer).get(f"/organizer/events/{event.slug}/results").content.decode()


@TX
def test_local_finals_compare_as_before(judged):
    first = services.compute_snapshot(judged, "final", actor=judged.organizer)
    second = services.compute_snapshot(judged, "final", actor=judged.organizer)
    assert first.diagnostics["scores_changed_since_last_final"] is False
    assert second.diagnostics["scores_changed_since_last_final"] is False
    assert second.diagnostics["previous_final"]["imported"] is False


@TX
def test_after_an_imported_final_the_flag_is_unknown_and_the_page_says_so(judged, client_for):
    local = services.compute_snapshot(judged, "final", actor=judged.organizer)
    imported_copy(ResultSnapshot, local, input_hash="c" * 64)
    after = services.compute_snapshot(judged, "final", actor=judged.organizer)
    assert after.diagnostics["scores_changed_since_last_final"] is None  # never False
    assert after.diagnostics["previous_final"]["imported"] is True
    html = results_page(client_for, judged)
    assert "comparison unavailable: previous final was imported" in html
    assert "scores changed since the last final" not in html


@TX
def test_a_real_change_still_shows_as_changed(judged, client_for):
    services.compute_snapshot(judged, "final", actor=judged.organizer)
    last = ResultSnapshot.objects.filter(event=judged, kind="final").latest("pk")
    imported_copy(ResultSnapshot, last, imported_from=None, input_hash="d" * 64)  # a local row, other hash
    after = services.compute_snapshot(judged, "final", actor=judged.organizer)
    assert after.diagnostics["scores_changed_since_last_final"] is True
    assert "scores changed since the last final" in results_page(client_for, judged)


@TX
def test_after_an_imported_tally_the_tally_flag_is_unknown_and_the_page_says_so(judged, client_for):
    add_vote(judged)
    p = judged.projects_list
    add_ballots(judged, [{p[1].pk: 16}])
    first = services.compute_snapshot(judged, "final", actor=judged.organizer).vote_tally
    imported_copy(VoteTallySnapshot, first, previous_id=first.pk, input_hash="e" * 64)
    snapshot = services.compute_snapshot(judged, "final", actor=judged.organizer)
    assert snapshot.vote_tally.changed_since_previous is None
    assert snapshot.diagnostics["vote_tally"]["changed_since_previous_tally"] is None
    html = results_page(client_for, judged)
    assert "comparison unavailable: previous tally was imported" in html
    assert "changed since the previous tally" not in html.replace("comparison unavailable", "")


@TX
def test_a_first_tally_or_an_unchanged_one_shows_nothing(judged, client_for):
    add_vote(judged)
    add_ballots(judged, [{judged.projects_list[1].pk: 16}])
    first = services.compute_snapshot(judged, "final", actor=judged.organizer).vote_tally
    second = services.compute_snapshot(judged, "final", actor=judged.organizer).vote_tally
    assert first.changed_since_previous is False and second.changed_since_previous is False
    html = results_page(client_for, judged)
    assert "comparison unavailable" not in html and "changed since the previous tally" not in html
