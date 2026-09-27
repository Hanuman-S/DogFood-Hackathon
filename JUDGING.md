# Judging (T2): claimed

**T2 is claimed: all four checks pass.** Built: the organizer's side (below: the rubric,
judge invites, the judging window, assignment, the progress dashboard), the judge side (a queue
of assigned projects, draft / submit / reopen, declaring a conflict, and `/api/judge/scores`,
which returns only the caller's own reviews), and the scoring engine (`scoring/engine/`, with
preview and final result snapshots; see PLAN.md and docs/t2-scoring-plan.md), and the CSV export
(below). Not built: published results pages. A score's values are shown only to the judge who
wrote it and, through the export, to the event's organizers and platform admins (the brief's role
matrix); the dashboard counts reviews, never their contents. Drafts are never exported. *(A full rewrite of this file is due in
the final docs pass.)*

All four T2 acceptance checks pass when the checker is run against a local stack (judge sees own
scores, judge cannot see peer scores, participant blocked, CSV export). The committed
[`acceptance-report.txt`](acceptance-report.txt) was regenerated from a fresh stack after PR #3
(2026-09-27) and shows all seven checks passing, so `.dogfood.toml` claims `["T1", "T2"]`. The peer
check asks for judge_a's real account (`?judge=judge.a@dogfood.local`); judge_a gets 200 there and
judge_b 403.

## What exists

The judging **data** is in place, imported from the organizers' fixture file, so T2 starts from the
real awkward data and not from a demo seed.

| Table | Rows after a default boot | What it holds |
|---|---|---|
| `events_eventmembership` (role `judge`) | 34 (30 fixture judges, plus the two demo judges in each of the fixture, live-demo and archive events) | who judges which event |
| `events_judgetrack` | one per track listed for each fixture judge | which tracks a judge covers |
| `scoring_criterion` | 3 | a rubric line: `key`, label, **`weight` as a percentage** (33.334 / 33.333 / 33.333), min, max, order, description, level descriptions |
| `scoring_score` | 123 | one judge's review of one project, with its comment; all **submitted** (`submitted_at` set) |
| `scoring_scoreitem` | 369 | the value for one criterion inside a review |
| `scoring_assignment` | 123 | the assignment each imported review implies (source `import`, status `assigned`) |
| `scoring_assignmentround`, `events_judgeinvite` | 0 | automatic assignment runs and one-time judge links, until an organizer makes some |

Why the counts differ from the file:

- **126 reviews in the file, 123 imported.** The fixture's duplicate submission `prj_41` is folded
  into `prj_07` (same team, second submission). Three judges reviewed both, and one judge may review
  a project only once (`score_unique_judge_project`). The review of the kept submission wins, and
  the importer lists the other three in the boot log. None is dropped silently.
- **The three criteria**, `functionality`, `quality` and `innovation`, come from the keys in
  `scores[].criteria`, scored `min 1` to `max 5`. The file supplies no weights, so they count
  equally: 33.334 / 33.333 / 33.333 percent (three decimals cannot split 100 evenly, so the
  leftover thousandth goes to the first). The fixture event closed long ago, so its rubric is
  locked (see below) and stays equal: that is what its judges scored against.

Guarantees that are **already live and tested**, and that T2 leans on:

- **Roles are per event.** A score hangs off the judge's `EventMembership`, not the user, so "this
  judge, in this event" is one column. A judge of one event has no standing in another.
- **Conflict of interest, in the database.** An exclusion constraint makes it impossible for anyone
  to be both a competitor and a judge (or organizer) in the same event. The organizer UI refuses it
  with a readable message, and the constraint backs that up. See [DATA-MODEL.md](DATA-MODEL.md).
- **One review per judge per project**, and **one value per criterion per review**: unique
  constraints.
- **A judging window** on every event, from `judging_starts_at` to `judging_ends_at`, strictly
  after the submission close and before the results date (if set), by CHECK constraints. The gap
  between the close and the judging start is when organizers assign judges.
- **Role isolation at the server.** The judge portal (`/judge/`) is refused with a 403 and an audit
  row to anyone who judges no event, over sessions and Bearer tokens alike. It currently shows only
  the events and tracks the caller judges.

The fixture's other deliberate awkwardness is imported **as given**, because correcting it is
exactly what T2's normalization has to do, visibly:

- a judge who gave every project the same score;
- two review batches nobody finished, so coverage is 2–5 reviews per project and 1–11 per judge.

Nothing in the schema assumes a balanced review matrix.

## The rubric (built)

Organizers edit it at `/organizer/events/<slug>/rubric`, linked from the event's control page. Every
write goes through `scoring/services.py` and leaves an audit row with the old and new values.

- **Weights are percentages** of each review's score, and an event's weights add up to exactly 100:
  what the organizer types is what judges see and what the results use. The whole rubric is saved
  at once, because adding, removing or reweighting one criterion changes the total; a rubric that
  adds up to anything else is refused whole, with the total shown. "Split equally" fills equal
  weights in for the organizer to check, without saving. A live total on the page (in `app.js`)
  turns red while it is not 100; the server check is the rule.
- **Locked from the submission close**, on the database clock like the deadline. From then on,
  weights, the scale's min and max, and the set of criteria cannot change, because they change the
  ranking; a refused attempt is logged (`rubric_change_refused`). Labels, descriptions and the
  written description per score level stay editable, because wording changes nobody's score; each
  such edit is logged with `while_locked: true`. An extension for everyone moves the close, and the
  lock with it.
- **Nothing already scored is rewritten.** A criterion with scores cannot be removed, and its scale
  cannot shrink past a value already given.
- **Scales** are whole numbers within 0-10. **A standard rubric** (functionality, quality, innovation
  on 1-5, equal weights, with a written description for every level) is one click for a new event.

**The one-time conversion.** Weights used to be stored as relative numbers (the importer wrote 1, 1,
1). Migration `scoring/0003_weights_are_percentages` rescales each existing rubric once, keeping its
proportions, so 1 / 1 / 1 became 33.334 / 33.333 / 33.333; a rubric already adding up to 100 is left
alone.

## Judge invitation (built)

On the event page's judges section, **add judge** makes an existing account a judge; **invite by
link** creates a one-time link for an email (optionally for chosen tracks). The link:

- is shown once; only its SHA-256 digest is stored (`events_judgeinvite.digest`);
- expires after 7 days, works once, and only for the invited email: the invitee logs in as that
  email, or creates the account from the link with the email fixed (even when public sign-up is
  closed: the organizer's invite is the permission);
- is cancelled by inviting the same email again, or revoked by an organizer;
- is refused for anyone competing in the event, checked both when the link is made and when it is
  accepted (the database's conflict-of-interest constraint is the backstop).

Audit rows: `judge_invited`, `judge_invite_accepted` (plus `judge_added`), `judge_invite_refused`
with the reason (wrong account, used, expired, conflict of interest), `judge_invite_revoked`.

The email is optional: without one the link is an **open link** that the first person to accept it
uses up. **Co-organizers** are invited the same way from the organizers section (the same model,
`events_judgeinvite`, with `role` "organizer"; links start `oinv_` and land on
`/invite/organizer/<token>`); accepting makes the holder an organizer of that event, refused for
anyone competing in it. Audit rows: `organizer_invited`, `organizer_invite_accepted` (plus
`organizer_added`), `organizer_invite_refused`, `organizer_invite_revoked`.

## The judging window (built)

Reviews may be written only from `judging_starts_at` to `judging_ends_at`, by the database clock,
half-open like the deadline. `core/judging.py` is the check for the judge side to call **first** in
every review write: `check_judging_window(request, event, action=...)` raises `JudgingNotOpen`
(code `judging_not_started` or `judging_closed`) and logs `judging_write_refused`;
`refusal_response()` turns it into a 409 for JSON clients. There is no database trigger for it
yet: the importer and the demo seed write reviews outside any window.

Organizers **extend judging** from the event page (`extend_judging`, audited as
`judging_extended` with the reason). The first end is kept in `original_judging_ends_at`; if a
results date is set and the new end reaches it, results move by the same amount. Extending also
reopens a finished event, which is how the fixture event (judging ended in March 2026) is brought
back to top up its reviews.

## Assignment (built)

`/organizer/events/<slug>/assignments`. The plan is computed in `scoring/assignment.py` and written
by `scoring.services.run_assignment`; both are deterministic for a given seed and database state.

1. **Hard rules.** A judge reviews only projects in tracks they cover (no tracks = every track).
   No judge-project pair twice. A judge who **declined** a project (conflict of interest) never
   gets it back. Competitors cannot be judges at all (the database's exclusion constraint).
   Only submitted projects are assigned.
2. **Balance.** Projects are filled to the **review target** (default 3) one review at a time, in
   round-robin order starting with the projects that have the fewest eligible judges, so a
   shortage is spread thinly instead of leaving some projects with none. Each review goes to the
   eligible judge with the **lowest load**, never past the optional **load cap**; a final pass
   moves reviews from the busiest judges to lighter ones who may take them, so loads differ by at
   most one wherever the rules allow.
3. **Randomness within the rules.** Among equally loaded judges, the one who shares the fewest
   projects with the project's existing reviewers is preferred, so judges meet many colleagues
   (which is what lets normalization compare them); any remaining tie is a seeded random draw.
   The **seed** is stored with the round (`scoring_assignmentround.seed`), so a round can be
   reproduced exactly and nobody can quietly re-roll it. Each judge's new projects are shuffled
   into their queue (`Assignment.position`) to remove position bias.
4. **Connectivity.** The model behind normalization can only compare two judges' leniency through
   shared projects. If the judge-project graph falls into separate groups, each smaller group is
   linked to the largest with one extra review (a judge of one group reviews a project of the
   other), or two from a judge in neither group who may review in both. A group that cannot be
   linked is reported, with the fix (add a judge who covers both tracks).
5. **Warnings before anything goes wrong:** a track with fewer eligible judges than the target, or
   whose judges have no room left under the load cap, and submissions that are still open (later
   projects will need a top-up). The page shows the current number of separate groups.

Running again **tops up** only what is missing, which is how the fixture event is handled: its 123
imported reviews become `import` assignments, and a top-up to 3 adds exactly the 8 reviews its
two-review projects lack (tested). By hand, an organizer can **add** a judge to a project, and
**move** or **withdraw** an assignment the judge has not started: a started review is never
taken away. **Reassign** (on the progress page or per judge) withdraws everything a stalled judge
has not started and tops up without them. **Declined** projects are listed with the reason (only
organizers see it) until they are covered again.

Every change is audited: `assignments_generated` (with seed, target, cap, counts),
`assignment_added`, `assignment_moved`, `assignment_withdrawn`, `assignment_declined`. Assignments
are never deleted; their status changes.

## The progress dashboard (built)

`/organizer/events/<slug>/progress`, organizers of the event only.

- **Judges**, not started first: submitted / assigned, drafts, not started, last active, last
  nudged. **Nudge** logs a reminder (`judge_nudged`) and shows it ready to send from the
  organizer's own mail (a mail link and the text to copy): the portal sends no email.
  **Reassign** gives a stalled judge's unstarted reviews to others.
- **Projects**, fewest reviews first: submitted reviews against the target, and how many are
  assigned. **Tracks**: projects, eligible judges, reviews in against needed.
- **Overall:** reviews in against the target, judges not started, projects short.
- Only **submitted** reviews count. The dashboard counts reviews; it never shows their scores.
- The tables **refresh themselves** every 30 seconds (`app.js` fetches `?partial=1` and swaps
  them in, pausing while the tab is hidden); without JavaScript the page is simply static.

## CSV export (built)

The export (`src/organizer/export.py`) grows with the event: each sheet belongs to a stage and
unlocks when that stage starts, by the database clock. Setup (event, tracks, prizes, questions,
rubric, judges, invites) and the audit trail are there from creation; teams, members and
projects from submissions open; assignments from submissions close; reviews from judging start;
results from judging end (until then rank, score and judge lean are empty everywhere). A sheet of
a stage that has not started is refused with 409 `stage_not_open` and its opening time, and left
out of the ZIP (whose README says when it opens).
`GET /api/export.zip?event=<slug>` gives one CSV per open sheet (event, tracks, prizes, questions,
rubric, judges, invites, teams, members, projects, assignments, assignment rounds,
reviews, results, audit) plus a README, all read in one REPEATABLE READ snapshot;
`GET /api/export.csv?event=<slug>&sheet=<name>` gives one sheet; `GET /api/export.csv` alone
(the checker's route) gives the projects of every event the caller manages. Organizers of the
event and platform admins only (401 / 403 / 404 otherwise, refusals audited); every download is
audited. Only submitted reviews are exported. Results are the latest final snapshot, or if none
has been computed yet, one computed for the export (labelled, not saved). Text cells that start like
a formula are escaped.

## For the judge side

What the organizer side provides, to build score entry on:

- **`core.judging.check_judging_window(request, event, action=...)`** first, before permission
  checks and validation; `refusal_response(error)` for the 409.
- A judge may review a project only while they hold an **`Assignment` with status `assigned`**
  for it. Sort their list by `Assignment.position`.
- **`scoring.services.decline_assignment(request, assignment, reason)`** for "declare a conflict":
  only the assigned judge, only before submitting, a reason is required; the project goes to the
  organizer's queue and never back to that judge.
- **`Score.submitted_at`**: empty is a draft; set it on submit. The dashboard and the results
  count submitted reviews only. The audit actions `score_saved`, `score_submitted`,
  `score_reopened` and `judging_write_refused` already exist.

## What is not implemented

- **Published results.** The engine computes preview and final snapshots (`manage.py
  score_event`), and the `Publication` model exists, but there is no publishing service or
  results page yet.

## The routes T2 will implement

Listed in `.dogfood.toml` so the checker probes them honestly. All three answer:

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

1. **Criteria management.** Done: see "The rubric" above.
2. **Assignment.** Done: see "Assignment" above.
3. **A judging-window guard.** Done (the server check): see "The judging window" above.
4. **Score entry** through that service: window, then "is a judge of this event and assigned this
   project", then each value against its criterion's min and max (`ScoreItem.clean`).
5. **Reading scores.** A judge sees their own. Organizers of the event and platform admins see all
   of them, which is the brief's role matrix. Participants see none until results are published.
   Enforce it in the query (scope by membership), not in the template.
6. **Normalization**, documented here, run against the fixture's uniform-scoring judge and unfinished
   batches.
7. **CSV export**, last, once there is something true to export. (Built.)
