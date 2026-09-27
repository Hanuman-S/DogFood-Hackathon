"""How much evidence the ranking stands on. Descriptive only.

There is no assignment table yet, so the engine cannot know which reviews were *assigned* but
never written: a project with 2 reviews may have been assigned 2 or 5. The output says so
rather than letting "2 reviews" read as "complete".
"""

from __future__ import annotations

import statistics

ASSIGNMENT_NOTE = (
    "Descriptive only: there is no assignment data, so reviews that were assigned but never "
    "written cannot be counted. A low count may be an unfinished batch, not a small assignment."
)


def coverage(projects, reviews, min_reviews: int) -> dict:
    """`projects`: every project id in scope (reviewed or not), in input order.
    `reviews`: the reviews actually used."""
    per_project = {p: 0 for p in projects}
    per_judge: dict[str, int] = {}
    for review in reviews:
        per_project[review.project_id] = per_project.get(review.project_id, 0) + 1
        per_judge[review.judge_id] = per_judge.get(review.judge_id, 0) + 1
    counts = list(per_project.values())
    judge_counts = list(per_judge.values())
    return {
        "projects": len(per_project),
        "reviews": len(reviews),
        "judges": len(per_judge),
        "reviews_per_project": _summary(counts),
        "projects_with_no_reviews": [p for p, n in per_project.items() if n == 0],
        "min_reviews": min_reviews,
        "projects_below_min_reviews": [p for p, n in per_project.items() if 0 < n < min_reviews],
        "reviews_per_judge": _summary(judge_counts),
        "reviews_by_judge": dict(sorted(per_judge.items())),
        "note": ASSIGNMENT_NOTE,
    }


def _summary(values):
    if not values:
        return {"min": None, "median": None, "max": None}
    return {"min": min(values), "median": float(statistics.median(values)), "max": max(values)}
