"""Run a scoring method, or several side by side. The same steps for every method:

    1. validate the input
    2. filters (config `filters`: duplicate submissions, then flat judges), each exclusion reported
    3. connected components of the judge-project graph; projects with no review are excluded
    4. per component: fit the method, check its output against the contract, rank, tie groups
       (from standard errors if the method has "uncertainty", else exact equality)
    5. flaggers (config `flaggers`)
    6. coverage
    7. explain (methods with "decomposition": the rank move split into lean and shrinkage)

There is an overall ranking only when the graph is one component; otherwise every rank and tie
group counts within its component and the diagnostics say why.

Deterministic: the same input and config give byte-identical `to_json()` output.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .components import components
from .config import EngineConfig
from .coverage import coverage
from .errors import ConfigError, MethodContractError
from .explain import decompose, movers
from .filters import FILTERS
from .flaggers import run_flaggers
from .methods import registry
from .methods.base import MethodOutput
from .prepare import PreparedData, prepare, validate
from .ties import exact_tie_groups, ordinal_rank, se_tie_groups
from .types import (ComparisonResult, ComparisonRow, EngineInput, EngineResult, Exclusion, JudgeResult,
                    ProjectResult)

SEVERAL_COMPONENTS = (
    "the judge-project graph has {n} disconnected components: no chain of shared judges links "
    "them, so their scores are not on a common scale. Each component is ranked on its own; there "
    "is no overall rank."
)


@dataclass(frozen=True)
class ComponentFit:
    index: int
    data: PreparedData
    output: MethodOutput


def resolve_config(inp: EngineInput, config=None) -> EngineConfig:
    """`config` (an EngineConfig or a dict) wins over `inp.config`; defaults otherwise.
    The result has its cv_seed resolved for this event."""
    for candidate in (config, inp.config):
        if candidate is None:
            continue
        if isinstance(candidate, EngineConfig):
            return candidate.resolved(inp.event_id)
        if isinstance(candidate, dict):
            return EngineConfig.from_dict(candidate).resolved(inp.event_id)
        raise ConfigError("config must be an EngineConfig or a dict.")
    return EngineConfig().resolved(inp.event_id)


def run(inp: EngineInput, method: str | None = None, config=None) -> EngineResult:
    cfg = resolve_config(inp, config)
    impl = registry.get(method or cfg.primary)
    validate(inp)

    reviews, projects = tuple(inp.reviews), dict(inp.projects)
    excluded = list(inp.excluded)
    for name in cfg.filters:
        kept = FILTERS[name](reviews, projects, inp.duplicates, cfg)
        reviews, projects = kept.reviews, kept.projects
        excluded.extend(kept.excluded)
    reviewed = {r.project_id for r in reviews}
    excluded.extend(Exclusion("project", p, "no reviews") for p in projects if p not in reviewed)
    scope = [p for p in projects if p in reviewed]

    groups = components(scope, reviews)
    several = len(groups) > 1
    fits, results, judges = [], [], []
    for k, (members, component_reviews) in enumerate(groups):
        seed = (cfg.cv_seed, k) if several else cfg.cv_seed
        data = prepare(inp, component_reviews, members, rng_seed=seed)
        out = impl.fit(data, cfg)
        _check_output(impl, out, data)
        fits.append(ComponentFit(k, data, out))
        results.extend(_rank_component(k, data, out, impl, cfg))
        judges.extend(_judges(k, data, out))

    flags = run_flaggers(cfg.flaggers, fits, cfg, impl)
    results = [_with_flags(r, flags.projects.get(r.project_id, ())) for r in results]
    judges = [_with_flags(j, flags.judges.get(j.judge_id, ())) for j in judges]

    pipeline_notes = {
        "reviews_in": len(inp.reviews),
        "reviews_used": len(reviews),
        "ranking": "per_component" if several else "overall",
    }
    if several:
        pipeline_notes["why_per_component"] = SEVERAL_COMPONENTS.format(n=len(groups))
    if not scope:
        pipeline_notes["empty"] = "no project has a review; nothing to rank"
    return EngineResult(
        method=impl.name,
        method_version=impl.version,
        capabilities=tuple(sorted(impl.capabilities)),
        config=cfg.to_dict(),
        params_chosen={"components": [{"component": f.index, **f.output.params} for f in fits]},
        components=tuple(
            {"component": f.index, "projects": f.data.P, "judges": f.data.J, "reviews": f.data.N} for f in fits
        ),
        projects=tuple(results),
        judges=tuple(judges),
        excluded=tuple(excluded),
        flags={"reviews": flags.reviews, "notes": flags.notes},
        coverage=coverage(list(projects), reviews, cfg.min_reviews),
        diagnostics={
            "method": {"components": [{"component": f.index, **f.output.diagnostics} for f in fits]},
            "pipeline": pipeline_notes,
        },
    )


def compare(inp: EngineInput, methods=None, baseline: str = "raw_mean", config=None,
            movers_k: int = 8) -> ComparisonResult:
    """Every method's ranks side by side, with each method's rank change against `baseline`.

    `methods` defaults to the config's primary followed by its comparison methods. The first
    method is the primary: rows are in its rank order. The baseline is always run.
    """
    cfg = resolve_config(inp, config)
    names = list(dict.fromkeys(methods if methods else (cfg.primary, *cfg.compare)))
    order = names + ([baseline] if baseline not in names else [])
    results = {name: run(inp, name, cfg) for name in order}

    by_method = {name: result.by_project() for name, result in results.items()}
    primary = by_method[names[0]]
    ranked = sorted(primary, key=lambda p: (primary[p].component, primary[p].rank))
    rest = [p for p in inp.projects if p not in primary]
    rows = []
    for project_id in ranked + rest:
        ranks, groups, scores, change = {}, {}, {}, {}
        base = by_method[baseline].get(project_id)
        for name in order:
            row = by_method[name].get(project_id)
            ranks[name] = row.rank if row else None
            groups[name] = row.tie_group if row else None
            scores[name] = row.score if row else None
            change[name] = base.rank - row.rank if (row and base) else None
        rows.append(ComparisonRow(
            project_id=project_id, track_id=inp.projects.get(project_id), ranks=ranks,
            tie_groups=groups, scores=scores, change_vs_baseline=change,
        ))
    return ComparisonResult(primary=names[0], baseline=baseline, methods=tuple(order), results=results,
                            rows=tuple(rows), movers=tuple(movers(results, names[0], baseline, list(inp.projects), movers_k)))


def _rank_component(k, data, out, impl, cfg):
    scores = np.asarray(out.scores, dtype=float)
    rank = ordinal_rank(scores, cfg.equal_decimals)
    if "uncertainty" in impl.capabilities:
        groups, p_next = se_tie_groups(scores, np.asarray(out.cov_q), rank, cfg.tie_threshold)
        se = np.asarray(out.se, dtype=float)
    else:
        groups, p_next, se = exact_tie_groups(scores, rank, cfg.equal_decimals), [None] * data.P, None
    parts = decompose(data, out) if "decomposition" in impl.capabilities else [{}] * data.P
    n_reviews = data.reviews_per_project()
    rows = []
    for i, project in enumerate(data.projects):
        extras = dict(parts[i])
        if "uncertainty" in impl.capabilities:
            extras["p_ahead_of_next"] = p_next[i]
        rows.append(ProjectResult(
            project_id=project, track_id=data.tracks[i], component=k, score=float(scores[i]),
            se=None if se is None else float(se[i]), rank=int(rank[i]), tie_group=int(groups[i]),
            n_reviews=int(n_reviews[i]), extras=extras,
        ))
    return sorted(rows, key=lambda r: r.rank)


def _judges(k, data, out):
    counts = data.reviews_per_judge()
    bias = None if out.judge_bias is None else np.asarray(out.judge_bias, dtype=float)
    centre = float(bias.mean()) if bias is not None and len(bias) else 0.0
    return [
        JudgeResult(judge_id=judge, component=k, n_reviews=int(counts[j]),
                    bias=None if bias is None else float(bias[j]),
                    bias_centred=None if bias is None else float(bias[j] - centre))
        for j, judge in enumerate(data.judges)
    ]


def _with_flags(row, flags):
    if not flags:
        return row
    cls = type(row)
    values = {name: getattr(row, name) for name in row.__dataclass_fields__}
    values["flags"] = tuple(flags)
    return cls(**values)


def _check_output(impl, out, data):
    caps = impl.capabilities
    name = impl.name
    scores = np.asarray(out.scores, dtype=float)
    if scores.shape != (data.P,):
        raise MethodContractError(f"{name}: expected {data.P} scores, got shape {scores.shape}.")
    if not np.isfinite(scores).all():
        bad = [data.projects[i] for i in np.flatnonzero(~np.isfinite(scores))]
        raise MethodContractError(f"{name}: no finite score for reviewed project(s) {', '.join(bad)}.")
    has_uncertainty = "uncertainty" in caps
    if has_uncertainty != (out.se is not None) or has_uncertainty != (out.cov_q is not None):
        raise MethodContractError(
            f"{name}: se and cov_q must be returned exactly when the method claims 'uncertainty'."
        )
    if has_uncertainty:
        se = np.asarray(out.se, dtype=float)
        if se.shape != (data.P,) or not (np.isfinite(se).all() and (se >= 0).all()):
            raise MethodContractError(f"{name}: se must be {data.P} finite values >= 0.")
        if np.asarray(out.cov_q).shape != (data.P, data.P):
            raise MethodContractError(f"{name}: cov_q must be {data.P} x {data.P}.")
    if ("judge_bias" in caps) != (out.judge_bias is not None):
        raise MethodContractError(f"{name}: judge_bias must match the 'judge_bias' capability.")
    if out.judge_bias is not None and np.asarray(out.judge_bias).shape != (data.J,):
        raise MethodContractError(f"{name}: judge_bias must have one value per judge.")
    has_fitted = out.fitted is not None and out.hat is not None and out.sigma2 is not None
    if ("fitted" in caps) != has_fitted:
        raise MethodContractError(f"{name}: fitted, hat and sigma2 must match the 'fitted' capability.")
    if has_fitted and (np.asarray(out.fitted).shape != (data.N,) or np.asarray(out.hat).shape != (data.N,)):
        raise MethodContractError(f"{name}: fitted and hat must have one value per review.")
    if "decomposition" in caps and not ({"judge_bias", "fitted"} <= caps):
        raise MethodContractError(f"{name}: 'decomposition' needs 'judge_bias' and 'fitted'.")
    if not isinstance(out.params, dict) or not isinstance(out.diagnostics, dict):
        raise MethodContractError(f"{name}: params and diagnostics must be dicts.")
