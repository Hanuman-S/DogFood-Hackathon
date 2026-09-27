# T2 scoring engine: full plan (v3)

The first step after approval copies this file verbatim to `docs/t2-scoring-plan.md`. Plan mode
blocks writing anywhere else until then.

## 0. Context

T2 needs a scoring and ranking engine. The goal is experimentation: a new method should be one new
file, with nothing else touched. This plan builds only the engine and its persistence. It does not
build assignment, score entry, judging-window enforcement for score writes, isolation, result
pages, publishing UI or services, CSV export, the proof page, or seed data.

The lab (`../scoring-lab/`) is a reference only. Code is copied from it into the portal, never
imported from it.

## 1. Repo history: what `c6f381c` ("Remodel of website") deleted

That commit deleted 79 files (12,875 lines). Thirteen came back later under the same path:

- CLAUDE.md, DATA-MODEL.md, JUDGING.md
- acceptance-report-offline.txt, docker-compose.offline.yml
- scripts/acceptance.sh, scripts/offline-check.sh, scripts/test.sh
- src/scoring/apps.py, src/scoring/models.py, src/scoring/migrations/0001_initial.py
- tests/test_acceptance_contract.py, tests/test_db_constraints.py

The other 66 are still gone.

- **Docs:** PLAN.md (724 lines).
- **core:** `clock.py`, `permissions.py`, `errors.py`, `guards.py`, `authentication.py`,
  `admin.py`, `admin_site.py`, `context_processors.py`.
- **Whole apps and modules:**
  - `src/api/*`
  - `src/gallery/*`
  - `src/seed/*` (importer, upsert, `import_fixtures`, `seed_demo`)
  - `events/urls.py` and `events/views.py`
  - `projects/{urls,views,search,validators}.py` and projects migrations 0002 and 0003
  - `teams/{urls,views}.py`
- **Static files:** `portal.css`, the vendored `htmx.min.js`, `pico.min.css` and their NOTICE.
- **Templates (17):** `_form.html`, `home.html`, and the `accounts/*`, `events/*`, `gallery/*`,
  `projects/*` and `teams/*` templates.
- **Tests (14 files, about 4,900 lines):**
  - test_api_gallery, test_api_submit, test_audit_trail, test_auth, test_bearer_auth
  - test_clock, test_http_permissions, test_permissions, test_import_fixtures, test_invites
  - test_project_uploads, test_seed_demo, test_smoke
  - the shared `tests/factories.py`

Most of these were replaced by the portal-v2 base. For example, `imports/fixtures.py` replaces
`seed/importer.py`; `test_imports`, `test_login`, `test_sessions_and_tokens` and
`test_projects_api` exist now; and `core/deadlines.db_now()` replaces the clock module. I have not
checked the old test files one by one to see whether every behaviour they covered still has a test.
That audit is worth doing, but it is outside T2 and I will not do it unless you ask.

Consequences for this plan:
- S1 starts a new `PLAN.md`. The old one is available with `git show 4a233b1:PLAN.md`.
- S1 updates CLAUDE.md to describe the current conventions:
  - the clock is `core.deadlines.db_now()`
  - permissions are `accounts/roles.py` (`is_organizer_of`, `can_compete_in`, `PORTAL_ACCESS`),
    `accounts.guards.portal_required`, and `events.services.get_managed_event`, which returns 404
    to non-managers
  - no reference to `core/clock.py` or `core/permissions.py` remains
- S1 also adds the new `scoring/engine` rules to CLAUDE.md: the purity rules and "one file per
  method".

## 2. Decisions (final)

- **R1. Clock and permissions.**
  - `db_now()` is the only clock.
  - Every scoring service checks `is_organizer_of(actor, event)` first. It covers platform admins.
    A refusal raises `PermissionDenied` (403).
  - The permission check comes before the window check, so a judge or participant gets 403 in
    every phase.
- **R2. CV seed.**
  - `EngineConfig.cv_seed=None` resolves to `int(sha256(event.slug).hexdigest()[:8], 16)`. The
    engine CLI uses the file's `event.id` in place of the slug.
  - A single component uses `default_rng(seed)`, which is what keeps the lab's folds. When there
    are several components, component k uses `default_rng([seed, k])`.
  - Snapshots store the **resolved** config: the seed plus the chosen λ for each component, never
    `None`.
  - The golden tests pass `cv_seed=20260926`, the lab's `MASTER_SEED`.
- **R3. Synthetic goldens.**
  - (a) Copy `syn_*.json`, the `syn_*_truth.csv` files and the `synthetic_events_eval.csv` rows
    for raw, z and M2 into `tests/golden/`. Port `lab/metrics.truth_metrics` into the test, and
    require Spearman, Kendall, top-1, top-5, MARE and track winner to match to 6 dp.
  - (b) Generate per-project expected ranks, scores, SEs, tie groups and λ **once**, by running the
    lab's own code in its `.venv` from a scratchpad script. The script text and a README ("how
    this was made, lab commit or hash") are committed under `tests/golden/`. The lab is never
    modified.
  - The fixtures golden comes straight from the lab's `rankings_fixtures.csv`,
    `m2_cv_fixtures.csv`, `m2_judges_fixtures.csv`, `rank_change_fixtures.csv` and
    `run_meta.json`.
- **R4. Duplicates.** `EngineConfig.duplicate_policy` is one of two values:
  - `"exclude"` (the default, and the lab's rule): drop the duplicate project and all of its
    reviews.
  - `"merge"`, **our own rule and not the lab's**: move the duplicate's reviews to the kept
    project, and where a judge reviewed both, keep the kept project's review. The lab's
    sensitivity "merge" **averages** the two reviews instead. No lab file holds merge output, so
    there is no golden for `merge`; its tests check counts and which review survives.

  The fixture event has 123 reviews in the database. `build_input` recognises jdg_18's review of
  `prj_41` (folded into `prj_07` by the importer) through `FixtureRef(kind="score",
  external_id="jdg_18:prj_41")` and the project ref's `duplicate_of`. It passes that review to the
  engine marked with its original duplicate id. Under `exclude`, the input holds 122 reviews and
  the fit uses 119 after the flat judge. Under `merge`, it keeps 123 and uses 120.
- **R5. Outlier flag** (new; the lab only recommends one).
  - The residual is studentized: `r*_i = r_i / sqrt(σ̂²·(1 − h_ii))`, with `h_ii = a_iᵀ M⁻¹ a_i`.
    That is the same sum as the one `trH` already uses, per review.
  - A review is flagged when `|r*| > outlier_k`, which defaults to 2. The flag goes on the review
    and is listed on its project and its judge.
  - Components with fewer than `outlier_min_reviews` reviews (default 10) are skipped with a note.
  - Methods that don't expose fitted values and `h_ii` (capability `"fitted"`) skip it with a note.
  - JUDGING.md says this is a heuristic that the study did not validate.
- **R6. Components.**
  - The judge–project graph is split with union-find, labelled largest component first.
  - Each component is fitted separately.
  - A component with fewer than `cv_min_reviews` reviews (default 20) uses λ = (0.5, 1.0), and
    the reason is recorded in diagnostics.
  - Projects with 0 reviews are excluded as `("project", id, "no reviews")`.
  - There is an overall rank only if there is exactly one component. Otherwise ranks are given per
    component, and diagnostics say why.
- **R7. Where duplicates are detected.** Duplicate detection happens in the input builders and
  produces `EngineInput.duplicates: {dup_id: kept_id}`.
  - The file loader uses the lab rule: union-find over projects linked by the same team or by the
    same `(title.strip().lower(), repo_url)`. The project kept is the one first by
    `(submitted_at, id)`.
  - `build_input` uses `FixtureRef.duplicate_of`.
  - T1's "possible duplicate" flags are deliberately **not** used.
- **R8. Repeated reviews.** The file loader averages repeated (judge, project) records, criterion
  by criterion, and logs it. The engine rejects repeats with `EngineInputError`.
- **R9. Near-flat judges.** The `near_flat_judges` flagger marks judges with var(y) < 0.1 and
  n ≥ 2 who are not flat. It never excludes anyone.
- **R10. Ranks and exact ties.**
  - `rank` is ordinal: 1 is best, ties are broken by input order, and NaN ranks last.
  - For methods without uncertainty, scores count as equal when they match after `round(9)`.
  - Every output that shows a rank also shows the tie group, and the CLI table marks tied rows.
  - *(Amended during S2: user decision B.)* Ranks are computed on scores **rounded to
    `equal_decimals` (9)**, with ties broken by input order, for **every** method. The lab ranked
    raw floats, so its order inside "equal" scores was last-bit float noise. Movers with equal
    move sizes are also in input order; that is what the lab's table order amounted to.
- **R19. The lab's arithmetic** *(amended during S2: user decision A).*
  - Rubric weights are normalised to sum to 1 before a review's criteria are averaged, which is
    the lab's arithmetic (1/3 each). The value is identical in exact arithmetic, and the floats
    now match the lab's.
  - The z-score floor is tolerance-aware: a judge counts as floored only if their SD is below
    `z_floor − 1e-9`. Fixture judge jdg_03, whose SD is exactly 0.5 on paper, is not floored,
    which matches the lab.
- **R20. The golden comparison rule** *(amended during S2).* It defines correctness and is
  written, with its reasons, in `tests/golden/README.md`:
  - numeric tolerances unchanged
  - ranks compared across lab-distinct scores (differing by more than 1e-9); within lab-equal
    sets, only the set of ranks
  - P(ahead) compared per unordered pair
  - movers compared as a set, plus each one's split
  - raw and z accuracy metrics tie-aware against the lab's own scores; M2's against the CSV as
    printed
  - The one known deviation is M2's accuracy on `syn_medium`, caused only by two near-tie pairs.
    It is reported, a strict `xfail`, and pinned by a test. It is not patched.
- **R11. Explain.** The decomposition exists only against `raw_mean`. Other baselines get rank
  changes only, with a note.
- **R12. Actor for `--save`.** `score_event --save` requires `--as <email>`, and that account is
  permission-checked like everyone else.
- **R13. Snapshot immutability.**
  - `ResultSnapshot` has **no `is_published` field**.
  - `save()` refuses updates, and a Postgres `BEFORE UPDATE` trigger rejects every UPDATE.
    DELETE is still allowed, so the snapshot cascades with its event.
  - The admin is read-only: it offers no add, change or delete.
  - *(Amended before S3.)* `created_by` is `on_delete=PROTECT`, not `SET_NULL`. SET_NULL would
    issue an UPDATE, which the trigger rejects.
    - T1's `AuditLog.actor` uses `SET_NULL` plus a denormalised `actor_email`. That table has no
      immutability trigger. We can't copy the SET_NULL part, so we match the other half: the
      snapshot also stores `created_by_email`.
    - Deleting a user who created a snapshot raises Django's `ProtectedError`, not a trigger
      error.
  - The snapshot stores its `rubric` (criterion ids, weights, min, max) next to `input_hash`, so
    the weights a result was computed with are on record (see Future work).
- **R14. Publication** (model, migration and constraints only; no service or UI yet).
  - Fields: `Publication(event FK CASCADE, snapshot FK RESTRICT, published_at, published_by FK
    user PROTECT, published_by_email, unpublished_at null, unpublished_by FK user PROTECT null,
    unpublished_by_email)`.
  - *(Amended before S3.)*
    - `snapshot` is `RESTRICT`, not `PROTECT`. PROTECT would block the event cascade; RESTRICT
      lets it through while still refusing to delete a published snapshot on its own.
    - The user FKs are `PROTECT`, for the same reason as R13.
  - It is append-only, enforced by a Postgres trigger:
    - On INSERT, `snapshot.kind` must be `'final'`, and `snapshot.event_id` must equal `event_id`.
    - On UPDATE, the only allowed change is setting `unpublished_at` and `unpublished_by` from
      NULL, once. Every other change is rejected.
  - There is also a partial unique constraint: one publication per event where
    `unpublished_at IS NULL`.
  - On SQLite, `Publication.clean()` performs the same checks, following the existing
    `RunPython` pattern.
- **R15. Config lock.**
  - `kind="preview"` accepts any method or config override, in any phase.
  - `kind="final"` always uses the event's `EventScoringConfig`, or the defaults when there is
    none. Any `method=` or `config=` override is refused with `FinalOverrideRefused` (400
    `final_uses_event_config`). `score_event --save final` with `--method`, `--compare` or
    `--config` gives the same refusal.
  - *(Amended at the start of S2.)* A **weights** override counts as an override too.
    - A final always uses the event's `Criterion` weights.
    - `compute_snapshot(..., weights=...)` is allowed for previews only.
    - `score_event --save final --weights ...` is refused with `final_uses_event_config`, the
      same as `--method` and `--config`.
    - S3 tests cover all three flags.
  - `set_engine_config(actor, event, overrides)` runs its checks in this order:
    1. permission (403)
    2. `judging_closed(event, db_now())`, which refuses with 409 `scoring_config_locked`
    3. `EngineConfig.from_dict` validation (400 `invalid_config`)
    4. save and audit (`scoring_config_changed`)
- **R16. Excluded reviews in `build_input`** *(amended before S3).* A review of a project that
  is not in `projects` (withdrawn or unsubmitted) is never dropped silently.
  - `build_input` lists it as `("review", "<judge>:<project>", "project not submitted")`.
  - It gets there through a new field, `EngineInput.excluded`, which the pipeline copies into
    `EngineResult.excluded`. The field is added in S2 with the other type changes.
- **R17. One transaction at REPEATABLE READ** *(amended before S3).*
  - In `compute_snapshot`, `build_input`, the engine run and the create all happen inside one
    `transaction.atomic()` whose first statement is
    `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ` (Postgres only).
  - That statement is valid only as the first one in a transaction. So when `compute_snapshot` is
    called inside an outer atomic block, it raises instead of silently running at a weaker level.
    The test that checks the level uses `transaction=True` and reads `SHOW transaction_isolation`
    inside the block.
  - Refusal audits stay outside the transaction.
- **R18. Several finals per event are allowed** *(amended before S3).*
  - Each final stores, in `ResultSnapshot.diagnostics`:
    - `previous_final`: `{id, created_at, input_hash}`, or null for the first final
    - `scores_changed_since_last_final`: `input_hash` differs from the previous final's; `false`
      for the first final
  - Tests cover both cases: two finals on unchanged data give false, and a changed `ScoreItem`
    in between gives true.
- **Worked example.** Asserts the computed SD(Δ) for P1 vs P3, which is 0.584. The PDF prints
  0.59; that typo appears only as a comment.
- **numpy.** Pin 2.5.3 if a cp312 wheel exists, since the image is Python 3.12. If it doesn't,
  stop and report.
- **Purity test** (AST over `scoring/engine/**`). It rejects:
  - `django` imports
  - the `random` module
  - `datetime.now`, `datetime.utcnow`, `date.today`, `time.time` and `time.monotonic`
  - any `np.random.*` except `np.random.default_rng(<argument>)` with an explicit argument

## 3. Lab maths ported exactly (summary)

- **Response.** `y = Σ w_c s_c / Σ_{present} w_c`. The weights are `Criterion.weight` as relative
  floats. The fixture's weights are all 1, which is the lab's `EQUAL`.
- **M2.** The design columns are `[μ | q_p | b_j]`, and `Λ = diag(0, λ_q, λ_b)`, so μ is not
  penalised.
  - Solve `(AᵀA+Λ)x = Aᵀy` with `inv`, or `pinv` if any λ ≤ 0.
  - `σ̂² = max(RSS/(N − trH), 0.05)`.
  - The covariance is `C = σ̂² M⁻¹`.
  - `SE_p = √(C₀₀ + C_pp + 2C_0p)`.
  - `P(ahead) = Φ(Δ/√max(Var, 1e-300))`, where Var uses the q block of the covariance. Φ is
    computed with `math.erf`.
- **CV.**
  - `array_split(default_rng(seed).permutation(N), 5)` over reviews, **in input order**.
  - The grid is {0.25, 0.5, 1, 2, 4}², scored by MSE.
  - Ties go to `min(round(err, 12), −λ_b, −λ_q)`.
  - `lambda_at_grid_boundary` is true if either chosen λ is the grid minimum or maximum.
- **Tie chaining.** Order by −S with a stable sort. Walk adjacent pairs, and start a new group
  when P(ahead) ≥ 0.84.
- **Flat judge.** n ≥ 2 and a single value across every criterion of every review. Such a judge
  is excluded before every method.
- **z-score.** Population SD per judge, floored at 0.5. Average z per project.
- **Raw mean.** The mean of y per project.
- **Decomposition.** `raw − S = mean(b_j) + mean(residual)` over the project's reviews, using the
  uncentred b. The label is "judge-lean correction" if |lean| > |shrinkage|, otherwise
  "shrinkage".

## 4. Layout

```
src/scoring/engine/                  pure: no django, no clock, seeded RNG only
  types.py        Review(judge_id, project_id, items, track_id), Rubric, EngineInput(event_id,
                  reviews, rubric, projects, duplicates, config), ProjectResult(project_id,
                  score, se, rank, tie_group, flags, extras), Exclusion(kind,id,reason),
                  EngineResult, ComparisonResult; frozen; to_json(): fixed order, NaN->null
  config.py       EngineConfig + from_dict (unknown key -> ConfigError) + resolved()
  prepare.py      EngineInput -> PreparedData (pi, ji, y arrays in input order)
  methods/__init__.py   pkgutil autoload of every module in the package
  methods/base.py       ScoringMethod Protocol, MethodOutput(scores, se, cov_q, judge_bias,
                        fitted, hat, params, diagnostics)
  methods/registry.py   @register, get(name) -> UnknownMethod, available()
  methods/raw_mean.py   caps set()
  methods/zscore.py     caps set()
  methods/m2_ridge.py   caps {"uncertainty","judge_bias","decomposition","fitted"}
  filters.py      exclude_flat_judges, exclude_duplicate_submissions(policy)
  components.py   union-find
  ties.py         se_chain(threshold) | exact(round 9)
  flaggers.py     insufficient_reviews(<2), near_flat_judges, outlier_residuals
  explain.py      vs raw_mean only
  coverage.py     reviews/project min/median/max, zero and <2, reviews/judge,
                  note "assignments unknown: cannot tell assigned-but-unscored"
  pipeline.py     run(input, method, config), compare(input, methods, baseline="raw_mean")
  io.py           load_organizer_file(path) -> EngineInput (lab duplicate + repeat rules)
  cli.py          python -m scoring.engine.cli FILE [--method] [--compare] [--config] [--json]
src/scoring/
  errors.py       JudgingOpen(409 judging_open), ScoringConfigLocked(409 scoring_config_locked),
                  FinalOverrideRefused(400 final_uses_event_config), InvalidConfig(400)
  services.py     build_input, judging_closed, compute_snapshot, set_engine_config
  models.py       + EventScoringConfig(event 1:1, overrides JSON, updated_by, updated_at)
                  + ResultSnapshot(event, kind, created_at, created_by, method, method_version,
                    engine_config, input_hash, result, comparison)   [immutable]
                  + Publication(...)                                  [append-only]
  admin.py        read-only snapshot and publication admins
  migrations/0002_scoring_engine.py, 0003_snapshot_publication_triggers.py (RunPython, pg only)
  management/commands/score_event.py, score_methods.py
src/core/models.py  AuditAction += snapshot_created, snapshot_refused,
                    scoring_config_changed, scoring_config_refused
tests/golden/       fixtures + syn_* inputs, expected JSON, truth CSVs, generator script, README
```

**Pipeline order:**
1. validate
2. filters
3. components
4. for each component: fit, then ties
5. rank (overall if there is one component, otherwise per component)
6. flaggers
7. coverage
8. explain
9. `EngineResult`

**Determinism:**
- `OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1` are set in the Dockerfile and at the top of
  `cli.py`.
- The JSON output is byte-stable: fixed field order and full-precision `repr` floats.

## 5. Phases

Each phase ends the same way: tests, `docker compose down -v && up --build`, `acceptance.sh` (T1
must stay green), a `PLAN.md` entry, one commit, a short summary, and then I stop for your
go-ahead.

### S1: engine core
- **Build:**
  - `types`, `config`, `registry` with autoload, `prepare`
  - the `raw_mean` and `zscore` methods
  - exact-equality `ties`, `coverage`
  - `pipeline.run` and `compare`
  - `io` and `cli`
  - the `score_methods`-equivalent listing in the CLI (`--list`)
  - the numpy pin and the BLAS env
- **Docs:** a new `PLAN.md`, and the CLAUDE.md update (§1).
- **Tests:**
  - contract, over every registered method:
    - every non-excluded project appears exactly once
    - ranks are 1..n within their scope and agree with the scores
    - tie groups are contiguous in rank order
    - methods without "uncertainty" report no SE
  - plugin: a dummy method registered inside the test goes through `run`, `compare` and the CLI
  - determinism: two runs give identical bytes
  - purity: the AST test
  - errors: an unknown method, an unknown config key
  - edge inputs: an empty event, one project, a project with 0 reviews, all-tied input
  - zscore: a zero-variance judge and a single-review judge both get the floor and z = 0
  - the lab's baseline tests

### S2: M2 and the analysis
- **Build:**
  - `m2_ridge`: solve, CV, σ² floor, SE, P(ahead), `h_ii`, `lambda_at_grid_boundary`
  - both filters, with `duplicate_policy`
  - per-component fitting, with the `cv_min_reviews` fallback
  - SE tie chaining
  - flaggers: `insufficient_reviews`, `near_flat_judges`, `outlier_residuals`
  - `explain`
- **S2 requirements from the user's go-ahead (binding):**
  1. `EngineConfig`'s default primary becomes `"m2"`, and a test asserts it. The S1
     `NotImplementedError` path for "uncertainty" is removed completely, along with S1's
     "not computed yet" notes.
  2. Goldens are the referee. Expected values come only from the lab's code (run in its `.venv`)
     or from its existing result files. On a mismatch I **stop and report the diff**. Tolerances,
     formulas and expected values are never adjusted to get green. The tolerances are fixed now:
     - lab CSV values printed at 6 dp: `|ours − csv| ≤ 5.01e-7`
     - `rank_change` parts printed at 4 dp: `≤ 5.01e-5`
     - golden (b) full-precision JSON: scores, SEs, σ̂², trH and CV MSE within `1e-8` absolute
     - ranks, tie groups, λ, counts and ids must match exactly
  3. The S2 summary pastes the CLI output on `acceptance/fixtures.json`, once with the defaults
     and once with `--config duplicate_policy=merge`. The defaults must show:
     - 40 projects ranked
     - 119 reviews used
     - the exclusions listed
     - λ = (4, 4) and `lambda_at_grid_boundary` true
     - one tie group of 40

     The merge run must show 120 reviews used.
  4. The colluder test reports the false-positive count as well as the hit (see test 5).
  5. The S3 weights-override amendment is in R15 above, and is also applied to
     `docs/t2-scoring-plan.md`, marked as an amendment.
- **Design details decided for S2:**
  - **Filter order** follows the lab: `exclude_duplicate_submissions`, then `exclude_flat_judges`.
    The lab resolves duplicates in `load()` before it detects flat judges.
  - **Merge policy.** See R4: this is our rule, not the lab's. Each moved review keeps its
    position in the input.
  - **Seeds.** `cv_seed=None` resolves to `int(sha256(event_id).hexdigest()[:8], 16)`. The engine
    puts `rng_seed` on `PreparedData`: `seed` for a single component, `(seed, k)` when there are
    several. M2 calls `default_rng(data.rng_seed)`. `EngineResult.config` carries the resolved
    integer seed.
  - **New `EngineConfig` keys:** `filters`, `flaggers`, `duplicate_policy`, `lambda_grid`,
    `cv_folds`, `cv_seed`, `cv_min_reviews`, `lambdas` (fixed λ that bypasses CV, each > 0),
    `sigma2_floor`, `outlier_k`, `outlier_min_reviews`, `near_flat_threshold` (0.1). Filter and
    flagger names are checked against their registries.
  - **Contract additions.**
    - `MethodOutput.sigma2` is required with "fitted".
    - `ProjectResult` gains `component`. With several components, `rank` and `tie_group` are
      per component.
    - `EngineResult` gains `judges` (id, n_reviews, bias, bias_centred, flags) and `flags`
      (flagged reviews, and notes on flaggers that were skipped).
    - `params_chosen` is `{"components": [{component, lam_q, lam_b, lambda_source, cv_seed, ...}]}`.
    - The contract test checks ties per component, and SE/P(ahead) for "uncertainty" methods.
  - **Explain.** It is computed in `run()` for "decomposition" methods, against the raw mean of
    the same filtered reviews. `ProjectResult.extras` holds `raw_mean`, `lean_part`,
    `shrinkage_part`, `explanation` and `p_ahead_of_next`. `explain.movers(comparison, k=8)`
    reproduces the lab's selection: order by the method's rank, then stable-sort by
    |raw rank − rank|.
  - **CLI.**
    - `--config` accepts a JSON file, or inline `key=value[,key=value]` when no such file exists.
      Values are parsed as JSON, and fall back to plain strings.
    - It prints the λ per component, `lambda_at_grid_boundary`, reviews in and reviews used, the
      exclusions, the flags and the top 8 movers.
  - **Golden files** go in `tests/golden/`, copied from `../scoring-lab`:
    - `rankings_fixtures.csv`, `m2_cv_fixtures.csv`, `m2_judges_fixtures.csv`,
      `rank_change_fixtures.csv`, `run_meta.json`
    - `syn_{small,medium,large}.json` and `*_truth.csv`
    - `synthetic_events_eval.csv`
    - `expected/syn_*.lab.json`, generated by `generate_expected.py`, which is run once with the
      lab's `.venv` Python from the lab root. It uses the lab's `load()` and
      `registry.run(m, d, lam=None, seed=20260926)` for raw, z and M2, and writes to the
      scratchpad before the output is copied in.
    - `README.md`, with the lab commit (`ff3057d`) and the sha256 of every copied file.

    Golden (a) ports `truth_metrics` with a numpy Spearman (Pearson on average ranks) and
    Kendall tau-b. The port is first checked against the lab's own golden-(b) scores, which must
    reproduce `synthetic_events_eval.csv`, so a bad metric port can't be mistaken for an engine
    bug.
- **Tests:**
  1. Every lab M2 test ported:
     - the worked example: leans; scores and SEs; trH, RSS and σ̂²; the P(ahead) table asserting
       0.584; one tie group; the flat judge excluded and the single-review judge kept, N = 10
     - the tiny illustration, both halves
     - the §6 shrinkage table
     - a constant shift on one judge
     - zero λ matching OLS
     - a single 5 pulled towards the mean
     - a disconnected graph detected
     - CV picking from the grid
     - the fixtures loading and fitting in under 10 s (relaxed from the lab's 1 s, because
       Docker Desktop on Windows is noisy)
  2. Fixtures golden (seed 20260926):
     - 40 projects and 119 reviews
     - λ = (4, 4), with `lambda_at_grid_boundary` true
     - scores, SEs and every method's ranks match `rankings_fixtures.csv` to 6 dp
     - the CV table matches `m2_cv_fixtures.csv`
     - leans match `m2_judges_fixtures.csv`
     - the movers match `rank_change_fixtures.csv`
     - one tie group of 40
  3. The synthetic golden (a): metrics against truth for raw, z and M2 on all three sets.
  4. The synthetic golden (b): per-project ranks, scores, SEs, tie groups and λ on all three sets.
  5. Colluder:
     - Take a synthetic input: `tests/golden/syn_small.json` under the default filters.
     - Pick a colluder deterministically: the first judge, in file order, who has at least 3
       reviews, is not the flat judge and is not the lab's own injected colluder. On one project
       where every criterion they gave is ≤ 3, add +2 to each criterion, so nothing is clipped.
     - Assert `outlier_residuals` flags that review.
     - Assert the clean run does not flag it.
     - Record the number of **other** reviews flagged on the same input, both clean and injected.
       The test prints the numbers and they go in the S2 summary. There is no pass/fail bound on
       them, because none was agreed.
     - Also report whether `outlier_residuals` flags **the lab's own injected colluder** in
       `syn_small`: the target review named in `syn_small_judges_truth.csv`'s role column, with
       +1.5 on the target. Report the false-positive count on each input: clean `syn_small`,
       `syn_small` with our +2 injected, and the fixtures.
     - `outlier_k` stays at 2 and is not tuned. The user expects about 5% false positives at k = 2
       and will decide later.
  6. `outlier_residuals` below `outlier_min_reviews` is skipped with a note. For a method without
     "fitted", it is also skipped with a note.
  7. `duplicate_policy`:
     - `exclude` drops `prj_41` and its 4 reviews
     - `merge` keeps jdg_18's review on `prj_07`, and for the three judges who reviewed both it
       keeps their `prj_07` review
     - both on the fixture file
  8. Components: two disconnected tracks give two components, ranks per component, no overall
     rank, and the reason in diagnostics. The seeds are `[seed, k]`.
  9. `cv_min_reviews`: a component below 20 uses (0.5, 1.0) with the reason recorded. A component
     at or above 20 runs CV.
  10. `lambda_at_grid_boundary`: true for the fixtures (4, 4), and false for a case where CV picks
      an interior λ.
  11. SE tie chaining:
      - a hand-built case where P(ahead) crosses 0.84, which splits the groups
      - chaining is transitive across adjacent pairs
      - the threshold comes from config
  12. The flat judge is excluded and listed in `excluded`. A single-review judge is kept, and
      their lean is shrunk.
  13. `near_flat_judges` flags jdg_05, jdg_17, jdg_18 and jdg_28 on the fixtures.
  14. The default primary is `"m2"`.
  15. The CLI end to end: defaults give 40 ranked, 119 used and λ (4, 4). `--config
      duplicate_policy=merge` gives 120 used. Inline `--config` parsing is tested.
- **If a golden does not match:** stop, report the diff, and commit nothing that hides it.
- **After S2:**
  - tests
  - `docker compose down -v && up --build`
  - `acceptance.sh`: T1 stays green
  - a `PLAN.md` entry
  - this plan file copied over `docs/t2-scoring-plan.md`, so the docs copy carries every
    amendment: R4's wording, R13, R14 and R15's weights, R16–R18, the S3 test changes, and
    Future work. Amended items are marked "(amended …)".
  - one commit
  - a summary with both CLI outputs and the colluder numbers
  - then stop

### S3: adapter, persistence, gate
- **Build:**
  - `build_input(event)`:
    - reviews ordered by `Score.pk`
    - the rubric from `Criterion`, with keys and float weights
    - `projects` holds `{str(pk): track}` for the event's submitted projects
    - duplicates from `FixtureRef`, with the folded review marked (R4)
    - reviews of projects that are not submitted go into `excluded`, per R16
    - `event_id` set to the slug, for the seed
  - `judging_closed(event, now)`, which is `now >= event.judging_ends_at`. It is the only place
    that compares against `judging_ends_at`.
  - `compute_snapshot(event, kind, method=None, config=None, weights=None, actor)`:
    1. Permission.
    2. The final-override refusal (R15): `method`, `config` or `weights` with `kind="final"`.
    3. For `final`, `judging_closed(event, db_now())`, else `JudgingOpen`.
    4. Resolve the config: the event's config, or preview overrides on top of it.
    5. Open one `transaction.atomic()` at REPEATABLE READ (R17). Inside it:
       - `build_input`
       - `pipeline.compare`
       - for a final, look up the previous final (R18)
       - create the `ResultSnapshot` with `engine_config` fully resolved (seed and λ per
         component), `input_hash = sha256(canonical input JSON)`, `rubric`, and `diagnostics`
    6. Audit `snapshot_created`.

    Refusals are audited as `snapshot_refused`, outside the transaction.
  - `set_engine_config`, with the R15 order and audit.
  - Models and migrations for `EventScoringConfig`, `ResultSnapshot` and `Publication`, with the
    triggers from R13 and R14 (Postgres only, as `RunPython` no-ops on SQLite).
  - The read-only admins.
  - `score_event <slug> [--method] [--compare] [--config] [--save preview|final --as EMAIL]`:
    - prints rank, tie group (tied rows marked), score ± SE, raw rank, change, flags, and
      coverage
    - `--save` goes through `compute_snapshot`
  - `score_methods`: name, version, capabilities.
- **Tests:**
  1. Gate:
     - final is refused (409 `judging_open`) at `judging_ends_at − 1s`, and at times inside the
       upcoming, open and judging phases
     - final is allowed exactly **at** `judging_ends_at` and after it
     - preview is allowed in all four phases
     - `now` is controlled by patching `db_now`
  2. Roles:
     - a participant and a judge of the event each get `PermissionDenied` (403) for preview and
       for final
     - an organizer of another event gets 403
     - a platform admin is allowed
     - each refusal writes an audit row
  3. Config lock:
     - `kind="final"` with a method, config or weights override is refused with
       `final_uses_event_config`, and so are `score_event --save final` with `--method`,
       `--config` or `--weights`
     - preview accepts overrides in every phase
     - `set_engine_config` succeeds before `judging_ends_at` and is audited
       (`scoring_config_changed`)
     - at and after `judging_ends_at` it is refused with 409 `scoring_config_locked`
     - an unknown key in the overrides gives `invalid_config`
  4. Immutability:
     - `snapshot.save()` after creation raises
     - `ResultSnapshot.objects.filter(pk=…).update(method="x")` is refused by the database
       trigger
     - deleting the event cascades and removes the snapshot
     - deleting a user who created a snapshot raises `ProtectedError`, not a trigger error
  5. Publication:
     - inserting one that points at a preview snapshot is refused by the database
     - inserting one that points at another event's final snapshot is refused
     - pointing at its own event's final snapshot succeeds
     - a second active publication for the event is refused
     - setting `unpublished_at` once succeeds; changing `snapshot_id` or `published_at` is refused
     - deleting an event that has a publication succeeds, and the cascade removes the
       publication and its snapshot
     - deleting a published snapshot on its own is refused (`RestrictedError`)
     - deleting a user named in `published_by` raises `ProtectedError`
  6. Adapter:
     - the fixture event has 123 `Score` rows
     - `build_input` gives 122 reviews under `exclude` and 123 under `merge`
     - the fit uses 119 under `exclude` and 120 under `merge`
     - `prj_41`'s review appears in `excluded` with its reason
     - weights come from `Criterion`
     - a review of a withdrawn (draft) project is listed as
       `("review", …, "project not submitted")` and is not in the input
  6b. Transaction:
     - the isolation level inside `compute_snapshot` is `repeatable read`, read with `SHOW` in a
       `transaction=True` test
     - calling it inside an outer atomic block raises, and never silently runs at a weaker level
  6c. Several finals:
     - two finals on unchanged data: the second has `previous_final` set and
       `scores_changed_since_last_final = false`
     - after a `ScoreItem` change it is `true`
     - the first final has `previous_final = null` and `false`
  7. The database result equals the file result: `build_input` on the imported fixture event with
     `cv_seed=20260926`, mapped through `FixtureRef`, matches the engine CLI output on
     `acceptance/fixtures.json`.
  8. Hash: `input_hash` is stable across two runs, and changes when one `ScoreItem.value` changes.
  9. The resolved config is stored: the snapshot's `engine_config` has an integer `cv_seed`
     (equal to the slug-derived value when none was set) and a λ pair per component, with no
     nulls.
  10. `score_methods` lists `raw_mean`, `zscore` and `m2`, with their versions and capabilities.
      `score_event` without `--save` writes nothing.

### S4: docs only
- A "Scoring engine" section in JUDGING.md:
  - how to add a method in one file
  - the methods available
  - preview vs final, the config lock, and why final waits for the window
  - Publication exists as a model only
  - coverage has limits without assignments
  - the study's summary and its limits:
    - the simulation partly favours M2's own model
    - the fixtures carry no signal, so the fixture event is one tie group
    - M2 raises a colluder's rank gain
  - *(Amended during S2.)* The outlier flag is **not** called a mitigation, or a defence
    against collusion. JUDGING.md states the measured results:
    - our +2 inflation is flagged
    - the lab's own +1.5 colluder is missed
    - about 4–6% of honest reviews are flagged at k = 2 (2.5% on the fixtures, which carry no
      signal)
    - it is a review aid, and `outlier_k` is untuned
  - *(Amended during S2.)* The determinism wording:
    - JSON is byte-identical on the same platform
    - ranks and tie groups are identical across platforms
    - full-precision scores may differ in the last bits
  - the app clock (`Event.phase`) vs the database clock (`judging_closed`), and that scoring
    consults only the latter
  - organizer-confirmed duplicates in live events are future work, and T1's "possible duplicate"
    flags are not used
- DATA-MODEL.md tables for `EventScoringConfig`, `ResultSnapshot` and `Publication`, with their
  triggers.
- No change to `.dogfood.toml`.

## Future work (not in T2's engine phases)

- **Rubric weights must lock at the first score** once rubric editing is built. Until then, each
  snapshot's stored `rubric` is the record of which weights produced it.
- **Organizer-confirmed duplicates** in live events. T1's "possible duplicate" flags are not used
  by the engine.
- **Publishing:** the service and UI on top of `Publication`.

## 6. Verification (every phase)

- `./scripts/test.sh`
- `docker compose down -v && docker compose up --build`
- `./scripts/acceptance.sh`: T1 passes, and T2 fails honestly
- S1 and later: `docker compose exec web python -m scoring.engine.cli /app/acceptance/fixtures.json --compare raw_mean,zscore`
- S3 and later:
  - `manage.py score_methods`
  - `manage.py score_event <fixture-slug> --save preview --as <organizer>`
  - `manage.py score_event <fixture-slug> --save final --as <organizer>`, which is allowed
    because judging ended on 2026-03-15
