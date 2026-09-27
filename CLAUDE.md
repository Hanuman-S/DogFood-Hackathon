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
  gallery/           public gallery: selectors.py holds the one query both doors use
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

### 8. Uploads are hostile input, and uploaded bytes are served by a view

`projects.services.verify_image` is the only way a file reaches storage. It ignores the extension,
the filename and the browser's declared content type, and consults only what the bytes decode to:
size checked first, `Image.MAX_IMAGE_PIXELS` capped at 40M with `DecompressionBombWarning`
**promoted to an error**, `verify()` then a rewind-and-reopen (verify leaves the image unusable),
format checked against `settings.ALLOWED_IMAGE_FORMATS`, then the image **re-encoded**. The
re-encode is not cosmetic: it strips EXIF, including the GPS tags a phone attaches, and it
neutralises polyglot files, which pass a format check but do not survive a decode. Stored names are
random; the uploaded name never reaches the filesystem or a URL.

Serving goes through `projects/views.py`, never a static handler -- `settings.MEDIA_URL` is `None`
so a stray `{{ image.url }}` fails loudly. The view re-applies `can_view_project` (404, not 403),
sends the Content-Type recorded from the decoded format, adds `X-Content-Type-Options: nosniff`, and
uses `Cache-Control: private, no-store` for anything not publicly visible so a draft's image cannot
sit in a shared cache.

Deleting a stored file happens in `transaction.on_commit`. Inline deletion means a rolled-back
transaction leaves a row pointing at nothing; the deferred version can only leave an orphan file,
which is the better direction to fail in. Orphans are documented in the README, not swept up by a
daemon.

### 9. No write path may answer 500

Two translations exist because of this, and both are easy to undo by accident:

- `Model.full_clean()` raises Django's `ValidationError`, which the API exception handler knows
  nothing about. `projects.services._full_clean` maps it to `ValidationFailed` (400), or to
  `ProjectExists` (409) when it names the one-active-project constraint. Any new service that calls
  `full_clean` needs the same treatment.
- An `IntegrityError` naming `project_one_active_per_team_per_event` becomes the same 409. Every
  other `IntegrityError` is re-raised: disguising an unknown one as a conflict hides a real bug
  behind a plausible message.

Counts that a constraint cannot express are enforced with a row lock. `add_image` takes
`select_for_update()` on the project before counting, because count-then-insert under READ COMMITTED
lets two uploads both read 7 and both insert.

### 10. Guard inside the service, and read the clock once

Every *public* function in a services module runs the deadline guard then the permission guard
itself. Not just the top-level ones: `set_tags`, `add_image` and friends are each reachable from a
view, and a public write that trusts its caller to have checked is one refactor from being an
unguarded write. `tests/test_audit_trail.py` sweeps all of them, and a reflection test there fails
if a new public write appears without being added to the sweep.

Each entry point takes `now = clock.now()` once and passes it to the guard *and* to any timestamp it
stores (`assert_submissions_open(event, now=...)`). Two separate reads leave a window where a
submission is admitted by the guard and then stamped with a time after the deadline.

Where a request body has to be validated, validate it **after** the guards -- on a later statement,
not in the same expression. Python evaluates arguments before the call, so
`service(..., **validate(body))` runs the validation first and turns a late request into a 400.

### 11. The gallery's scoper is viewer-independent, and must stay that way

`gallery.selectors` calls `permissions.gallery_projects()` and **never** `visible_projects(user)`.
Neither `gallery_queryset` nor `gallery_page` takes a `user` or `request` argument, so the
distinction cannot be blurred by accident, and `tests/test_gallery.py` asserts both that the
signatures have no such parameter and that the rendered listing is identical for an anonymous
visitor, a team member with a draft, an organizer and a platform admin.

The reason is a participant trap, not a permission one. If a team saw their own unsubmitted draft in
the public listing, they would reasonably conclude it was public and never press submit. An organizer
seeing hidden projects there would have no way to know what the public actually sees. Drafts stay
reachable at `/projects/<id>` for the people entitled to them -- *that* page is viewer-dependent --
but the listing shows one thing to everyone.

`GET /api/projects` calls the same `gallery_page()`. Not "applies the same rules": the same function.
A second filter implementation is how a JSON endpoint ends up listing a draft the HTML gallery hid.

### 12. Search takes user input, so it uses `websearch_to_tsquery`

`projects.search.search_projects` passes `search_type="websearch"`. **Never `"raw"`, and never
`to_tsquery`**: those expect operator syntax and raise a database error on ordinary human input, so
a stray `&`, an unbalanced quote or a `:` in the search box becomes a 500.
`tests/test_gallery.py` sweeps a dozen such strings.

Two matchers are OR'd because they fail in opposite directions: the `search_vector` matches whole
stemmed words, and `name__icontains` catches the three letters a visitor actually typed. The second
is backed by a pg_trgm GIN index on `Upper("name")` (migration 0003) -- `Upper` because that is what
Django renders `icontains` as on Postgres; an index on the bare column would not be used.

Tags are filtered by **exact normalized name**, never through the vector: stemming would make
`react` match `reactivity`, and a tag pill has to mean exactly that tag.

### 13. Every ordering ends in a primary-key tiebreak

`gallery.selectors.SORTS` is `(-submitted_at, -id)` and `(name, id)`. Without the `id`, rows that tie
on the leading column come back in whatever order Postgres finds convenient, and LIMIT/OFFSET over an
unstable ordering shows a visitor one project twice and another not at all. Any new sort needs the
same tiebreak; a test asserts each entry ends in `id` or `-id`.

### 14. Query-string parameters are parsed tolerantly, and never reach the database raw

`GalleryQuery.from_params` cannot raise. A malformed scalar (`page=abc`, `sort=purple`) falls back to
the default; a filter naming something that does not exist matches nothing, which is deliberate --
dropping an unresolvable filter would widen the result set and show projects the visitor did not ask
for.

Text parameters go through `_clean_text`, which strips C0 control characters. This is not tidiness:
psycopg rejects a **null byte** in a query parameter before the query is even sent, so `?q=a%00b`
was a 500 until that existed. `tests/test_gallery.py` parameterizes 25 garbage query strings and
requires 200 from every one.

Page counts are pinned with `django_assert_num_queries` (7 for the HTML page, 4 for the API), plus a
test that compares the count at 2 and 22 projects. The gallery is the most-reloaded page in the
portal; an N+1 there is the difference between a demo and a stall.

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

### Testing with curl from Git Bash on Windows

Set `MSYS_NO_PATHCONV=1` first. MSYS rewrites any argument that looks like a POSIX path, so
`--data-urlencode "next=/profile"` is silently sent as `next=C:/Program Files/Git/profile`, and a
container path like `/app/src/manage.py` becomes `C:/Program Files/Git/app/src/manage.py`. Both
produce confusing failures that look like application bugs. One session was spent chasing a
redirect "bug" that was entirely this.

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

Before starting any phase, read plan.md — especially the latest handoff section.
