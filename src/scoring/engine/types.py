"""The engine's inputs and outputs: frozen dataclasses, JSON-serialisable with a stable order.

Ids are strings everywhere (criterion keys, project and judge ids), so the same result shape
comes out whether the input was built from the database or read from an organizer file.

Order is meaningful and preserved:
* `EngineInput.projects` is in input order, and ranks break exact ties by that order (the lab's
  `rank()`).
* `EngineInput.reviews` is in input order, and M2's cross-validation folds depend on it.

`to_json()` keeps dataclass field order and mapping insertion order, writes floats at full
precision, and turns NaN into null, so the same input and config give byte-identical JSON.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, fields, is_dataclass
from types import MappingProxyType
from typing import Any, Mapping


def _frozen_map(value) -> Mapping:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True)
class Criterion:
    """One rubric line. `weight` is relative: the engine never rescales it to sum to 1."""

    id: str
    weight: float = 1.0
    min: float = 1.0
    max: float = 5.0


@dataclass(frozen=True)
class Rubric:
    criteria: tuple[Criterion, ...]

    def __post_init__(self):
        object.__setattr__(self, "criteria", tuple(self.criteria))

    def get(self, criterion_id):
        for criterion in self.criteria:
            if criterion.id == criterion_id:
                return criterion
        return None


@dataclass(frozen=True)
class Review:
    """One judge's review of one project: a value per criterion (a criterion may be missing)."""

    judge_id: str
    project_id: str
    items: Mapping[str, float]
    track_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "items", _frozen_map(self.items))


@dataclass(frozen=True)
class Exclusion:
    """Something left out of the ranking, and why. `kind` is "project", "review" or "judge".
    A review's id is "<judge>:<project>"."""

    kind: str
    id: str
    reason: str


@dataclass(frozen=True)
class EngineInput:
    """Everything a method may look at.

    `projects` maps project id -> track id (or None), in input order. A project listed here with
    no reviews is reported as excluded ("no reviews"), never silently dropped.
    `duplicates` maps a duplicate submission's id -> the id of the submission that is kept.
    `excluded` is what the input builder already left out (a review of a project that is not
    submitted, say); the pipeline reports it alongside its own exclusions.
    `config` is optional: `pipeline.run(..., config=...)` takes precedence over it.
    """

    event_id: str
    reviews: tuple[Review, ...]
    rubric: Rubric
    projects: Mapping[str, str | None]
    duplicates: Mapping[str, str] = field(default_factory=dict)
    excluded: tuple[Exclusion, ...] = ()
    config: Any = None

    def __post_init__(self):
        object.__setattr__(self, "reviews", tuple(self.reviews))
        object.__setattr__(self, "excluded", tuple(self.excluded))
        object.__setattr__(self, "projects", _frozen_map(self.projects))
        object.__setattr__(self, "duplicates", _frozen_map(self.duplicates))


@dataclass(frozen=True)
class ProjectResult:
    """One ranked project.

    `rank` is ordinal (1 = best; exact ties broken by input order), so it must always be read
    together with `tie_group`: projects in one tie group are not separated by the method.
    `rank` and `tie_group` count within `component`; with one component that is the whole event.
    `se` is None for methods without the "uncertainty" capability.
    """

    project_id: str
    track_id: str | None
    component: int
    score: float
    se: float | None
    rank: int
    tie_group: int
    n_reviews: int
    flags: tuple[str, ...] = ()
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "flags", tuple(self.flags))
        object.__setattr__(self, "extras", _frozen_map(self.extras))


@dataclass(frozen=True)
class JudgeResult:
    """One judge whose reviews were used. `bias` is the method's lean estimate (None without the
    "judge_bias" capability); `bias_centred` subtracts the mean lean of the judge's component."""

    judge_id: str
    component: int
    n_reviews: int
    bias: float | None = None
    bias_centred: float | None = None
    flags: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "flags", tuple(self.flags))


@dataclass(frozen=True)
class EngineResult:
    method: str
    method_version: str
    capabilities: tuple[str, ...]
    config: Mapping[str, Any]
    params_chosen: Mapping[str, Any]
    components: tuple[Mapping[str, Any], ...]
    projects: tuple[ProjectResult, ...]
    judges: tuple[JudgeResult, ...]
    excluded: tuple[Exclusion, ...]
    flags: Mapping[str, Any]
    coverage: Mapping[str, Any]
    diagnostics: Mapping[str, Any]

    def __post_init__(self):
        for name in ("config", "params_chosen", "flags", "coverage", "diagnostics"):
            object.__setattr__(self, name, _frozen_map(getattr(self, name)))
        for name in ("components", "projects", "judges", "excluded"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    def by_project(self) -> dict[str, ProjectResult]:
        return {p.project_id: p for p in self.projects}

    def to_json(self) -> str:
        return dumps(self)


@dataclass(frozen=True)
class ComparisonRow:
    """One project across every method. A method that excluded the project has None."""

    project_id: str
    track_id: str | None
    ranks: Mapping[str, int | None]
    tie_groups: Mapping[str, int | None]
    scores: Mapping[str, float | None]
    change_vs_baseline: Mapping[str, int | None]

    def __post_init__(self):
        for name in ("ranks", "tie_groups", "scores", "change_vs_baseline"):
            object.__setattr__(self, name, _frozen_map(getattr(self, name)))


@dataclass(frozen=True)
class ComparisonResult:
    """Several methods side by side. `change_vs_baseline` is baseline rank - method rank, so a
    positive number means the method puts the project higher than the baseline does."""

    primary: str
    baseline: str
    methods: tuple[str, ...]
    results: Mapping[str, EngineResult]
    rows: tuple[ComparisonRow, ...]
    # The primary method's biggest rank moves against the raw mean, each split into judges' lean
    # and shrinkage (only for methods with "decomposition" and a raw_mean baseline).
    movers: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "methods", tuple(self.methods))
        object.__setattr__(self, "results", _frozen_map(self.results))
        object.__setattr__(self, "rows", tuple(self.rows))
        object.__setattr__(self, "movers", tuple(_frozen_map(m) for m in self.movers))

    def to_json(self) -> str:
        return dumps(self)


def to_jsonable(value):
    """Plain JSON types, in a stable order. NaN and infinities become None."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(to_jsonable(v) for v in value)
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if hasattr(value, "item") and callable(value.item):  # numpy scalar
        value = value.item()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        return value
    if hasattr(value, "tolist"):  # numpy array
        return to_jsonable(value.tolist())
    raise TypeError(f"not JSON-serialisable: {type(value).__name__}")


def dumps(value) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
