"""Raw mean: each project's average weighted review score. Ported from the lab's
`methods/baselines.py::raw_mean` (the ridge PDF's section 14 baseline).

No correction of any kind: a harsh judge's projects score low. It is the baseline every other
method's rank changes are measured against, and it reports no uncertainty.
"""

import numpy as np

from .base import MethodOutput
from .registry import register


@register
class RawMean:
    name = "raw_mean"
    version = "1"
    capabilities = frozenset()

    def fit(self, data, config):
        n = np.bincount(data.pi, minlength=data.P)
        total = np.bincount(data.pi, weights=data.y, minlength=data.P)
        with np.errstate(invalid="ignore", divide="ignore"):
            scores = np.where(n > 0, total / np.maximum(n, 1), np.nan)
        return MethodOutput(scores=scores)
