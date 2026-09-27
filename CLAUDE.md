# CLAUDE.md — conventions for this repository

Read this first. It is written for a future session picking up work on this portal (T2
judging, and beyond) and it records the decisions that are easy to break by accident.

## What this is

A self-hostable hackathon submission and judging platform, built for the DOGFOOD 2026
hackathon. `AboutTheHackathon.md` is the full brief. Tier T1 (core) is implemented; T2
(judging) is not — see `JUDGING.md` for what exists and what does not.

## The one rule that outranks the others

**Never edit anything inside `acceptance/`.** Those files belong to the organizers:
`run.py` is the acceptance checker, `fixtures.json` is the shared dataset every team loads.
They are read-only inputs. If a requirement seems to conflict with what `run.py` actually
does, the code changes, not `run.py`.

Related: never special-case the checker. No branch anywhere may inspect a user agent, a
header or a known fixture title to decide what to return. The checks pass because the portal
behaves correctly, or they fail honestly.

## Stack

Python 3.12 · Django 5.2 LTS · PostgreSQL 16 · Django REST Framework · Gunicorn ·
WhiteNoise · Pillow · `markdown-it-py` + `nh3` · pytest + pytest-django.

Server-rendered Django templates with HTMX for small interactions. No SPA. Versions are
pinned exactly in `requirements.txt`.

Vendored front-end assets live in `src/static/vendor/` (Pico.css, htmx) with provenance and
licences in `src/static/vendor/NOTICE.md`. **Never add a CDN link, a Google Font or any
external asset reference.** The portal must render with the network off; `tests/test_smoke.py`
asserts this and will fail if a CDN URL appears in a template.

## Layout

```
acceptance/          organizer files — READ ONLY
src/
  config/            settings, urls, wsgi. settings_test.py holds test-only overrides
  core/              clock, deadlines, permissions, errors, audit log, token auth
  accounts/          custom User (email login), ApiToken, signup/login/profile
  events/            Event, Track, Prize, EventMembership, JudgeTrack, CustomQuestion
  teams/             Team, TeamMember, TeamInvite
  projects/          Project, ProjectImage, Tag, CustomAnswer, protected media serving
  gallery/           public gallery views + search
  scoring/           Criterion, Score, ScoreItem — models + fixture import only (T2 uses them)
  api/               DRF views, serializers, urls
  seed/              management commands: import_fixtures, seed_demo
tests/               pytest suite
docker/              entrypoint.sh
scripts/             test.sh, acceptance.sh
```

## Conventions that must not be broken

### 1. All writes go through the service layer

Views are thin. A view authenticates, resolves objects, calls a service function and renders
the result. Business rules — deadline checks, permission checks, audit logging, search-vector
maintenance — live in each app's `services.py`, never in a view, a form, a serializer or a
model's `save()`.

Why: the UI and the API must enforce identical rules. Two code paths to the same write is how
a platform ends up accepting a late submission through one door and refusing it at another.

### 2. Time comes only from `core.clock.now()`

Never call `datetime.now()`, `datetime.utcnow()` or `django.utils.timezone.now()` in
application code. `core/clock.py` is the single source of time, always UTC-aware, and it is
injectable so tests can pin an instant without freezing the process clock.

Everything is stored in UTC. Every date the UI renders or accepts is labelled UTC.

### 3. Deadlines are enforced by `core.deadlines.assert_submissions_open(event)`

One implementation, called by every participant write path: create/edit/submit a project,
upload or delete images, change tags or answers, create a team, join or leave via invite,
create or revoke an invite.

The window is half-open `[open, close)` — **the close instant itself is refused.** A refusal
raises `SubmissionsClosed`, which the API renders as HTTP 409 with
`{"error": "submissions_closed", "closed_at": "<ISO UTC>"}` and the UI renders as a banner
naming the instant, also with status 409.

Order of checks on every write:

```
authenticate → resolve event (404 if missing) → deadline → permission → validation
```

The deadline is checked before permissions and before the request body is examined, so a late
write is refused *as a late write* and never masked by a validation or CSRF error.

### 4. The import path bypasses the deadline guard, and is unreachable over HTTP

Fixture data is historical and already past its deadline, so no participant write path could
ever create it. `src/seed/importer.py` is therefore the import path: it writes through the ORM
directly, never calls `assert_submissions_open`, and never calls a participant service
function. Two rules keep the bypass honest:

1. It is imported only by the `import_fixtures` and `seed_demo` management commands, and appears
   in no `urls.py`.
2. Its methods are named `_import_*` / `_seed_*`, so a call site reusing one for a live
   participant write would be doing so obviously.

`seed/upsert.py` is what makes both commands idempotent, and it is **create-only by default**:
it inserts missing rows and never modifies existing ones. Use it for any new seeding code rather
than `update_or_create`.

Both properties matter, for different reasons:

- *Idempotent*: `update_or_create` issues an UPDATE unconditionally, so a boot would bump
  `updated_at` on hundreds of rows without importing anything new.
- *Not authoritative*: the entrypoint runs the import on **every boot**. An importer that wrote
  the fixture value back whenever it differed would revert real edits — an organizer extends
  `submissions_close_at`, the container restarts, and the deadline silently snaps back. A drifted
  row is reported as `preserved` with the differing field names and left alone.
  `import_fixtures --sync` opts into overwriting and only a human runs it.

If you add seeding that genuinely must write to an existing row (the demo event's window refresh
is the one example), do it as an explicit narrow `.update()` with a comment, not by flipping
`sync=True` on a shared helper.

`SEED_FIXTURES=0` skips the boot-time import entirely, for a real deployment that does not want
121 invented accounts. It defaults to 1 because `docker compose up` must reach a portal whose
gallery actually shows the fixture projects.

### 5. Permissions live in `core/permissions.py`

Named policy functions (`can_edit_project(user, project)`) and queryset scopers
(`visible_projects(user)`). Views and API endpoints call them. **No inline role checks in
views.** A template hiding a button is a convenience, never the enforcement.

Roles are **per event** (`events.EventMembership`), except `User.is_platform_admin`. In one
event a user cannot be both a participant and a judge/organizer — enforced in the service
layer *and* by a database constraint.

Resources a caller is not allowed to know exist (another team's draft) return **404**, not
403: a 403 confirms the thing exists.

### 6. Fixture-imported rows carry `external_id`

Every model importable from `fixtures.json` has a nullable, unique `external_id`
(`evt_01`, `prj_41`, …). Imports upsert by it, which is what makes them idempotent. Never
match fixture records by name — the fixture contains duplicate team names on purpose
(`StillTrail` ×3).

### 7. API authentication

Two classes, both configured in `REST_FRAMEWORK`:

- `core.authentication.BearerTokenAuthentication` — `Authorization: Bearer <token>`, hashed
  SHA-256 in `accounts.ApiToken`, **no CSRF**. Correct because a bearer token is not an
  ambient credential: a third-party page cannot make a browser attach it.
- DRF `SessionAuthentication` — for the browser, CSRF enforced as normal.

The acceptance checker can attach exactly one header, so it cannot send a CSRF token. The
Bearer path is what makes its closed-event POST a real test of the deadline.

## Running things

```bash
docker compose up                      # migrated, seeded portal on http://localhost:8080
docker compose down -v                 # and forget the data
./scripts/test.sh                      # full suite in the container
./scripts/test.sh tests/test_clock.py -vv
./scripts/acceptance.sh                # the organizers' checker → acceptance-report.txt
```

Tests use `config.settings_test`, forced via `--ds` in `pytest.ini` because the Dockerfile
exports `DJANGO_SETTINGS_MODULE` and the env var would otherwise outrank the ini setting.
`DEBUG` stays False in tests so they exercise production error handling.

Generating a migration (no database needed):

```bash
docker compose run --rm --no-deps --entrypoint python -v "${PWD}/src:/app/src" \
    web /app/src/manage.py makemigrations <app>
```

## Honesty rules

These are scored, and they matter more than feature count:

- `.dogfood.toml` claims only tiers that actually pass. T2 routes point at the paths T2 will
  implement and are expected to **FAIL** until then. Never add a stub endpoint that fakes a
  pass.
- `acceptance-report.txt` is committed from a real run, failures included.
- Anything cut goes in the README's "Not done yet" section. Never claim a feature that does
  not work.
- The importer records synthesized values (the fixture supplies only `submissions_close`, so
  the other event dates are derived) rather than presenting them as fixture data. It never
  invents content for absent fields.

## Style

Boring, readable Django over cleverness. Thin views, explicit service functions, comments
that explain *why* rather than restate the code. Match the surrounding file.
