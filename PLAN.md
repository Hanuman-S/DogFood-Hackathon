# Build plan and progress log — DOGFOOD 2026, tier T1

Working document. The plan is at the top, the running log of what actually happened is at the
bottom. Deadline: code freeze **2026-09-28 18:00 UTC**. Solo build.

---

## Step 0 — what the organizer files actually do

`acceptance/run.py` is byte-identical to Appendix A of `AboutTheHackathon.md`. Verified before
writing any code, because every design decision below depends on it.

### The seven requests

`urllib` with a 10-second timeout. Each URL is `base_url.rstrip("/") + routes[key]`
concatenated raw, so a route string must be a complete path and may carry a query string.
A connection error is reported as status `0`.

| # | Tier | Check | Method | Route | Header | Body | Passes when |
|---|------|-------|--------|-------|--------|------|-------------|
| 1 | T1 | gallery is public | GET | `gallery` | none | — | `200` |
| 2 | T1 | project from fixtures shown | — | *reuses #1's body* | — | — | substring match |
| 3 | T1 | closed event refuses submissions | POST | `submit` | `auth.participant` + `Content-Type: application/json` | `{"title": "dogfood-late-submission-probe", "summary": "probe"}` | `400 ≤ s < 500` |
| 4 | T2 | judge sees own scores | GET | `judge_scores` | `auth.judge_a` | — | `200` |
| 5 | T2 | judge cannot see peer scores | GET | `peer_scores` | `auth.judge_b` | — | `401` or `403` |
| 6 | T2 | participant blocked | GET | `judge_scores` | `auth.participant` | — | `401` or `403` |
| 7 | T2 | csv export works | GET | `csv_export` | `auth.organizer` | — | `200` and a comma in line 1 |

### `[auth]` header parsing — arbitrary headers, not only cookies

```python
name, _, value = header.partition(":")
req.add_header(name.strip(), value.strip())
```

`partition` splits on the first colon only, so `"Authorization: Bearer abc123"` arrives as a
proper `Authorization` header. Any `Header-Name: value` string works. Two consequences:

- **One header per role, so the checker can never send a CSRF token.** The write endpoint it
  probes must therefore authenticate by bearer token with CSRF not enforced — which is why
  `core.authentication.BearerTokenAuthentication` exists and why that is correct rather than
  merely convenient (a bearer token is not an ambient credential).
- Real `tomllib` parses the file when Python ≥ 3.11, so `.dogfood.toml` must be valid TOML.
  The fallback parser additionally truncates each line at the first `#`, so no token value may
  contain `#`. Tokens are `secrets.token_urlsafe`, alphabet `[A-Za-z0-9_-]`, so this holds by
  construction and a test asserts it.

### The fixture-title check

`fixture_titles(fixture, n=3)` takes the **first three projects in file order** and passes if
**any one** title appears as a case-insensitive substring of the gallery body:
`prj_01` "Glass Signal", `prj_02` "Small Meadow", `prj_03` "Deep Compass".

Position of those three among the 40 visible fixture projects (41 minus the duplicate):

| Sort | Glass Signal | Small Meadow | Deep Compass | best |
|------|--------------|--------------|--------------|------|
| newest first | 36 | 29 | 19 | **19** |
| name A→Z | 15 | 38 | 5 | **5** |

**Decision: default sort `newest`, default page size 50.** All 40 visible projects fit on page
one, so every title is present under any sort. 50 is defensible on its own merits — one
hackathon cohort per page — and page size 20 would already pass, so this is margin rather than
contrivance. Nothing in the code inspects the request to decide what to show.

All 41 titles are plain `[A-Za-z0-9 ]`, max 13 characters, so HTML autoescaping cannot break
the match.

### CSV detection (T2, recorded so the next session need not re-derive it)

`status == 200` and at least one comma in the first line. No content-type check, no parsing.

### Tier arithmetic

A tier counts only if all of its checks pass *and* every tier below it passed. Claiming
`["T1"]` with T1 green prints `claimed T1, verified T1` with no overclaim note, while the four
T2 lines read FAIL. That is the intended, honest output.

---

## Fixture reconnaissance

8 tracks · 30 judges · 40 teams · 41 projects · 126 scores. Event `evt_01` "Sample Hack 2026",
`submissions_close = 2026-03-01T18:00:00Z` — in the past, so an honestly seeded portal is
already closed.

Referential integrity is perfect: no dangling reference, no duplicate `(judge, project)` pair,
no judge scoring outside their assigned tracks.

Findings that de-risk the schema:

- **No email collisions.** 91 participant emails, 30 judge emails, disjoint sets, no email in
  two teams. So `unique(event, user)` on `TeamMember` and the participant-vs-judge conflict
  constraint both import cleanly with no special-casing. 121 users total.
- Team sizes 1–4, so `max_team_size = 4` admits every fixture team.
- `tm_01` "NorthKiln" members are `priya1@example.org`, `member1_1@…`, `member1_2@…` — priya1
  is first, therefore captain, which is what the checker's participant needs.
- `jdg_02` = Wei Lindqvist (trk_02, trk_04); `jdg_26` = Jonas Vogel (trk_03, trk_01).
- Duplicate team *names* are real: `StillTrail` ×3, `AmberSwitch` ×2, `OpenSignal` ×2. Teams
  import by id; `Team.name` is not unique.
- The planted duplicate: `tm_07` submitted `prj_07` "Dry Harbour" (`…/repo/07`, 04:29Z) and
  `prj_41` "Dry Harbour" (same repo, 17:57Z, three minutes before close). `prj_07` is
  canonical.
- Absent from the fixture: track `description`; project `description`, images, video, live URL,
  tags. These import **empty**; the import report lists them rather than inventing content.
- All 41 summaries are the same string, so tagline-weighted search is non-discriminating on
  fixture data. Noted in the README.
- Scores: `functionality`, `quality`, `innovation` on all 126 rows, values 2–5 (range stays
  1–5). 51 empty comments. Reviews per project 2–5, never zero.
- T2 fodder: scores per judge range 1–11; `jdg_07` gave all 4s across three projects and
  `jdg_01` a single all-2s review — the planted flat-scorer and unfinished batches. Judge means
  run 2.00 to 4.22.

### Synthesized event dates

`submissions_close_at` = 2026-03-01T18:00:00Z (from the fixture).
`starts_at` = `submissions_open_at` = close − 72h = 2026-02-26T18:00:00Z.
`judging_ends_at` = close + 10d = 2026-03-11T18:00:00Z. Slug `sample-hack-2026`.

Cross-checked: fixture `submitted_at` values span 2026-02-26T23:30Z – 2026-03-01T17:57Z, so
every project falls inside the synthesized window and the validator
`starts_at ≤ submissions_open_at < submissions_close_at ≤ judging_ends_at` holds. The window is
consistent with the data, not merely legal.

---

## Decisions and deviations

1. Gallery defaults: sort `newest`, page size 50 (above).
2. **Exact-path routes.** `urllib` follows redirects and rewrites POST to GET on a 301/302, so
   a route answering only via `APPEND_SLASH` would make check 3 measure the wrong thing. Every
   path advertised in `.dogfood.toml` is registered at exactly that string, asserted by test.
3. **`search_vector` maintained in the service layer**, not a Postgres trigger: one
   `SearchVectorField` + GIN index, recomputed on every write path and by the importer.
   Deterministic and testable, with no raw-SQL migration to defend.
4. **`scripts/acceptance.sh` probes `python3` then `python`** — `python3` is the Microsoft Store
   shim on the dev machine. The documented command stays `python3 acceptance/run.py`.
5. Track descriptions and absent project fields import empty, recorded in the import report.
6. **Login throttling is in**, DB-backed: 5 failures per (email, IP) per 15 minutes, counted
   from the audit-log rows the brief already requires. Survives restarts and applies across
   gunicorn workers, unlike a per-process cache counter.
7. **T2 route shape**: query parameter (`?judge=jdg_02`), so the peer probe is an explicit
   "read as another judge" request — the cleanest single thing for T2 to refuse.

No requirement in the brief conflicts with what `run.py` or the fixtures actually do.

### Routes committed in `.dogfood.toml`

```toml
gallery      = "/projects"
submit       = "/api/events/sample-hack-2026/projects"
judge_scores = "/api/judge/scores"                       # T2 — 404 until then
peer_scores  = "/api/judge/scores?judge=jdg_02"          # T2 — 404 until then
csv_export   = "/api/events/sample-hack-2026/export.csv" # T2 — 404 until then
```

---

## Phases

Each phase: run the suite, `docker compose down -v && docker compose up` from scratch, run the
checker (from phase 2, once tokens exist), update this log, one commit.
Stops for go-ahead after **phase 2, phase 4 and phase 7**.

1. **Skeleton** — Django project, custom User, settings, Docker/compose/entrypoint, `/healthz`,
   CLAUDE.md, LICENSE.
2. **Schema + import** — all models and migrations, `import_fixtures`, `seed_demo`, credentials
   printout, import tests.
3. **Auth + permissions + clock/deadline core** — login/signup/sessions, API tokens, Bearer
   auth, `core/permissions.py`, audit log, permission-matrix and deadline tests.
4. **Events + teams** — organizer event/track/prize/question management, memberships, team
   creation, invites, join/leave.
5. **Projects** — draft/edit/submit UI + API, uploads, protected media, custom answers.
6. **Gallery** — SSR gallery with search/filter/pagination, detail page, JSON API.
7. **Acceptance + docs** — `.dogfood.toml`, real checker run, README / ARCHITECTURE /
   DATA-MODEL / JUDGING, final clean-clone test with the network off.

---

## Progress log

### Phase 1 — Skeleton ✅ (2026-09-26)

Done:

- Django 5.2.17 project under `src/` with `config`, `core`, `accounts`. Dependencies pinned to
  versions verified as existing on PyPI, not guessed.
- **Custom `User` in the first migration** (`accounts/0001_initial`): email as login
  identifier, `display_name`, `is_platform_admin`, `can_create_events`, `external_id`.
  `ApiToken` alongside it (SHA-256 digest only, plaintext never stored).
- `core/clock.py` — injectable UTC clock. `core/deadlines.py` — the single deadline guard,
  half-open `[open, close)`. `core/errors.py` — stable error codes and the DRF exception
  handler. `core/authentication.py` — Bearer token auth, CSRF not enforced, with the reasoning
  written down.
- `core/admin_site.py` — admin gated on `is_platform_admin` rather than Django's weaker
  `is_staff`. The default `admin.site` is never routed.
- Dockerfile (python:3.12-slim, non-root user, `collectstatic` at build), compose (`db`
  postgres:16-alpine with healthcheck + named volume; `web` on 8080:8000 with a media volume
  and `acceptance/fixtures.json` mounted read-only), POSIX-sh entrypoint that waits for the
  database through Django's own connection.
- `/healthz` touches the database, so compose "healthy" means the portal can serve a page.
- Vendored Pico.css 2.0.6 (MIT) and htmx 2.0.4 (Zero-Clause BSD) with full licence texts in
  `src/static/vendor/NOTICE.md`. No CDN, no webfont; a test asserts this.
- `LICENSE` (MIT, Anurag V Rao), `CLAUDE.md`, `.env.example`, `.gitignore`, `.dockerignore`,
  `scripts/test.sh`.

Verified:

- `docker compose down -v` then `docker compose up` from scratch: migrations applied, gunicorn
  serving, compose healthcheck passing. `GET /healthz` → 200 `{"status":"ok","database":"ok"}`,
  `GET /` → 200, `GET /static/vendor/pico.min.css` → 200.
- **44 tests pass** (clock, deadline boundaries, admin gating, user model, API tokens, offline
  asset assertions).

Two things worth recording because they cost time:

- The generated migration exposed **redundant indexes**: `unique=True` already creates a B-tree
  index, so the extra `Meta.indexes` entries on `User.external_id` and `ApiToken.token_hash`
  were pure write-time cost. Removed and the migration regenerated.
- Test settings could not be applied from `conftest.py`: pytest-django calls `django.setup()`
  in `pytest_load_initial_conftests`, *before* the root conftest is imported, so environment
  variables set there arrive too late. Worse, the Dockerfile's `DJANGO_SETTINGS_MODULE`
  outranks `pytest.ini`. Fixed properly with `config/settings_test.py` plus `--ds` in
  `addopts`, which sits at the highest precedence level. `DEBUG` stays False in tests.

Deviations from the phase plan, both deliberate:

- `core/clock.py`, `core/deadlines.py`, `core/errors.py` and `core/authentication.py` landed in
  phase 1 rather than phase 3. `settings.py` references the auth class and exception handler,
  and a half-declared module would have been a stub. Their *tests* for the HTTP paths still
  come in phase 3 as planned; the pure-logic boundary tests are already written.
- The entrypoint's `import_fixtures` / `seed_demo` steps arrive with phase 2, when those
  commands exist. `.dogfood.toml` and `scripts/acceptance.sh` likewise land in phase 2, when
  there are tokens to put in them.

### Phase 2 — Schema + import ✅ (2026-09-26)

Done:

- **All T1 models**, across `events` (Event, Track, Prize, EventMembership, JudgeTrack,
  CustomQuestion), `teams` (Team, TeamMember, TeamInvite), `projects` (Project, ProjectImage,
  Tag, ProjectTag, CustomAnswer), `scoring` (Criterion, Score, ScoreItem) and `core` (AuditLog).
  `external_id` everywhere importable, `created_at`/`updated_at` throughout.
- **`import_fixtures`** — imports the real organizer file with counts matching the fixture
  exactly: 1 event, 8 tracks, 30 judge users + 30 memberships + 39 track assignments, 40 teams,
  91 participants, 91 team members, 41 projects, 3 criteria, 126 scores, 378 score items,
  9 demo prizes. Flags `prj_41` as a duplicate of `prj_07`. Prints a report that separates what
  came from the fixture from what was synthesized or left empty.
- **`seed_demo`** — admin, organizer, the two named fixture judges, priya1 as the checker's
  participant, five fixed-value Bearer tokens, and the open "Dogfood Live Demo" event with the
  8 mirrored tracks, 3 prizes, 3 custom questions (one required), a team with a draft project
  and a copyable invite link. Prints the credentials block at every boot.
- Entrypoint now runs `migrate` → `import_fixtures` → `seed_demo`, all idempotent.
- `.dogfood.toml`, `scripts/acceptance.sh` (probes `python3` then `python`), `scripts/test.sh`.
- **112 tests pass in 14s.**

Two schema decisions worth defending:

- **The conflict-of-interest rule is enforced by Postgres, not just by services.** A CHECK
  constraint cannot see other rows, and a unique index on `(user, event)` would wrongly forbid
  judge + organizer. So `EventMembership.side` is a *stored generated column*
  (`participant → competitor`, else `staff`), computed by the database, and an
  `ExclusionConstraint` forbids two rows for the same `(user, event)` disagreeing about it.
  Verified in the live database: `EXCLUDE USING gist (user_id WITH =, event_id WITH =, side
  WITH <>)`. Needs the `btree_gist` extension, created by the migration and documented for
  self-hosters.
- **Idempotency is real, not approximate.** `update_or_create` issues an UPDATE every time,
  which would bump `updated_at` on ~700 rows on every container restart. `seed/upsert.py`
  compares field by field and writes only on a genuine difference, so the second boot reports
  every row `unchanged` and touches nothing — asserted by a test that snapshots `updated_at`.

Also fixed: the first generated migration revealed redundant indexes (a `unique=True` column
already has a B-tree index), and the test suite was importing the 700-row fixture once per test
(95s). A session-scoped import inside `django_db_blocker.unblock()` cut the whole suite to 14s
while keeping per-test rollback isolation.

**Honest note on the committed `acceptance-report.txt` at this phase.** It currently reads
`claimed T1, verified nothing`, because `/projects` (phase 6) and the submit API (phase 5) do
not exist yet. Worse, `T1 closed event refuses submissions` reads **PASS for the wrong reason**:
the route 404s, and 404 is inside the 4xx range the checker accepts. Phase 5 must make that
route return a genuine **409 `submissions_closed`**, and the test suite asserts the specific
status and error code rather than "any 4xx" — precisely so this cannot be mistaken for working.

#### Two review findings fixed before committing phase 2

**1. The import was idempotent but *authoritative*, which is worse.** `upsert` compared against
the fixture and wrote back on any difference. Since the entrypoint runs the import on every boot,
that meant: an organizer extends `submissions_close_at` (which the brief explicitly permits),
someone restarts the container, and the deadline silently reverts to 2026-03-01. It broke a T1
feature and contradicted this repo's own rule that seeding never overwrites user data.

Fixed properly rather than patched:

- `upsert` is now **create-only by default** — inserts missing rows, never modifies existing ones.
- A row that exists and differs is reported as `preserved`, naming the diverged fields, so the
  divergence is visible instead of silent. The boot log now prints
  `event: Event evt_01: judging_ends_at, submissions_close_at` and a stderr warning.
- `import_fixtures --sync` restores overwriting, for an operator who deliberately wants the
  fixture to win.
- `seed_demo`'s two writes that legitimately must touch existing rows — the demo event's expired
  window, and the demo password — are now explicit narrow updates with their reasoning, not a
  side effect of a generic helper.
- `SEED_FIXTURES=0` skips the boot import entirely, so a production deployment need not
  materialize 121 invented accounts. Defaults to 1 because `docker compose up` must reach a
  gallery that really shows the fixture projects.

Verified on a live stack: edited `evt_01`'s close date in Postgres, restarted the container, and
the edit survived while the log named the diverged fields; `--sync` then restored the fixture
value; `SEED_FIXTURES=0` skipped the import. Four new tests cover all of it.

**2. Line-ending hardening.** `.gitattributes` already pinned `*.sh` and `docker/entrypoint.sh`
to `eol=lf` (which overrides `core.autocrlf`, so a normal Windows clone was already safe — the
index stores `#!/bin/sh\n`). Extended anyway: explicit rules for `Dockerfile`, `*.py`, `*.yml`,
`*.toml`, plus `sed -i 's/\r$//'` on the entrypoint in the Dockerfile. That covers the paths
`.gitattributes` cannot — a GitHub ZIP download, or a file round-tripped through a Windows editor
— where the failure mode is `/bin/sh^M: bad interpreter` and no useful clue why.

### Phase 3 — Auth, permissions, audit wiring ✅ (2026-09-26)

Done:

- **`core/permissions.py`** — every authorization decision in the portal, as named policy functions
  plus queryset scopers. No inline role checks anywhere else.
- **`accounts/services.py`** — signup, login, logout, password change, token create/revoke. Views
  are thin: validate the form, call a service, render.
- **Login throttling**, DB-backed: 5 failures per (email, IP) per 15 minutes, counted from the
  `login.failed` audit rows the brief already requires. One store, survives restarts, applies
  across both gunicorn workers.
- **Auth UI**: signup, login, logout (POST only), password change, and a profile page listing the
  caller's per-event roles and their API tokens. A new token's plaintext is shown exactly once and
  is not stored anywhere that survives a refresh.
- **Audit log in the admin**, deliberately append-only: no add, no change, no delete, not even for
  a platform admin.
- **195 tests pass** (+83 this phase): 32 auth/throttle, 17 Bearer-token, 30 permission, plus a
  `tests/factories.py` of builders that phases 4–6 reuse.

Decisions worth defending:

- **Throttle scope is (email, IP) together.** By email alone, anyone could lock a known user out of
  their own account by failing five times on their behalf. By IP alone, one fumbled password locks
  out everyone behind a shared NAT.
- **`X-Forwarded-For` is ignored unless `TRUST_PROXY_HEADERS=1`.** It is client-supplied, so
  trusting it by default would let anyone forge the IP the throttle counts against.
- **Login is not an account-existence oracle**: unknown email and wrong password return the same
  status and the same message. Tested.
- **`next=` is validated as a same-site path**, so the login form cannot be used as an open
  redirect. Absolute and protocol-relative URLs both fall back to `/`.

#### A spec conflict I resolved, and you should know about

The permission matrix lists **"no"** for both organizer and admin on *create/edit/submit project*,
while §4's prose says "Admin can do everything". I implemented the matrix, because an admin able to
rewrite a submission after the deadline would undermine the one guarantee this software exists to
provide. Admins and organizers get full *visibility* (drafts included) and moderation (hide, flag
duplicate) instead — they never author on a team's behalf. It is commented at
`core/permissions.py:can_edit_project` and asserted by a test. Say the word if you want the looser
reading instead.

#### A "bug" that wasn't

A live curl check showed `next=/profile` redirecting to `/` while the test passed. The cause was
Git Bash: MSYS rewrote `/profile` into `C:/Program Files/Git/profile` before curl ever sent it, and
`safe_next` correctly refused a non-relative path. The application was right the whole time — and
the open-redirect guard even did the right thing with mangled input. Recorded in CLAUDE.md, since
the same quirk had already cost time on container paths.

Also noted: the session-scoped fixture import means ~40 organizer projects are always in the test
database, so three visibility tests that asserted on whole-table contents passed alone and failed
in the suite. Fixed by scoping them to their own rows, and one test now deliberately checks the
object-level and queryset-level visibility rules agree across the entire real fixture set —
including `prj_41`, the row where they are most likely to diverge.

### Phase 4 — Events and teams ✅ (2026-09-26)

Done:

- **Organizer management**: create/edit events (every date labelled UTC, with a `UTCDateTimeField`
  so a `datetime-local` input cannot silently shift a deadline into the viewer's timezone),
  tracks, prizes, custom questions, and a `reorder` helper shared by all three.
- **Refuse-on-referenced-delete**: a track with projects cannot be deleted (checked before the
  `PROTECT` constraint would raise a 500), and a question with answers cannot be deleted — nor can
  its `kind` change, which would silently reinterpret every stored answer.
- **Memberships**: add a judge (with tracks) or co-organizer by email. Refuses an unknown address
  (no email delivery exists, so an invitation cannot be sent), refuses a participant as a
  conflict of interest, and refuses removing the last organizer.
- **Organizer dashboard**: counts, every project including drafts, flagged duplicates, teams,
  judges/organizers, and the 25 most recent audit entries for the event.
- **Teams**: create (creator becomes captain and is registered as a participant), `/invite/<token>`
  landing page that works while logged out, join, leave, transfer captaincy.
- **All seven named invite refusals**, each with its own sentence, asserted distinct by test:
  unknown, expired, revoked, exhausted, team full, already on a team, judge/organizer.
- **293 tests pass** (+98 this phase).

#### The bug this phase nearly shipped

`tests/test_invites.py::test_a_refused_join_is_audited` failed, and the cause was worth the
detour: **every service function was decorated `@transaction.atomic`**, so when a guard recorded
"refused: deadline" and then raised, the audit row was rolled back along with the write it
described. The audit trail would have contained *nothing* for refusals — silently failing the
brief's explicit requirement to log "every refused write due to the deadline".

`core/guards.py` already documented why the guard must run outside the transaction; the service
layer then violated it. Fixed properly: no service function is decorated any more. Guards run
first, outside any transaction, and only the writes sit in an explicit `with transaction.atomic():`
block — added just where several writes must land together (event + first organizer membership,
membership + judge tracks, demote + promote on captaincy transfer, the reorder sweep).
`join_via_invite` catches its own refusal outside the atomic block specifically so the entry
survives.

`tests/test_audit_trail.py` now pins this down, including a parameterized sweep over all six
guarded team paths asserting each records its own refusal with the attempted action named. Also
verified: a deadline refusal records the exact `closed_at` instant, so an organizer can see how
late an attempt was.

Two smaller fixes: a URL namespace (`include((patterns, "invites"))`) made every
`reverse("invite_accept")` fail at runtime rather than at import — now included as a bare pattern
list; and `save_track` recorded `created` by testing `pk is None` *after* saving, which is never
true.

Verified on the live stack, not just in tests: signed up a new account arriving from the seeded
invite link, was returned to the invite rather than the home page, accepted it, and the team page
showed both members with the captain flag and the draft project. Then removed that test account
through the ORM — a raw SQL `DELETE` was correctly refused by the foreign keys, since Django
emulates cascades in Python rather than in the database.

Next: phase 5 — project draft/edit/submit UI and API, uploads with Pillow verification, protected
media, custom answers. This is the phase that must turn the acceptance checker's third T1 check
from an accidental 404 into a genuine 409 `submissions_closed`.
