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

## S2: M2 and the analysis (done)

**Built:**
- `methods/m2_ridge.py`: M2, ported from the lab.
  - Closed-form ridge, with the μ column not penalised.
  - σ² = max(RSS/(N − trH), 0.05), Cov = σ²M⁻¹, SE_p = √(C₀₀ + C_pp + 2C₀p).
  - k-fold CV over {0.25, 0.5, 1, 2, 4}². Ties go to the larger λ_b, then the larger λ_q.
  - Also returns each review's leverage h_ii and `lambda_at_grid_boundary`.
- `filters.py`:
  - `exclude_duplicate_submissions`: `exclude` is the lab's rule. `merge` is ours: the kept
    project's review wins, where the lab averaged the two.
  - `exclude_flat_judges`: the PDF's §11 rule.
- `components.py`: union-find. Each component is fitted and ranked on its own. The seed is
  `cv_seed` for a single component and `(cv_seed, k)` for several. A component below
  `cv_min_reviews` (20) uses λ = (0.5, 1.0), with the reason recorded.
- `ties.py`: P(ahead) via `math.erf`, and SE tie chaining at 0.84.
- `flaggers.py`: `insufficient_reviews`, `near_flat_judges`, and `outlier_residuals`
  (studentized residuals, k = 2, skipped with a note below 10 reviews or when the method has no
  fitted values).
- `explain.py`: the rank-move split (lean + shrinkage) against the raw mean, and the top-8
  movers.
- Contract: `ProjectResult.component`, `JudgeResult`, `EngineResult.flags`,
  `EngineInput.excluded` (for R16 in S3), and `MethodOutput.sigma2`.
- Config: the default primary is now `m2`, and every M2, filter and flagger setting is in
  `EngineConfig`. `cv_seed=None` resolves to `sha256(event_id)[:8]`, and results record the
  resolved seed.
- CLI: `--config` also takes inline `key=value[,…]`. The output shows reviews in and used, the λ
  per component, exclusions, flags and movers.
- S1's `NotImplementedError` path and its "not computed yet" notes are gone.

**Decisions made during S2** (R10 amended, R19 and R20 added to the plan; see
`docs/t2-scoring-plan.md`):
- The first golden run failed. Every rank mismatch was an ordering between projects whose lab
  scores are equal to within 1e-9: last-bit float noise from the lab's 1/3 weights. I stopped and
  reported. The user chose A + B:
  - **A:** the lab's arithmetic. Weights are normalised before averaging, and the z floor uses a
    1e-9 allowance (jdg_03's SD is exactly 0.5, so it is not floored).
  - **B:** ranks on scores rounded to 9 dp, with ties in input order, for every method.
- The golden comparison rule, and why, is in `tests/golden/README.md`.
- The movers port was wrong and is fixed. The lab broke equal move sizes by file order, because
  its CSV was sorted but the table it sorted was not; I had broken them by M2 rank. File order
  is R10 as well.
- **One known deviation, reported and not patched:** M2's accuracy against truth on
  `syn_medium`. Two M2 score pairs there are equal to within 1e-14, and their order is platform
  noise, which moves the lab's tie-unaware Spearman, Kendall and MARE: +0.000168, +0.000404 and
  −0.02. It is a strict `xfail`, and a separate test shows that giving only those pairs the lab's
  last bits makes every metric match.

**Goldens:** `tests/golden/`. The lab's result files and synthetic events are copied unchanged.
`expected/*.lab.json` was generated once by `generate_expected.py`, run with the lab's own
`.venv` (lab commit `ff3057d`). The README lists the sha256 of every file.

**Tests** (engine: 175 in all, 174 passed and 1 strict xfail):
- `test_engine_m2.py`:
  - the lab's M2 tests: the worked example, including SD(Δ) P1 vs P3 = 0.584; the tiny
    illustration; the §6 shrinkage table; a constant shift; zero λ = OLS; a single 5 shrunk; a
    disconnected graph; CV from the grid; the fixtures in under 10 s
  - both duplicate policies
  - the flat judge; single-review shrinkage; near-flat judges jdg_05, 17, 18 and 28
  - components and seeds, `cv_min_reviews`, `lambda_at_grid_boundary` (fixtures true,
    `syn_medium` (0.5, 0.5) false)
  - SE tie chaining; the outlier skip notes; the exact decomposition
  - the CLI end to end
- `test_engine_golden.py`:
  - the fixtures CSVs: rankings, CV table, leans, movers
  - golden (b) on four inputs
  - golden (a): the metric port self-check, raw and z tie-aware, and M2 against the CSV
- `test_engine_colluder.py` reports:
  - our +2 on `jdg_10:prj_001` is flagged, and is not flagged when clean
  - the lab's own +1.5 colluder (`jdg_09:prj_013`) is **missed**
  - honest reviews flagged: clean `syn_small` 7 of 121 (5.8%); with our +2, 5 of 120 (4.2%);
    fixtures 3 of 119 (2.5%)
  - `outlier_k` untuned
- The contract test now also checks the R10 input order inside ties, and SE/P(ahead)
  consistency for methods with uncertainty.

**Recorded now for S4 (docs):**
- JUDGING.md must **not** call the outlier flag a mitigation, or a defence against collusion. It
  states the measured results above, and calls it a review aid with an untuned `outlier_k`.
- Determinism wording: JSON is byte-identical on the same platform; ranks and tie groups are
  identical across platforms; full-precision scores may differ in the last bits.

**Verification:**
- `./scripts/test.sh`: 471 passed, 1 xfailed (the `syn_medium` deviation above), 0 failed.
- `docker compose down -v && up --build`: boots clean.
- `./scripts/acceptance.sh`: "claimed T1, verified T1". The T2 checks fail as before, and the
  report's content is unchanged.
- Container CLI on `acceptance/fixtures.json`:
  - default: 40 ranked, 126 in and 119 used, λ = (4, 4) from CV with the event-derived seed
    1111942736, `lambda_at_grid_boundary=true`, one tie group of 40, exclusions listed
  - `--config duplicate_policy=merge`: 120 used, and prj_07 has 6 reviews
## S3: adapter, persistence, gate (done)

**Built:**
- `scoring/services.py`:
  - `build_input(event, weights=None)`:
    - the event's submitted projects, and its reviews in `Score.pk` order
    - the rubric from `Criterion`
    - `event_id` = slug, from which the CV seed is derived
    - the importer's folded duplicate: jdg_18's review, which the importer moved onto `prj_07`,
      is given back to `dup:prj_41` and declared a duplicate of prj_07, so the engine's policy
      decides
    - reviews of projects that are not submitted go into `EngineInput.excluded` as "project not
      submitted"
  - `judging_closed(event, now)`: `now >= judging_ends_at`, the only such comparison.
  - `compute_snapshot(event, kind, actor, method, config, weights)`:
    1. permission (403)
    2. overrides on a final (400 `final_uses_event_config`; method, config or weights)
    3. the window (409 `judging_open`, on `db_now()`)
    4. a refusal if called inside another transaction
    5. one `transaction.atomic()` whose first statement is `SET TRANSACTION ISOLATION LEVEL
       REPEATABLE READ`. Inside it: the event config, `build_input`, the engine, the previous
       final by `(created_at, id)`, and the insert.

    Refusals are audited outside the transaction.
  - `set_engine_config`: permission, then locked at close (409 `scoring_config_locked`), then
    validation (400 `invalid_config`), then save and audit.
  - `input_hash`: sha256 of the canonical engine input.
- `scoring/errors.py`: each refusal carries its HTTP status and code.
- Models, in migration `scoring/0002`:
  - `EventScoringConfig`
  - `ResultSnapshot`: immutable, no `is_published`, `created_by` PROTECT plus its email, and
    stores the resolved config, rubric, hash, result, comparison and diagnostics.
  - `Publication`: snapshot RESTRICT, user FKs PROTECT plus emails, one active per event,
    unpublished fields set together.
- `scoring/0003`: Postgres triggers. `dogfood_snapshot_immutable` refuses any UPDATE;
  `dogfood_publication_guard` allows only a final of the same event, and is append-only.
- `core/0002`: four audit actions. Read-only admins for snapshots and publications.
- `manage.py score_event`:
  - flags: `--method`, `--compare`, `--config`, `--weights`, `--save preview|final --as EMAIL`
  - without `--save` it writes nothing
  - with `--save` it goes through `compute_snapshot`
- `manage.py score_methods`.
- **S2 follow-ups:**
  - The CLI's rank columns are now named `raw_rank` and `z_rank`.
  - The `syn_medium` strict xfail is replaced by a platform-independent test. It passes if the
    metrics match exactly, or if they deviate within stated bounds (Spearman and Kendall ≤ 1e-3,
    MARE ≤ 0.04, the rest exact), the lab's near-tie sets are exactly the two listed pairs, and
    the CSV value is reachable by reordering only those pairs. In the container it takes the
    deviation branch: +0.000168, +0.000404, −0.02.

**How the numbers were read:**
- `ATOMIC_REQUESTS` is not set anywhere, so Django's default `False` applies (CLAUDE.md and R17
  record the rule for views).
- The fixture adapter: 123 reviews in the database, and `build_input` passes all 123 (one marked
  as the duplicate's).
  - Under `exclude`, the duplicate policy leaves 122 and the fit uses 119.
  - Under `merge`, it leaves 123 and the fit uses 120.
- The database input is ordered by creation, and the importer creates projects oldest submission
  first. The file is ordered by its list. So under R10, equal scores (e.g. prj_09 and prj_17 under
  M2) can hold their ranks in the opposite order in the two sources. The "database equals file"
  test therefore requires equal scores, tie groups, λ and leans, and equal ranks across distinct
  scores. Within a set of equal scores it requires only the same set of ranks.

**Tests** (`tests/test_scoring_services.py`, 34 tests; every `compute_snapshot` test uses
`transaction=True`):
- **The gate:** final refused (409) in every phase through `judging_ends_at − 1s`; allowed at and
  after the boundary; preview allowed in all phases.
- **Roles:** a participant and a judge get 403 for both kinds in all phases, with an audit row
  each; the organizer of another event gets 403; an admin is allowed.
- **The config lock:**
  - a final with a method, config or weights override is refused, and so is `score_event --save
    final` with `--method`, `--compare`, `--config` or `--weights`
  - preview accepts overrides in every phase
  - `set_engine_config` succeeds before close, is refused at and after the close, validates its
    input and checks permission
  - a final uses the event's config
- **Immutability:** `save()` raises; `update()` is refused by the database trigger; the event
  cascade works; deleting a user who created a snapshot raises `ProtectedError`.
- **Publication:**
  - preview refused; another event's final refused
  - one active publication per event; append-only (only unpublishing, once)
  - `RestrictedError` when a published snapshot is deleted alone; deleting the event succeeds
  - `ProtectedError` when the publisher is deleted
- **The adapter:** 123 / 122 / 119 and 123 / 123 / 120; weights from `Criterion` (plus the
  preview override); a withdrawn project's reviews are listed; the database result equals the
  file result; the hash is stable and changes when a score changes.
- **The transaction:** `SHOW transaction_isolation` inside the block reads `repeatable read`; a
  call inside another transaction raises `SnapshotInsideTransaction`.
- **Several finals:** the first has no previous final and `false`; the second is `false`; after
  a score change the third is `true`. All three share one `created_at`, so the id breaks the tie.
- **Stored config:** the resolved seed equals `seed_for(slug)`, float λ per component, the
  rubric, the email.
- **Commands:** `score_methods` lists the registry; `score_event` without `--save` writes
  nothing; `--save final --as` works; `--save` without `--as` is refused.

**Docs:**
- DATA-MODEL.md: the three tables, and a note that actor emails in immutable result rows cannot be
  erased, as a deliberate audit trade-off.
- CLAUDE.md: the scoring-results rules (the transaction rule, `non_atomic_requests`, the test
  marker).
- The plan: R17, R18 and R21 amendments, and `docs/t2-scoring-plan.md` refreshed.

**Verification:** see the S3 summary.

### After S3
- `score_event` names every project "name (fixture id)" where the importer recorded one, else
  "name (#pk)". The folded duplicate is "kept name, duplicate (prj_41)". Judges are shown by
  email and tracks by name, including in the exclusion, flag and mover lines. No bare database id
  is printed. The engine CLI, which reads files, is unchanged because its ids are the file's own.
- For the final docs pass (also in the plan's S4 list):
  - **The exact-tie policy.** An ordinal rank breaks exact ties by input order; in the database
    that is creation order, the earliest submission first. That is the stated tie-break, and the
    ordinal rank is internal. Public results must show **shared** ranks for exact ties,
    competition style: =12, =12, 14.
  - **Operator note.** Accounts referenced by snapshots or publications cannot be deleted
    (PROTECT). Deactivate them instead (`is_active=False`).

## S4: docs (deferred)
Deferred by the user. The docs are written once, in the final docs pass, against the complete T2
system. See the S4 list in `docs/t2-scoring-plan.md`.
