"""Shared helpers for the scoring-engine tests (pure: no database needed)."""

from pathlib import Path

from scoring.engine.types import Criterion, EngineInput, Review, Rubric

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "acceptance" / "fixtures.json"
KEYS = ("functionality", "quality", "innovation")


def make_input(rows, weights=(1.0, 1.0, 1.0), tracks=None, extra_projects=(), event_id="evt_test",
               duplicates=None):
    """rows: (project, judge, scores); scores is one number (every criterion) or a 3-tuple.

    Projects are in order of first appearance, then `extra_projects` (listed, never reviewed).
    """
    projects = list(dict.fromkeys([r[0] for r in rows] + list(extra_projects)))
    tracks = tracks or {}
    reviews = []
    for project, judge, scores in rows:
        values = scores if isinstance(scores, (tuple, list)) else (scores,) * 3
        items = {k: float(v) for k, v in zip(KEYS, values) if v is not None}
        reviews.append(Review(judge, project, items, tracks.get(project, "t1")))
    rubric = Rubric(tuple(Criterion(k, float(w), 1.0, 5.0) for k, w in zip(KEYS, weights)))
    return EngineInput(
        event_id=event_id, reviews=tuple(reviews), rubric=rubric,
        projects={p: tracks.get(p, "t1") for p in projects}, duplicates=duplicates or {},
    )


def assert_contract(inp, result):
    """What every method's result must satisfy, whatever the method."""
    ranked = [p.project_id for p in result.projects]
    excluded = [e.id for e in result.excluded if e.kind == "project"]
    # every project exactly once: ranked or excluded, never both, never missing
    assert len(ranked) == len(set(ranked))
    assert not set(ranked) & set(excluded)
    assert sorted(ranked + excluded) == sorted(inp.projects)
    # ranks are 1..n in order, and scores never increase down the ranking
    assert [p.rank for p in result.projects] == list(range(1, len(ranked) + 1))
    scores = [p.score for p in result.projects]
    assert all(a >= b for a, b in zip(scores, scores[1:]))
    # tie groups: 0..G-1, contiguous and non-decreasing down the ranking
    groups = [p.tie_group for p in result.projects]
    if groups:
        assert groups[0] == 0
        assert all(b - a in (0, 1) for a, b in zip(groups, groups[1:]))
    # methods without uncertainty never report an SE
    if "uncertainty" not in result.capabilities:
        assert all(p.se is None for p in result.projects)
        decimals = result.config["equal_decimals"]
        for a, b in zip(result.projects, result.projects[1:]):
            same = round(a.score, decimals) == round(b.score, decimals)
            assert same == (a.tie_group == b.tie_group)
