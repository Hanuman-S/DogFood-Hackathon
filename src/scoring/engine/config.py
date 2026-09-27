"""Engine configuration: typed, frozen, with defaults. Unknown keys are an error, never ignored.

S1 holds only the settings the S1 pipeline uses. S2 adds M2's (λ grid, CV folds and seed,
filters, flaggers, duplicate policy) and makes "m2" the primary method; until then the primary
is "raw_mean", because a default that names an unregistered method would fail every run.

Method names are checked when the pipeline runs (against the registry), not here, so a method
registered after the config was built -- a plugin in a test, say -- can still be named.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from .errors import ConfigError


@dataclass(frozen=True)
class EngineConfig:
    primary: str = "raw_mean"
    compare: tuple[str, ...] = ("raw_mean", "zscore")
    # Methods with the "uncertainty" capability: adjacent projects join one tie group while
    # P(ahead) is below this. Used from S2 (M2); kept here so an event's config is stable.
    tie_threshold: float = 0.84
    # Methods without "uncertainty": scores equal after rounding to this many decimals tie.
    equal_decimals: int = 9
    # z-score: per-judge SD floor (population SD), as in the lab and the ridge PDF's section 14.
    z_floor: float = 0.5
    # Coverage: projects with fewer reviews than this are listed as thinly reviewed.
    min_reviews: int = 2

    def __post_init__(self):
        _check_name(self.primary, "primary")
        compare = self.compare
        if isinstance(compare, str) or not isinstance(compare, (list, tuple)):
            raise ConfigError("compare must be a list of method names.")
        for name in compare:
            _check_name(name, "compare")
        object.__setattr__(self, "compare", tuple(dict.fromkeys(compare)))
        _check_number(self.tie_threshold, "tie_threshold", 0.5, 1.0, open_high=True)
        _check_int(self.equal_decimals, "equal_decimals", 0, 15)
        _check_number(self.z_floor, "z_floor", 0.0, None, open_low=True)
        _check_int(self.min_reviews, "min_reviews", 1, 1000)

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
        if "compare" in values and isinstance(values["compare"], list):
            values["compare"] = tuple(values["compare"])
        return cls(**values)

    def with_overrides(self, data: dict | None) -> "EngineConfig":
        merged = self.to_dict()
        merged.update(data or {})
        return EngineConfig.from_dict(merged)

    def to_dict(self) -> dict:
        out = asdict(self)
        out["compare"] = list(self.compare)
        return out


def _check_name(value, key):
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a method name (a non-empty string).")


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
