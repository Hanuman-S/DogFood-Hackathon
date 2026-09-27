"""Pre-fit filters. Each takes the reviews and projects still in play and returns what it keeps,
plus an Exclusion (with a readable reason) for everything it drops. Which filters run, and in
what order, is config (`filters`); the default order is the lab's: duplicates, then flat judges.

A filter never drops anything silently, and never changes a review's scores.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import Exclusion, Review

FILTERS = {}


@dataclass(frozen=True)
class Filtered:
    reviews: tuple[Review, ...]
    projects: dict                 # project id -> track, in input order
    excluded: tuple[Exclusion, ...]


def _filter(fn):
    FILTERS[fn.__name__] = fn
    return fn


def review_id(review) -> str:
    return f"{review.judge_id}:{review.project_id}"


@_filter
def exclude_duplicate_submissions(reviews, projects, duplicates, config) -> Filtered:
    """A duplicate submission never competes as a project of its own.

    policy "exclude" (default; the lab's rule): the duplicate and all its reviews are dropped.
    policy "merge" (our rule, not the lab's -- the lab's sensitivity run averaged instead): the
    duplicate's reviews move to the kept project, in their original position; where the same
    judge reviewed both, the kept project's review wins and the other is dropped.
    """
    duplicates = {d: k for d, k in duplicates.items() if d in projects}
    if not duplicates:
        return Filtered(tuple(reviews), dict(projects), ())
    excluded = []
    kept_pairs = {(r.judge_id, r.project_id) for r in reviews if r.project_id not in duplicates}
    out = []
    for review in reviews:
        kept = duplicates.get(review.project_id)
        if kept is None:
            out.append(review)
        elif config.duplicate_policy == "exclude":
            excluded.append(Exclusion("review", review_id(review),
                                      f"review of duplicate submission {review.project_id} (kept: {kept})"))
        elif (review.judge_id, kept) in kept_pairs:
            excluded.append(Exclusion("review", review_id(review),
                                      f"judge also reviewed {kept}, the kept submission; that review is used"))
        else:
            out.append(Review(review.judge_id, kept, review.items, projects.get(kept)))
            kept_pairs.add((review.judge_id, kept))
    for dup, kept in duplicates.items():
        what = ("excluded with its reviews" if config.duplicate_policy == "exclude"
                else f"its reviews merged into {kept}")
        excluded.append(Exclusion("project", dup, f"duplicate submission of {kept}; {what}"))
    remaining = {p: t for p, t in projects.items() if p not in duplicates}
    return Filtered(tuple(out), remaining, tuple(excluded))


@_filter
def exclude_flat_judges(reviews, projects, duplicates, config) -> Filtered:
    """The ridge PDF's section 11 rule, as in the lab: a judge with at least 2 reviews who gave one
    single value to every criterion of every review carries no ranking information, and is
    excluded from every method."""
    by_judge = {}
    for review in reviews:
        by_judge.setdefault(review.judge_id, []).append(review)
    flat = {}
    for judge, rows in by_judge.items():
        values = {v for r in rows for v in r.items.values()}
        if len(rows) >= 2 and len(values) == 1:
            flat[judge] = (len(rows), next(iter(values)))
    if not flat:
        return Filtered(tuple(reviews), dict(projects), ())
    excluded = [Exclusion("judge", j, f"flat judge: all {n} reviews give {v:g} on every criterion")
                for j, (n, v) in flat.items()]
    excluded += [Exclusion("review", review_id(r), f"by flat judge {r.judge_id}")
                 for r in reviews if r.judge_id in flat]
    return Filtered(tuple(r for r in reviews if r.judge_id not in flat), dict(projects), tuple(excluded))
