# Architecture

## Shape of the system

```
browser ──HTML forms──┐
                      ├──►  gunicorn ─► Django ─► Postgres 16
script  ──Bearer API──┘      (web)                 (db)
```

This is one Django 5.2 LTS app in one container, with Postgres in a second. Pages are
rendered on the server with plain CSS and a few small scripts, so there is no build step and
no single-page app. Everything the browser needs, including the fonts, is served by the app
itself (WhiteNoise). The Content-Security-Policy forbids every other origin, so the offline
rule is enforced by the browser, not just by review.

### Why Django

| | Django (chosen) | FastAPI + hand-written HTML | Node (Express/Hono) |
| --- | --- | --- | --- |
| Sessions, CSRF, password hashing | built in, well audited | hand-built or several libraries | several libraries |
| Migrations and admin | built in | Alembic; no admin | Prisma/Knex; no admin |
| OpenAPI for the API bonus | extra package later | free | extra package |
| Speed of building within 72 h | fastest | slower | slower |

The hackathon's rules punish broken auth far more than they reward a fashionable stack. Django
gives us the security-critical parts pre-built and lets us spend the time on judging.

### One app per audience

```
public/         /               visitors
participant/    /participant/
judge/          /judge/
organizer/      /organizer/     (admins too)
platform_admin/ /admin/         and the database admin at /admin/db/
```

Each portal is its own Django app with its own `urls.py`, `views.py` and `templates/<app>/`.
Models and rules shared between portals live in `events/`, `teams/` and `projects/`. A
teammate can own one portal and rarely touch anyone else's files. Shared things live in `core/`
(audit log, headers, errors) and `accounts/` (identity), plus the shared layout in
`src/templates/`.

**Adding a page to your portal:** write a view in `<portal>/views.py` and decorate it with
`@portal_required("<portal>")`. Add it to `<portal>/urls.py` and write a template that extends
`_portal.html`. Every page needs that decorator. It is the access check, and the server is the
only place that check lives.

## Authentication and sessions

### The role model

Each account has exactly one role: `participant`, `judge`, `organizer` or `admin`. A
*visitor* is anyone not logged in; visitors are not stored. The table of who may enter which
portal is in one place, `accounts/roles.py`:

| Portal | Participant | Judge | Organizer | Admin |
| --- | --- | --- | --- | --- |
| `/participant/` | yes | – | – | – |
| `/judge/` | – | yes | – | – |
| `/organizer/` | – | – | yes | yes |
| `/admin/` | – | – | – | yes |

Judges and participants are strictly separated. The database refuses any role outside the
four through a CHECK constraint.

**Alternative considered: a role per event.** Here the same person could judge one event and
compete in another, which is more realistic for a platform that runs a dozen events a year.
The cost is that every page must first work out "your role in which event?", and the idea of
one portal per role gets blurry. We chose one role per account for clarity and speed. If
multi-event roles are needed later, an `EventMembership(user, event, role)` table can be added
without breaking these portals: the account role would become the default.

**How someone gets each role:** sign-up always creates a participant, and a `role` field
posted to `/signup` is ignored. An admin creates judges, organizers and admins in the admin
portal, or with `manage.py create_account` for the very first admin when `DEMO_MODE=0`.

### Sessions: server-side, not JWT

A login creates a row in Postgres (`django_session`). The cookie holds only a random key.
It is `HttpOnly`, so page scripts cannot read it; `SameSite=Lax`, so it is not sent on
cross-site POSTs; and `Secure` behind TLS.

| | Server-side sessions (chosen) | Signed JWT in a cookie | External provider (Auth0, Supabase) |
| --- | --- | --- | --- |
| Revoke one session instantly | delete the row | impossible until expiry, unless you add a denylist (which is a session table again) | provider feature |
| Works offline on a laptop | yes | yes | **no, against the rules** |
| Cost per request | one indexed lookup | none | network call |
| "Where am I logged in?" page | natural | awkward | provider UI |

At hackathon scale, one indexed lookup per request costs nothing. Being able to revoke a
session instantly is exactly what an organizer needs when a judge's laptop goes missing.

On top of Django's session row, a `UserSession` row records the device, IP, start time and
last-seen time. This powers the *active sessions* list. "Last seen" is refreshed at most once
every 5 minutes per session, to avoid writing to the database on every request.

Session lifetime is 14 days if "remember this terminal" is ticked. Otherwise the cookie ends
when the browser closes, while the server-side row still expires after 14 days.

**Password change** keeps the current session, rotates its key, and deletes every other
session outright. Django would invalidate them lazily anyway; deleting them makes the sessions
list truthful immediately.

### Passwords

Passwords are hashed with Argon2id, the current OWASP first choice: memory-hard, so GPU
guessing is expensive. PBKDF2 is kept as a fallback verifier, and any hash in an older format
is upgraded on the next login. The validators require at least 10 characters, not all digits,
not a common password and not similar to the name or email.

### API tokens (Bearer)

`Authorization: Bearer <token>` authenticates scripts, the future REST API and the acceptance
checker, which sends exactly one header per request and cannot do a login form.

- A token is `dfk_` plus 32 random bytes. It is shown once, and only its **SHA-256 digest** is
  stored, so a database leak does not leak usable tokens. A fast hash is fine here, unlike
  for passwords, because the token is high-entropy random, not something a person chose.
- A bad or revoked token is a **hard 401**. It never falls back to anonymous, so a broken
  script fails loudly.
- A Bearer request ignores any session cookie, and **CSRF is not enforced for it**. CSRF exists
  because browsers attach cookies automatically, and a browser never attaches an
  `Authorization` header on its own. Cookie requests keep full CSRF checks, and a test proves
  both halves.

### Login throttle

The limits are 5 failures per (email, IP) and 30 per IP, within a sliding 15-minute window.
They are counted from audit-log rows in Postgres, so they survive restarts and are shared
across gunicorn workers. A successful login resets that email-and-IP counter. The throttle is
checked **before** the password, so a throttled guess reveals nothing.

**Why not lock the account after N failures?** That lets anyone lock a judge out on judging day
by typing their email five times. Keying on (email, IP) stops guessing without handing out
that denial of service.

A wrong email and a wrong password give the same message. Django hashes a dummy password for
unknown emails, so the two also take the same time.

**Stated trade-off:** sign-up says "an account with this email already exists". The
leak-free alternative ("check your inbox") needs outbound email, which an offline portal
doesn't have yet.

### Access checks

`accounts/guards.py` provides `login_required`, `roles_required(...)` and
`portal_required("<portal>")`.

- A visitor gets a redirect to `/login?next=…` on an HTML page, or a `401` JSON response
  with `WWW-Authenticate` on the API.
- The wrong role gets a `403` page or JSON response, and an `access_denied` audit row.

The database admin (`/admin/db/`) uses **our** login page, so the throttle and the audit trail
cannot be skipped through Django's built-in login. The audit-log table there is read-only even
for admins, because an audit trail an admin can edit proves nothing.

### Audit log

`core.AuditLog` is append-only. Each row holds who did it (plus an email snapshot, so the row
outlives the account), the action, the subject, the IP, the user agent and JSON detail. It is
written through a single function, `core.audit.record()`. Later modules add their own actions
(submission edits, score changes, votes) the same way.

## Events, teams and projects

These three apps hold **models and rules only** (`models.py`, `services.py`, `forms.py`). The
pages that use them live in the portal apps: the organizer portal configures events, the
participant portal forms teams and edits projects, and the public app shows events. Every
write, from a page or the JSON API, goes through a function in a `services.py`, so both paths
enforce the same rules.

### Events

- Organizers can create any number of events. Each has a `slug` (`/events/<slug>`), UTC dates
  (start, submissions open, submissions close, judging end), a minimum and maximum team size
  (1–20), tracks, prizes and custom questions.
- **All times are UTC**, stored and shown, and every date field is labelled so. We chose this
  over a per-event display timezone for simplicity.
- **Who manages an event:** the organizers linked to it (the creator is linked automatically,
  and co-organizers can be added by email) plus any admin. For anyone else the event's
  management URLs return **404, not 403**, so nobody can probe which events exist.
- **Visibility:** `is_published` is the only stored state. An unpublished event is invisible
  (404) to everyone but its managers.
- **Phase** (upcoming, submissions open, judging, finished) is computed from the dates on
  every read, so it can never drift from them.
- The database refuses impossible timelines with CHECK constraints: submissions must close
  after they open, judging can't end before submissions close, and the event must start
  before submissions close. The form checks the same things first, so organizers get a
  readable error instead of a crash.
- **Nothing silently disappears.** A track that projects use, or a question that has answers,
  can be hidden but not deleted. An answered question's kind can't change, because that would
  change the meaning of every stored answer. The team-size limit can't drop below the size of
  the largest existing team.

### Teams

| Rule | Enforced by |
| --- | --- |
| one team per participant per event | database unique constraint on `(event, user)` |
| team name unique within an event, ignoring case | database unique constraint on `(event, lower(name))` |
| team size ≥ `event.min_team_size` to submit; a submitted team can't shrink below it | `projects/services.py::missing_for_submission`, `teams/services.py::_require_size_kept`; an organizer can't raise the minimum above a submitted team's size |
| team size ≤ `event.max_team_size` | `teams/services.py`, under a row lock on the team, so two people can't take the last seat at once |
| only *participants* can be on a team | `teams/services.py::_require_participant` |
| captain is a member; only the captain renames, removes, replaces the link or hands over | `teams/services.py` |

**The participant-only rule, and where it would change.** With one role per account, judges,
organizers and admins can never compete, so a conflict of interest is impossible by
construction. If roles ever become per-event, this rule becomes "not staff *in this event*".
It lives in one function, `_require_participant` in `teams/services.py`, and the table above
is repeated in the docstring of `teams/models.py`.

- **Invite links:** one reusable link per team (`/join/<token>`, 128 random bits). The captain
  can replace it, which kills the old one. The team size cap limits how many people can use
  it. The link page works for logged-out visitors: it shows the team and event, and sends
  them through log-in or sign-up and back.
- **Solo participants:** starting a project without a team creates a team of one. Every
  project belongs to a team, so there's a single code path.
- **Leaving:** the captain must hand over captaincy first, unless they're the last member. The
  last member leaving disbands the team and its draft. It refuses if the project is
  submitted; the team must withdraw it to draft first.
- **Registration is implicit:** being on a team in an event means you're registered for it.

### Projects

- **One project per team** (a one-to-one link). Status is `draft` or `submitted`, and the
  database requires `submitted` exactly when `submitted_at` is set.
- **Draft-and-edit:**
  - A draft may be incomplete; only the name is required.
  - Submitting requires a name, tagline, description, repository URL, a track (if the event
    has tracks) and every required custom question answered.
  - A submitted project can still be edited, but only into another complete state. To save
    incomplete work, withdraw it to draft.
  - Only submitted projects will appear in the gallery and in judging.
- **History:** no revision snapshots. Each project records `updated_at` and `last_edited_by`,
  and every edit, submission and withdrawal writes an audit row naming who did it and which
  fields changed.
- **Who can edit:** any team member.
- **Fields:** name, tagline, description (Markdown), thumbnail, image gallery (≤ 8), demo video
  URL, repository URL, live link, tags (free-form, normalised to lower case, ≤ 50), track, and
  one answer per custom question.
- **Links** must be `http(s)`. `javascript:`, `data:` and `ftp:` are refused.
- **Markdown** is rendered with raw HTML switched off, then sanitised with nh3 to an allow-list
  of tags. Links are forced to `rel="nofollow noopener noreferrer"`, and images are not
  allowed, because an image on another host would break the offline rule.
- **Images** (`projects/images.py`) go through these steps:
  1. The size is checked.
  2. The format is identified from the bytes; the file name and type are ignored.
  3. The pixel count is checked from the header, so a decompression bomb is refused before
     decoding.
  4. The image is decoded, rotated upright, shrunk to at most 2400 px on the long side and
     **re-encoded**. This drops EXIF and GPS data and any bytes appended after the image,
     which is how polyglot files carry HTML.
  5. It is stored under a random name.

  Images are served by a view that applies the project's visibility rule: draft images are
  visible only to the team and the event's organizers.

## Deadline enforcement

The spec asks for deadline enforcement "that actually holds (refused at the API, not just in
the UI)". Here it is enforced twice, on one clock, with one rule.

**The rule.** A participant write is allowed only while `now < effective close`:

- The *effective close* is the event's `submissions_close_at`, or the team's extension if an
  organizer granted one and it ends later.
- The window is half-open, so **the close instant itself is already closed**.
- Starting or editing a project also needs `now >= submissions_open_at`. Teams may form as
  soon as the event is published, even before submissions open.
- "Participant writes" covers everything: creating, joining, leaving, renaming or managing a
  team; starting, editing, submitting or withdrawing a project; adding or removing images.
  After the close, nothing participant-side can change, including withdrawing a submission.
- Drafts stay drafts: a draft still open at the close is not judged.

**One clock.** "Now" is the database's `statement_timestamp()`, not the web worker's clock, so
every gunicorn worker and the trigger agree on what time it is.

**Layer 1: the service check** (`core/deadlines.py::check_submission_window`).
- It is the first thing every participant service function does, and the first thing every
  write view and API endpoint does, before the permission check and before the form is read.
- A late write is therefore refused **as late**: HTTP **409** with
  `{"error": "submissions_closed", "closed_at": "…Z", "late_by_seconds": N}`. It is never
  disguised as a 403 or a 400. An early project write gets 409 `submissions_not_open`.
- Pages get the same 409 as a readable page, and before the close, participant pages render
  their forms disabled with a banner, so nobody is surprised.
- Every refusal is written to the audit log with the attempted action and **how late it was**,
  so organizers can see deadline gaming.

**Layer 2: the database trigger** (`projects/migrations/0002_deadline_trigger.py`).
- A PL/pgSQL function runs `BEFORE INSERT OR UPDATE OR DELETE` on every table a participant
  can write: projects, answers, images, project tags, teams and team members. It resolves the
  row's event and team, takes the effective close (reading the extensions table too) and
  raises when `statement_timestamp()` has reached it.
- It catches everything layer 1 might miss: a code path that forgot the check, a raw SQL
  statement, or a request that passed the check at 23:29:59.9 and reached the database at
  23:30:00.1.
- `DeadlineMiddleware` turns the trigger's error into the same clean 409 (and audits it), so a
  forgotten check still never shows a 500. A test removes the service check on purpose and
  confirms the trigger still refuses the write.

**Organizer writes after the close.** Code that must write after the close (the seeded closed
event now; judging decisions later) runs inside `deadline_bypass(request, reason)`. This sets
`dogfood.deadline_bypass` with `SET LOCAL`, so the bypass ends with its transaction and can't
leak into another request, and it writes a `deadline_bypassed` audit row.

**Extensions.**

| | What it does | Rules |
| --- | --- | --- |
| Extend for everyone | moves `submissions_close_at` later | must be later than the current close; the first close is kept in `original_submissions_close_at`; if the new close passes the judging end, the judging end moves by the same amount |
| Extend for one team | a `TeamExtension(team, until, reason, granted_by)` row | must end after the event close, in the future, and before judging ends; can be revoked |

Both are audited with their reason, and both are honoured by the service check and the
trigger alike. The team sees a banner: "your team has an extension until …".

**Countdown.** Participant and organizer pages show a live countdown. It counts against the
**server's** clock, whose time is embedded in the page, so a participant with a wrong laptop
clock still sees the true deadline.

**The acceptance checker.** The demo seed creates **Dogfood Archive 2026**, an event that closed
three days before boot, and `.dogfood.toml`'s `submit` route points at
`/api/events/dogfood-archive-2026/projects`. The checker's "closed event refuses submissions"
test gets a genuine 409 `submissions_closed`, not an accidental 404.

## Public gallery

`/projects` is the public gallery. `projects/gallery.py` holds its one query, which the page
and `GET /api/projects` share, so they always agree.

- **Visibility:** only `submitted` projects in `published` events appear. Drafts can't be
  reached through any filter or URL, and `/projects/<id>` for a draft is a 404. Uploaded images
  follow the same rule.
- **Search (Postgres):** a weighted full-text vector over name (A), tagline, team name and
  tags (B) and description (C), with English stemming, using web-search syntax: `"exact
  phrase"`, `-exclude` and `or`.
  - Results are ranked by relevance.
  - Matching uses the `@@` operator. The rank is only for ordering, because `ts_rank` can give
    a small positive score to rows that don't match.
  - Plain queries also match substrings of name, tagline, team and tags, so a partial word
    like "comp" finds "Compass". Queries that use the advanced syntax skip the substring match,
    so `-exclude` isn't undone.
- **Filters:** event, track (within the chosen event) and tag. Sorting is newest, oldest or
  name, with 48 projects per page. Unknown filter values are ignored rather than rejected, so
  a stale link still shows a gallery.
- **The path is exactly `/projects`,** with no trailing-slash redirect, because the acceptance
  checker requests it.

**Possible duplicates** (organizer control page): projects in the same event with the same name
(ignoring case) or the same repository URL, plus duplicates recorded during import. They are
shown for a human to decide on, never removed automatically, because two teams can
legitimately fork the same starter repo.

## Importing the fixture data

`imports/fixtures.py` maps the organizers' `acceptance/fixtures.json` onto our schema. It runs on
boot when `SEED_FIXTURES=1` (the default under compose), and by hand with
`python src/manage.py import_fixtures [--path …]`.

- **Create-only and idempotent.** Every created row gets a `FixtureRef(kind, external_id →
  object_id)`. A second run finds the refs and creates nothing, and it never overwrites a row
  an organizer has since edited.
- **All or nothing.** The whole import is one transaction.
- **Edge cases are reported, not smoothed over:**
  - A team that submitted twice (`prj_41` duplicates `prj_07` in the real file): the first
    submission is kept, and the second external id points at the same project with
    `duplicate_of` set. Scores for either id will land on one project when judging is imported.
  - A judge who is also listed as a team member: kept as a judge, left off the team, and
    reported as a conflict of interest. The real file has none, but a test covers it.
  - A person on two teams in one event: kept on the first team, and reported.
- **Derived dates.** The file gives only the close time, so the importer derives the open time
  (72 hours before the close, or before the earliest submission) and the judging end (14 days
  after the close), and reports both.
- **Past the deadline, on purpose.** The fixture event closed in March 2026, so the import runs
  inside the audited `deadline_bypass()`, the same path organizer tools use.
- **Accounts.** Imported accounts use the demo password in demo mode. Otherwise they have no
  usable password until an admin sets one.
- **Scores** are not imported yet. The scoring model arrives with T2 (judging), and the
  `FixtureRef` table already maps both duplicate project ids to one project for it.

## Design system: violet CRT

The look is ASCII and terminal, kept calm: one colour family (lavender on near-black), thin
rules, and at most one illustration per page. The only other hue, pink, is reserved for errors.

- **Fonts:** JetBrains Mono for text, and VT323 (a pixel font) for titles and labels. Both are
  vendored and OFL-licensed. JetBrains Mono is subset to include box-drawing and block
  characters, so ANSI art lines up.
- **Components** (`static/css/crt.css`):
  - `.frame` is a bordered window with a title bar.
  - `.kv` is the key-value "subject file" sheet.
  - `.table` is a data table.
  - `.btn` is a bracketed button.
  - `_field.html` renders a form field as a terminal prompt (`role@dogfood:~$ whoami --email`).
  - `.boot` shows `[ OK ]` status lines, and `.msg` shows flash messages.
- **Art:** the home banner is the figlet "ANSI Shadow" font. The login page shows a small
  rotating ASCII torus (`static/js/torus.js`), which renders a single frame for anyone who asks
  for reduced motion.
- **No inline styles or scripts:** the CSP would block them, and a test checks that none exist.
