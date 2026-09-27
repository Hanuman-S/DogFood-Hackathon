"""The outlier flag (outlier_residuals) against colluding judges.

A heuristic: the scoring study recommended a residual flag but never validated one, so these tests
assert only the one thing agreed -- an injected +2 review is flagged -- and *report* everything
else: whether the lab's own injected colluder is caught, and how many other reviews get flagged
(false positives) on each input. outlier_k stays at its default of 2; it is not tuned here.
Run with -s to see the report.
"""

import csv

import pytest
from engine_helpers import FIXTURES, GOLDEN, LAB_SEED

from scoring.engine import pipeline
from scoring.engine.io import load_organizer_file
from scoring.engine.types import EngineInput, Review

CONFIG = {"cv_seed": LAB_SEED}


def flagged(result):
    return {(f["judge"], f["project"]) for f in result.flags["reviews"]}


def lab_colluder():
    """(judge, target project) of the lab's own injected colluder in syn_small (+1.5 on target)."""
    with open(GOLDEN / "syn_small_judges_truth.csv", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["role"].startswith("colluder"):
                return row["judge"], row["role"].split(" on ")[1].rstrip(")")
    raise AssertionError("syn_small has no colluder in its truth file")


def inject(inp, result):
    """Our colluder: the first judge in file order with >= 3 reviews used, who is not flat and not
    the lab's colluder; on their first review (file order) where every criterion is <= 3, +2 on
    every criterion (so nothing is clipped at 5)."""
    used = {}
    for review in inp.reviews:
        used.setdefault(review.judge_id, []).append(review)
    excluded_reviews = {e.id for e in result.excluded if e.kind == "review"}
    lab_judge, _ = lab_colluder()
    for judge, reviews in used.items():
        kept = [r for r in reviews if f"{r.judge_id}:{r.project_id}" not in excluded_reviews]
        if judge == lab_judge or len(kept) < 3:
            continue
        for target in kept:
            if all(v <= 3 for v in target.items.values()):
                bumped = [
                    Review(r.judge_id, r.project_id, {k: v + 2 for k, v in r.items.items()}, r.track_id)
                    if r is target else r for r in inp.reviews
                ]
                injected = EngineInput(inp.event_id, tuple(bumped), inp.rubric, inp.projects, inp.duplicates)
                return injected, (target.judge_id, target.project_id)
    raise AssertionError("no review suitable for injection")


@pytest.fixture(scope="module")
def syn_small():
    inp, _ = load_organizer_file(GOLDEN / "syn_small.json")
    return inp, pipeline.run(inp, "m2", CONFIG)


def test_injected_colluder_review_is_flagged_and_other_flags_are_reported(syn_small):
    inp, clean = syn_small
    injected_inp, target = inject(inp, clean)
    injected = pipeline.run(injected_inp, "m2", CONFIG)

    lab = lab_colluder()
    fixtures_inp, _ = load_organizer_file(FIXTURES)
    fixtures = pipeline.run(fixtures_inp, "m2", CONFIG)
    used = {name: r.diagnostics["pipeline"]["reviews_used"]
            for name, r in (("clean", clean), ("injected", injected), ("fixtures", fixtures))}
    others_clean = flagged(clean) - {lab}
    others_injected = flagged(injected) - {target, lab}
    report = [
        "outlier_residuals report (outlier_k = 2, studentized residuals):",
        f"  our +2 colluder: review {target[0]}:{target[1]} -> flagged when injected: {target in flagged(injected)}; "
        f"flagged in the clean run: {target in flagged(clean)}",
        f"  lab's own colluder (+1.5): review {lab[0]}:{lab[1]} -> flagged in clean syn_small: {lab in flagged(clean)}",
        f"  false positives, clean syn_small: {len(others_clean)} of {used['clean'] - 1} other reviews "
        f"({100 * len(others_clean) / (used['clean'] - 1):.1f}%) {sorted(others_clean)}",
        f"  false positives, syn_small with our +2: {len(others_injected)} of {used['injected'] - 2} other reviews "
        f"({100 * len(others_injected) / (used['injected'] - 2):.1f}%) {sorted(others_injected)}",
        f"  flags on the fixtures (no injected colluder): {len(flagged(fixtures))} of {used['fixtures']} reviews "
        f"({100 * len(flagged(fixtures)) / used['fixtures']:.1f}%) {sorted(flagged(fixtures))}",
    ]
    print("\n" + "\n".join(report))

    assert target in flagged(injected)
    assert target not in flagged(clean)
