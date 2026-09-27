"""Ranks and tie groups.

`rank` is ordinal: 1 = best, computed on scores rounded to `equal_decimals` (9), so scores that are
equal on paper but differ in their last bits tie, and ties are broken by input order -- for every
method. (The lab ranked raw floats, so its order inside such ties was float noise.) On its own a
rank would overstate what a method knows, so every rank comes with a tie group:

* Methods with the "uncertainty" capability: walking down the ranking, a project joins the
  group of the one above it while P(the one above is truly ahead) < tie_threshold (0.84 by
  default, the lab's rule). P(ahead) = Phi(Delta / SD(Delta)), with the covariance of the two
  scores included.
* Methods without it: only scores that are equal (after rounding to `equal_decimals`, so two
  means that are equal on paper but differ in the last bit still tie) share a group. No
  uncertainty is invented for them.
"""

from __future__ import annotations

import math

import numpy as np


def ordinal_rank(scores: np.ndarray, decimals: int | None = None) -> np.ndarray:
    """1 = best. Scores equal after rounding to `decimals` are ordered by index (input order);
    NaN ranks last."""
    scores = np.asarray(scores, dtype=float)
    if decimals is not None:
        scores = np.round(scores, decimals)
    s = np.where(np.isnan(scores), -np.inf, scores)
    order = np.lexsort((np.arange(len(s)), -s))
    rank = np.empty(len(s), dtype=int)
    rank[order] = np.arange(1, len(s) + 1)
    return rank


def exact_tie_groups(scores: np.ndarray, rank: np.ndarray, decimals: int) -> np.ndarray:
    """Group id per project (0 = the group containing rank 1), equal rounded scores together."""
    groups = np.empty(len(scores), dtype=int)
    group, previous = -1, None
    rounded = np.round(np.asarray(scores, dtype=float), decimals)
    for index in np.argsort(rank, kind="stable"):
        key = float(rounded[index])
        if key != previous:
            group, previous = group + 1, key
        groups[index] = group
    return groups


def phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_ahead(cov: np.ndarray, scores: np.ndarray, a: int, b: int):
    """(Delta, SD, z, P) that a is truly ahead of b (the lab's `p_ahead`, PDF section 10)."""
    var = cov[a, a] + cov[b, b] - 2 * cov[a, b]
    sd = float(np.sqrt(max(var, 1e-300)))
    delta = float(scores[a] - scores[b])
    return delta, sd, delta / sd, phi(delta / sd)


def se_tie_groups(scores: np.ndarray, cov: np.ndarray, rank: np.ndarray, threshold: float):
    """(group id per project, P(ahead of the next project down) per project or None for the last)."""
    order = np.argsort(rank, kind="stable")
    groups = np.empty(len(scores), dtype=int)
    p_next: list[float | None] = [None] * len(scores)
    group = 0
    for i, index in enumerate(order):
        if i > 0:
            above = order[i - 1]
            _, _, _, p = p_ahead(cov, scores, above, index)
            p_next[above] = p
            if p >= threshold:
                group += 1
        groups[index] = group
    return groups, p_next
