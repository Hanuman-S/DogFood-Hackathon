"""Why a project moved between the raw mean and a bias-correcting method. Ported from the lab's
`run_all.py::movers`.

For a method with the "decomposition" capability (additive judge lean, fitted on the same
reviews as the raw mean), each project's change splits exactly:

    raw mean - corrected score = mean lean of its reviewers + mean residual of its reviews
                                  (judge-lean correction)     (shrinkage towards the mean)

The split is exact only against the raw mean of the same filtered reviews, so it is offered only
against the raw_mean baseline; other baselines get rank changes alone.
"""

from __future__ import annotations

import numpy as np


def decompose(data, output) -> list[dict]:
    """Per project (in data order): raw_mean, lean_part, shrinkage_part, explanation."""
    lean = np.asarray(output.judge_bias)
    residual = data.y - np.asarray(output.fitted)
    out = []
    for p in range(data.P):
        rows = np.flatnonzero(data.pi == p)
        raw = float(data.y[rows].mean())
        lean_part = float(lean[data.ji[rows]].mean())
        shrinkage_part = float(residual[rows].mean())
        main = "judge-lean correction" if abs(lean_part) > abs(shrinkage_part) else "shrinkage towards the mean"
        out.append({
            "raw_mean": raw, "lean_part": lean_part, "shrinkage_part": shrinkage_part,
            "explanation": f"mainly {main}: raw - corrected = lean {lean_part:+.2f} + shrinkage {shrinkage_part:+.2f}",
        })
    return out


def movers(results, primary_name, baseline, input_order, k: int = 8) -> list[dict]:
    """The primary method's k biggest rank moves against the raw_mean baseline, as the lab
    selects them: largest |raw rank - rank| first, equal move sizes in input order. (The lab
    stable-sorted a table kept in file order; its CSV was sorted by rank, the table it sorted was
    not.) Input order is also the portal's rule for every tie (R10)."""
    primary = results[primary_name]
    if baseline != "raw_mean" or "decomposition" not in primary.capabilities:
        return []
    base = results[baseline].by_project()
    position = {p: i for i, p in enumerate(input_order)}
    rows = [p for p in primary.projects if p.project_id in base]
    rows.sort(key=lambda p: (-abs(base[p.project_id].rank - p.rank), position[p.project_id]))
    out = []
    for p in rows[:k]:
        raw_rank = base[p.project_id].rank
        out.append({
            "project_id": p.project_id, "component": p.component, "n_reviews": p.n_reviews,
            "raw_rank": raw_rank, "rank": p.rank, "change": raw_rank - p.rank,
            "raw_mean": p.extras.get("raw_mean"), "score": p.score, "se": p.se,
            "lean_part": p.extras.get("lean_part"), "shrinkage_part": p.extras.get("shrinkage_part"),
            "explanation": p.extras.get("explanation"),
        })
    return out
