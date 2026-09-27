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
organizer/      /organizer/     (platform admins too)
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

### The role model: roles per event, portals per audience

**Roles are held per event.** `EventMembership(user, event, role)` says that someone is a
`participant`, `judge` or `organizer` *of this event*. The same person can judge one hackathon
and compete in the next, and an organizer's powers stop at the edge of their own events. The only
platform-wide facts about an account are two flags: `is_platform_admin` (runs the platform) and
`can_create_events` (may start an event, and so becomes its first organizer). A *visitor* is
anyone not logged in; visitors are not stored. All of this is in one place, `accounts/roles.py`.

**Portals are views onto memberships.** The one-app-per-audience layout above is unchanged. Each
portal answers two questions, both on the server:

1. *May this account enter at all?* `PORTAL_ACCESS`, checked by `portal_required`:

   | Portal | Who may enter |
   | --- | --- |
   | `/participant/` | any account except platform admins (joining a team is what makes you a participant of an event) |
   | `/judge/` | anyone who judges at least one event |
   | `/organizer/` | anyone who organizes at least one event, may create events, or is a platform admin |
   | `/admin/` | platform admins |

2. *What may they do in **this** event?* Every event-scoped page checks the role in that event.
   The organizer control page goes through `get_managed_event` (organizers of the event and
   platform admins; 404 for anyone else, so slugs cannot be probed). Team and project writes go
   through `can_compete_in`. Staff of an event, and platform admins, cannot compete in it.

A login lands on the most powerful portal the account can enter (admin, then organizer, judge,
participant), and the navigation lists every portal it can enter.

**Conflict of interest, twice.** `can_compete_in` refuses in the service layer with a sentence the
page can show. An **exclusion constraint** on `events_eventmembership` refuses in the database,
comparing a stored generated `side` column (competitor or staff) across all rows for the same
`(user, event)`. Judge plus organizer in one event is allowed; participant plus either is not. The
constraint is Postgres-only (`btree_gist`), installed by a migration that does nothing on SQLite,
the same arrangement as the deadline trigger.

**Why this differs from the portal-v2 design document.** That design chose one role per account,
which is simpler but means one person cannot judge one event and compete in another. For a
platform the organizers intend to run a dozen events on, that is the wrong trade. The design
itself anticipated the fix ("adding `EventMembership(user, event, role)` later is possible without
breaking the portals"), and that is what this merge did. The portals, URLs, 403 pages and audit
rows are exactly as designed. Only "who may enter" became a question about memberships.

**How someone gets each role:**

- **participant:** sign up (a plain account, always, and any `role` or flag posted to `/signup` is
  ignored), then form or join a team. Leaving the team ends the participant role in that event.
- **judge / co-organizer:** an organizer of the event adds the account by email on the event's
  control page (judges optionally for chosen tracks). The service refuses anyone competing in that
  event, and the constraint backs it up.
- **judges' work, per event:** the organizer assigns projects to judges on the assignment page
  (constrained random, balanced, seeded and reproducible; see JUDGING.md) and follows progress on
  a dashboard that refreshes itself. Both are organizer-only pages of that event.
- **judge, by invite link:** for someone without an account (or to avoid typing one in), the
  organizer creates a one-time link for an email, optionally for chosen tracks. It is shown once
  (only its SHA-256 digest is stored, like API tokens), expires after 7 days, and works only for
  that email: the invitee logs in as it, or creates the account from the link, even when public
  sign-up is closed. Inviting the same email again cancels the old link; organizers can revoke a
  pending one. Conflict of interest is checked when the link is made and again when it is
  accepted, and every step (created, accepted, refused, revoked) is in the audit log.
- **organizer of a new event:** create it, which needs `can_create_events`. A platform admin sets
  that flag when creating the account, or with `manage.py create_account --can-create-events`.
- **platform admin:** another admin, or `manage.py create_account --admin` for the very first one
  when `DEMO_MODE=0`.

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

`accounts/guards.py` provides `login_required`, `portal_required("<portal>")` and `refuse()`.

- A visitor gets a redirect to `/login?next=…` on an HTML page, or a `401` JSON response
  with `WWW-Authenticate` on the API.
- An account the portal does not admit gets a `403` page or JSON response, and an
  `access_denied` audit row. So does a co-organizer without `can_create_events` who tries to
  create an event.
- Inside a portal, an event the caller has no role in is a `404` (organizer pages) or a refused
  write with a readable reason (team and project services).

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

- Organizers can create any number of events. Each has a `slug` (`/events/<slug>`), a tagline
  and a description (both required), UTC dates (event start, submissions open, submissions close,
  judging start, judging end, and an optional results date that may stay "to be announced"), a
  minimum and maximum team size (1–20), tracks, prizes and custom questions.
- **Dates are entered as a date box plus a time box**, not one combined datetime box: some
  browsers' pickers set only the day of a combined box, leaving the month and year to be typed.
  A date without a time is refused with "pick a time as well as the date" rather than guessed.
  Autofill is off on date fields, so a new event starts with every date empty.
- **All times are UTC**, stored and shown, and every date field is labelled so. We chose this
  over a per-event display timezone for simplicity.
- **Who manages an event:** its organizers, meaning accounts holding the `organizer` role in
  it (the creator gets it automatically, and co-organizers are added by email), plus any platform
  admin. Judges are added the same way, optionally for chosen tracks. For anyone else the event's
  management URLs return **404, not 403**, so nobody can probe which events exist.
- **Visibility:** `is_published` is the only stored state. An unpublished event is invisible
  (404) to everyone but its managers. **Publishing is refused** until the event has a tagline, a
  description and a rubric whose weights add up to 100%; the control page lists what is missing
  and disables the button until then. The rubric is required because it locks when submissions
  close: an event published without one could reach its close and never be judged.
- **Phase** (upcoming, submissions open, submissions closed, judging, finished) is computed from
  the dates on every read, so it can never drift from them. "Submissions closed" is the gap
  between the close and the judging start, when organizers assign judges.
- The database refuses impossible timelines with CHECK constraints: each date must come
  strictly after the one before it (event starts < submissions open < submissions close <
  judging starts < judging ends < results, when set). The form checks the same things first, so
  organizers get a readable error on the right field instead of a crash.
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
| nobody who is staff *in this event* (or a platform admin) is on one of its teams | `teams/services.py::_require_can_compete` (`accounts.roles.can_compete_in`), and the exclusion constraint on `events_eventmembership` |
| captain is a member; only the captain renames, removes, replaces the link or hands over | `teams/services.py` |

**Being on a team is being a participant.** Creating or joining a team creates the participant
`EventMembership`, and leaving, being removed or disbanding removes it, in the same transaction.
That membership is what the conflict-of-interest constraint compares against any judge or
organizer membership in the same event. So the database, not just this service, refuses a person
on both sides. The table above is repeated in the docstring of `teams/models.py`.

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
| Extend for everyone | moves `submissions_close_at` later | must be later than the current close; the first close is kept in `original_submissions_close_at`; if the new close reaches the judging start, the judging start, judging end and results date (if set) all move by the same amount, so the timeline stays in order |
| Extend for one team | a `TeamExtension(team, until, reason, granted_by)` row | must end after the event close, in the future, and before judging starts; can be revoked |

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
    `duplicate_of` set. Reviews of either id land on that one project.
  - A judge who is also listed as a team member: kept as a judge, left off the team, and
    reported as a conflict of interest. The real file has none, but a test covers it.
  - A person on two teams in one event: kept on the first team, and reported.
- **Derived dates.** The file gives only the close time, so the importer derives the rest and
  reports each: submissions open 72 hours before the close (or before the earliest submission),
  the event starts an hour before that, judging starts an hour after the close and ends 14 days
  after it. Results stay "to be announced".
- **Past the deadline, on purpose.** The fixture event closed in March 2026, so the import runs
  inside the audited `deadline_bypass()`, the same path organizer tools use.
- **Accounts.** Imported accounts use the demo password in demo mode. Otherwise they have no
  usable password until an admin sets one.
- **Judges and scores.** Each judge becomes a judge *of the fixture event*, with the tracks the
  file lists (`JudgeTrack`). The three criteria keys become `Criterion` rows (weight 1), and each
  review becomes a `Score` on the judge's membership, with one `ScoreItem` per criterion,
  range-checked. Three judges reviewed both `prj_07` and its duplicate. With the two folded into
  one project they would review it twice, which `score_unique_judge_project` forbids, so the
  review of the kept submission wins and the other three are reported. 126 reviews import as 123.
  Nothing reads scores yet (see JUDGING.md).
- **Team members** become participants of the event (`EventMembership`), and in demo mode the
  demo organizer and both demo judges are given their roles in the fixture event.

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
