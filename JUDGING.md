# Judging (T2): not implemented yet

**There is no judging in this portal yet.** No judge can score anything, and nothing in the running
system shows a score to anyone. This file exists so that someone evaluating the submission can
confirm that in a minute, rather than discovering it by clicking around.

The four T2 acceptance checks are expected to **FAIL**, and they do: see
[`acceptance-report.txt`](acceptance-report.txt). `.dogfood.toml` claims `["T1"]` only, and there
are no stub endpoints anywhere faking a pass. `tests/test_acceptance_contract.py` asserts that the
T2 routes still 404, so adding one forces a deliberate update of the claim.

## What exists

The judging **data** is in place, imported from the organizers' fixture file, so T2 starts from the
real awkward data and not from a demo seed.

| Table | Rows after a default boot | What it holds |
|---|---|---|
| `events_eventmembership` (role `judge`) | 34 (30 fixture judges, plus the two demo judges in each of the fixture, live-demo and archive events) | who judges which event |
| `events_judgetrack` | one per track listed for each fixture judge | which tracks a judge covers |
| `scoring_criterion` | 3 | a rubric line: `key`, label, **decimal `weight`**, min, max, order |
| `scoring_score` | 123 | one judge's review of one project, with its comment |
| `scoring_scoreitem` | 369 | the value for one criterion inside a review |

Why the counts differ from the file:

- **126 reviews in the file, 123 imported.** The fixture's duplicate submission `prj_41` is folded
  into `prj_07` (same team, second submission). Three judges reviewed both, and one judge may review
  a project only once (`score_unique_judge_project`). The review of the kept submission wins, and
  the importer lists the other three in the boot log. None is dropped silently.
- **The three criteria**, `functionality`, `quality` and `innovation`, come from the keys in
  `scores[].criteria`. Each has `weight 1`, `min 1`, `max 5`, because the file supplies no
  weights. Configuring them is T2's job, and the weights are data, not code.

Guarantees that are **already live and tested**, and that T2 leans on:

- **Roles are per event.** A score hangs off the judge's `EventMembership`, not the user, so "this
  judge, in this event" is one column. A judge of one event has no standing in another.
- **Conflict of interest, in the database.** An exclusion constraint makes it impossible for anyone
  to be both a competitor and a judge (or organizer) in the same event. The organizer UI refuses it
  with a readable message, and the constraint backs that up. See [DATA-MODEL.md](DATA-MODEL.md).
- **One review per judge per project**, and **one value per criterion per review**: unique
  constraints.
- **A judging window** on every event (`judging_ends_at`), kept after the submission close by a
  CHECK constraint.
- **Role isolation at the server.** The judge portal (`/judge/`) is refused with a 403 and an audit
  row to anyone who judges no event, over sessions and Bearer tokens alike. It currently shows only
  the events and tracks the caller judges.

The fixture's other deliberate awkwardness is imported **as given**, because correcting it is
exactly what T2's normalization has to do, visibly:

- a judge who gave every project the same score;
- two review batches nobody finished, so coverage is 2–5 reviews per project and 1–11 per judge.

Nothing in the schema assumes a balanced review matrix.

## What is not implemented

Everything the tier actually asks for:

- **Judge invitation and assignment.** Organizers can make any account a judge of their event and
  pick its tracks (event control page → judges). Nothing assigns *projects* to judges yet, and
  nothing balances review load.
- **A weighted rubric the organizer can configure.** `Criterion.weight` is a column. There is no UI
  or API to create, edit, weight or reorder criteria; the only rows were made by the importer.
- **Score entry.** No form, endpoint or service function writes a `Score` outside the importer.
- **Reading scores**, including a judge reading their own. Nothing reads a score, so nothing can
  leak one.
- **A live organizer progress dashboard.** The event control page shows teams and submissions, not
  review progress.
- **Cross-judge normalization.** Not implemented, and no method is chosen yet.
- **CSV export.** No export endpoint.

## The routes T2 will implement

Listed in `.dogfood.toml` so the checker probes them honestly. All three 404 today:

```toml
judge_scores = "/api/judge/scores"
peer_scores  = "/api/judge/scores?judge=judge_a"
csv_export   = "/api/export.csv"
```

The `peer_scores` shape is deliberate: an explicit "read as another judge" parameter gives T2 one
clean thing to refuse (403 plus an audit row), rather than hiding the isolation question inside a
path that could be mistaken for a legitimate organizer view.

## Where T2 should start

In this order, because each step makes the next one testable:

1. **Criteria management.** Organizer CRUD for `Criterion` on the event control page, through a
   `scoring/services.py` function. Weights are relative. Do not silently rescale them to sum to 1;
   say what happens.
2. **Assignment.** A table from judge membership to project that respects `events_judgetrack`, and
   the conflict-of-interest rule, which the schema already guarantees.
3. **A judging-window guard**, in the same style as `core/deadlines.py` (server check, then a
   database trigger, on the database clock), written *before* the first score write path.
4. **Score entry** through that service: window, then "is a judge of this event and assigned this
   project", then each value against its criterion's min and max (`ScoreItem.clean`).
5. **Reading scores.** A judge sees their own. Organizers of the event and platform admins see all
   of them, which is the brief's role matrix. Participants see none until results are published.
   Enforce it in the query (scope by membership), not in the template.
6. **Normalization**, documented here, run against the fixture's uniform-scoring judge and unfinished
   batches.
7. **CSV export**, last, once there is something true to export.
