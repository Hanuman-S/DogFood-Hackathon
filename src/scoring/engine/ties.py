"""Ranks and tie groups.

`rank` is the lab's ordinal rank: 1 = best, exact ties broken by input order. On its own it would
overstate what a method knows, so every rank comes with a tie group:

* Methods without the "uncertainty" capability: only scores that are equal (after rounding to
  `equal_decimals`, so two means that are equal on paper but differ in the last bit still tie)
  share a group. No uncertainty is invented for them.
* Methods with "uncertainty": adjacent projects are chained into one group while
  P(ahead) < tie_threshold. That path arrives with M2 in S2.
"""

from __future__ import annotations

import numpy as np


def ordinal_rank(scores: np.ndarray) -> np.ndarray:
    """1 = best. Exact ties are broken by index (input order); NaN ranks last."""
    s = np.where(np.isnan(scores), -np.inf, scores)
    order = np.lexsort((np.arange(len(s)), -s))
    rank = np.empty(len(s), dtype=int)
    rank[order] = np.arange(1, len(s) + 1)
    return rank


def exact_tie_groups(scores: np.ndarray, rank: np.ndarray, decimals: int) -> np.ndarray:
    """Group id per project (0 = the group containing rank 1), equal rounded scores together."""
    groups = np.empty(len(scores), dtype=int)
    group, previous = -1, None
    for index in np.argsort(rank, kind="stable"):
        key = round(float(scores[index]), decimals)
        if key != previous:
            group, previous = group + 1, key
        groups[index] = group
    return groups
