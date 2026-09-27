"""Validate an EngineInput and turn it into the index arrays every method works on.

One row per review, in input order:
    pi[i]  project index of review i (into `projects`)
    ji[i]  judge index of review i (into `judges`, in order of first appearance)
    S[i,c] the value for criterion c, NaN where the review has none
    y[i]   the review's weighted score: sum(w_c * s_c) / sum(w_c over the criteria present)
           (the lab's rule, from the ridge PDF's section 2), with w normalised to sum to 1
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .errors import EngineInputError
from .types import EngineInput, Review


@dataclass(frozen=True)
class PreparedData:
    projects: tuple[str, ...]
    tracks: tuple[str | None, ...]
    judges: tuple[str, ...]
    criteria: tuple[str, ...]
    weights: np.ndarray
    pi: np.ndarray
    ji: np.ndarray
    S: np.ndarray
    y: np.ndarray
    # Seed for any randomness in a method (M2's CV folds): the resolved cv_seed for a
    # single-component event, (cv_seed, k) for component k of several.
    rng_seed: int | tuple = 0

    @property
    def P(self) -> int:
        return len(self.projects)

    @property
    def J(self) -> int:
        return len(self.judges)

    @property
    def N(self) -> int:
        return len(self.pi)

    def reviews_per_project(self) -> np.ndarray:
        return np.bincount(self.pi, minlength=self.P)

    def reviews_per_judge(self) -> np.ndarray:
        return np.bincount(self.ji, minlength=self.J)


def validate(inp: EngineInput) -> None:
    """Refuse input the engine cannot score honestly. Raises EngineInputError."""
    criteria = inp.rubric.criteria
    ids = [c.id for c in criteria]
    if len(set(ids)) != len(ids):
        raise EngineInputError("The rubric lists a criterion twice.")
    for c in criteria:
        if not (math.isfinite(c.weight) and c.weight >= 0):
            raise EngineInputError(f"Criterion {c.id}: weight must be a number >= 0.")
        if not c.min < c.max:
            raise EngineInputError(f"Criterion {c.id}: min must be below max.")
    for dup, kept in inp.duplicates.items():
        if dup not in inp.projects or kept not in inp.projects:
            raise EngineInputError(f"Duplicate {dup} -> {kept} names a project that is not in the input.")
    seen = set()
    for review in inp.reviews:
        _validate_review(inp, review)
        pair = (review.judge_id, review.project_id)
        if pair in seen:
            raise EngineInputError(
                f"Judge {review.judge_id} reviewed project {review.project_id} more than once. "
                "One review per judge per project; collapse repeats before scoring."
            )
        seen.add(pair)


def _validate_review(inp, review: Review):
    where = f"review by {review.judge_id} of {review.project_id}"
    if review.project_id not in inp.projects:
        raise EngineInputError(f"{where}: unknown project.")
    if not review.items:
        raise EngineInputError(f"{where}: no criterion values.")
    present_weight = 0.0
    for key, value in review.items.items():
        criterion = inp.rubric.get(key)
        if criterion is None:
            raise EngineInputError(f"{where}: criterion {key} is not in the rubric.")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise EngineInputError(f"{where}: {key} is not a number.")
        if not criterion.min <= value <= criterion.max:
            raise EngineInputError(
                f"{where}: {key}={value:g} is outside {criterion.min:g}-{criterion.max:g}."
            )
        present_weight += criterion.weight
    if present_weight <= 0:
        raise EngineInputError(f"{where}: every criterion it scores has weight 0.")


def prepare(inp: EngineInput, reviews=None, projects=None, rng_seed=0) -> PreparedData:
    """Arrays for `reviews` (default: all of them) over `projects` (default: all of them).

    Call `validate(inp)` first; this assumes the input is valid.
    """
    reviews = inp.reviews if reviews is None else tuple(reviews)
    projects = tuple(inp.projects) if projects is None else tuple(projects)
    pindex = {p: i for i, p in enumerate(projects)}
    judges = tuple(dict.fromkeys(r.judge_id for r in reviews))
    jindex = {j: i for i, j in enumerate(judges)}
    criteria = tuple(c.id for c in inp.rubric.criteria)
    weights = np.array([c.weight for c in inp.rubric.criteria], dtype=float)

    S = np.full((len(reviews), len(criteria)), np.nan)
    for i, review in enumerate(reviews):
        for c, key in enumerate(criteria):
            if key in review.items:
                S[i, c] = float(review.items[key])
    # Normalised to sum to 1 first: the same value in exact arithmetic, and the lab's float
    # arithmetic (its weights were 1/3 each), so "equal" means come out bit-identical to the lab's.
    unit = weights / weights.sum() if len(weights) and weights.sum() > 0 else weights
    present = ~np.isnan(S)
    y = np.nansum(S * unit, axis=1) / np.where(present, unit, 0.0).sum(axis=1)
    return PreparedData(
        projects=projects,
        tracks=tuple(inp.projects.get(p) for p in projects),
        judges=judges,
        criteria=criteria,
        weights=weights,
        pi=np.array([pindex[r.project_id] for r in reviews], dtype=int),
        ji=np.array([jindex[r.judge_id] for r in reviews], dtype=int),
        S=S,
        y=y if len(reviews) else np.zeros(0),
        rng_seed=rng_seed,
    )
