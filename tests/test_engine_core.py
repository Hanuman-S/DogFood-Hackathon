"""Scoring engine S1: baselines, pipeline, ties, coverage, config, input checks, file loader, CLI,
determinism. Pure computation: no database."""

import io
import json

import numpy as np
import pytest
from engine_helpers import FIXTURES, make_input

from scoring.engine import pipeline
from scoring.engine.cli import main as cli_main
from scoring.engine.config import EngineConfig
from scoring.engine.errors import ConfigError, EngineInputError, UnknownMethod
from scoring.engine.io import from_organizer_dict, load_organizer_file
from scoring.engine.ties import exact_tie_groups, ordinal_rank
from scoring.engine.types import Criterion, EngineInput, Review, Rubric

PDF_WEIGHTS = (0.5, 0.3, 0.2)
# The ridge PDF's worked example, step 1 (as in the lab's tests/test_m2_pdf.py).
WORKED = [
    ("P1", "Harsh", (4, 3, 3)), ("P2", "Harsh", 2.8), ("P3", "Harsh", 2.2),
    ("P3", "Lenient", 4.7), ("P4", "Lenient", 4.5), ("P5", "Lenient", 3.8),
    ("P1", "Normal", 4.5), ("P2", "Normal", 3.8), ("P4", "Normal", 3.7),
    ("P1", "Flat", 3), ("P2", "Flat", 3), ("P5", "Flat", 3),
    ("P5", "Single", 5),
]


def scores(result):
    return {p.project_id: p.score for p in result.projects}


# ------------------------------------------------------------------ baselines (ported from the lab)

def test_raw_mean_worked_example_step2():
    """PDF step 2: raw averages with weights 0.5/0.3/0.2, the flat judge still included."""
    got = scores(pipeline.run(make_input(WORKED, weights=PDF_WEIGHTS), "raw_mean"))
    for project, expected in {"P4": 4.10, "P5": 3.93, "P1": 3.67, "P3": 3.45, "P2": 3.20}.items():
        assert got[project] == pytest.approx(expected, abs=0.005)


def test_weighted_score_renormalises_over_present_criteria():
    """Lab M1 hand calculation: a review missing a criterion is averaged over the ones present."""
    inp = make_input([("A", "J1", (4, 3, 3)), ("A", "J2", (5, None, 2)), ("B", "J1", (2, 2, 2))],
                     weights=PDF_WEIGHTS)
    y2 = (0.5 * 5 + 0.2 * 2) / 0.7
    got = scores(pipeline.run(inp, "raw_mean"))
    assert got["A"] == pytest.approx((3.5 + y2) / 2)
    assert got["B"] == pytest.approx(2.0)


def test_zscore_floor_for_single_and_flat_judges():
    """Lab test: a single-review judge and a flat judge have SD 0, fall to the floor, give z = 0."""
    rows = [("A", "Flat", 3), ("B", "Flat", 3), ("A", "Single", 5), ("B", "N", 2), ("A", "N", 4)]
    result = pipeline.run(make_input(rows), "zscore")
    assert result.diagnostics["method"]["sd_floored_judges"] == ["Flat", "Single"]
    # N: mean 3, population SD 1 -> A +1, B -1; Flat and Single contribute 0
    assert scores(result) == pytest.approx({"A": 1 / 3, "B": -0.5})


def test_zscore_zero_variance_and_single_review_judges_score_zero():
    rows = [("C", "Flat", 4), ("D", "Flat", 4), ("E", "Single", 5), ("C", "N", 2), ("D", "N", 4)]
    got = scores(pipeline.run(make_input(rows), "zscore"))
    assert got["E"] == 0.0  # only reviewed by the single-review judge
    assert got["C"] == pytest.approx(-0.5) and got["D"] == pytest.approx(0.5)


def test_zscore_floor_is_configurable():
    rows = [("A", "J", 3), ("B", "J", 3.2)]  # SD 0.1
    default = scores(pipeline.run(make_input(rows), "zscore"))
    tight = scores(pipeline.run(make_input(rows), "zscore", {"z_floor": 0.1}))
    assert default["B"] == pytest.approx(0.1 / 0.5)
    assert tight["B"] == pytest.approx(1.0)


# ------------------------------------------------------------------ pipeline edge cases

def test_empty_event():
    result = pipeline.run(make_input([], extra_projects=("A",)), "raw_mean")
    assert result.projects == ()
    assert [(e.kind, e.id, e.reason) for e in result.excluded] == [("project", "A", "no reviews")]
    assert result.coverage["reviews"] == 0
    assert "empty" in result.diagnostics["pipeline"]


def test_one_project():
    (only,) = pipeline.run(make_input([("A", "J1", 4), ("A", "J2", 2)]), "zscore").projects
    assert (only.project_id, only.rank, only.tie_group, only.n_reviews) == ("A", 1, 0, 2)


def test_project_with_no_reviews_is_excluded_not_ranked():
    result = pipeline.run(make_input([("A", "J1", 4)], extra_projects=("Z",)), "raw_mean")
    assert [p.project_id for p in result.projects] == ["A"]
    assert result.excluded[0].id == "Z"
    assert result.coverage["projects_with_no_reviews"] == ["Z"]


def test_all_tied_is_one_tie_group_in_input_order():
    result = pipeline.run(make_input([(p, "J1", 3) for p in "DCBA"]), "raw_mean")
    assert [p.project_id for p in result.projects] == list("DCBA")
    assert {p.tie_group for p in result.projects} == {0}


def test_equal_means_that_differ_in_the_last_bit_still_tie():
    scores_ = np.array([0.1 + 0.2, 0.3, 0.2])
    assert scores_[0] != scores_[1]  # 0.30000000000000004 vs 0.3
    rank = ordinal_rank(scores_)
    assert list(exact_tie_groups(scores_, rank, 9)) == [0, 0, 1]
    assert list(exact_tie_groups(scores_, rank, 17)) == [0, 1, 2]


def test_ties_only_on_equal_scores():
    result = pipeline.run(make_input([("A", "J1", 4), ("B", "J1", 4), ("C", "J1", 3)]), "raw_mean")
    assert [(p.project_id, p.rank, p.tie_group) for p in result.projects] == [
        ("A", 1, 0), ("B", 2, 0), ("C", 3, 1)]


def test_coverage_counts():
    rows = [("A", "J1", 4), ("A", "J2", 3), ("A", "J3", 3), ("B", "J1", 2)]
    cov = pipeline.run(make_input(rows, extra_projects=("C",)), "raw_mean").coverage
    assert cov["reviews_per_project"] == {"min": 0, "median": 1.0, "max": 3}
    assert cov["projects_below_min_reviews"] == ["B"]
    assert cov["projects_with_no_reviews"] == ["C"]
    assert cov["reviews_by_judge"] == {"J1": 2, "J2": 1, "J3": 1}
    assert "assignment" in cov["note"]


def test_compare_reports_rank_change_against_the_baseline():
    rows = [("A", "Harsh", 2), ("B", "Harsh", 1), ("B", "Kind", 5), ("C", "Kind", 5), ("C", "Harsh", 1)]
    result = pipeline.compare(make_input(rows), ["zscore"], baseline="raw_mean")
    assert result.methods == ("zscore", "raw_mean")
    for row in result.rows:
        raw, z = row.ranks["raw_mean"], row.ranks["zscore"]
        assert row.change_vs_baseline["zscore"] == raw - z
        assert row.change_vs_baseline["raw_mean"] == 0


def test_compare_defaults_to_the_config_methods():
    result = pipeline.compare(make_input([("A", "J1", 4), ("B", "J1", 3)]))
    assert result.primary == EngineConfig().primary
    assert set(result.methods) == {"raw_mean", "zscore"}


# ------------------------------------------------------------------ errors

def test_unknown_method_is_a_clear_error():
    with pytest.raises(UnknownMethod, match="no_such_method.*Available: .*raw_mean"):
        pipeline.run(make_input([("A", "J1", 4)]), "no_such_method")


@pytest.mark.parametrize("bad", [
    {"tie_treshold": 0.9},              # typo: unknown key
    {"primary": "raw_mean", "extra": 1},
])
def test_unknown_config_key_is_an_error(bad):
    with pytest.raises(ConfigError, match="Unknown engine config key"):
        EngineConfig.from_dict(bad)


@pytest.mark.parametrize("bad", [
    {"tie_threshold": 1.0}, {"tie_threshold": "0.84"}, {"equal_decimals": 2.5},
    {"z_floor": 0}, {"min_reviews": True}, {"compare": "zscore"}, {"primary": ""},
])
def test_config_values_are_checked(bad):
    with pytest.raises(ConfigError):
        EngineConfig.from_dict(bad)


def test_config_round_trips():
    config = EngineConfig.from_dict({"compare": ["zscore"], "z_floor": 0.25})
    assert EngineConfig.from_dict(config.to_dict()) == config


@pytest.mark.parametrize("rows, message", [
    ([("A", "J1", 4), ("A", "J1", 3)], "more than once"),
    ([("A", "J1", 6)], "outside"),
    ([("A", "J1", (None, None, None))], "no criterion values"),
])
def test_input_is_validated(rows, message):
    with pytest.raises(EngineInputError, match=message):
        pipeline.run(make_input(rows), "raw_mean")


def test_unknown_project_and_criterion_are_refused():
    rubric = Rubric((Criterion("c", 1.0),))
    bad_project = EngineInput("e", (Review("J", "X", {"c": 3}),), rubric, {"A": None})
    bad_criterion = EngineInput("e", (Review("J", "A", {"d": 3}),), rubric, {"A": None})
    zero_weight = EngineInput("e", (Review("J", "A", {"c": 3}),), Rubric((Criterion("c", 0.0),)), {"A": None})
    for inp, message in ((bad_project, "unknown project"), (bad_criterion, "not in the rubric"),
                         (zero_weight, "weight 0")):
        with pytest.raises(EngineInputError, match=message):
            pipeline.run(inp, "raw_mean")


# ------------------------------------------------------------------ file loader

def test_fixture_file_loads_with_the_duplicate_declared():
    inp, log = load_organizer_file(FIXTURES)
    assert inp.event_id == "evt_01"
    assert len(inp.projects) == 41 and len(inp.reviews) == 126
    assert dict(inp.duplicates) == {"prj_41": "prj_07"}
    assert [c.id for c in inp.rubric.criteria] == ["functionality", "quality", "innovation"]
    assert all(c.weight == 1.0 for c in inp.rubric.criteria)
    assert any("prj_41 duplicates prj_07" in line for line in log)


def test_loader_averages_repeated_records_and_drops_unknown_projects():
    raw = {
        "event": {"id": "e"},
        "projects": [{"id": "p1", "team": "t1", "track": "k"}, {"id": "p2", "team": "t2", "track": "k"}],
        "scores": [
            {"judge": "j", "project": "p1", "criteria": {"a": 2, "b": 4}},
            {"judge": "j", "project": "p2", "criteria": {"a": 3, "b": 3}},
            {"judge": "j", "project": "p1", "criteria": {"a": 4, "b": None}},
            {"judge": "j", "project": "ghost", "criteria": {"a": 1, "b": 1}},
        ],
    }
    inp, log = from_organizer_dict(raw)
    assert [(r.project_id, dict(r.items)) for r in inp.reviews] == [
        ("p1", {"a": 3.0, "b": 4.0}), ("p2", {"a": 3.0, "b": 3.0})]
    assert any("repeats: 1" in line for line in log)
    assert any("unknown projects: 1" in line for line in log)


def test_loader_duplicate_rule_is_team_or_title_and_repo():
    raw = {"event": {"id": "e"}, "scores": [], "projects": [
        {"id": "a", "team": "t1", "title": "Dry Harbour ", "repo_url": "r1", "submitted_at": "2026-03-01T10:00:00Z"},
        {"id": "b", "team": "t2", "title": "dry harbour", "repo_url": "r1", "submitted_at": "2026-03-01T09:00:00Z"},
        {"id": "c", "team": "t3", "title": "dry harbour", "repo_url": "r9"},   # title alone: not a duplicate
        {"id": "d", "team": "t1", "title": "Other", "repo_url": "r4", "submitted_at": "2026-03-01T11:00:00Z"},
    ]}
    inp, _ = from_organizer_dict(raw)
    assert dict(inp.duplicates) == {"a": "b", "d": "b"}


def test_loader_weights_override():
    inp, _ = load_organizer_file(FIXTURES, weights={"functionality": 0.5, "quality": 0.3, "innovation": 0.2})
    assert [c.weight for c in inp.rubric.criteria] == [0.5, 0.3, 0.2]
    with pytest.raises(EngineInputError, match="not in the file"):
        load_organizer_file(FIXTURES, weights={"speed": 1})


# ------------------------------------------------------------------ determinism and CLI

def test_same_input_and_config_give_byte_identical_json():
    first = pipeline.compare(load_organizer_file(FIXTURES)[0]).to_json()
    second = pipeline.compare(load_organizer_file(FIXTURES)[0]).to_json()
    assert first == second
    parsed = json.loads(first)
    assert list(parsed) == ["primary", "baseline", "methods", "results", "rows"]


def test_cli_prints_a_ranked_table_with_tie_groups(tmp_path):
    out = io.StringIO()
    target = tmp_path / "out.json"
    assert cli_main([str(FIXTURES), "--method", "zscore", "--compare", "raw_mean", "--json", str(target)], out) == 0
    text = out.getvalue()
    assert "method zscore v1 | baseline raw_mean" in text
    assert "tie group" in text and "coverage:" in text
    assert json.loads(target.read_text(encoding="utf-8"))["primary"] == "zscore"
    again = tmp_path / "again.json"
    cli_main([str(FIXTURES), "--method", "zscore", "--compare", "raw_mean", "--json", str(again)], io.StringIO())
    assert target.read_bytes() == again.read_bytes()


def test_cli_marks_tied_rows():
    out = io.StringIO()
    cli_main([str(FIXTURES)], out)
    ranked = [line for line in out.getvalue().splitlines() if line[:4].strip().isdigit()]
    assert any(line[4] == "=" for line in ranked)  # the fixtures have exact ties under raw_mean


def test_cli_lists_methods_and_reports_errors(tmp_path):
    out = io.StringIO()
    assert cli_main(["--list"], out) == 0
    assert "raw_mean" in out.getvalue() and "zscore" in out.getvalue()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"tie_treshold": 0.9}), encoding="utf-8")
    out = io.StringIO()
    assert cli_main([str(FIXTURES), "--config", str(bad)], out) == 2
    assert "Unknown engine config key" in out.getvalue()
    out = io.StringIO()
    assert cli_main([str(FIXTURES), "--method", "nope"], out) == 2
    assert "No scoring method named 'nope'" in out.getvalue()
