# Golden files: the scoring lab is the referee

The engine in `src/scoring/engine/` is a port of the scoring lab (`../scoring-lab/`, at commit
`ff3057db002bb28ab8bf247d714515e856cbd34b`). The tests in `tests/test_engine_golden.py` check
that the port reproduces the lab. **Every expected value in this folder comes from the lab.**
Nothing here was computed by the portal, and no value may be edited to make a test pass.

The portal never imports the lab, at runtime or in tests. These files are the only things that
cross over.

## Where each file comes from

| file | origin |
|---|---|
| `rankings_fixtures.csv`, `m2_cv_fixtures.csv`, `m2_judges_fixtures.csv`, `rank_change_fixtures.csv`, `run_meta.json`, `synthetic_events_eval.csv` | copied unchanged from the lab's `results/` (its full run, `run_all.py`) |
| `syn_small.json`, `syn_medium.json`, `syn_large.json`, and their `_truth.csv` and `_judges_truth.csv` | copied unchanged from the lab's `data/synthetic/` |
| `expected/*.lab.json` | written by `generate_expected.py`, run once with the lab's own interpreter from outside the lab (`<lab>/.venv/Scripts/python.exe generate_expected.py <lab> <out>`; Python 3.14.7, numpy 2.5.3, scipy 1.18.1). It calls the lab's `lab.data.load` and `methods.registry.run(m, d, lam=None, seed=20260926)` for `raw`, `z` and `M2`, and records every score, rank, SE, tie group, λ, CV table and lean at full precision. It wrote nothing inside the lab; `git status` in the lab was unchanged. |

The fixture input is the repo's own `acceptance/fixtures.json`. It is byte-identical to the lab's
`reference/fixtures.json` (sha256 `252896bc…9121`).

sha256 of every file here:

```
d7cf151d4aad38376d4db2b349a296de1add86b3756f559a1df8781f34238b85  m2_cv_fixtures.csv
5d1e35f593bbf31b72e8489dd1ca882fd92b07fe9e7ad517d00757f50e9f3a26  m2_judges_fixtures.csv
e609108c432e241ded6d7a21fedd3a05294afa842c05d94438cb33ed23786cee  rank_change_fixtures.csv
52af154c745c2cd95dc416a0cb0d2a1a2c63f99014223372703f9278c25ef947  rankings_fixtures.csv
2fd579956fa4894fde5be635ac99eac39739701ad8b1d9b2974a4fcd00d7503e  syn_large_judges_truth.csv
658910c33eb25fa1b987eeb91aec6faea084bd18360195bc7fdbe89a8e94db7e  syn_large_truth.csv
92443d9fd863a4812975b86e0318d7e25c1ca5b4d24107820663eba2355ec1dd  syn_medium_judges_truth.csv
a7782e96522ca827d741b37bdcf7f8c5497ef7815d25bc29526061c1d9ced65d  syn_medium_truth.csv
2c92cb1141a08d1e7355d49a42112a499d1003019e4496eeed911064b3e32fc1  syn_small_judges_truth.csv
30c2d154c09bdae737b5a1955d57956e887d9add77618e623537f24f58292281  syn_small_truth.csv
7dadb0798f88ac1e82d00e1bb0e39ff8f4d5adeb2c8b334c7c8a17ba9de7a0db  synthetic_events_eval.csv
20b09d5cdf7c0a9668c6535a9ed241370a8270b47def6ae32a8741ad7b424741  run_meta.json
8a0f3f2d77dc513e84835f68ba1a5d7bffd96e254a9c5cd96dc77f81e17a8566  syn_large.json
9bc9b5296a6bec27d869f1d29d5a8983f07ccd30de67d28d118a2000c1f5c85d  syn_medium.json
0863a69d62404f980737e9a76ebee1faeea22d9347063fd12ccad9d4801c1b26  syn_small.json
46759491699b6430af3c4fc03e9b581eafe85c26d0699c2b0ebf6ad906cbc116  expected/fixtures.lab.json
92717765bdd778f36c30287d93b393328e01b5272099ef6901dfae7c1ca10e16  expected/syn_large.lab.json
f33da0c9a7dd7327835356d4810a59e62d0cf2b5cf60a24b18350fe022cdce73  expected/syn_medium.lab.json
02b082f08017036f4ff8bac0899bea221b239b81723da6504f892f346ac04d3c  expected/syn_small.lab.json
8fcca4481c29885cec3f01f969aa335357f86801b4241b18dcf0d2ea1f50b443  generate_expected.py
```

## The comparison rule

This rule defines what "matches the lab" means. It is not a loosened tolerance.

**Numbers: fixed tolerances, set before the first run.**

| values | allowed difference |
|---|---|
| printed by the lab at 6 dp | ≤ 5.01e-7 |
| printed by the lab at 4 dp | ≤ 5.01e-5 |
| from the full-precision lab JSON (scores, SEs, σ̂², tr H, RSS, μ, CV MSE, leans) | ≤ 1e-8 |
| λ, tie groups, counts, ids, the floored-judge list | exactly equal |

**Ranks.**
- For every pair of projects whose lab scores differ by more than 1e-9, our order must be the
  lab's.
- Where lab scores are equal to within 1e-9, only the set of ranks those projects hold must
  match.
- The order *inside* such a set is decided by the portal's own rule, R10: ranks are computed on
  scores rounded to 9 dp, and ties go by input order. The contract test asserts this for every
  method.

**P(ahead)** is compared per unordered pair, using P(a over b) = 1 − P(b over a). For each pair
of neighbours the lab reports, we compute P from our own fit's covariance.

**Movers** (`rank_change_fixtures.csv`): the set of the 8 biggest movers must match, and each
one's raw mean, score, SE, lean part, shrinkage part and explanation too. Equal move sizes are in
input order. This is R10, and it is also what the lab did: its `movers()` stable-sorted a table
held in file order. Only the copy it saved to CSV was sorted by rank.

**Accuracy against truth** (`synthetic_events_eval.csv`):
- M2 is compared with the CSV as printed, using the lab's own metric function.
- raw and z are compared tie-aware: Spearman, Kendall and MARE on scores rounded to 9 dp, with
  average ranks for ties. The expected values come from the same function applied to the lab's
  own scores in `expected/*.lab.json`.
- A separate test first checks that our port of the metric function, fed the lab's scores,
  reproduces the CSV. A mismatch can therefore never be the port's fault.

### Why ranks inside ties are not compared

The lab ranked raw floating-point scores and broke only bit-identical ties by index. Scores that
are equal on paper are often not bit-identical. In the fixtures, prj_16 and prj_33 both have a raw
mean of exactly 4, but the lab stored `3.9999999999999996` and `4.0`. Its order inside such ties
was therefore **float noise**:
- It depends on the order of additions, the weights' representation (1/3 vs 1) and the linear
  algebra library.
- It is **not reproducible across platforms**. The M2 scores of `syn_medium`'s prj_001 and
  prj_008 are equal to about 1e-14. They come out in a different order from the lab's in our
  Linux container, and also in a Windows run with numpy 2.5.2, even with the lab's arithmetic.
  That order is set by the linear-algebra library's rounding, which no code controls.
- It was **never a specification**: nothing in the lab or the ridge PDF says which of two equal
  projects should rank first.

The portal adopts the lab's arithmetic (weights normalised to sum to 1) so that its numbers match
the lab's to the last bit wherever the platform allows. It then states its own tie rule (R10),
tests that rule directly, and never claims to reproduce noise.

The same reasoning covers the z-score floor. A judge is "floored" only if their SD is below
`z_floor − 1e-9`. An SD that is exactly 0.5 on paper, like fixture judge jdg_03 with weighted
scores 13/3 and 10/3, is not floored, which matches the lab.

## Known deviation (reported, not patched)

`syn_medium`, M2, accuracy against truth. Two pairs of M2 scores are equal to within 1e-9 in the
lab's own output (prj_001/prj_008 and prj_026/prj_052). Their order is noise, ours differs, and
Spearman, Kendall and MARE move with it, because the lab computed those without tie handling:
Spearman +0.000168, Kendall +0.000404, MARE −0.02.

The test `test_m2_accuracy_matches_synthetic_events_eval_csv[syn_medium]` is a **strict xfail**
with this reason, so it fails loudly if the deviation ever disappears.
`test_syn_medium_m2_deviation_is_only_the_near_tie_pairs` pins the cause: when only those
projects take the lab's last bits, every metric matches the CSV.
