# Judging, results and community voting

**T2 (judging) is claimed**: all four of its acceptance checks pass (judge sees own scores, judge
cannot see peer scores, participant blocked, CSV export), in the committed
[`acceptance-report.txt`](acceptance-report.txt) and [`acceptance-report-offline.txt`](acceptance-report-offline.txt).

**T3 (community voting) is built but not claimed.** The organizers' checker (`acceptance/run.py`) has
no T3 checks, so claiming T3 would print "claimed but not verified: T3". The evidence is
[`t3-report.txt`](t3-report.txt), written by `scripts/t3-check.sh` against a fresh stack. Comments on
projects (part of the T3 brief) are **not built**.

Contents: the judging data · rubric · judge invites · judging window · assignment · the judge side ·
progress · scoring (the engine and M2) · results · community voting · the final score · CSV export ·
caveats · the Normalization Proof.

## The judging data

The fixture event (**Sample Hack 2026**) is imported with its judging data, so everything below is
tested against the real awkward data, not a tidy demo:

| Table | Rows after a default boot | What it holds |
|---|---|---|
| `events_eventmembership` (role `judge`) | 34 (30 fixture judges, plus the two demo judges in each of the fixture, live-demo and archive events) | who judges which event |
| `scoring_criterion` | 3 on the fixture event | a rubric line: key, label, **relative weight**, the 1-5 scale, description, level descriptions |
| `scoring_score` / `scoring_scoreitem` | 123 / 369 | the reviews (all submitted) and their per-criterion values |
| `scoring_assignment` | 123 | the assignment each imported review implies (source `import`) |

- **126 reviews in the file, 123 imported.** The duplicate submission `prj_41` is folded into
  `prj_07`; three judges reviewed both, and one judge may review a project once, so the kept
  project's review wins and the importer lists the other three. Nothing is dropped silently. The
  engine gives the three back to the duplicate (id `dup:prj_41`) and its duplicate policy decides.
- The fixture's deliberate awkwardness is imported **as given**: a judge who gave every project the
  same score, and unfinished batches (2-5 reviews per project, 1-11 per judge).

## The rubric

`/organizer/events/<slug>/rubric`. Every write goes through `scoring/services.py` with an audit row.

- **Weights are relative**: each is above 0 (a CHECK constraint), and a criterion's share is its
  weight over the sum (1 / 1 / 1 = exact thirds). Percentages that must add to 100 made equal
  weights impossible (33.334 / 33.333 / 33.333 made one criterion a hidden tie-break).
- **Every criterion is scored 1-5** (CHECK `criterion_scale_is_1_to_5`): the engine's tuning is in
  score units sized for that scale.
- **Locked from the submission close** (database clock): weights, the scale and the set of
  criteria cannot change once judging can start. Labels and descriptions stay editable; every edit
  is audited.

## Judge invites

Add an existing account as a judge, or create a one-time **invite link** (for an email, or open;
optionally for chosen tracks). A link is shown once (only its SHA-256 digest is stored), expires
after 7 days, works once, can be revoked, and is refused for anyone competing in the event (the
database's conflict-of-interest constraint is the backstop). All audited.

## The judging window

Reviews are written only from `judging_starts_at` to `judging_ends_at`, by the database clock,
half-open. The window is checked **first** in every review write (409 `judging_not_started` /
`judging_closed`, audited). Organizers can:

- **extend judging** (audited with a reason). Refused once the event has any final result: a final
  is a record of judging as it closed.
- **end judging now**: sets `judging_ends_at` to the database clock. It only moves the end earlier,
  is refused once judging has ended or before it starts, locks the event row, and is audited.

## Assignment

`/organizer/events/<slug>/assignments` (`scoring/assignment.py`, written by
`scoring.services.run_assignment`; deterministic for a seed and database state).

1. **Hard rules**: a judge reviews only projects in their tracks; no pair twice; a judge who
   declined a project (conflict) never gets it back; competitors cannot judge.
2. **Balance**: projects are filled to the review target (default 3) round-robin, fewest eligible
   judges first; each review goes to the least-loaded eligible judge, never past the optional cap.
3. **Randomness within the rules**: ties prefer the judge who shares the fewest projects with the
   project's reviewers (so judges meet many colleagues, which normalization needs), then a seeded
   draw. The seed is stored with the round. Each judge's queue is shuffled (position bias).
4. **Connectivity**: separate judge-project groups are linked with extra reviews where possible,
   and reported with the fix where not.

Running again **tops up** only what is missing. Organizers can add, move or withdraw an unstarted
assignment and reassign a stalled judge's unstarted reviews. Everything is audited; nothing is
deleted.

## The judge side

`/judge/`: each event's queue in the judge's own order; a review is a draft (any criteria) until
submitted (all criteria), and reopening a submitted review makes it a draft again. A judge declares
a conflict of interest with a reason; the project goes back to the organizer's queue. Order of checks
on every write: the judging window (409), then a live assignment (403 `not_assigned`), then the
values (400). `GET /api/judge/scores` returns only the caller's own reviews; `?judge=<x>` resolves an
email, an account id or a fixture judge id, and answers only for the caller or for an organizer of an
event that judge judges (everything else 403, audited).

## The progress dashboard

`/organizer/events/<slug>/progress`: judges (not started first), projects (fewest reviews first),
tracks, overall. Only submitted reviews count; it shows counts, never scores. It refreshes itself
every 30 seconds.

## Scoring: the engine and M2

`src/scoring/engine/` is pure Python and numpy: no Django, no clock, randomness only from a seeded
generator, and the same input and configuration give byte-identical JSON (`test_engine_purity.py`).
The primary method is **M2**, a ridge bias model:

    y_pj = mu + q_p + b_j + error      (y: one review's weighted score)

`q_p` is the project's quality and `b_j` the judge's **lean**: a harsh judge's low scores are
explained by their `b`, not by the projects they happened to review. Ridge penalties
(`lambda_q`, `lambda_b`, chosen by cross-validation with a seed derived from the event) shrink
thinly-evidenced estimates towards the mean: a project with two reviews is pulled in more than one
with five. Before the fit, filters drop (and list, with reasons) the duplicate submission and
**flat judges** (every score identical: they carry no ranking information). After it, flaggers mark
projects with too few reviews, near-flat judges and outlying reviews; a flag never changes a score.

- **Uncertainty and tie groups.** Each score has a standard error. Walking down the ranking, a
  project joins the tie group of the one above while P(the one above is truly ahead) is below 0.84.
  Ranks inside a group are not a real order.
- **Exact ties.** The engine's ordinal rank breaks exactly equal scores by input order (creation
  order). Every page shows **shared** ranks instead: equal after rounding to 9 decimals is a tie
  (competition style: =12, =12, 14).
- **Comparison.** The raw mean and z-score are run beside M2, and every page shows the raw-mean rank
  next to the M2 rank. How much each project moved, and why, is in the Normalization Proof below.
- **Snapshots.** `compute_snapshot` stores a **preview** (any time) or a **final** (only once judging
  has closed, always with the event's own configuration). A snapshot is immutable (a Postgres trigger
  refuses UPDATE) and records the engine configuration, the rubric and the SHA-256 of the exact
  input. It runs in its own REPEATABLE READ transaction, so it sees one consistent database.

## Results

`/organizer/events/<slug>/results` (and `POST /api/events/<slug>/results/{compute,publish,unpublish,settings}`):

- **Compute** a preview or the final result. **Publish** the latest final (append-only publication;
  unpublishing records who and when, and the result can be published again).
- **Publishing is refused** while the event's community vote is open (409 `voting_open`, checked
  first), for a final computed before the vote closed (409 `final_predates_vote_close`: it has no
  tally), for an older final (409 `not_latest_final`) and while something is published
  (409 `already_published`). All audited.
- **Who sees it** (`result_visibility`): the full ranking, the winners only, or organizers and
  admins only. `/events/<slug>/results` is a 404 unless a result is published with a public
  visibility; organizers and admins always see a full preview.
- **The page**: shared ranks for exact ties, M2 tie groups, "no separable winner" when first place is
  shared, the winners (top N overall, a tie on the cut bringing in everyone on it, plus the top
  project of each track), the raw-mean rank beside the M2 rank, and People's Choice. **No per-judge
  data** appears on any public page.
- **winners.csv** (every mode; there is no outbound mail, so this is how organizers reach winners):
  award, rank, track, project, team, each member's name and email. **results.csv**: the latest
  final, every project: final rank, judged percentile, M2 rank, tie group, influence, vote
  percentile, People's Choice position. Organizers only, audited, formula-escaped.

## Community voting (T3)

`/organizer/events/<slug>/voting` sets it up; voters use `/participant/events/<slug>/vote` (logged in)
or `/events/<slug>/vote/<token>` (a link), or `POST /api/events/<slug>/ballot`. Rules live in
`voting/services.py`; every write is audited.

**The window.** It opens at or after the submission close (the latest team extension included), so
the projects on the ballot are final, and it may overlap judging. Moving the close or granting an
extension past a scheduled opening is refused (`voting_scheduled`). Once voting opens only the close
can move, and only to a future time; **end voting now** is the one way to close at once. Checked by
the server first (409 `voting_not_open` / `voting_closed`, audited), then by a **Postgres trigger**:
no ballot write outside the window, **no ballot deleted once voting has opened** (the foreign keys
into ballots are PROTECT, so no cascade removes one either), and the vote's own row cannot be deleted
or re-timed once open. The one way past it is the audited `voting_bypass`.

**Access modes**, strongest to weakest:

| mode | who votes | strength |
|---|---|---|
| authenticated | any logged-in account (one ballot each) | as strong as sign-up is; the option "only accounts created before voting opened" (default on) stops sign-up stuffing during the vote |
| email_gated | one personal link per allowlisted email (paste or CSV); organizers download `voter-links.csv` and send them (no outbound mail) | as strong as the allowlist; a link can be revoked (a new one is issued if the email is added again) |
| open_link | anyone with the event's link; a signed cookie identifies the browser | **weak: a new browser is a new voter.** Marked on both pages. Give any prize it decides a small weight |

**Who may not vote**: the event's judges and organizers and platform admins (403
`staff_cannot_vote`), and nobody votes for their own team's project (403 `own_project`; it is not on
their ballot). For a link whose email matches an account, those two rules apply; the account-age
rule does not (the allowlist is the gate).

**Methods.** One person, one vote is a budget of 1. **Quadratic**: each voter spreads
`credit_budget` credits (default 16), and a project's influence from one ballot is **sqrt(credits)**:
the same curve as "n votes cost n² credits". Spreading support counts for more than piling it on.
A cast replaces the whole ballot (projects not named get 0), with the ballot row locked, so two tabs
can never add up to more than the budget (400 `over_budget`). Votes can be changed until the close.

**Ordering.** Each ballot shows the projects in its own order: a shuffle seeded from the ballot id and
a per-event secret, stored as `shown_position`, so it differs between voters and never changes on
reload. A GET never creates a ballot (the ballot, and its order, is made by the first POST).

**Hidden results.** While voting is open, and after, only the event's organizers and platform
admins see any tally (the page, `tally.csv`, `/api/events/<slug>/votes/tally`). The gallery and
project pages never show votes. The public sees People's Choice only in a published result.

**Anti-abuse.**
- **Rate limits** on vote writes (opening, casting, changing, and refused attempts), per voter and
  per IP hash, counted from audit rows in Postgres: 30 per voter and 300 per network in 10 minutes
  by default (a venue shares one address). Over the limit: 429 `rate_limited`, audited. Checked
  after the window, so a late write is still a 409.
- **No IP address is stored**, anywhere: the audit log, sessions and ballots keep a keyed hash
  (HMAC under a key derived from this install's own secret key).
- **Flags** on the integrity page (`/organizer/events/<slug>/voting/integrity`), computed when the
  page is shown, never acted on automatically: many ballots from one network within 10 minutes;
  identical ballots (spread over two or more projects) within 10 minutes of each other; a new account
  voting within 10 minutes of signing up. Ballots with no credits and voided ballots are left out;
  empty ballots are counted separately.
- **Void / restore**: an organizer voids a ballot with a reason (kept, audited, left out of every
  tally from then on) and can restore it (with a reason). Both work after the close; a frozen tally is
  never changed, the next freeze includes the change.
- **Evidence against position bias**: the integrity page shows the average credits by shown
  position. With the per-ballot shuffle, a flat profile says the order did not help anyone.

**What this does not stop**: open-link sybils (one person, many browsers or private windows);
colluding real accounts (friends voting as a bloc look like honest voters); an allowlisted person
sharing their link; a determined attacker spreading writes across networks below the limits. The
flags make clusters visible; they do not prove intent.

## The final score (judges and community)

`final = judge_weight/100 × judged percentile + community_weight/100 × vote percentile`
(`scoring/engine/combine.py`, pure).

- **Percentiles by mid-rank** within the event, over exactly the projects M2 ranked: equal values
  share the middle of the ranks they span (the same rounded equality as the shared ranks), and every
  project with no votes shares the bottom. Projects M2 could not rank stay "not ranked" even with
  votes (People's Choice may still list them).
- **Why ranks, not raw numbers**: a percentile cannot be pushed past first place by piling on more
  votes, and it puts two very differently scaled numbers on one scale. The price: magnitude is lost
  (a landslide and a narrow win look the same). **The combined score has no standard error**: M2's
  tie groups are shown beside the judged component only, and first place is shared only by an exact
  combined tie. The arithmetic is exact (fractions), so there is no hidden tie-break from rounding.
- **Weights** are whole numbers, each at least 0, summing to 100 (default 100/0: judges only). They
  **lock once judging or voting opens** (409 `weights_locked`; a trigger backs it). With a community
  weight of 0 the final ranking is exactly the M2 ranking (tested).
- **Frozen tallies.** "Compute final results" after the vote has closed freezes an immutable,
  append-only vote tally and records it on the result; each tally links to the previous one and says
  whether anything changed and which ballots were voided or restored in between. Two finals at the
  same moment cannot both claim to be first (the vote's row is locked; the second gets 409
  `final_in_progress` and is simply computed again). A final with a community weight needs a vote
  that has closed (409 otherwise). Previews use the live tally and freeze nothing.
- **People's Choice** (most influence) is listed separately whenever the event had a vote: the full
  list in `public_full`, the top N in `public_winners`, organizers only when private.

## CSV export

`GET /api/export.zip?event=<slug>` (one CSV per open sheet, read in one REPEATABLE READ snapshot) and
`GET /api/export.csv?event=<slug>&sheet=<name>`. Each sheet unlocks when its stage starts (setup and
the audit trail from creation; teams and projects from submissions open; assignments from the close;
reviews from judging start; results from judging end), else 409 `stage_not_open`. Organizers of the
event and admins only; every download audited; only submitted reviews; the audit sheet shows IP
hash prefixes, not addresses. The export's results sheet is the M2 comparison of the latest final;
the combined ranking is in `results.csv`. Every CSV the portal writes goes through one writer
(`core/csvfile.py`) that escapes text starting like a formula.

## Caveats

- **Exact ties.** The ordinal rank's input-order tie-break is internal; every page shows shared ranks.
- **Accounts referenced by results cannot be deleted** (snapshots, publications, ballots, tallies
  are PROTECT). Deactivate them (`is_active=False`).
- **The scale is fixed at 1-5.** An organizer file with another scale must be rescaled before import.
- **Open judge invite links** can be used by whoever holds them, once.
- **Leaving as the last member** shows a confirm page decided in the view; if the other member leaves
  between that page and the POST, the team is deleted without it (low impact; not fixed). After
  voting opens, a team whose project has votes cannot be deleted at all.
- **One person, one vote: the identical-ballots flag cannot fire** (it needs a ballot spread over two
  or more projects). Only the network and new-account flags apply in that mode.
- **Flags are prompts, not verdicts**: a venue's shared network can trip the burst flag honestly.
- **The IP-hash migrations are split** (convert, then drop the column): Postgres refuses to ALTER a
  table in the transaction that just updated its rows while deferred foreign-key checks are pending.
  An empty test database could not show this; a real one did.
- **One developer database had a migration marked applied by hand** (`accounts/0003`, after an
  earlier unsplit version had already dropped the column). Only that database; **a fresh boot is
  authoritative**, and the committed reports come from one.
- **Rotating SECRET_KEY** invalidates sessions, voter links and open-link cookies, and resets
  IP-based rate limits and flag clustering.
- **Tests move the voting window** through the audited bypass, as a real repair would.
- **The demo seed** (DEMO_MODE only): the archive event's vote is open through its judging phase,
  quadratic, 80/20 weights set through the audited weights bypass (its judging had already opened).
  Its "accounts created before voting opened" option is **off**, so the demo participant (created at
  boot) can vote. Six seed-only honest voters are dated a week before the vote opened, so they do not
  trip the new-account flag; the four-account suspicious cluster is not, and is flagged three ways.
  The fixture event gets a vote that closed in March 2026 with no ballots, so a late vote is a real
  409 and its final still publishes (with an empty People's Choice).
- **`t3-check.sh` changes demo data**: it rate-limits judge_b for ten minutes, and on the fixture
  event computes a final, publishes it under each visibility, then unpublishes it and sets it back to
  private, as on a fresh boot.

<!-- normalization-proof:start (generated by scripts/normalization_proof.py; do not edit) -->

## Normalization Proof (the fixture event)

Generated from `acceptance/fixtures.json` by `scripts/normalization_proof.py` with the engine's default configuration: 126 reviews by 30 judges of 40 ranked projects, in 1 connected component(s). M2's ridge strengths were chosen by cross-validation (seed 1111942736): component 0: lambda_q 4, lambda_b 4.

**31 of 40 projects change rank** between the plain mean of their reviews and M2. Ranks here are shared by exact ties, as on the results page: the raw means have 21 exact repeats (many projects average exactly 3.333), and an ordinal rank would count their file order as movement. The split below is exact: over every ranked project, |(raw - M2) - (lean + shrinkage)| is at most 4.7e-16.

### Top 10: raw mean vs M2

| M2 rank | project | M2 score | ± se | raw rank | raw mean | reviews |
|---:|---|---:|---:|---:|---:|---:|
| 1 | Salt Ledger (`prj_11`) | 3.856 | 0.228 | 1 | 4.333 | 4 |
| 2 | Iron Switch (`prj_34`) | 3.853 | 0.243 | 1 | 4.333 | 3 |
| 3 | Salt Loom (`prj_37`) | 3.795 | 0.228 | 5 | 4.083 | 4 |
| 4 | Dry Relay (`prj_25`) | 3.749 | 0.243 | 4 | 4.111 | 3 |
| 5 | Slow Trail (`prj_33`) | 3.720 | 0.242 | 6 | 4.000 | 3 |
| 6 | Salt Kiln (`prj_16`) | 3.704 | 0.242 | 6 | 4.000 | 3 |
| 7 | Still Beacon (`prj_10`) | 3.680 | 0.260 | 3 | 4.167 | 2 |
| 8 | North Drift (`prj_08`) | 3.643 | 0.216 | 9 | 3.800 | 5 |
| 9 | Copper Kiln (`prj_21`) | 3.628 | 0.242 | 8 | 3.889 | 3 |
| 10 | Green Switch (`prj_04`) | 3.608 | 0.242 | 10 | 3.778 | 3 |

### The 10 biggest moves, and why

Positive lean = its reviewers score high on everything, so M2 takes that back; positive shrinkage = its reviews sit above what the model expects, and with few reviews M2 pulls it towards the mean. raw mean - M2 score = lean + shrinkage.

| project | reviews | raw rank | M2 rank | move | raw mean | M2 score | lean | shrinkage | mainly |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Flat Meadow (`prj_28`) | 3 | 20 | 32 | -12 | 3.444 | 3.396 | +0.210 | -0.161 | judge lean |
| Warm Beacon (`prj_35`) | 5 | 19 | 28 | -9 | 3.467 | 3.464 | +0.045 | -0.042 | judge lean |
| Flat Thread (`prj_27`) | 3 | 20 | 26 | -6 | 3.444 | 3.472 | +0.033 | -0.060 | shrinkage |
| Hollow Signal (`prj_09`) | 2 | 26 | 21 | +5 | 3.333 | 3.482 | -0.079 | -0.070 | judge lean |
| Quiet Anchor (`prj_13`) | 3 | 26 | 31 | -5 | 3.333 | 3.427 | +0.027 | -0.120 | shrinkage |
| Small Loom (`prj_17`) | 2 | 26 | 21 | +5 | 3.333 | 3.482 | -0.079 | -0.070 | judge lean |
| Deep Compass (`prj_03`) | 3 | 26 | 30 | -4 | 3.333 | 3.439 | -0.003 | -0.103 | shrinkage |
| Still Beacon (`prj_10`) | 2 | 3 | 7 | -4 | 4.167 | 3.680 | +0.159 | +0.327 | shrinkage |
| Paper Anchor (`prj_39`) | 2 | 16 | 20 | -4 | 3.500 | 3.486 | +0.077 | -0.063 | judge lean |
| Small Meadow (`prj_02`) | 3 | 14 | 17 | -3 | 3.556 | 3.519 | +0.035 | +0.002 | judge lean |

Of these 10 moves, 6 are mainly the judge-lean correction and 4 mainly shrinkage. "mainly" compares the two parts' sizes; they can pull in opposite directions. M2's tie groups (shown on the results page) say which of these ranks the reviews can actually separate.

<!-- normalization-proof:end -->
