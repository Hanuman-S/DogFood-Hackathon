# Architecture

> **T1 snapshot.** This describes the system as it stands with tier T1 complete. It will be expanded
> after T2 (judging) is built; the judging layer is deliberately absent here rather than sketched, so
> nothing in this document describes code that does not exist.

Boring Django, deliberately. Server-rendered templates, one database, no SPA, no queue, no cache
layer, no background worker. The interesting decisions are all about *where a rule lives*, because
the thing this software is judged on is whether its guarantees actually hold.

## Components

```
                    ┌──────────────────────────────────────────────┐
  browser ─────────▶│ gunicorn ── Django 5.2                       │
  (HTML, htmx)      │                                              │
                    │  views ──▶ services ──▶ models ──▶ Postgres 16│
  API client ──────▶│    │          │                              │
  (Bearer token)    │    │          ├─ core.clock                  │
                    │    │          ├─ core.deadlines              │
                    │    │          ├─ core.permissions            │
                    │    │          └─ core.audit                  │
                    │    └─ WhiteNoise (vendored CSS/JS)           │
                    │    └─ protected media view (uploads)         │
                    └──────────────────────────────────────────────┘
```

Two containers: `web` (gunicorn) and `db` (postgres:16-alpine). Uploaded images live on a named
volume. Static assets are collected into the image at build time and served by WhiteNoise. Nothing
reaches the network at runtime.

## Request flow

A participant write — submitting a project, creating a team, uploading an image — always runs the
same five steps, in this order:

```
authenticate → resolve the event (404 if missing) → deadline → permission → validation
```

The order is the design. The deadline is checked **before** permissions and **before** the request
body is examined, so a late submission is refused *as a late submission* rather than being masked by
a validation error or a permission message. `core.deadlines.assert_submissions_open` is the only
implementation of that comparison in the codebase, and the window is half-open `[open, close)` — a
deadline of 18:00 refuses 18:00:00 itself.

In the API this ordering is visible as a structural detail: the body is validated on a **later
statement** than the guard call, never as an argument to it. Python evaluates arguments before the
call, so `service(..., **validate(body))` would run validation first and turn a late request into a
400. `tests/test_api_submit.py` posts a deliberately unrecognisable body to a closed event and
requires 409.

## The layers

### Views are thin

A view authenticates, resolves objects, calls a service function, renders the result. No view
contains a deadline comparison, a role check or an audit write.

The reason is that the UI and the API must enforce identical rules. Two code paths to the same write
is how a platform accepts a late submission through one door and refuses it at another.

### Services own the rules

`<app>/services.py` holds every write. Each public function calls the deadline guard, then the
permission guard, then validates, then writes, then records an audit entry — and each is safe to call
on its own, because a public write that trusts its caller to have checked is one refactor away from
being unguarded.

Transactions are explicit and narrow. No service is decorated `@transaction.atomic`: guards run
*outside* any transaction so that the audit row describing a refusal survives the exception that
follows it. An early version had every service decorated, which silently discarded exactly the
evidence the brief requires keeping; `tests/test_audit_trail.py` now sweeps every guarded path and
asserts the refusal row is in the database afterwards.

### `core.clock` is the only source of time

`datetime.now()`, `utcnow()` and `django.utils.timezone.now()` appear nowhere in application code.
`core/clock.py` is UTC-aware, refuses naive datetimes, renders ISO-8601 with `Z`, and is injectable so
tests pin an instant without freezing the process clock.

Each service entry point reads the clock **once** and passes that instant to both the guard and any
timestamp it stores. Two separate reads leave a window in which a submission is admitted by the guard
and then stamped with a time after the deadline.

### `core.permissions` holds policies and scopers

Named predicates (`can_edit_project(user, project)`) and queryset scopers (`visible_projects(user)`).
Views and API endpoints call them; no view contains an inline role check, and a template hiding a
button is a convenience, never the enforcement.

Roles are **per event** (`events.EventMembership`), except the platform-admin flag. A resource the
caller may not know exists — another team's draft — returns **404**, not 403, because a 403 confirms
it exists.

Two decisions worth stating because they look like bugs otherwise:

- **Organizers and platform admins cannot author projects.** The permission matrix says no for both
  on create/edit/submit, and that specific rule beats the looser "an admin can do anything" prose
  elsewhere: an admin able to rewrite a submission after the deadline would undermine the one
  guarantee this software exists to provide. They get full visibility and moderation instead.
- **The gallery uses a different scoper from everything else.** `gallery_projects()` takes no user at
  all and returns the same rows to everyone. If a team saw its own unsubmitted draft in the public
  listing it would conclude the draft was public and never press submit. Drafts remain reachable at
  `/projects/<id>` for the people entitled to them — that page *is* viewer-dependent.

### Errors become HTTP statuses in one place

Every refusal is a `core.errors.PortalError` subclass carrying a stable code. DRF renders them
through one exception handler; the UI renders the same message in a banner **with the same status**,
so a write refused by the deadline answers 409 in a browser too.

No write path may answer 500. Two translations exist for that reason: `Model.full_clean()`'s Django
`ValidationError` becomes a 400 (or a 409 when it names the one-active-project constraint), and an
`IntegrityError` naming that constraint becomes the same 409 — while every *other* `IntegrityError`
is re-raised, because disguising an unknown one as a conflict hides a real bug.

### Authentication: two classes, one of them CSRF-free

- `core.authentication.BearerTokenAuthentication` — `Authorization: Bearer <token>`, SHA-256 digests
  only, **no CSRF**. Correct rather than merely convenient: a bearer token is not an ambient
  credential, so a third-party page cannot make a browser attach it. This is also what makes the
  acceptance checker's closed-event POST a real test of the deadline, since it can attach exactly one
  header and therefore never a CSRF token.
- DRF `SessionAuthentication` — for the browser, with CSRF enforced normally.

Login throttling is database-backed, counted from the audit rows that already have to exist, scoped
to (email, IP). It survives restarts and applies across gunicorn workers, which a per-process counter
would not.

## Uploads and media

Uploaded files are hostile input. `projects.services.verify_image` ignores the extension, the
filename and the declared content type, and consults only what the bytes decode to: a size check, a
40-megapixel ceiling with Pillow's decompression-bomb *warning* promoted to an error, `verify()` then
a rewind-and-reopen (verify leaves the image unusable), a format allow-list, and finally a
**re-encode**. Re-encoding strips EXIF — including the GPS tags a phone attaches — and neutralises
polyglot files, which pass a format check but do not survive having only their pixels copied.

Nothing serves `MEDIA_ROOT`. `MEDIA_URL` is `None` so a stray `{{ image.url }}` fails loudly instead
of leaking a path, and every image goes out through a Django view that re-applies the owning
project's visibility rules, sends the Content-Type recorded at upload, adds
`X-Content-Type-Options: nosniff`, and uses `Cache-Control: private, no-store` for anything not
publicly visible.

## Search

One `SearchVectorField` with a GIN index, maintained in the service layer on every write rather than
by a database trigger — a trigger would be invisible to the test suite and would need raw SQL in a
migration to defend. Weights: project name A, tagline/tags/team B, description C.

Visitor input goes through `websearch_to_tsquery`, never `to_tsquery` and never
`search_type="raw"`: those expect operator syntax and raise a database error on an apostrophe or a
stray `&`. A second matcher covers partial words in the name (`icontains`, backed by a pg_trgm GIN
index on `Upper(name)`), because the vector matches whole stemmed words and three letters is what
people type.

## The fixture import, and why it bypasses the guards

`acceptance/fixtures.json` is historical: its event closed in March 2026, so no participant write
path could ever create its rows. `src/seed/importer.py` therefore writes through the ORM directly and
never calls a participant service function. Two rules keep that bypass honest: it is imported only by
the two management commands and appears in no `urls.py`, and its methods are named `_import_*` /
`_seed_*` so a call site reusing one for a live write would be doing so obviously.

The import is **create-only**. It runs on every boot, so an importer that wrote the fixture value back
whenever it differed would revert real edits — an organizer extends a deadline, the container
restarts, and the deadline silently snaps back. A drifted row is reported as `preserved` with the
differing field names and left alone; `--sync` is the opt-in escape hatch only a human runs.

## Testing

556 tests, in the container, against the real Postgres — the constraints, the exclusion constraint,
the partial unique indexes and the full-text search are all things SQLite cannot check. `DEBUG` stays
`False` so tests exercise production error handling. The organizers' fixture file is imported once per
session (about 700 rows) and each test's own writes roll back around it, which cut the suite from 95
seconds to under 30.

Three kinds of test carry more weight than the rest and should be kept if anything is ever trimmed:
the audit sweep over every guarded write path, the set-equality test proving the gallery is
viewer-independent, and the parameterized garbage-input sweeps that assert nothing returns 500.
