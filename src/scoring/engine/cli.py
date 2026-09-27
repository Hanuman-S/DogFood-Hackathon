"""Run the engine on an organizer-format file, with no database.

    python -m scoring.engine.cli acceptance/fixtures.json
    python -m scoring.engine.cli FILE --method zscore --compare raw_mean
    python -m scoring.engine.cli FILE --config my.json --weights functionality=0.5,quality=0.3,innovation=0.2
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
        config = EngineConfig.from_dict(_read_json(args.config)) if args.config else EngineConfig()
        inp, log = load_organizer_file(args.file, weights=_parse_weights(args.weights))
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


def print_comparison(result, out):
    """The primary method's ranking, with the baseline's rank and every other method's rank."""
    primary = result.results[result.primary]
    base = result.baseline
    others = [m for m in result.methods if m != result.primary]
    group_size = Counter(p.tie_group for p in primary.projects)
    rows = primary.by_project()
    print(f"method {primary.method} v{primary.method_version} | baseline {base}", file=out)
    header = f"{'rank':>5} {'tie':>4}  {'project':<14} {'track':<10} {'n':>2} {'score':>14}"
    header += "".join(f" {m[:10]:>10}" for m in others) + f" {'chg':>4}  flags"
    print(header, file=out)
    for row in result.rows:
        p = rows.get(row.project_id)
        if p is None:
            continue
        tied = "=" if group_size[p.tie_group] > 1 else " "
        score = f"{p.score:.3f}" + (f" ±{p.se:.3f}" if p.se is not None else "")
        line = f"{p.rank:>4}{tied} {p.tie_group:>4}  {p.project_id:<14} {str(p.track_id or '-'):<10} {p.n_reviews:>2} {score:>14}"
        line += "".join(f" {_fmt(row.ranks[m]):>10}" for m in others)
        line += f" {_signed(row.change_vs_baseline[result.primary]):>4}  {','.join(p.flags)}"
        print(line, file=out)
    groups = len(group_size)
    print(f"# {len(primary.projects)} ranked in {groups} tie group(s); '=' marks a row that shares "
          "its tie group, whose order inside the group is not a result.", file=out)
    for ex in primary.excluded:
        print(f"# excluded {ex.kind} {ex.id}: {ex.reason}", file=out)
    cov = primary.coverage
    rpp = cov["reviews_per_project"]
    print(f"# coverage: {cov['reviews']} reviews, {cov['judges']} judges; reviews per project "
          f"min {rpp['min']} median {rpp['median']} max {rpp['max']}; "
          f"{len(cov['projects_below_min_reviews'])} project(s) below {cov['min_reviews']}", file=out)
    print(f"# {cov['note']}", file=out)
    for key, note in primary.diagnostics["pipeline"].items():
        print(f"# {key}: {note}", file=out)


def _fmt(value):
    return "-" if value is None else str(value)


def _signed(value):
    return "-" if value is None else f"{value:+d}"


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EngineError(f"Cannot read config {path}: {error}") from error


def _parse_weights(text):
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
    p.add_argument("--config", help="JSON file of engine config overrides")
    p.add_argument("--weights", help="criterion weights, e.g. functionality=0.5,quality=0.3")
    p.add_argument("--json", help="also write the full comparison as JSON to this path")
    p.add_argument("--list", action="store_true", help="list the registered methods and exit")
    return p


if __name__ == "__main__":
    sys.exit(main())
