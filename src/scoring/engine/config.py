"""Engine configuration: typed, frozen, with defaults. Unknown keys are an error, never ignored.

Method names are checked when the pipeline runs (against the registry), not here, so a method
registered after the config was built -- a plugin in a test, say -- can still be named. Filter
and flagger names are checked here, against their own registries.

`cv_seed=None` means "derive the seed from the event": `resolved(event_id)` replaces it with
`int(sha256(event_id)[:8], 16)`, so the same event always gets the same cross-validation folds
and a stored result always records the seed it actually used.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, fields, replace

from .errors import ConfigError

TUPLE_KEYS = ("compare", "filters", "flaggers", "lambda_grid", "lambdas")
DUPLICATE_POLICIES = ("exclude", "merge")
TIE_RULES = ("anchor", "chain")


@dataclass(frozen=True)
class EngineConfig:
    primary: str = "m2"
    compare: tuple[str, ...] = ("raw_mean", "zscore")
    # Pre-fit filters, applied in this order (the lab resolves duplicates before flat judges).
    filters: tuple[str, ...] = ("exclude_duplicate_submissions", "exclude_flat_judges")
    # "exclude": drop a duplicate submission and all its reviews (the lab's rule).
    # "merge": move its reviews to the kept project; where a judge reviewed both, the kept
    # project's review wins. Our rule -- the lab's sensitivity "merge" averaged the two instead.
    duplicate_policy: str = "exclude"
    flaggers: tuple[str, ...] = ("insufficient_reviews", "near_flat_judges", "outlier_residuals")
    # Methods with "uncertainty": a project joins the current tie group while P(ahead) < this.
    tie_threshold: float = 0.84
    # Whose P(ahead) decides: "anchor" = the group's top project (a group is only as wide as one
    # clear gap); "chain" = the project just above (the lab's rule: small gaps chain, so a
    # low-signal event becomes one group however far apart its ends are).
    tie_rule: str = "anchor"
    # Methods without "uncertainty": scores equal after rounding to this many decimals tie.
    equal_decimals: int = 9
    # z-score: per-judge SD floor (population SD), as in the lab and the ridge PDF's section 14.
    z_floor: float = 0.5
    # Projects with fewer reviews than this are flagged and listed in coverage.
    min_reviews: int = 2
    # M2 (ridge): (lambda_q, lambda_b) chosen by k-fold CV over lambda_grid x lambda_grid.
    lambda_grid: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0)
    cv_folds: int = 5
    cv_seed: int | None = None
    # A component with fewer reviews than this skips CV and uses the lab's fixed (0.5, 1.0).
    cv_min_reviews: int = 20
    # Fixed (lambda_q, lambda_b): bypasses CV entirely.
    lambdas: tuple[float, float] | None = None
    sigma2_floor: float = 0.05
    # outlier_residuals: flag a review whose studentized residual exceeds outlier_k. A heuristic
    # the scoring study did not validate.
    outlier_k: float = 2.0
    outlier_min_reviews: int = 10
    # near_flat_judges: a judge whose weighted scores have variance below this (n >= 2).
    near_flat_threshold: float = 0.1

    def __post_init__(self):
        from .filters import FILTERS
        from .flaggers import FLAGGERS

        _check_name(self.primary, "primary")
        for key in ("compare", "filters", "flaggers"):
            names = _as_tuple(getattr(self, key), key)
            for name in names:
                _check_name(name, key)
            object.__setattr__(self, key, tuple(dict.fromkeys(names)))
        for name in self.filters:
            if name not in FILTERS:
                raise ConfigError(f"Unknown filter {name!r}. Known: {', '.join(sorted(FILTERS))}.")
        for name in self.flaggers:
            if name not in FLAGGERS:
                raise ConfigError(f"Unknown flagger {name!r}. Known: {', '.join(sorted(FLAGGERS))}.")
        if self.duplicate_policy not in DUPLICATE_POLICIES:
            raise ConfigError(f"duplicate_policy must be one of {', '.join(DUPLICATE_POLICIES)}.")
        _check_number(self.tie_threshold, "tie_threshold", 0.5, 1.0, open_high=True)
        if self.tie_rule not in TIE_RULES:
            raise ConfigError(f"tie_rule must be one of {', '.join(TIE_RULES)}.")
        _check_int(self.equal_decimals, "equal_decimals", 0, 15)
        _check_number(self.z_floor, "z_floor", 0.0, None, open_low=True)
        _check_int(self.min_reviews, "min_reviews", 1, 1000)
        grid = _as_tuple(self.lambda_grid, "lambda_grid")
        if not grid:
            raise ConfigError("lambda_grid must list at least one value.")
        for value in grid:
            _check_number(value, "lambda_grid", 0.0, None, open_low=True)
        object.__setattr__(self, "lambda_grid", tuple(float(v) for v in grid))
        _check_int(self.cv_folds, "cv_folds", 2, 50)
        if self.cv_seed is not None:
            _check_int(self.cv_seed, "cv_seed", 0, 2**63 - 1)
        _check_int(self.cv_min_reviews, "cv_min_reviews", 2, 10**6)
        if self.lambdas is not None:
            pair = _as_tuple(self.lambdas, "lambdas")
            if len(pair) != 2:
                raise ConfigError("lambdas must be [lambda_q, lambda_b].")
            for value in pair:
                _check_number(value, "lambdas", 0.0, None, open_low=True)
            object.__setattr__(self, "lambdas", (float(pair[0]), float(pair[1])))
        _check_number(self.sigma2_floor, "sigma2_floor", 0.0, None, open_low=True)
        _check_number(self.outlier_k, "outlier_k", 0.0, None, open_low=True)
        _check_int(self.outlier_min_reviews, "outlier_min_reviews", 1, 10**6)
        _check_number(self.near_flat_threshold, "near_flat_threshold", 0.0, None)

    @classmethod
    def from_dict(cls, data: dict | None) -> "EngineConfig":
        """Build from a plain dict (a --config file, an event's stored overrides)."""
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError("An engine config must be a JSON object.")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(
                f"Unknown engine config key(s): {', '.join(unknown)}. Known: {', '.join(sorted(known))}."
            )
        values = dict(data)
        for key in TUPLE_KEYS:
            if isinstance(values.get(key), list):
                values[key] = tuple(values[key])
        return cls(**values)

    def with_overrides(self, data: dict | None) -> "EngineConfig":
        merged = self.to_dict()
        merged.update(data or {})
        return EngineConfig.from_dict(merged)

    def resolved(self, event_id: str) -> "EngineConfig":
        """The same config with `cv_seed` fixed: derived from the event when it was None."""
        if self.cv_seed is not None:
            return self
        return replace(self, cv_seed=seed_for(event_id))

    def to_dict(self) -> dict:
        out = asdict(self)
        for key in TUPLE_KEYS:
            if out[key] is not None:
                out[key] = list(out[key])
        return out


def seed_for(event_id: str) -> int:
    return int(hashlib.sha256(str(event_id).encode("utf-8")).hexdigest()[:8], 16)


def _as_tuple(value, key):
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ConfigError(f"{key} must be a list.")
    return tuple(value)


def _check_name(value, key):
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a name (a non-empty string).")


def _check_number(value, key, low, high, *, open_low=False, open_high=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number.")
    if low is not None and (value <= low if open_low else value < low):
        raise ConfigError(f"{key} must be {'>' if open_low else '>='} {low}.")
    if high is not None and (value >= high if open_high else value > high):
        raise ConfigError(f"{key} must be {'<' if open_high else '<='} {high}.")


def _check_int(value, key, low, high):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be a whole number.")
    if not low <= value <= high:
        raise ConfigError(f"{key} must be between {low} and {high}.")
