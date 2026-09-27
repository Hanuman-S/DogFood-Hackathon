"""Post-fit flaggers. A flag marks something for a human to look at; it never changes a score, a
rank or what was excluded. Which flaggers run is config (`flaggers`). A flagger that cannot run
(the method lacks what it needs, or there is too little data) says so in a note, rather than
reporting "nothing found".

    insufficient_reviews  a ranked project with fewer than `min_reviews` reviews
    near_flat_judges      a judge (n >= 2) whose weighted scores vary less than
                          `near_flat_threshold` (the lab flags these and keeps them)
    outlier_residuals     a review whose studentized residual exceeds `outlier_k`:
                              r* = r / sqrt(sigma^2 (1 - h_ii))
                          where r is the review's residual under the method's fit and h_ii its
                          leverage. Needs the "fitted" capability. A heuristic: the scoring study
                          recommended a residual flag but did not validate one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

FLAGGERS = {}


@dataclass
class Flags:
    projects: dict = field(default_factory=dict)   # project id -> [flag]
    judges: dict = field(default_factory=dict)     # judge id -> [flag]
    reviews: list = field(default_factory=list)    # [{judge, project, flag, ...}]
    notes: list = field(default_factory=list)

    def add_project(self, project, flag):
        self.projects.setdefault(project, []).append(flag)

    def add_judge(self, judge, flag):
        if flag not in self.judges.setdefault(judge, []):
            self.judges[judge].append(flag)


def _flagger(fn):
    FLAGGERS[fn.__name__] = fn
    return fn


def run_flaggers(names, fits, config, method) -> Flags:
    flags = Flags()
    for name in names:
        FLAGGERS[name](fits, config, method, flags)
    return flags


@_flagger
def insufficient_reviews(fits, config, method, flags):
    for fit in fits:
        counts = fit.data.reviews_per_project()
        for i, project in enumerate(fit.data.projects):
            if counts[i] < config.min_reviews:
                flags.add_project(project, "insufficient_reviews")


@_flagger
def near_flat_judges(fits, config, method, flags):
    for fit in fits:
        d = fit.data
        for j, judge in enumerate(d.judges):
            rows = d.ji == j
            if rows.sum() < 2:
                continue
            values = d.S[rows]
            if len(np.unique(values[~np.isnan(values)])) == 1:
                continue  # flat, not near-flat (normally already excluded by the filter)
            if float(np.var(d.y[rows])) < config.near_flat_threshold:
                flags.add_judge(judge, "near_flat")


@_flagger
def outlier_residuals(fits, config, method, flags):
    if "fitted" not in method.capabilities:
        flags.notes.append(f"outlier_residuals skipped: {method.name} has no fitted values or leverages")
        return
    for fit in fits:
        d, out = fit.data, fit.output
        if d.N < config.outlier_min_reviews:
            flags.notes.append(
                f"outlier_residuals skipped for component {fit.index}: {d.N} reviews is below "
                f"outlier_min_reviews={config.outlier_min_reviews}"
            )
            continue
        residual = d.y - np.asarray(out.fitted)
        room = 1.0 - np.asarray(out.hat)
        for i in range(d.N):
            if room[i] <= 1e-12:
                continue  # a review that fully determines its own fit has no residual to judge
            studentized = float(residual[i] / np.sqrt(out.sigma2 * room[i]))
            if abs(studentized) > config.outlier_k:
                judge, project = d.judges[d.ji[i]], d.projects[d.pi[i]]
                flags.reviews.append({
                    "judge": judge, "project": project, "flag": "outlier_residual",
                    "y": float(d.y[i]), "fitted": float(out.fitted[i]),
                    "residual": float(residual[i]), "studentized": studentized,
                })
                flags.add_project(project, "outlier_review")
                flags.add_judge(judge, "outlier_review")
