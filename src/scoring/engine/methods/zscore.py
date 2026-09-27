"""Per-judge z-score, floored. Ported from the lab's `methods/baselines.py::zscore`.

For each review: z = (y - judge mean) / max(judge SD, floor), then averaged per project. The SD
is the *population* SD (divide by n), so a judge with a single review, or one who gave every
project the same score, has SD 0 and falls to the floor -- their reviews all get z = 0. The
floor (config `z_floor`, default 0.5) keeps a near-flat judge's tiny differences from being
blown up into huge z values. Judges that hit the floor are listed in the diagnostics.
"""

import numpy as np

from .base import MethodOutput
from .registry import register


@register
class ZScore:
    name = "zscore"
    version = "1"
    capabilities = frozenset()

    def fit(self, data, config):
        floor = float(config.z_floor)
        y = data.y
        nj = np.bincount(data.ji, minlength=data.J)
        mean_j = np.bincount(data.ji, weights=y, minlength=data.J) / np.maximum(nj, 1)
        var_j = np.bincount(data.ji, weights=(y - mean_j[data.ji]) ** 2, minlength=data.J) / np.maximum(nj, 1)
        sd_j = np.maximum(np.sqrt(var_j), floor)
        z = (y - mean_j[data.ji]) / sd_j[data.ji]
        n = np.bincount(data.pi, minlength=data.P)
        total = np.bincount(data.pi, weights=z, minlength=data.P)
        with np.errstate(invalid="ignore", divide="ignore"):
            scores = np.where(n > 0, total / np.maximum(n, 1), np.nan)
        floored = [data.judges[j] for j in range(data.J) if nj[j] > 0 and np.sqrt(var_j[j]) < floor]
        return MethodOutput(
            scores=scores,
            params={"z_floor": floor},
            diagnostics={"sd_floored_judges": floored},
        )
