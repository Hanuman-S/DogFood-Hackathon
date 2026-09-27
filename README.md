# DOGFOOD portal

A self-hosted hackathon submission and judging portal, built for DOGFOOD 2026. It runs
entirely on a laptop with the network off: no cloud accounts, hosted database or auth provider.

**Status: T1 complete.** All seven T1 modules are built:
- authentication and sessions
- the role model
- event creation
- team formation by invite link
- project submission with draft-and-edit
- deadline enforcement
- the public gallery with search and filters

`.dogfood.toml` claims **T1**, and `acceptance-report.txt` shows all three T1 checks passing.
T2 (judging) is next; its four checks fail, and T2 is not claimed.

## Run it

You need Docker (Docker Desktop on Windows or macOS). Then:

```bash
docker compose up --build
```

The first build needs internet to pull the Python and Postgres images and the pinned
packages. After that, it runs offline. When the log shows `Portal ready`, open
**http://localhost:8080**.

On boot, the log prints one demo account per role (password `dogfood-demo`):

| Role | Email | Lands on |
| --- | --- | --- |
| admin | `admin@dogfood.local` | `/admin/` |
| organizer | `organizer@dogfood.local` | `/organizer/` |
| judge | `judge.a@dogfood.local`, `judge.b@dogfood.local` | `/judge/` |
| participant | `participant@dogfood.local` | `/participant/` |

Anyone can sign up at `/signup`, and a sign-up is always a participant. Judges and organizers
are created by an admin in the admin portal.

On boot the portal also imports the organizers' `acceptance/fixtures.json` (`SEED_FIXTURES=1`):
- 1 event (**Sample Hack 2026**, closed 2026-03-01), 8 tracks, 30 judges, 40 teams and their
  121 accounts, and 40 submitted projects.
- The file's deliberate duplicate submission (`prj_41`, a second copy of `prj_07`) is recorded
  as a duplicate, not imported twice.
- The import report is printed in the boot log, and running the import again changes nothing.
- In demo mode, imported accounts use the demo password.

The demo seed also creates two events:

- **Dogfood Live Demo** (`/events/dogfood-live-demo`) is open for 30 days from first boot. It has
  three tracks, prizes, two custom questions, and a team ("Demo Team", captained by the demo
  participant) with a draft project.
- **Dogfood Archive 2026** (`/events/dogfood-archive-2026`) closed three days before first boot.
  It has one submitted project from the demo participant's team, so you can see the read-only,
  closed state. The acceptance checker's submit route points here.

Reset everything with `docker compose down -v`.

### Without Docker, for development

```bash
pip install -r requirements.txt
cd src
DJANGO_DEBUG=1 DEMO_MODE=1 python manage.py migrate
DJANGO_DEBUG=1 DEMO_MODE=1 python manage.py seed_demo
DJANGO_DEBUG=1 python manage.py runserver 8080
```

With no `POSTGRES_HOST` set, this uses a local SQLite file. The submission itself always runs
on Postgres under compose.

### Tests

```bash
docker compose run --rm web pytest          # inside the container, against Postgres
# or locally:  pip install -r requirements.txt && pytest
```

### Run the acceptance checker

```bash
docker compose up -d
python acceptance/run.py .dogfood.toml > acceptance-report.txt   # `python3` on macOS/Linux
```

### Try the API

```bash
curl -H "Authorization: Bearer dogfood-demo-judge-a-token" http://localhost:8080/api/me
curl http://localhost:8080/api/events

# start a project as the demo participant (their team already has one, so this is refused;
# sign up a new participant and create a token on /account to try it for real)
curl -X POST -H "Authorization: Bearer dogfood-demo-participant-token" \
     -H "Content-Type: application/json" -d '{"name": "My project"}' \
     http://localhost:8080/api/events/dogfood-live-demo/projects
```

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/api/me` | who you are |
| GET | `/api/events`, `/api/events/<slug>` | published events, one event with tracks, prizes and questions |
| GET | `/api/projects?q=&event=&track=&tag=&sort=&page=` | the public gallery, same search and filters as `/projects` |
| POST | `/api/events/<slug>/projects` | start your team's project (a solo team is made if you have none) |
| GET / PATCH | `/api/projects/<id>` | read, or partially update, a project |
| POST | `/api/projects/<id>/submit`, `/unsubmit` | draft → submitted, and back |

## What works

- Email and password login backed by server-side sessions in Postgres. Passwords are hashed
  with Argon2id and the session key rotates on login.
- "Remember this terminal": 14 days, or until the browser closes.
- An account page listing every live session (device, IP, last seen). Each session can be
  revoked, or all the others signed out at once. Changing the password signs out every other
  session.
- Personal API tokens (`Authorization: Bearer …`), shown once at creation and stored only as
  a SHA-256 digest. Tokens can be revoked.
- A login throttle: 5 failures per email and IP, or 30 per IP, within 15 minutes. It is
  counted in Postgres, so it survives restarts and is shared by every worker.
- Five roles (visitor, participant, judge, organizer and admin), each with its own portal and
  URL prefix. Access is checked by the server on every request.
- An audit trail of logins, failures, throttles, revocations, token changes and refused portal
  access. The admin portal shows it, and it is read-only in the database admin.
- A strict Content-Security-Policy, POST-only logout and CSRF protection on every form.
- **Events** (organizer portal): any number of events, each with UTC dates (start, submissions
  open, submissions close, judging end), a max team size, tracks, prizes and custom
  submission questions. An event is a draft until it is published. Its phase (upcoming, open,
  judging or finished) is computed from the dates. Co-organizers can be added. Organizers
  only see their own events, and admins see all.
- **Teams** (participant portal): create a team, share its reusable invite link at `/join/<token>`,
  and the captain can rename it, remove members, replace the link or hand over captaincy.
  A participant is on at most one team per event, which the database enforces. Solo
  participants get a team of one automatically.
- **Projects** (participant portal): draft, submit, withdraw and edit, with name, tagline,
  Markdown description, thumbnail, an image gallery (up to 8), demo video URL, repository URL,
  live link, up to 50 tech tags, track, and answers to the organizer's questions. Uploaded
  images are re-encoded (metadata removed), and draft images are private.
- **Minimum and maximum team size** per event. Smaller teams can form and draft, but can't
  submit, and a submitted team can't shrink below the minimum.
- **Deadline enforcement:**
  - After the close, every participant write is refused with **409 `submissions_closed`**, by
    the server first and by a **Postgres trigger** as a backstop.
  - The close instant itself counts as closed, on the database's clock.
  - Late attempts are audited with how late they were.
  - Organizers can extend the deadline for everyone, or for one team with a reason.
  - Pages go read-only after the close and show a live countdown. See ARCHITECTURE.md.
- **Public gallery** at `/projects`, no login needed:
  - Shows only submitted projects of published events.
  - **Search** across names, taglines, teams, tags and descriptions, with Postgres full-text
    ranking and partial-word matching. It supports `"exact phrase"` and `-exclude`.
  - **Filters** by event, track and tag, with sorting and pagination.
  - Each project has a public page at `/projects/<id>`, and the same data is available as JSON.
- **Possible duplicates** are flagged for organizers: the same project name or repository within
  an event, plus duplicates caught during import. Nothing is removed automatically.

## What it does not do yet

- Judging (T2).
- Password reset by email. The portal has no outbound mail yet. In the meantime, an operator
  can run `docker compose exec web python src/manage.py changepassword user@example.org`.
- Two-factor authentication.

## Layout

```
src/
  config/          settings, root URLs
  core/            audit log, security headers, error pages, markdown, deadline hook
  accounts/        users, roles, sessions, tokens, login/signup/account pages
  events/          events, tracks, prizes, custom questions, co-organizers (models + rules)
  teams/           teams, members, invite links (models + rules)
  projects/        projects, images, tags, answers (models + rules), gallery query, JSON API, media
  imports/         fixture import (create-only, idempotent, duplicate-aware)
  public/          pages for visitors            /
  participant/     participant portal            /participant/
  judge/           judge portal                  /judge/
  organizer/       organizer portal              /organizer/
  platform_admin/  admin portal + database admin /admin/, /admin/db/
  templates/       shared layout and components
  static/          violet CRT stylesheet, vendored fonts, small scripts
tests/             pytest suite
acceptance/        the organizers' checker and fixtures (read-only)
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design and the reasons behind it.

## Licence

MIT. See [LICENSE](LICENSE). The fonts are under the SIL OFL; see `src/static/fonts/NOTICE.md`.
