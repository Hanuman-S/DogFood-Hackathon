"""Run a scoring method, or several side by side. The same steps for every method:

    1. validate the input
    2. (S2: filters -- flat judges, duplicate submissions)
    3. (S2: connected components; one ranking per component)
    4. fit the method, then check its output against the contract
    5. rank and tie groups
    6. (S2: flaggers)
    7. coverage
    8. (S2: explain)

Deterministic: the same input and config give byte-identical `to_json()` output.
"""

from __future__ import annotations

import numpy as np

from .config import EngineConfig
from .coverage import coverage
from .errors import ConfigError, MethodContractError
from .methods import registry
from .prepare import prepare, validate
from .ties import exact_tie_groups, ordinal_rank
from .types import ComparisonResult, ComparisonRow, EngineInput, EngineResult, Exclusion, ProjectResult

S1_NOTES = {
    "components": "not computed yet (S2): every reviewed project is ranked in one list",
    "filters": "none yet (S2): flat judges and declared duplicates are still included",
}


def resolve_config(inp: EngineInput, config=None) -> EngineConfig:
    """`config` (an EngineConfig or a dict) wins over `inp.config`; defaults otherwise."""
    for candidate in (config, inp.config):
        if candidate is None:
            continue
        if isinstance(candidate, EngineConfig):
            return candidate
        if isinstance(candidate, dict):
            return EngineConfig.from_dict(candidate)
        raise ConfigError("config must be an EngineConfig or a dict.")
    return EngineConfig()


def run(inp: EngineInput, method: str | None = None, config=None) -> EngineResult:
    cfg = resolve_config(inp, config)
    impl = registry.get(method or cfg.primary)
    validate(inp)

    reviewed = {r.project_id for r in inp.reviews}
    scope = [p for p in inp.projects if p in reviewed]
    excluded = [Exclusion("project", p, "no reviews") for p in inp.projects if p not in reviewed]
    reviews = inp.reviews

    params, method_diagnostics, results = {}, {}, []
    if scope:
        data = prepare(inp, reviews, scope)
        out = impl.fit(data, cfg)
        _check_output(impl, out, data)
        params, method_diagnostics = dict(out.params), dict(out.diagnostics)
        scores = np.asarray(out.scores, dtype=float)
        if "uncertainty" in impl.capabilities:
            raise NotImplementedError("Tie groups from standard errors arrive with M2 (S2).")
        rank = ordinal_rank(scores)
        groups = exact_tie_groups(scores, rank, cfg.equal_decimals)
        n_reviews = data.reviews_per_project()
        results = sorted(
            (
                ProjectResult(
                    project_id=p,
                    track_id=data.tracks[i],
                    score=float(scores[i]),
                    se=None,
                    rank=int(rank[i]),
                    tie_group=int(groups[i]),
                    n_reviews=int(n_reviews[i]),
                )
                for i, p in enumerate(data.projects)
            ),
            key=lambda r: r.rank,
        )

    pipeline_notes = dict(S1_NOTES)
    if inp.duplicates:
        pipeline_notes["duplicates_declared"] = dict(inp.duplicates)
    if not scope:
        pipeline_notes["empty"] = "no project has a review; nothing to rank"
    return EngineResult(
        method=impl.name,
        method_version=impl.version,
        capabilities=tuple(sorted(impl.capabilities)),
        config=cfg.to_dict(),
        params_chosen=params,
        components=None,
        projects=tuple(results),
        excluded=tuple(excluded),
        coverage=coverage(list(inp.projects), reviews, cfg.min_reviews),
        diagnostics={"method": method_diagnostics, "pipeline": pipeline_notes},
    )


def compare(inp: EngineInput, methods=None, baseline: str = "raw_mean", config=None) -> ComparisonResult:
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
    ranked = sorted(primary, key=lambda p: primary[p].rank)
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
                            rows=tuple(rows))


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
    if ("judge_bias" in caps) != (out.judge_bias is not None):
        raise MethodContractError(f"{name}: judge_bias must match the 'judge_bias' capability.")
    if ("fitted" in caps) != (out.fitted is not None and out.hat is not None):
        raise MethodContractError(f"{name}: fitted and hat must match the 'fitted' capability.")
    if not isinstance(out.params, dict) or not isinstance(out.diagnostics, dict):
        raise MethodContractError(f"{name}: params and diagnostics must be dicts.")
