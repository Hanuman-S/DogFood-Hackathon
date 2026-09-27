# PLAN: T2 scoring engine (phase log)

The approved plan is [docs/t2-scoring-plan.md](docs/t2-scoring-plan.md), copied word for word
from the version that was approved. This file records what each phase actually did, and every
departure from the plan. The T1 phase log was deleted in `c6f381c`. It is recoverable with
`git show 4a233b1:PLAN.md`.

Scope, from the plan: the scoring and ranking engine and its persistence. No assignment, no
score entry, no results pages, no publishing service or UI, no CSV export, no seed data.
`.dogfood.toml` still claims T1 only.

## S1: engine core (done)

**Built** (`src/scoring/engine/`, pure Python and numpy, no Django):

| file | what it does |
|---|---|
| `types.py` | Frozen inputs and outputs: `Criterion`, `Rubric`, `Review`, `EngineInput` (with `duplicates`), `Exclusion`, `ProjectResult`, `EngineResult`, `ComparisonRow`, `ComparisonResult`. `to_json()` keeps field order and insertion order, writes full-precision floats, and turns NaN into null. |
| `config.py` | `EngineConfig`, frozen and typed, with `from_dict`. An unknown key raises `ConfigError`. |
| `errors.py` | `EngineInputError`, `ConfigError`, `UnknownMethod`, `MethodContractError`. |
| `prepare.py` | Validates the input: unknown project or criterion, repeated (judge, project), out-of-range values, all-zero weights. Builds the `pi`, `ji`, `S` and `y` arrays in input order. `y` is the lab's weighted mean, renormalised over the criteria present. |
| `methods/base.py` | The contract: `ScoringMethod`, `MethodOutput`, `CAPABILITIES`. |
| `methods/registry.py` | `@register` checks the declared contract at import. Also `get`, `available`, `describe`, and `unregister` for tests. |
| `methods/__init__.py` | Imports every module in `methods/`, so a new method is one file. |
| `methods/raw_mean.py`, `methods/zscore.py` | Ported from the lab's `baselines.py`. z-score uses the population SD with a 0.5 floor. |
| `ties.py` | The lab's ordinal rank, and tie groups on exactly equal scores (rounded to 9 dp). |
| `coverage.py` | Reviews per project and per judge, and the projects with 0 or fewer than 2 reviews. It states that without assignment data, reviews that were assigned but never written cannot be counted. |
| `pipeline.py` | `run` and `compare`. Checks each method's output against the contract. Projects with no reviews are excluded with a reason. |
| `io.py` | Reads the organizer file format. Duplicates are declared using the lab's rule; repeated records are averaged. Weights default to 1, with `--weights` to override. |
| `cli.py` | `python -m scoring.engine.cli FILE [--method] [--compare] [--baseline] [--config] [--weights] [--json]`, and `--list`. Every table row shows the tie group, and tied rows are marked `=`. |

**Also:**
- `numpy==2.5.3` is pinned. A cp312 wheel exists, and 2.5.3 is the lab's version.
- `OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1` are set in the Dockerfile and at the top of
  the CLI.
- CLAUDE.md now says where the clock and permissions live, and states the engine rules.

**Tests** (engine tests are pure; no database):
- `test_engine_contract.py` runs every registered method on six inputs: the fixtures, a small
  case, an unreviewed project, all-tied, one project, and empty.
- `test_engine_plugin.py`:
  - an inline method goes through `run`, `compare` and the CLI
  - broken methods are refused
  - bad registrations are refused
- `test_engine_purity.py`:
  - an AST check over every engine module, plus a check that the checker itself catches 14
    kinds of violation
  - a fresh interpreter imports the whole engine and loads no Django
- `test_engine_core.py`:
  - the lab's baseline tests: raw averages from PDF step 2; renormalisation over the criteria
    present; the z floor for single-review and flat judges
  - edge cases, ties, coverage, compare
  - config and input errors
  - the file loader: counts, duplicate rule, repeats
  - byte-identical JSON across runs
  - the CLI

**Departures from the plan, all deliberate:**
- The default `primary` is `"raw_mean"` in S1 (the plan says `"m2"`). A default that names an
  unregistered method would make every run fail. S2 switches it to `"m2"` when M2 is registered.
- `EngineConfig` in S1 holds only the settings S1 uses: `primary`, `compare`, `tie_threshold`,
  `equal_decimals`, `z_floor`, `min_reviews`. S2 adds M2's λ grid, CV folds and seed,
  `cv_min_reviews`, the filters, the flaggers, `duplicate_policy`, and the outlier settings.
- There are no filters, components or flaggers in S1. The pipeline says so in
  `diagnostics.pipeline`, and the CLI prints it. In S1 the fixture file is therefore ranked with
  `prj_41` and the flat judge `jdg_07` still included: 41 projects, 126 reviews. From S2 on, the
  default policy excludes both.
- A method that claims "uncertainty" raises `NotImplementedError` in S1. SE tie chaining arrives
  with M2.

**Verification:**
- `./scripts/test.sh`: 387 passed, 0 failed. 90 of those are the new engine tests.
- `docker compose down -v && docker compose up --build`: the stack boots on a fresh volume, runs
  the migrations and fixture import, and serves.
- `./scripts/acceptance.sh`: "claimed T1, verified T1". T2's checks fail as before (404s), and
  the report's content is unchanged.
- In the container, `python -m scoring.engine.cli /app/acceptance/fixtures.json --compare
  raw_mean,zscore` ranks 41 projects in 20 exact-tie groups, with coverage and the S1 notes.
  `--list` shows `raw_mean` v1 and `zscore` v1.

## S2: M2 and the analysis (not started)
## S3: adapter, persistence, gate (not started)
## S4: docs (not started)
