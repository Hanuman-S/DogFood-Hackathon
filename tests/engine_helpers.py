"""Shared helpers for the scoring-engine tests (pure: no database needed)."""

from pathlib import Path

import numpy as np

from scoring.engine.config import EngineConfig
from scoring.engine.filters import FILTERS
from scoring.engine.methods import m2_ridge
from scoring.engine.prepare import prepare
from scoring.engine.types import Criterion, EngineInput, Review, Rubric

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "acceptance" / "fixtures.json"
GOLDEN = Path(__file__).resolve().parent / "golden"
KEYS = ("functionality", "quality", "innovation")
LAB_SEED = 20260926  # the scoring lab's MASTER_SEED; goldens are computed with it


def make_input(rows, weights=(1.0, 1.0, 1.0), tracks=None, extra_projects=(), event_id="evt_test",
               duplicates=None, bounds=(1.0, 5.0)):
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
    rubric = Rubric(tuple(Criterion(k, float(w), *bounds) for k, w in zip(KEYS, weights)))
    return EngineInput(
        event_id=event_id, reviews=tuple(reviews), rubric=rubric,
        projects={p: tracks.get(p, "t1") for p in projects}, duplicates=duplicates or {},
    )


NO_FILTERS = {"filters": []}


def rounded(value, decimals):
    """The engine's rounding (np.round), so a test can never disagree with it on a half-way case."""
    return float(np.round(value, decimals))


def fit_m2_direct(inp, config):
    """Filters, then M2's fit on the prepared arrays: gives the covariance, which results omit.
    Assumes one component (every golden input is one; the golden tests assert it)."""
    cfg = EngineConfig.from_dict(config).resolved(inp.event_id)
    reviews, projects = inp.reviews, dict(inp.projects)
    for name in cfg.filters:
        kept = FILTERS[name](reviews, projects, inp.duplicates, cfg)
        reviews, projects = kept.reviews, kept.projects
    reviewed = {r.project_id for r in reviews}
    data = prepare(inp, reviews, [p for p in projects if p in reviewed], rng_seed=cfg.cv_seed)
    return data, m2_ridge.RidgeBiasModel().fit(data, cfg)


def assert_contract(inp, result):
    """What every method's result must satisfy, whatever the method."""
    ranked = [p.project_id for p in result.projects]
    excluded = [e.id for e in result.excluded if e.kind == "project"]
    # every project exactly once: ranked or excluded, never both, never missing
    assert len(ranked) == len(set(ranked))
    assert not set(ranked) & set(excluded)
    assert sorted(ranked + excluded) == sorted(inp.projects)
    components = sorted({p.component for p in result.projects})
    assert components == list(range(len(result.components)))
    decimals = result.config["equal_decimals"]
    position = {p: i for i, p in enumerate(inp.projects)}
    for component in components:
        rows = [p for p in result.projects if p.component == component]
        # ranks are 1..n within the component, and (rounded) scores never increase down the ranking
        assert [p.rank for p in rows] == list(range(1, len(rows) + 1))
        assert all(rounded(a.score, decimals) >= rounded(b.score, decimals) for a, b in zip(rows, rows[1:]))
        # R10: scores equal after rounding are ranked in input order
        for a, b in zip(rows, rows[1:]):
            if rounded(a.score, decimals) == rounded(b.score, decimals):
                assert position[a.project_id] < position[b.project_id], (a.project_id, b.project_id)
        # tie groups: 0..G-1, contiguous and non-decreasing down the ranking
        groups = [p.tie_group for p in rows]
        assert groups[0] == 0
        assert all(b - a in (0, 1) for a, b in zip(groups, groups[1:]))
        if "uncertainty" in result.capabilities:
            assert all(p.se is not None and p.se >= 0 for p in rows)
            threshold = result.config["tie_threshold"]
            for a, b in zip(rows, rows[1:]):
                p = a.extras["p_ahead_of_next"]
                assert 0.0 <= p <= 1.0
                assert (a.tie_group == b.tie_group) == (p < threshold)
            assert rows[-1].extras["p_ahead_of_next"] is None
        else:
            # methods without uncertainty never report an SE, and tie only on equal scores
            assert all(p.se is None for p in rows)
            for a, b in zip(rows, rows[1:]):
                same = rounded(a.score, decimals) == rounded(b.score, decimals)
                assert same == (a.tie_group == b.tie_group)
    if len(result.components) > 1:
        assert result.diagnostics["pipeline"]["ranking"] == "per_component"
