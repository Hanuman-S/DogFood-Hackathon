"""M2 (ridge bias model) and the S2 pipeline: filters, components, SE ties, flaggers, explain.

The first half ports the scoring lab's tests/test_m2_pdf.py, which check M2 against the ridge
PDF's worked example. Expected values are the lab's, unchanged.
"""

import io
import json
import time

import numpy as np
import pytest
from engine_helpers import FIXTURES, GOLDEN, LAB_SEED, NO_FILTERS, assert_contract, fit_m2_direct, make_input

from scoring.engine import pipeline
from scoring.engine.cli import main as cli_main
from scoring.engine.cli import read_config
from scoring.engine.components import components
from scoring.engine.config import EngineConfig
from scoring.engine.errors import EngineError
from scoring.engine.filters import FILTERS
from scoring.engine.io import load_organizer_file
from scoring.engine.methods import m2_ridge
from scoring.engine.prepare import prepare
from scoring.engine.ties import p_ahead, se_tie_groups

PDF_WEIGHTS = (0.5, 0.3, 0.2)
# The ridge PDF's worked example, step 1 (lab tests/test_m2_pdf.py). Scores are the weighted value
# on every criterion, except Harsh on P1, which the PDF spells out as 4/3/3.
WORKED = [
    ("P1", "Harsh", (4, 3, 3)), ("P2", "Harsh", 2.8), ("P3", "Harsh", 2.2),
    ("P3", "Lenient", 4.7), ("P4", "Lenient", 4.5), ("P5", "Lenient", 3.8),
    ("P1", "Normal", 4.5), ("P2", "Normal", 3.8), ("P4", "Normal", 3.7),
    ("P1", "Flat", 3), ("P2", "Flat", 3), ("P5", "Flat", 3),
    ("P5", "Single", 5),
]
WORKED_CONFIG = {"lambdas": [0.5, 1.0]}


# ================================================================== lab: the PDF's worked example

class TestWorkedExample:
    @pytest.fixture(scope="class")
    def result(self):
        return pipeline.run(make_input(WORKED, weights=PDF_WEIGHTS), "m2", WORKED_CONFIG)

    def test_flat_judge_excluded_single_kept(self, result):
        assert [(e.kind, e.id) for e in result.excluded if e.kind == "judge"] == [("judge", "Flat")]
        assert result.diagnostics["pipeline"]["reviews_used"] == 10   # "The fit used N = 10 reviews"
        assert "Single" in [j.judge_id for j in result.judges]

    def test_leans(self, result):
        bias = {j.judge_id: j.bias for j in result.judges}
        for judge, value in {"Harsh": -0.84, "Lenient": 0.33, "Normal": -0.01, "Single": 0.51}.items():
            assert bias[judge] == pytest.approx(value, abs=0.005), judge

    def test_scores_and_se(self, result):
        got = result.by_project()
        expect = {"P1": (4.33, 0.54), "P5": (3.97, 0.53), "P4": (3.94, 0.53),
                  "P2": (3.77, 0.54), "P3": (3.75, 0.53)}
        for project, (score, se) in expect.items():
            assert got[project].score == pytest.approx(score, abs=0.005), project
            assert got[project].se == pytest.approx(se, abs=0.005), project

    def test_fit_statistics(self, result):
        params = result.params_chosen["components"][0]
        assert params["trH"] == pytest.approx(5.86, abs=0.005)
        assert params["rss"] == pytest.approx(1.56, abs=0.005)
        assert params["sigma2"] == pytest.approx(0.38, abs=0.005)
        assert (params["lambda_source"], params["lambda_at_grid_boundary"]) == ("fixed", None)

    def test_p_ahead_table_step6(self):
        data, out = fit_m2_direct(make_input(WORKED, weights=PDF_WEIGHTS), WORKED_CONFIG)
        index = {p: i for i, p in enumerate(data.projects)}
        rows = {  # (Delta, SD, z, P) as printed in the PDF
            ("P1", "P5"): (0.36, 0.65, 0.55, 0.71),
            ("P5", "P4"): (0.03, 0.60, 0.05, 0.52),
            ("P4", "P2"): (0.17, 0.58, 0.29, 0.62),
            ("P2", "P3"): (0.02, 0.58, 0.03, 0.51),
            ("P1", "P3"): (0.58, 0.58, 0.99, 0.84),
        }
        for (a, b), expected in rows.items():
            got = p_ahead(out.cov_q, out.scores, index[a], index[b])
            for value, want, name in zip(got, expected, ("delta", "sd", "z", "p")):
                assert value == pytest.approx(want, abs=0.005), f"{a} vs {b} {name}"

    def test_p1_vs_p3_sd_is_the_computed_value(self):
        # The PDF prints SD(Delta) = 0.59 for P1 vs P3. That is a typo: its own z = 0.99 and
        # P = 0.84 only fit the computed 0.584, which is what we assert.
        data, out = fit_m2_direct(make_input(WORKED, weights=PDF_WEIGHTS), WORKED_CONFIG)
        index = {p: i for i, p in enumerate(data.projects)}
        _, sd, _, _ = p_ahead(out.cov_q, out.scores, index["P1"], index["P3"])
        assert sd == pytest.approx(0.584, abs=0.0005)

    def test_one_tie_group(self, result):
        """Every neighbouring pair has z < 1, so all five form one tie group."""
        assert {p.tie_group for p in result.projects} == {0}


class TestTinyIllustration:
    ROWS = [("P1", "Harsh", 3), ("P2", "Harsh", 2), ("P1", "Normal", 4), ("P3", "Normal", 3)]

    def run_fit(self, lq, lb):
        data = prepare(make_input(self.ROWS))
        x, *_ = m2_ridge.solve(data.y, data.pi, data.ji, data.P, data.J, lq, lb)
        S = x[0] + x[1:1 + data.P]
        return x[1 + data.P + data.judges.index("Harsh")], S[1], S[2]

    def test_reproduces_only_with_lam_q_near_zero(self):
        for lb, expected in {1e-8: (-0.50, 2.50, 2.50), 1: (-0.25, 2.25, 2.75), 3: (-0.125, 2.125, 2.875)}.items():
            for got, want in zip(self.run_fit(1e-8, lb), expected):
                assert got == pytest.approx(want, abs=0.0005), f"lam_b={lb}"

    def test_does_not_reproduce_at_default_lam_q(self):
        _, p2, p3 = self.run_fit(0.5, 1.0)
        assert p2 == pytest.approx(2.48, abs=0.005)
        assert p3 == pytest.approx(2.77, abs=0.005)


@pytest.mark.parametrize("n, expected", [(1, -0.50), (3, -0.75), (10, -0.91)])
def test_shrinkage_section6_table(n, expected):
    """One truly 1-point-harsh judge among many calibrated ones: lean ~ n / (n + lam_b) x -1."""
    rows = [(f"P{p:02d}", f"A{k:03d}", 3.0 + 0.1 * p) for p in range(10) for k in range(300)]
    rows += [(f"P{p:02d}", "Harsh", 2.0 + 0.1 * p) for p in range(n)]
    data = prepare(make_input(rows))
    x = m2_ridge.solve(data.y, data.pi, data.ji, data.P, data.J, 1e-6, 1.0, full=False)
    assert x[1 + data.P + data.judges.index("Harsh")] == pytest.approx(expected, abs=0.01)


def random_design(seed=1, P=12, J=8, per_judge=5, bounds=(1.0, 5.0)):
    rng = np.random.default_rng(seed)
    rows = []
    for j in range(J):
        for p in rng.choice(P, per_judge, replace=False):
            rows.append((f"P{p:02d}", f"J{j}", tuple(int(v) for v in rng.integers(1, 6, 3))))
    return rows


def test_constant_on_one_judge_changes_its_lean_not_the_others():
    rows = random_design()
    base = pipeline.run(make_input(rows, bounds=(0.0, 10.0)), "m2", {**NO_FILTERS, **WORKED_CONFIG})
    shifted_rows = [(p, j, tuple(v + 0.7 for v in s) if j == "J0" else s) for p, j, s in rows]
    shifted = pipeline.run(make_input(shifted_rows, bounds=(0.0, 10.0)), "m2", {**NO_FILTERS, **WORKED_CONFIG})
    before = {j.judge_id: j.bias for j in base.judges}
    after = {j.judge_id: j.bias for j in shifted.judges}
    delta = {j: after[j] - before[j] for j in before}
    assert delta["J0"] > 0.3
    assert max(abs(v) for j, v in delta.items() if j != "J0") < delta["J0"]


def test_zero_lambda_matches_ols():
    data = prepare(make_input(random_design()))
    x, *_ = m2_ridge.solve(data.y, data.pi, data.ji, data.P, data.J, 0.0, 0.0)
    A = np.zeros((data.N, 1 + data.P + data.J))
    A[np.arange(data.N), 0] = 1
    A[np.arange(data.N), 1 + data.pi] = 1
    A[np.arange(data.N), 1 + data.P + data.ji] = 1
    ols = np.linalg.lstsq(A, data.y, rcond=None)[0]
    np.testing.assert_allclose(A @ x, A @ ols, atol=1e-8)


def test_single_five_is_pulled_to_the_mean():
    rows = [("P1", "A", 3), ("P1", "B", 3), ("P2", "A", 4), ("P2", "B", 2), ("P3", "C", 5), ("P1", "C", 3)]
    got = pipeline.run(make_input(rows), "m2", {**NO_FILTERS, **WORKED_CONFIG}).by_project()
    assert got["P3"].score < 4.5


def test_disconnected_track_detected():
    rows = [("P1", "J1", 3), ("P2", "J1", 4), ("P1", "J2", 2), ("P3", "J3", 5), ("P4", "J3", 1), ("P4", "J4", 2)]
    inp = make_input(rows)
    groups = components(list(inp.projects), inp.reviews)
    assert [g[0] for g in groups] == [("P1", "P2"), ("P3", "P4")]


def test_cv_picks_from_the_grid():
    data = prepare(make_input(random_design(P=20, J=10, per_judge=8)))
    (lq, lb), table = m2_ridge.cross_validate(data.y, data.pi, data.ji, data.P, data.J, seed=3)
    assert len(table) == 25
    assert lq in (0.25, 0.5, 1.0, 2.0, 4.0) and lb in (0.25, 0.5, 1.0, 2.0, 4.0)


def test_fixtures_load_and_fit_quickly():
    """The lab's bound is 1 s; relaxed to 10 s because Docker Desktop on Windows is noisy."""
    started = time.perf_counter()
    inp, _ = load_organizer_file(FIXTURES)
    result = pipeline.run(inp, "m2")
    assert time.perf_counter() - started < 10.0
    assert all(np.isfinite(p.score) for p in result.projects)


# ================================================================== filters and duplicate policy

def fixture_run(**config):
    inp, _ = load_organizer_file(FIXTURES)
    return inp, pipeline.run(inp, "m2", {"cv_seed": LAB_SEED, **config})


def test_duplicate_exclude_drops_prj41_and_its_four_reviews():
    _, result = fixture_run()
    assert result.diagnostics["pipeline"]["reviews_used"] == 119
    assert "prj_41" not in result.by_project()
    dropped = [e.id for e in result.excluded if e.kind == "review" and "duplicate" in e.reason]
    assert sorted(dropped) == ["jdg_18:prj_41", "jdg_19:prj_41", "jdg_21:prj_41", "jdg_26:prj_41"]
    assert result.by_project()["prj_07"].n_reviews == 5


def test_duplicate_merge_keeps_the_kept_projects_reviews():
    inp, result = fixture_run(duplicate_policy="merge")
    assert result.diagnostics["pipeline"]["reviews_used"] == 120
    assert "prj_41" not in result.by_project()
    assert result.by_project()["prj_07"].n_reviews == 6       # 5 of its own + jdg_18's from prj_41
    dropped = sorted(e.id for e in result.excluded if e.kind == "review" and "also reviewed" in e.reason)
    assert dropped == ["jdg_19:prj_41", "jdg_21:prj_41", "jdg_26:prj_41"]

    cfg = EngineConfig(duplicate_policy="merge")
    kept = FILTERS["exclude_duplicate_submissions"](inp.reviews, dict(inp.projects), inp.duplicates, cfg)
    original = {(r.judge_id, r.project_id): dict(r.items) for r in inp.reviews}
    merged = {(r.judge_id, r.project_id): dict(r.items) for r in kept.reviews}
    for judge in ("jdg_19", "jdg_21", "jdg_26"):
        assert merged[(judge, "prj_07")] == original[(judge, "prj_07")]   # kept project's review wins
    assert merged[("jdg_18", "prj_07")] == original[("jdg_18", "prj_41")]  # moved, not averaged
    # a moved review keeps its position in the input
    order = [(r.judge_id, r.project_id) for r in kept.reviews]
    source = [(r.judge_id, r.project_id) for r in inp.reviews]
    assert order.index(("jdg_18", "prj_07")) == [p for p in source if p not in
                                                 {("jdg_19", "prj_41"), ("jdg_21", "prj_41"),
                                                  ("jdg_26", "prj_41")}].index(("jdg_18", "prj_41"))


def test_flat_judge_excluded_and_single_review_judge_kept_and_shrunk():
    inp, result = fixture_run()
    assert [(e.kind, e.id) for e in result.excluded if e.kind == "judge"] == [("judge", "jdg_07")]
    judges = {j.judge_id: j for j in result.judges}
    assert "jdg_07" not in judges
    single = judges["jdg_01"]
    assert single.n_reviews == 1
    (review,) = [r for r in inp.reviews if r.judge_id == "jdg_01"]
    y = sum(review.items.values()) / len(review.items)
    others = [sum(r.items.values()) / 3 for r in inp.reviews
              if r.project_id == review.project_id and r.judge_id != "jdg_01" and r.project_id != "prj_41"]
    assert abs(single.bias) < abs(y - np.mean(others))   # the lean is shrunk, not the full gap


def test_near_flat_judges_flagged_on_the_fixtures():
    _, result = fixture_run()
    flagged = sorted(j.judge_id for j in result.judges if "near_flat" in j.flags)
    assert flagged == ["jdg_05", "jdg_17", "jdg_18", "jdg_28"]


def test_insufficient_reviews_flag():
    _, result = fixture_run()
    thin = sorted(p.project_id for p in result.projects if "insufficient_reviews" in p.flags)
    assert thin == ["prj_19"]   # 1 review left once the flat judge is excluded


# ================================================================== components and lambdas

def two_tracks(per_side=3):
    rows = []
    for side, (projects, judges) in enumerate(((("A", "B", "C"), ("J1", "J2")), (("D", "E", "F"), ("J3", "J4")))):
        for i, p in enumerate(projects):
            for k, j in enumerate(judges):
                rows.append((p, j, 2 + (i + k + side) % 4))
    return make_input(rows)


def test_disconnected_graph_is_ranked_per_component():
    inp = two_tracks()
    result = pipeline.run(inp, "m2", {"cv_seed": 11})
    assert_contract(inp, result)
    assert len(result.components) == 2
    notes = result.diagnostics["pipeline"]
    assert notes["ranking"] == "per_component" and "no overall rank" in notes["why_per_component"]
    ranks = {c: sorted(p.rank for p in result.projects if p.component == c) for c in (0, 1)}
    assert ranks == {0: [1, 2, 3], 1: [1, 2, 3]}
    seeds = [c["cv_seed"] for c in result.params_chosen["components"]]
    assert seeds == [[11, 0], [11, 1]]


def test_single_component_uses_the_seed_itself():
    _, result = fixture_run()
    assert result.diagnostics["pipeline"]["ranking"] == "overall"
    assert result.params_chosen["components"][0]["cv_seed"] == LAB_SEED


def cv_rows(n_reviews):
    """A connected design with exactly n_reviews reviews and no flat judge."""
    rows, i = [], 0
    while len(rows) < n_reviews:
        rows.append((f"P{i % 6}", f"J{(i // 6) % 5}", 1 + (i * 7) % 5))
        i += 1
    return rows


def test_cv_min_reviews_falls_back_to_fixed_lambdas():
    small = pipeline.run(make_input(cv_rows(19)), "m2", NO_FILTERS)
    params = small.params_chosen["components"][0]
    assert (params["lam_q"], params["lam_b"], params["lambda_source"]) == (0.5, 1.0, "fallback")
    assert "below cv_min_reviews=20" in small.diagnostics["method"]["components"][0]["lambda_reason"]
    enough = pipeline.run(make_input(cv_rows(20)), "m2", NO_FILTERS)
    assert enough.params_chosen["components"][0]["lambda_source"] == "cv"


def test_lambda_at_grid_boundary():
    _, fixtures = fixture_run()
    params = fixtures.params_chosen["components"][0]
    assert (params["lam_q"], params["lam_b"], params["lambda_at_grid_boundary"]) == (4.0, 4.0, True)
    inp, _ = load_organizer_file(GOLDEN / "syn_medium.json")
    params = pipeline.run(inp, "m2", {"cv_seed": LAB_SEED}).params_chosen["components"][0]
    assert (params["lam_q"], params["lam_b"], params["lambda_at_grid_boundary"]) == (0.5, 0.5, False)


# ================================================================== SE tie chaining

def test_se_tie_groups_split_where_p_ahead_crosses_the_threshold():
    scores = np.array([3.0, 2.0, 1.9, 0.5])
    cov = np.eye(4) * 0.01
    rank = np.array([1, 2, 3, 4])
    groups, p_next = se_tie_groups(scores, cov, rank, 0.84)
    assert list(groups) == [0, 1, 1, 2]      # 2.0 vs 1.9: P = 0.76 < 0.84, tied
    assert p_next[1] == pytest.approx(0.760, abs=0.001) and p_next[3] is None


def test_se_tie_chaining_is_transitive():
    """1.0 vs 0.8 alone would be separable (P = 0.92), but each adjacent pair is not: one group."""
    scores = np.array([1.0, 0.9, 0.8])
    groups, _ = se_tie_groups(scores, np.eye(3) * 0.01, np.array([1, 2, 3]), 0.84)
    assert list(groups) == [0, 0, 0]
    assert p_ahead(np.eye(3) * 0.01, scores, 0, 2)[3] > 0.84


def test_tie_threshold_comes_from_config():
    _, default = fixture_run()
    assert {p.tie_group for p in default.projects} == {0}     # the fixtures: one tie group of 40
    _, loose = fixture_run(tie_threshold=0.55)
    assert len({p.tie_group for p in loose.projects}) > 1


# ================================================================== flaggers and explain

def test_outlier_flag_skipped_with_a_note_when_it_cannot_run():
    small = pipeline.run(make_input(cv_rows(9)), "m2", NO_FILTERS)
    assert small.flags["reviews"] == []
    assert any("below outlier_min_reviews=10" in note for note in small.flags["notes"])
    raw = pipeline.run(make_input(cv_rows(30)), "raw_mean", NO_FILTERS)
    assert any("raw_mean has no fitted values" in note for note in raw.flags["notes"])


def test_decomposition_is_exact_against_the_raw_mean():
    _, result = fixture_run()
    for p in result.projects:
        e = p.extras
        assert e["raw_mean"] - p.score == pytest.approx(e["lean_part"] + e["shrinkage_part"], abs=1e-12)
        assert e["explanation"].startswith("mainly ")


# ================================================================== CLI end to end

def cli(*args):
    out = io.StringIO()
    code = cli_main([str(FIXTURES), *args], out)
    return code, out.getvalue()


def test_cli_default_on_the_fixtures():
    code, text = cli()
    assert code == 0
    assert "method m2 v1 | baseline raw_mean" in text
    assert "# 40 projects ranked in 1 tie group(s)" in text
    assert "# reviews: 126 in the input, 119 used; ranking: overall" in text
    assert "lambda_q=4 lambda_b=4 (cv, seed" in text and "lambda_at_grid_boundary=true" in text
    assert "# excluded project prj_41" in text and "# excluded judge jdg_07" in text
    assert "# biggest moves vs raw_mean" in text


def test_cli_merge_policy_uses_120_reviews():
    code, text = cli("--config", "duplicate_policy=merge")
    assert code == 0
    assert "# reviews: 126 in the input, 120 used" in text


def test_cli_inline_config_parsing(tmp_path):
    assert read_config("duplicate_policy=merge,cv_seed=7,lambdas=[0.5,1]") == {
        "duplicate_policy": "merge", "cv_seed": 7, "lambdas": [0.5, 1]}
    with pytest.raises(EngineError):
        read_config("not-a-file-and-no-equals")
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"cv_seed": 3}), encoding="utf-8")
    assert read_config(str(path)) == {"cv_seed": 3}
    code, text = cli("--config", "tie_treshold=0.9")
    assert code == 2 and "Unknown engine config key" in text
