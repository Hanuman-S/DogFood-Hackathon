"""Goldens: the portal's engine must reproduce the scoring lab. The lab is the referee.

Expected values come only from the lab's own result files (copied into tests/golden/) or from
the lab's own code run in its own environment (tests/golden/expected/*.lab.json, made by
tests/golden/generate_expected.py). The comparison rule is written out, with its reasons, in
tests/golden/README.md. In short:

* Numbers are compared to fixed tolerances, set before the first run and never loosened:
      values the lab printed at 6 dp          |ours - lab| <= 5.01e-7
      values the lab printed at 4 dp          |ours - lab| <= 5.01e-5
      full-precision lab JSON                 |ours - lab| <= 1e-8
      lambdas, tie groups, counts, ids        exactly equal
* Ranks: every pair of projects whose lab scores differ by more than 1e-9 must be in the lab's
  order; a set of lab scores equal to within 1e-9 must occupy the same set of ranks. The order
  inside such a set is ours by rule (R10: input order) and is asserted by the contract test --
  the lab's order there was float noise.
* P(ahead) is compared per unordered pair: P(a over b) = 1 - P(b over a).

Every comparison collects all mismatches and fails once, with a readable diff.
"""

import csv
import itertools
import json

import numpy as np
import pytest
from engine_helpers import FIXTURES, GOLDEN, LAB_SEED, fit_m2_direct

from scoring.engine import pipeline
from scoring.engine.io import load_organizer_file
from scoring.engine.ties import p_ahead

TOL_6DP = 5.01e-7
TOL_4DP = 5.01e-5
TOL_FULL = 1e-8
EQUAL = 1e-9
LAB_METHOD = {"raw": "raw_mean", "z": "zscore", "M2": "m2"}
INPUTS = {"fixtures": FIXTURES, "syn_small": GOLDEN / "syn_small.json",
          "syn_medium": GOLDEN / "syn_medium.json", "syn_large": GOLDEN / "syn_large.json"}
SYNTHETIC = ("syn_small", "syn_medium", "syn_large")
CONFIG = {"cv_seed": LAB_SEED}


def read_csv(name):
    with open(GOLDEN / name, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def lab_json(name):
    return json.loads((GOLDEN / "expected" / f"{name}.lab.json").read_text(encoding="utf-8"))


def num(text):
    return float("nan") if text in ("", None) else float(text)


def is_missing(value):
    return value is None or (isinstance(value, float) and np.isnan(value))


class Diff:
    """Collects mismatches, then fails once with all of them."""

    def __init__(self, title):
        self.title, self.lines = title, []

    def close(self, what, ours, lab, tol):
        if is_missing(ours) or is_missing(lab):
            if not (is_missing(ours) and is_missing(lab)):
                self.lines.append(f"{what}: ours {ours!r}, lab {lab!r}")
            return
        if abs(ours - lab) > tol:
            self.lines.append(f"{what}: ours {ours:.12g}, lab {lab:.12g}, diff {ours - lab:+.3g} (tol {tol:g})")

    def equal(self, what, ours, lab):
        if ours != lab:
            self.lines.append(f"{what}: ours {ours!r}, lab {lab!r}")

    def check(self):
        assert not self.lines, f"{self.title}: {len(self.lines)} mismatch(es)\n  " + "\n  ".join(self.lines[:60])


def lab_equal_sets(projects, lab_scores):
    """Projects whose lab scores are equal to within 1e-9 (chained), as lists."""
    order = sorted(range(len(projects)), key=lambda i: lab_scores[i])
    sets, current = [], [order[0]] if order else []
    for a, b in zip(order, order[1:]):
        if lab_scores[b] - lab_scores[a] <= EQUAL:
            current.append(b)
        else:
            sets.append(current)
            current = [b]
    if current:
        sets.append(current)
    return [[projects[i] for i in s] for s in sets]


def compare_ranks(diff, label, projects, lab_scores, lab_ranks, ours):
    """The rank rule. `ours`: project -> our rank."""
    n = len(projects)
    for i in range(n):
        for j in range(i + 1, n):
            if abs(lab_scores[i] - lab_scores[j]) > EQUAL:
                lab_first = lab_scores[i] > lab_scores[j]
                ours_first = ours[projects[i]] < ours[projects[j]]
                if lab_first != ours_first:
                    a, b = (projects[i], projects[j]) if lab_first else (projects[j], projects[i])
                    diff.lines.append(f"{label}: lab ranks {a} above {b} (scores differ by "
                                      f"{abs(lab_scores[i] - lab_scores[j]):.3g}), ours does not")
    lab_rank = dict(zip(projects, lab_ranks))
    for members in lab_equal_sets(projects, lab_scores):
        if len(members) > 1:
            diff.equal(f"{label}: ranks held by the lab-equal set {sorted(members)}",
                       sorted(ours[p] for p in members), sorted(lab_rank[p] for p in members))


def compare_p_ahead(diff, label, lab_order, lab_p_next, inp, tol):
    """Per unordered pair: for each pair the lab reports (a over its next b), our P(a over b)."""
    data, out = fit_m2_direct(inp, CONFIG)
    index = {p: i for i, p in enumerate(data.projects)}
    for a, b in zip(lab_order, lab_order[1:]):
        ours = p_ahead(out.cov_q, out.scores, index[a], index[b])[3]
        diff.close(f"{label} P({a} over {b})", ours, lab_p_next.get(a), tol)


def our_p_next_is_consistent(diff, label, result, inp):
    """Our reported p_ahead_of_next equals P(a over b) for our own neighbours."""
    data, out = fit_m2_direct(inp, CONFIG)
    index = {p: i for i, p in enumerate(data.projects)}
    rows = sorted(result.projects, key=lambda p: p.rank)
    for a, b in zip(rows, rows[1:]):
        want = p_ahead(out.cov_q, out.scores, index[a.project_id], index[b.project_id])[3]
        diff.close(f"{label} our p_ahead_of_next {a.project_id}", a.extras["p_ahead_of_next"], want, 1e-12)


@pytest.fixture(scope="module")
def fixtures():
    inp, _ = load_organizer_file(FIXTURES)
    return inp, pipeline.compare(inp, ["m2", "raw_mean", "zscore"], config=CONFIG)


# ================================================================== fixtures vs the lab's CSVs

def test_fixtures_counts_and_lambdas(fixtures):
    _, comparison = fixtures
    m2 = comparison.results["m2"]
    meta = json.loads((GOLDEN / "run_meta.json").read_text(encoding="utf-8"))
    params = m2.params_chosen["components"][0]
    diff = Diff("fixtures: counts and lambdas")
    diff.equal("projects ranked", len(m2.projects), 40)
    diff.equal("reviews used", m2.diagnostics["pipeline"]["reviews_used"], 119)
    diff.equal("lambda", [params["lam_q"], params["lam_b"]], meta["lam"]["fixtures"])
    diff.equal("lambda_at_grid_boundary", params["lambda_at_grid_boundary"], True)
    diff.equal("tie groups", sorted({p.tie_group for p in m2.projects}), [0])
    diff.check()


def test_fixtures_rankings_match_rankings_fixtures_csv(fixtures):
    inp, comparison = fixtures
    results = {m: r.by_project() for m, r in comparison.results.items()}
    rows = read_csv("rankings_fixtures.csv")
    projects = [r["project"] for r in rows]
    diff = Diff("rankings_fixtures.csv")
    diff.equal("projects", sorted(results["m2"]), sorted(projects))
    for row in rows:
        p = row["project"]
        diff.equal(f"{p} n_reviews", results["m2"][p].n_reviews, int(row["n_reviews"]))
        for lab, ours in LAB_METHOD.items():
            diff.close(f"{p} {lab}_score", results[ours][p].score, num(row[f"{lab}_score"]), TOL_6DP)
        diff.close(f"{p} M2_se", results["m2"][p].se, num(row["M2_se"]), TOL_6DP)
        diff.equal(f"{p} M2_tie_group", results["m2"][p].tie_group, int(row["M2_tie_group"]))
    for lab, ours in LAB_METHOD.items():
        compare_ranks(diff, f"{lab} rank", projects, [num(r[f"{lab}_score"]) for r in rows],
                      [int(r[f"{lab}_rank"]) for r in rows], {p: results[ours][p].rank for p in projects})
    lab_order = [r["project"] for r in sorted(rows, key=lambda r: int(r["M2_rank"]))]
    compare_p_ahead(diff, "M2", lab_order, {r["project"]: num(r["M2_p_ahead_of_next"]) for r in rows}, inp, TOL_6DP)
    our_p_next_is_consistent(diff, "M2", comparison.results["m2"], inp)
    diff.check()


def test_fixtures_cv_table_matches_m2_cv_fixtures_csv(fixtures):
    _, comparison = fixtures
    ours = {(c["lam_q"], c["lam_b"]): c["mse"]
            for c in comparison.results["m2"].diagnostics["method"]["components"][0]["cv"]}
    diff = Diff("m2_cv_fixtures.csv")
    rows = read_csv("m2_cv_fixtures.csv")
    diff.equal("grid", sorted(ours), sorted((float(r["lam_q"]), float(r["lam_b"])) for r in rows))
    for row in rows:
        key = (float(row["lam_q"]), float(row["lam_b"]))
        diff.close(f"cv_mse {key}", ours.get(key), num(row["cv_mse"]), TOL_6DP)
    diff.check()


def test_fixtures_leans_match_m2_judges_fixtures_csv(fixtures):
    _, comparison = fixtures
    judges = {j.judge_id: j for j in comparison.results["m2"].judges}
    diff = Diff("m2_judges_fixtures.csv")
    rows = [r for r in read_csv("m2_judges_fixtures.csv") if int(r["n_reviews"]) > 0]
    diff.equal("judges with reviews", sorted(judges), sorted(r["judge"] for r in rows))
    for row in rows:
        j = judges.get(row["judge"])
        if j is None:
            continue
        diff.equal(f"{row['judge']} n_reviews", j.n_reviews, int(row["n_reviews"]))
        diff.close(f"{row['judge']} lean", j.bias, num(row["lean"]), TOL_6DP)
        diff.close(f"{row['judge']} lean_centred", j.bias_centred, num(row["lean_centred"]), TOL_6DP)
    diff.check()


def test_fixtures_movers_match_rank_change_fixtures_csv(fixtures):
    """The set of movers and each one's split must match; equal move sizes are in input order (R10,
    which is also what the lab's table order amounted to)."""
    inp, comparison = fixtures
    ours = list(comparison.movers)
    rows = read_csv("rank_change_fixtures.csv")
    diff = Diff("rank_change_fixtures.csv")
    diff.equal("set of movers", sorted(m["project_id"] for m in ours), sorted(r["project"] for r in rows))
    position = {p: i for i, p in enumerate(inp.projects)}
    sizes = [(abs(m["change"]), position[m["project_id"]]) for m in ours]
    diff.equal("our order: move size descending, then input order", sizes, sorted(sizes, key=lambda s: (-s[0], s[1])))
    by_id = {m["project_id"]: m for m in ours}
    for row in rows:
        p = row["project"]
        m = by_id.get(p)
        if m is None:
            continue
        diff.equal(f"{p} n_reviews", m["n_reviews"], int(row["n_reviews"]))
        for key, col in (("raw_mean", "raw_mean"), ("score", "M2_score"), ("se", "M2_se"),
                         ("lean_part", "lean_part"), ("shrinkage_part", "shrinkage_part")):
            diff.close(f"{p} {col}", m[key], num(row[col]), TOL_4DP)
        diff.equal(f"{p} explanation", m["explanation"].split(":")[0], row["why"].split(":")[0])
    diff.check()


# ================================================================== golden (b): the lab's code, full precision

@pytest.mark.parametrize("name", sorted(INPUTS))
def test_matches_the_labs_own_output(name):
    lab = lab_json(name)
    assert lab["seed"] == LAB_SEED
    inp, _ = load_organizer_file(INPUTS[name])
    comparison = pipeline.compare(inp, ["m2", "raw_mean", "zscore"], config=CONFIG)
    results = {m: r.by_project() for m, r in comparison.results.items()}
    m2 = comparison.results["m2"]
    params = m2.params_chosen["components"][0]
    projects = lab["projects"]
    diff = Diff(f"{name} vs expected/{name}.lab.json")

    diff.equal("components", len(m2.components), lab["n_components"])
    diff.equal("reviews used", m2.diagnostics["pipeline"]["reviews_used"], lab["n_reviews_used"])
    diff.equal("projects ranked", sorted(results["m2"]), sorted(projects))
    lab_dups = sorted(line.split()[1] for line in lab["lab_log"] if line.startswith("duplicate:"))
    diff.equal("duplicates excluded", sorted(e.id for e in m2.excluded if e.kind == "project"), lab_dups)
    lab_flat = sorted(line.split()[2] for line in lab["lab_log"] if line.startswith("flat judge:"))
    diff.equal("flat judges excluded", sorted(e.id for e in m2.excluded if e.kind == "judge"), lab_flat)

    diff.equal("lambda", (params["lam_q"], params["lam_b"]), (lab["m2"]["lam_q"], lab["m2"]["lam_b"]))
    for key in ("sigma2", "trH", "rss", "mu"):
        diff.close(key, params[key], lab["m2"][key], TOL_FULL)
    ours_cv = {(c["lam_q"], c["lam_b"]): c["mse"] for c in m2.diagnostics["method"]["components"][0]["cv"]}
    for row in lab["m2"]["cv"]:
        diff.close(f"cv {row['lam_q']},{row['lam_b']}", ours_cv.get((row["lam_q"], row["lam_b"])), row["mse"], TOL_FULL)

    for i, p in enumerate(projects):
        diff.equal(f"{p} n_reviews", results["m2"][p].n_reviews, lab["n_reviews"][i])
        for lab_name, ours_name in LAB_METHOD.items():
            diff.close(f"{p} {lab_name} score", results[ours_name][p].score, lab["methods"][lab_name]["score"][i], TOL_FULL)
        diff.close(f"{p} M2 se", results["m2"][p].se, lab["methods"]["M2"]["se"][i], TOL_FULL)
        diff.equal(f"{p} M2 tie group", results["m2"][p].tie_group, lab["m2"]["tie_group"][i])
    for lab_name, ours_name in LAB_METHOD.items():
        compare_ranks(diff, f"{lab_name} rank", projects, lab["methods"][lab_name]["score"],
                      lab["methods"][lab_name]["rank"], {p: results[ours_name][p].rank for p in projects})
    lab_order = [p for _, p in sorted(zip(lab["methods"]["M2"]["rank"], projects))]
    compare_p_ahead(diff, "M2", lab_order, lab["m2"]["p_ahead_of_next"], inp, TOL_FULL)
    our_p_next_is_consistent(diff, "M2", m2, inp)

    judges = {j.judge_id: j for j in m2.judges}
    diff.equal("judges with reviews", sorted(judges), sorted(lab["m2"]["lean"]))
    for judge, lean in lab["m2"]["lean"].items():
        if judge in judges:
            diff.close(f"{judge} lean", judges[judge].bias, lean, TOL_FULL)
            diff.close(f"{judge} lean_centred", judges[judge].bias_centred, lab["m2"]["lean_centred"][judge], TOL_FULL)
    floored = comparison.results["zscore"].diagnostics["method"]["components"][0]["sd_floored_judges"]
    diff.equal("z floored judges", sorted(floored), sorted(lab["z_floored_judges"]))
    diff.check()


# ================================================================== golden (a): accuracy against truth

def average_ranks(values):
    """scipy.stats.rankdata(method="average"): 1 = smallest."""
    values = np.asarray(values, dtype=float)
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values))
    sorted_values = values[order]
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and sorted_values[j + 1] == sorted_values[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(a, b):
    """scipy.stats.spearmanr: Pearson correlation of average ranks."""
    return float(np.corrcoef(average_ranks(a), average_ranks(b))[0, 1])


def kendall_tau_b(a, b):
    """scipy.stats.kendalltau (variant b): (P - Q) / sqrt((pairs untied in a)(pairs untied in b))."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    iu = np.triu_indices(len(a), 1)
    da, db = np.sign(a[:, None] - a[None, :])[iu], np.sign(b[:, None] - b[None, :])[iu]
    return float((da * db).sum()) / np.sqrt(float((da != 0).sum()) * float((db != 0).sum()))


def lab_rank(score):
    """The lab's methods/common.py rank(): ordinal, 1 = best, ties broken by index."""
    s = np.where(np.isnan(score), -np.inf, score)
    order = np.lexsort((np.arange(len(s)), -s))
    r = np.empty(len(s), dtype=int)
    r[order] = np.arange(1, len(s) + 1)
    return r


def _winners(score, truth, track):
    track = np.asarray(track)
    hits = []
    for t in np.unique(track):
        idx = np.flatnonzero(track == t)
        if len(idx) >= 2:
            hits.append(float(np.argmax(score[idx]) == np.argmax(truth[idx])))
    return float(np.mean(hits)) if hits else float("nan")


def truth_metrics(score, truth, track, k=5):
    """The lab's lab/metrics.py truth_metrics, as the lab computed it (single-component events:
    every golden event is one component, asserted in test_matches_the_labs_own_output)."""
    score, truth = np.asarray(score, float), np.asarray(truth, float)
    rs, rt = lab_rank(score), lab_rank(truth)
    kk = min(k, len(score))
    return {
        "spearman": spearman(score, truth),
        "kendall": kendall_tau_b(score, truth),
        "top1": float(np.argmin(rs) == np.argmin(rt)),
        "top5": len(set(np.flatnonzero(rs <= kk)) & set(np.flatnonzero(rt <= kk))) / kk,
        "mare": float(np.abs(rs - rt).mean()),
        "track_winner": _winners(score, truth, track),
    }


def truth_metrics_tie_aware(score, truth, track, k=5):
    """The same metrics on scores rounded to 9 dp, with average ranks for ties (so float noise in
    "equal" scores cannot move them). Used for raw and z, whose lab values depended on that noise."""
    score = np.round(np.asarray(score, float), 9)
    truth = np.asarray(truth, float)
    out = truth_metrics(score, truth, track, k)
    out["mare"] = float(np.abs(average_ranks(-score) - lab_rank(truth)).mean())
    return out


def lab_eval_rows(name):
    return {r["method"]: r for r in read_csv("synthetic_events_eval.csv") if r["event"] == name}


def truth_for(name, projects):
    truth = {r["project"]: float(r["true_quality"]) for r in read_csv(f"{name}_truth.csv")}
    return [truth[p] for p in projects]


METRICS = ("spearman", "kendall", "top1", "top5", "mare", "track_winner")


@pytest.mark.parametrize("name", SYNTHETIC)
def test_metric_port_reproduces_the_lab_from_the_labs_own_scores(name):
    """The referee's referee: our port of truth_metrics, fed the lab's own scores, gives the lab's
    synthetic_events_eval.csv. So a mismatch below cannot be the port's fault."""
    lab = lab_json(name)
    rows = lab_eval_rows(name)
    truth = truth_for(name, lab["projects"])
    diff = Diff(f"{name}: metric port on the lab's scores")
    for lab_name in LAB_METHOD:
        got = truth_metrics(lab["methods"][lab_name]["score"], truth, lab["tracks"])
        for key in METRICS:
            diff.close(f"{lab_name} {key}", got[key], num(rows[lab_name][key]), TOL_6DP)
    diff.check()


@pytest.mark.parametrize("name", SYNTHETIC)
def test_raw_and_z_accuracy_against_truth(name):
    """raw and z, tie-aware: against the same function applied to the lab's own scores."""
    lab = lab_json(name)
    inp, _ = load_organizer_file(GOLDEN / f"{name}.json")
    comparison = pipeline.compare(inp, ["raw_mean", "zscore"], config=CONFIG)
    rows = lab_eval_rows(name)
    truth = truth_for(name, lab["projects"])
    diff = Diff(f"{name}: raw and z accuracy against truth")
    for lab_name in ("raw", "z"):
        result = comparison.results[LAB_METHOD[lab_name]]
        by_project = result.by_project()
        diff.equal(f"{lab_name} projects", len(by_project), int(rows[lab_name]["projects"]))
        diff.equal(f"{lab_name} reviews", result.diagnostics["pipeline"]["reviews_used"], int(rows[lab_name]["reviews"]))
        got = truth_metrics_tie_aware([by_project[p].score for p in lab["projects"]], truth, lab["tracks"])
        want = truth_metrics_tie_aware(lab["methods"][lab_name]["score"], truth, lab["tracks"])
        for key in METRICS:
            diff.close(f"{lab_name} {key}", got[key], want[key], TOL_6DP)
    diff.check()


def m2_accuracy(name, lab_bits_for_equal_pairs=False):
    lab = lab_json(name)
    inp, _ = load_organizer_file(GOLDEN / f"{name}.json")
    by_project = pipeline.run(inp, "m2", CONFIG).by_project()
    scores = [by_project[p].score for p in lab["projects"]]
    if lab_bits_for_equal_pairs:
        lab_scores = lab["methods"]["M2"]["score"]
        for members in lab_equal_sets(lab["projects"], lab_scores):
            if len(members) > 1:
                for p in members:
                    i = lab["projects"].index(p)
                    scores[i] = lab_scores[i]
    return truth_metrics(scores, truth_for(name, lab["projects"]), lab["tracks"]), lab_eval_rows(name)["M2"]


# syn_medium: M2 scores equal to within 1e-9 in the lab's own output. Their order is last-bit
# float noise and differs by platform; the lab's M2 accuracy metrics (no tie handling) move with it.
SYN_MEDIUM_NEAR_TIE_PAIRS = ({"prj_001", "prj_008"}, {"prj_026", "prj_052"})
# The most those two pairs can move each metric on 100 projects (MARE: 2 pairs x 2 projects x 1
# rank / 100 = 0.04; Spearman and Kendall well under 1e-3 for two adjacent swaps). Any other
# metric must match exactly.
SYN_MEDIUM_BOUND = {"spearman": 1e-3, "kendall": 1e-3, "mare": 0.04, "top1": 0.0, "top5": 0.0, "track_winner": 0.0}


@pytest.mark.parametrize("name", ["syn_small", "syn_large"])
def test_m2_accuracy_matches_synthetic_events_eval_csv(name):
    """M2 against the lab's printed synthetic_events_eval.csv, with the lab's own metric function."""
    got, row = m2_accuracy(name)
    diff = Diff(f"{name}: M2 accuracy vs synthetic_events_eval.csv")
    for key in METRICS:
        diff.close(f"M2 {key}", got[key], num(row[key]), TOL_6DP)
    diff.check()


def test_syn_medium_m2_accuracy_matches_or_deviates_only_through_the_near_tie_pairs():
    """Platform-independent. Either every metric matches the CSV, or: the lab's near-tie sets are
    exactly the two listed pairs; each metric is within its stated bound; and the CSV value is one
    this platform's scores produce when only the listed pairs are reordered (so nothing else can
    be the cause)."""
    got, row = m2_accuracy("syn_medium")
    want = {key: num(row[key]) for key in METRICS}
    if all(abs(got[key] - want[key]) <= TOL_6DP for key in METRICS):
        return
    lab = lab_json("syn_medium")
    lab_scores = lab["methods"]["M2"]["score"]
    near_ties = [set(m) for m in lab_equal_sets(lab["projects"], lab_scores) if len(m) > 1]
    assert sorted(map(sorted, near_ties)) == sorted(map(sorted, SYN_MEDIUM_NEAR_TIE_PAIRS))
    diff = Diff("syn_medium: M2 deviation bound")
    for key in METRICS:
        if abs(got[key] - want[key]) > SYN_MEDIUM_BOUND[key] + TOL_6DP:
            diff.lines.append(f"M2 {key}: ours {got[key]:.9g}, lab {want[key]:.9g}, beyond the bound {SYN_MEDIUM_BOUND[key]}")
    diff.check()

    inp, _ = load_organizer_file(GOLDEN / "syn_medium.json")
    ours = pipeline.run(inp, "m2", CONFIG).by_project()
    truth = truth_for("syn_medium", lab["projects"])
    reachable = []
    for flips in itertools.product((False, True), repeat=len(SYN_MEDIUM_NEAR_TIE_PAIRS)):
        scores = {p: ours[p].score for p in lab["projects"]}
        for flip, pair in zip(flips, SYN_MEDIUM_NEAR_TIE_PAIRS):
            first, second = sorted(pair)
            mid = (scores[first] + scores[second]) / 2
            high, low = (second, first) if flip else (first, second)
            scores[high], scores[low] = mid + 1e-12, mid
        reachable.append(truth_metrics([scores[p] for p in lab["projects"]], truth, lab["tracks"]))
    assert any(all(abs(r[key] - want[key]) <= TOL_6DP for key in METRICS) for r in reachable), (
        "the CSV's M2 metrics are not reachable by reordering only the listed near-tie pairs")


def test_syn_medium_m2_deviation_is_only_the_near_tie_pairs():
    """If only the projects whose lab M2 scores are equal to within 1e-9 take the lab's last bits,
    every metric matches the CSV: nothing else differs."""
    got, row = m2_accuracy("syn_medium", lab_bits_for_equal_pairs=True)
    diff = Diff("syn_medium: M2 accuracy with the lab's bits on its near-tie pairs")
    for key in METRICS:
        diff.close(f"M2 {key}", got[key], num(row[key]), TOL_6DP)
    diff.check()
