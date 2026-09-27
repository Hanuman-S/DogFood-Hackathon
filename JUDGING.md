# Judging (T2) — not implemented

**There is no judging in this portal.** No judge can score anything, because nothing in the running
system reads or writes a score. This file exists so that a judge evaluating this submission can
confirm that in a minute, rather than discovering it by clicking around.

If you are looking for the T2 acceptance checks: all four are expected to **FAIL**, and they do. See
[`acceptance-report.txt`](acceptance-report.txt). `.dogfood.toml` claims `["T1"]` only.

## What exists

Three tables, their models, and the fixture data loaded into them.

| Table | Rows after a default boot | What it holds |
|---|---|---|
| `scoring_criterion` | 3 | A rubric line: `key`, label, `min_value`, `max_value`, **`weight`**, `order` |
| `scoring_score` | 126 | One judge's review of one project, with a comment |
| `scoring_scoreitem` | 378 | The per-criterion value inside a review |

The three criteria come from the keys in the fixture's `scores[].criteria` objects —
`functionality`, `quality`, `innovation` — each created with `weight 1.000`, `min 1`, `max 5`. Equal
weights because the fixture supplies none.

Two constraints in that schema are the ones T2 will lean on:

- `score_unique_judge_project` — a judge cannot review the same project twice.
- `criterion_unique_key_per_event` and `criterion_min_below_max` — a rubric cannot contain two
  `quality` lines, or a line whose minimum exceeds its maximum.

Also already present, and relevant:

- **Per-event roles**, including `judge`, with track assignments in `events_judgetrack` (39 rows from
  the fixture).
- **The conflict-of-interest rule**, enforced by a Postgres exclusion constraint: nobody can be both a
  competitor and a judge or organizer in the same event. This is the one judging-adjacent guarantee
  that *is* live and tested — see [DATA-MODEL.md](DATA-MODEL.md).
- **A judging window** on every event (`judging_ends_at`), validated against the submission window by
  a check constraint.

## What is not implemented

Everything the tier actually asks for:

- **Judge invitation and assignment.** An organizer can grant someone the `judge` role and record
  which tracks they cover. Nothing assigns *projects* to judges, and nothing distributes review load.
- **A weighted scoring rubric the organizer can configure.** `Criterion.weight` is a column. There is
  no UI, no service function and no API to create, edit, weight or reorder criteria; the only rows
  that exist were made by the importer.
- **Score entry.** There is no form, no endpoint and no service function that writes a `Score` or a
  `ScoreItem` outside `seed/importer.py`.
- **Backend-enforced judge isolation.** `core.permissions.can_view_project_scores` exists and returns
  `False` for **everyone**, including admins and organizers. It is a deliberate placeholder: the
  question has one obvious home for T2 to implement, and until then no code path can leak a score
  because no code path shows one.
- **A live organizer progress dashboard.** The organizer dashboard shows submission counts, not
  review progress.
- **Cross-judge normalization.** Not implemented, and no method is chosen. Note the fixture's coverage
  is deliberately uneven — projects carry 2–5 reviews and judges 1–11 — so whatever T2 does here has
  to cope with an unbalanced matrix rather than assuming one.
- **CSV export.** No export endpoint of any kind.
- **Judging-window enforcement.** `core.deadlines.assert_judging_open(event)` exists and
  **raises `NotImplementedError`**. It refuses rather than defaulting to open, because a guard that
  silently allows everything is worse than no guard at all. No T1 code path calls it.

## The routes T2 will implement

Listed in `.dogfood.toml` so the checker probes them honestly. All three 404 today:

```toml
judge_scores = "/api/judge/scores"
peer_scores  = "/api/judge/scores?judge=jdg_02"
csv_export   = "/api/events/sample-hack-2026/export.csv"
```

The `peer_scores` shape is a deliberate choice: an explicit "read as another judge" query parameter
gives T2 one clean thing to refuse, rather than hiding the isolation question inside a path that could
be mistaken for a legitimate admin view.

## Where T2 should start

In this order, because each step makes the next one testable:

1. **Criteria management** — organizer CRUD for `Criterion`, in `events/services.py` or a new
   `scoring/services.py`, with the weights summing however the organizer likes (do not silently
   normalize them to 1.0; say what happens).
2. **Assignment** — a table mapping judge membership to project, respecting `events_judgetrack` and
   the conflict-of-interest rule that already exists.
3. **`assert_judging_open`** — implement the guard before implementing score writes, so the first
   write path is guarded from its first commit. Follow the half-open window semantics in
   `core/deadlines.py`.
4. **Score entry** through a service function that calls the judging guard, then a permission check,
   then validates each value against its criterion's min and max.
5. **`can_view_project_scores`** — replace the `False`. A judge sees their own scores; an organizer
   sees all scores for their event; a participant sees none until results are published.
   `tests/test_permissions.py` is where the matrix lives.
6. **CSV export**, last, once there is something true to export.

Read [CLAUDE.md](CLAUDE.md) first. The conventions there — the service layer, the single clock, the
check order, guards before transactions so refusal audit rows survive — are what make the T1
guarantees hold, and T2 is where they are easiest to break.
