"""Run the engine on an organizer-format file, with no database.

    python -m scoring.engine.cli acceptance/fixtures.json
    python -m scoring.engine.cli FILE --method zscore --compare raw_mean
    python -m scoring.engine.cli FILE --config my.json --weights functionality=0.5,quality=0.3,innovation=0.2
    python -m scoring.engine.cli FILE --config duplicate_policy=merge,cv_seed=20260926
    python -m scoring.engine.cli FILE --json out.json
    python -m scoring.engine.cli --list

It reads the file it is given and writes only the --json file; nothing is imported anywhere.
The table always shows the tie group next to the rank, and marks tied rows with "=", so the order
inside a tie group is never read as a result.
"""

import os

# Single-threaded BLAS before numpy loads, so results cannot depend on thread timing.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
from collections import Counter  # noqa: E402
from pathlib import Path  # noqa: E402

from .config import EngineConfig  # noqa: E402
from .errors import EngineError  # noqa: E402
from .io import load_organizer_file  # noqa: E402
from .methods import registry  # noqa: E402
from .pipeline import compare  # noqa: E402


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    try:
        if args.list:
            print_methods(out)
            return 0
        if not args.file:
            print("error: give a file, or --list", file=out)
            return 2
        config = EngineConfig.from_dict(read_config(args.config)) if args.config else EngineConfig()
        inp, log = load_organizer_file(args.file, weights=parse_weights(args.weights))
        method = args.method or config.primary
        others = [m.strip() for m in args.compare.split(",") if m.strip()] if args.compare else list(config.compare)
        result = compare(inp, [method, *others], baseline=args.baseline, config=config)
    except EngineError as error:
        print(f"error: {error}", file=out)
        return 2
    for line in log:
        print(f"# {line}", file=out)
    print_comparison(result, out)
    if args.json:
        Path(args.json).write_text(result.to_json() + "\n", encoding="utf-8")
        print(f"# wrote {args.json}", file=out)
    return 0


def print_methods(out):
    for m in registry.describe():
        caps = ", ".join(m["capabilities"]) or "-"
        print(f"{m['name']:<16} v{m['version']:<6} {caps}", file=out)


def print_comparison(result, out, labels=None):
    """The primary method's ranking, with the baseline's rank and every other method's rank.

    `labels` ({"projects": {id: text}, "judges": {...}, "tracks": {...}}) replaces ids in the
    output with readable names; the portal passes it so no bare database id is ever shown."""
    labels = labels or {}
    project = _labeller(labels.get("projects"))
    judge = _labeller(labels.get("judges"))
    track = _labeller(labels.get("tracks"))

    def review(review_id):
        judge_id, _, project_id = review_id.partition(":")  # judge ids never contain ':'
        return f"{judge(judge_id)} on {project(project_id)}"

    def excluded(ex):
        return {"project": project, "judge": judge, "review": review}.get(ex.kind, str)(ex.id)

    primary = result.results[result.primary]
    base = result.baseline
    others = [m for m in result.methods if m != result.primary]
    group_size = Counter((p.component, p.tie_group) for p in primary.projects)
    several = len(primary.components) > 1
    rows = primary.by_project()
    width = max([14] + [len(project(p.project_id)) for p in primary.projects])
    track_width = max([10] + [len(track(p.track_id)) for p in primary.projects if p.track_id])
    print(f"method {primary.method} v{primary.method_version} | baseline {base}", file=out)
    header = f"{'rank':>5} {'tie':>4}  {'project':<{width}} {'track':<{track_width}} {'n':>2} {'score':>16}"
    header += "".join(f" {rank_column(m):>10}" for m in others) + f" {'chg':>4}  flags"
    if several:
        header = "comp " + header
    print(header, file=out)
    for row in result.rows:
        p = rows.get(row.project_id)
        if p is None:
            continue
        tied = "=" if group_size[(p.component, p.tie_group)] > 1 else " "
        score = f"{p.score:.3f}" + (f" +-{p.se:.3f}" if p.se is not None else "")
        where = track(p.track_id) if p.track_id else "-"
        line = f"{p.rank:>4}{tied} {p.tie_group:>4}  {project(p.project_id):<{width}} {where:<{track_width}} {p.n_reviews:>2} {score:>16}"
        line += "".join(f" {_fmt(row.ranks[m]):>10}" for m in others)
        line += f" {_signed(row.change_vs_baseline[result.primary]):>4}  {','.join(p.flags)}"
        print((f"{p.component:>4} " if several else "") + line, file=out)

    notes = primary.diagnostics["pipeline"]
    print(f"# {len(primary.projects)} projects ranked in {len(group_size)} tie group(s); '=' marks a row "
          "that shares its tie group, whose order inside the group is not a result.", file=out)
    print(f"# reviews: {notes['reviews_in']} in the input, {notes['reviews_used']} used; "
          f"ranking: {notes['ranking']}", file=out)
    if "why_per_component" in notes:
        print(f"# {notes['why_per_component']}", file=out)
    for params in primary.params_chosen["components"]:
        if "lam_q" in params:
            print(f"# component {params['component']}: lambda_q={params['lam_q']:g} lambda_b={params['lam_b']:g} "
                  f"({params['lambda_source']}, seed {params['cv_seed']}), "
                  f"lambda_at_grid_boundary={_bool(params['lambda_at_grid_boundary'])}, "
                  f"sigma2={params['sigma2']:.4f}, trH={params['trH']:.2f}", file=out)
    for component in primary.diagnostics["method"]["components"]:
        if "lambda_reason" in component:
            print(f"# component {component['component']}: {component['lambda_reason']}", file=out)
    for ex in primary.excluded:
        print(f"# excluded {ex.kind} {excluded(ex)}: {_label_reason(ex.reason, project, judge)}", file=out)
    for flag in primary.flags["reviews"]:
        print(f"# flagged review {judge(flag['judge'])} on {project(flag['project'])}: {flag['flag']} "
              f"(y {flag['y']:.2f}, fitted {flag['fitted']:.2f}, studentized {flag['studentized']:+.2f})", file=out)
    flagged_judges = [(judge(j.judge_id), ",".join(j.flags)) for j in primary.judges if j.flags]
    if flagged_judges:
        print("# flagged judges: " + "; ".join(f"{j} ({f})" for j, f in flagged_judges), file=out)
    for note in primary.flags["notes"]:
        print(f"# {note}", file=out)
    cov = primary.coverage
    rpp = cov["reviews_per_project"]
    print(f"# coverage: {cov['reviews']} reviews, {cov['judges']} judges; reviews per project "
          f"min {rpp['min']} median {rpp['median']} max {rpp['max']}; "
          f"{len(cov['projects_below_min_reviews'])} project(s) below {cov['min_reviews']}", file=out)
    print(f"# {cov['note']}", file=out)
    if result.movers:
        print(f"# biggest moves vs {base} (raw - corrected = judges' lean + shrinkage):", file=out)
        for m in result.movers:
            print(f"#   {project(m['project_id'])}: raw {m['raw_rank']} -> {m['rank']} ({m['change']:+d}); "
                  f"{m['explanation']}", file=out)


# Where the engine's exclusion reasons name a project or judge id (see engine/filters.py).
_REASON_IDS = (
    (re.compile(r"(review of duplicate submission )(\S+)( \(kept: )([^)]+)(\))"), ("project", "project")),
    (re.compile(r"(duplicate submission of )(\S+?)(;)"), ("project",)),
    (re.compile(r"(judge also reviewed )(\S+?)(, the kept)"), ("project",)),
    (re.compile(r"(by flat judge )(\S+)()$"), ("judge",)),
)


def _label_reason(reason, project, judge):
    for pattern, kinds in _REASON_IDS:
        def swap(match, kinds=kinds):
            parts = list(match.groups())
            for n, kind in enumerate(kinds):          # ids sit at groups 2, 4, ...
                i = 1 + 2 * n
                parts[i] = (project if kind == "project" else judge)(parts[i])
            return "".join(parts)
        reason = pattern.sub(swap, reason)
    return reason


def _labeller(mapping):
    mapping = mapping or {}
    return lambda value: mapping.get(value, str(value))


RANK_COLUMNS = {"raw_mean": "raw_rank", "zscore": "z_rank"}


def rank_column(method):
    """The other methods' columns show ranks, not scores, and are named so."""
    return RANK_COLUMNS.get(method, f"{method}_rank")[:10]


def _bool(value):
    return "n/a" if value is None else str(value).lower()


def read_config(text):
    """A JSON file path, or inline `key=value[,key=value]` (values parsed as JSON, else strings)."""
    path = Path(text)
    if path.is_file():
        return _read_json(path)
    if "=" not in text:
        raise EngineError(f"--config {text!r}: no such file, and not key=value pairs.")
    values = {}
    for part in _split_top_level(text):
        key, sep, raw = part.partition("=")
        if not key.strip() or not sep:
            raise EngineError(f"--config: {part!r} is not key=value.")
        try:
            values[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            values[key.strip()] = raw.strip()
    return values


def _split_top_level(text):
    """Split on commas that are not inside [...] (so lambdas=[0.5,1] stays one value)."""
    parts, depth, current = [], 0, []
    for char in text:
        depth += {"[": 1, "]": -1}.get(char, 0)
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _fmt(value):
    return "-" if value is None else str(value)


def _signed(value):
    return "-" if value is None else f"{value:+d}"


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EngineError(f"Cannot read config {path}: {error}") from error


def parse_weights(text):
    if not text:
        return None
    weights = {}
    for part in text.split(","):
        key, _, value = part.partition("=")
        try:
            weights[key.strip()] = float(value)
        except ValueError:
            raise EngineError(f"--weights: {part!r} is not key=number.") from None
    return weights


def _parser():
    p = argparse.ArgumentParser(prog="python -m scoring.engine.cli", description=__doc__.split("\n\n")[0])
    p.add_argument("file", nargs="?", help="organizer-format JSON (fixtures or a lab file)")
    p.add_argument("--method", help="primary method (default: the config's primary)")
    p.add_argument("--compare", help="comma-separated methods to show next to it")
    p.add_argument("--baseline", default="raw_mean", help="rank changes are measured against this")
    p.add_argument("--config", help="engine config overrides: a JSON file, or key=value[,key=value]")
    p.add_argument("--weights", help="criterion weights, e.g. functionality=0.5,quality=0.3")
    p.add_argument("--json", help="also write the full comparison as JSON to this path")
    p.add_argument("--list", action="store_true", help="list the registered methods and exit")
    return p


if __name__ == "__main__":
    sys.exit(main())
