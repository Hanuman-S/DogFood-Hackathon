"""The contract every scoring method meets.

A method is a class with three class attributes and one function:

    @register
    class MyMethod:
        name = "my_method"            # registry key; what --method and configs use
        version = "1"                 # bump whenever the maths changes (snapshots record it)
        capabilities = frozenset()    # see CAPABILITIES below; claim only what fit() returns

        def fit(self, data: PreparedData, config: EngineConfig) -> MethodOutput:
            ...

`fit` sees one set of reviews (already filtered) and returns a score for every project in
`data.projects`; higher is better. The pipeline does everything else -- ranks, tie groups,
exclusions, coverage, comparison -- the same way for every method, and the contract test runs
over every registered method automatically.

Rules the pipeline enforces:
* `scores` has one finite value per project in `data.projects`.
* `se` is present if and only if the method claims "uncertainty" (and then `cov_q` too, for
  P(ahead) between neighbours). A method without uncertainty never reports an SE.
* `judge_bias` only with "judge_bias"; `fitted` and `hat` only with "fitted".
* `fit` must be deterministic: no clock, and randomness only from
  `np.random.default_rng(<seed from config>)`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

import numpy as np

from ..config import EngineConfig
from ..prepare import PreparedData

CAPABILITIES = frozenset({
    "uncertainty",    # per-project SE and the covariance of the scores
    "judge_bias",     # a per-judge lean estimate
    "decomposition",  # rank moves vs the raw mean can be split into lean + shrinkage
    "fitted",         # fitted value and leverage per review (for residual-based flags)
})


@dataclass(frozen=True)
class MethodOutput:
    scores: np.ndarray                    # (P,) higher is better
    se: np.ndarray | None = None          # (P,) with "uncertainty"
    cov_q: np.ndarray | None = None       # (P, P) with "uncertainty"
    judge_bias: np.ndarray | None = None  # (J,) with "judge_bias"
    fitted: np.ndarray | None = None      # (N,) with "fitted"
    hat: np.ndarray | None = None         # (N,) leverage h_ii, with "fitted"
    params: dict[str, Any] = field(default_factory=dict)       # hyperparameters chosen
    diagnostics: dict[str, Any] = field(default_factory=dict)  # anything worth reporting


class ScoringMethod(Protocol):
    name: ClassVar[str]
    version: ClassVar[str]
    capabilities: ClassVar[frozenset[str]]

    def fit(self, data: PreparedData, config: EngineConfig) -> MethodOutput: ...
