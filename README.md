# DOGFOOD portal

A self-hosted hackathon submission and judging portal, built for DOGFOOD 2026. It runs
entirely on a laptop with the network off: no cloud accounts, hosted database or auth provider.

Demo video: [VIDEO_URL]

**Status: T1 and T2 claimed and verified; T3 voting and results built, not claimed.**
- **T1**, all seven modules: authentication and sessions, the role model with roles held **per
  event**, event creation, team formation by invite link, project submission with
  draft-and-edit, deadline enforcement, and the public gallery with search and filters.
- **T2**: rubric, judge invites, assignment, reviews, the scoring engine (M2), results snapshots,
  publishing, and the published results pages (`/events/<slug>/results`). See
  [JUDGING.md](JUDGING.md).
- **T3**: community voting with anti-abuse, and the final score combining judges and community.
  The evidence is `t3-report.txt`; see "What it does not do yet" for why T3 is not claimed.

`.dogfood.toml` claims **T1 and T2**, and `acceptance-report.txt` shows all seven checks passing
(three T1, four T2: own scores, peer scores refused, participant refused, CSV export).

The portal follows the portal-v2 design (one app per audience, Violet CRT look, deadline trigger)
with one deliberate change: **roles are held per event**, so the same person can judge one
hackathon and compete in the next, and a database constraint stops anyone being on both sides of
one event. [ARCHITECTURE.md](ARCHITECTURE.md) explains why.

## Run it

You need Docker (Docker Desktop on Windows or macOS). Then:

```bash
docker compose up --build
```

The first build needs internet to pull the Python and Postgres images and the pinned
packages. After that, it runs offline. When the log shows `Portal ready`, open
**http://localhost:8080**.

On boot, the log prints the demo accounts (password `dogfood-demo`):

| Account | Email | What it is | Lands on |
| --- | --- | --- | --- |
| admin | `admin@dogfood.local` | platform admin | `/admin/` |
| organizer | `organizer@dogfood.local` | may create events; organizes all three demo events | `/organizer/` |
| judge | `judge.a@dogfood.local`, `judge.b@dogfood.local` | judges all three demo events | `/judge/` |
| participant | `participant@dogfood.local` | on a team in the live and archive demo events | `/participant/` |

Every fixture account (for example `tomas.varga@example.org`, a judge) also uses the demo password
in demo mode.

Anyone can sign up at `/signup`. A sign-up is a plain account, and it becomes a participant of an
event by forming or joining a team there. Organizers make accounts judges or co-organizers of
their event from its control page. Platform admins create accounts and grant "may create events".

On boot the portal also imports the organizers' `acceptance/fixtures.json` (`SEED_FIXTURES=1`):
- 1 event (**Sample Hack 2026**, closed 2026-03-01), 8 tracks, 30 judges (with their tracks),
  40 teams, 121 accounts, and 40 submitted projects.
- The rubric (3 criteria) and 123 of the file's 126 reviews. The file's deliberate duplicate
  submission (`prj_41`, a second copy of `prj_07`) is recorded as a duplicate, not imported
  twice. Three judges reviewed both copies, and the review of the kept one wins; the report lists
  the other three.
- The import report is printed in the boot log, and running the import again changes nothing.
- In demo mode, imported accounts use the demo password.

The demo seed also creates two events:

- **Dogfood Live Demo** (`/events/dogfood-live-demo`) is open for 30 days from first boot. It has
  three tracks, prizes, two custom questions, and a team ("Demo Team", captained by the demo
  participant) with a draft project.
- **Dogfood Archive 2026** (`/events/dogfood-archive-2026`) closed three days before first boot.
  It has five submitted projects (one from the demo participant's team), so you can see the
  read-only, closed state. The acceptance checker's submit route points here. Its community vote
  is **open** for the whole judging phase (quadratic, 16 credits, judges 80 / community 20), with
  ten seeded ballots from seed-only accounts, including one deliberately suspicious cluster of
  four that the voting integrity page flags. The demo participant can vote there.
- **Sample Hack 2026** (the fixture event, above) gets a **closed** community vote in demo mode:
  its window ran in March 2026, and it has no ballots.

Reset everything with `docker compose down -v`.

**The secret key.** Nothing in this repository is a usable `SECRET_KEY`. If you do not set
`DJANGO_SECRET_KEY`, the first boot generates a random key into the `secrets` Docker volume (never
the repo, never the image) and every restart reuses it, so `docker compose up` stays one command
and every install has its own key. For a real deployment, set your own:
`DJANGO_SECRET_KEY=... docker compose up`. Outside demo mode (`DEMO_MODE=0`) the portal refuses to
start with a key from this repository or one shorter than 32 characters. Each use gets its own
key derived from it (`core/keys.py`: HMAC of the key and a purpose, such as `voter-links` or
`ip-hash`). Changing the key signs everyone out, invalidates every voter link and open-link
cookie, and resets IP-based rate limits and integrity clustering. `docker compose down -v`
deletes the generated key along with the database.

**Comment rate limits.** Logged-in comments are limited per account (`COMMENT_RATE_PER_USER`, 5
per 10 minutes, the main control) and per IP hash (`COMMENT_RATE_PER_IP`, 200 per 10 minutes).
Anonymous attempts are refused and audited, and they don't count towards either limit. At a venue
where everyone shares one NAT address, raise `COMMENT_RATE_PER_IP`.

**After changing code, rebuild:** `docker compose up --build`. The image copies `src/` and
collects static files at build time (no source mount, `DEBUG` off), so without `--build` the
portal keeps serving the old templates, CSS and JavaScript. Pages themselves are never cached:
the busy ones (organizer progress and assignments, the judge console) refresh every 30 seconds
and when you come back to the tab, and Back/Forward fetches a page again.

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
./scripts/test.sh                  # the whole suite, in the web image, against Postgres
./scripts/test.sh tests/test_db_constraints.py -vv
```

The suite needs Postgres: the conflict-of-interest constraint and the deadline trigger are
Postgres features, and tests prove the database itself refuses.

### Run the acceptance checker

```bash
docker compose up -d
./scripts/acceptance.sh            # writes acceptance-report.txt
```

### Prove it runs with the network off

```bash
./scripts/offline-check.sh         # writes acceptance-report-offline.txt
```

This starts the stack on a compose network with no route off it (`docker-compose.offline.yml`),
checks that DNS and the internet really are unreachable from inside, and runs the organizers'
checker from inside that sealed network. Building the image still needs the network once.

### Community voting probes (T3)

```bash
docker compose down -v && docker compose up --build -d   # a fresh boot
./scripts/t3-check.sh              # writes t3-report.txt
```

Standard-library probes in the checker's style: tallies hidden from a visitor, participant and
judge while voting is open; a late vote (409), over budget (400), own project (403), a judge (403),
a burst (429); publishing refused while voting is open (409); and each result visibility. The top of
`scripts/t3_check.py` lists what it changes on the demo data and how it puts it back.

### Normalization proof

```bash
PYTHONPATH=src python scripts/normalization_proof.py --write   # regenerates JUDGING.md's section
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
- An account page listing every live session (device, network, last seen). Each session can be
  revoked, or all the others signed out at once. Changing the password signs out every other
  session.
- Personal API tokens (`Authorization: Bearer …`), shown once at creation and stored only as
  a SHA-256 digest. Tokens can be revoked.
- A login throttle: 5 failures per email and IP, or 30 per IP, within 15 minutes. It is
  counted in Postgres, so it survives restarts and is shared by every worker.
- **No IP address is stored anywhere.** The audit log, sessions and ballots keep only a keyed hash
  of it (HMAC under a key derived from the install's own secret key), enough to count and to
  cluster, not to say where anyone was. The account page says "this network" for the current one.
- Five roles (visitor, participant, judge, organizer and admin), each with its own portal and
  URL prefix. Access is checked by the server on every request. Participant, judge and organizer
  are held **per event**, and every event page checks the role in *that* event. A person can
  judge one event and compete in another, but never both in the same event: the services refuse
  it, and a Postgres exclusion constraint backs that up.
- An audit trail of logins, failures, throttles, revocations, token changes and refused portal
  access. The admin portal shows it, and it is read-only in the database admin.
- A strict Content-Security-Policy, POST-only logout and CSRF protection on every form.
- **Events** (organizer portal): any number of events, each with a tagline, a description, UTC
  dates in strict order (start, submissions open, submissions close, judging start, judging end,
  and an optional results date), a max team size, tracks, prizes and custom submission
  questions. An event is a draft until it is published, and it cannot be published without a
  tagline, a description and a rubric. Its phase (upcoming, open, closed, judging or finished) is
  computed from the dates. Co-organizers and judges (optionally per
  track) are added by email on the event's control page. Organizers only see their own events,
  and platform admins see all.
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
- **Judging** (T2, claimed): rubric with relative weights, judge invites, a judging window checked
  first on every write, randomised balanced assignment with a stored seed, the judge's queue with
  draft / submit / decline, a progress dashboard, and a CSV export that grows stage by stage. See
  [JUDGING.md](JUDGING.md).
- **Scoring**: a pure engine (`src/scoring/engine/`) whose primary method, M2, separates each
  project's quality from each judge's lean and shrinks thinly-reviewed projects towards the mean,
  with standard errors and tie groups. JUDGING.md's **Normalization Proof** shows, on the fixture
  event, which projects moved and how much of each move was judge lean vs shrinkage (generated by
  `scripts/normalization_proof.py`).
- **Results** (organizer portal, `/organizer/events/<slug>/results`, and a JSON API): compute a
  preview or the final result, "end judging now", publish or unpublish, and choose who sees it: the
  full ranking, the winners only, or organizers only. The public page is `/events/<slug>/results`:
  shared ranks for exact ties, tie groups, "no separable winner", track winners, the plain-mean rank
  beside the M2 rank, People's Choice, and no per-judge data. `winners.csv` (names and emails, in
  every mode) and `results.csv` for organizers.
- **Final score (judges + community)**: judge and community weights (whole numbers summing to 100,
  locked once judging or voting opens) combine each project's judged and vote percentile ranks.
  Computing the final result freezes an immutable vote tally; with a community weight of 0 the
  ranking is exactly M2's.
- **Community voting** (T3, built; evidence in [`t3-report.txt`](t3-report.txt); not claimed, see
  below):
  - Organizers set a window (at or after the submission close; it may overlap judging), one person
    one vote or quadratic (a project's influence from one ballot is the square root of the credits
    placed on it), and who votes: logged-in accounts, an email allowlist (one personal link each;
    there is no outbound mail, so organizers download `voter-links.csv`), or an open link (the
    weakest: each browser is a voter).
  - The event's judges and organizers and platform admins cannot vote, nobody votes for their own
    team, and each ballot shows the projects in its own shuffled order.
  - The window is enforced by the server and a Postgres trigger; once voting opens, no vote can be
    deleted, only voided (with a reason, and restorable). Tallies are visible to organizers only.
  - Rate limits per voter and per network (429), and an integrity page with flags, voided ballots,
    credits by shown position and the voting audit trail. Nothing is removed automatically.
  - The demo seed opens a vote on **Dogfood Archive 2026** (quadratic, 80/20), with seeded ballots
    including one deliberately suspicious cluster, so the integrity page has something to show.
    The demo participant can vote there.

## What it does not do yet

- **Comments on projects** (part of the T3 brief) are not built.
- **T3 is not claimed** in `.dogfood.toml`. The organizers' checker has no T3 checks, so a claimed
  T3 would print "claimed but not verified"; `t3-report.txt` (from `scripts/t3-check.sh`) is the
  evidence instead. What voting does not stop is listed in JUDGING.md, under "Community voting
  (T3)", in the paragraph "What this does not stop".
- Password reset by email. The portal has no outbound mail yet. In the meantime, an operator
  can run `docker compose exec web python src/manage.py changepassword user@example.org`.
- Two-factor authentication.

## Layout

```
src/
  config/          settings, root URLs
  core/            audit log, security headers, error pages, markdown, deadline hook
  accounts/        users, the role model (roles.py), sessions, tokens, login/signup/account pages
  events/          events, tracks, prizes, custom questions, per-event memberships (models + rules)
  teams/           teams, members, invite links (models + rules)
  projects/        projects, images, tags, answers (models + rules), gallery query, JSON API, media
  imports/         fixture import (create-only, idempotent, duplicate-aware)
  scoring/         rubric, reviews, assignment, the pure scoring engine (engine/), result
                   snapshots, publications, the results read side (models + rules)
  voting/          community vote: config, ballots, voter links, tallies, integrity (models + rules)
  public/          pages for visitors            /
  participant/     participant portal            /participant/
  judge/           judge portal                  /judge/
  organizer/       organizer portal              /organizer/
  platform_admin/  admin portal + database admin /admin/, /admin/db/
  templates/       shared layout and components
  static/          violet CRT stylesheet, vendored fonts, small scripts
tests/             pytest suite
scripts/           test.sh                  pytest in the web image against Postgres
                   acceptance.sh            the organizers' checker -> acceptance-report.txt
                   offline-check.sh         boot on a sealed network, run the checker inside it
                   t3-check.sh, t3_check.py T3 probes (voting, results) -> t3-report.txt
                   normalization_proof.py   regenerates JUDGING.md's Normalization Proof
acceptance/        the organizers' checker and fixtures (read-only)
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design and the reasons behind it,
[DATA-MODEL.md](DATA-MODEL.md) for the schema and the import and export paths, and
[JUDGING.md](JUDGING.md) for the state of judging.

## Credits

This portal merges two lines of work on the same repository: the T1 foundation (per-event roles,
the conflict-of-interest constraint, the scoring schema and fixture score import, the offline
check) and the portal-v2 remodel by [@Hanuman-S](https://github.com/Hanuman-S) (the per-audience
portals, the Violet CRT design, the deadline trigger and extensions, sessions and throttling). The
commit history shows what came from where.

## Licence

MIT. See [LICENSE](LICENSE). The fonts are under the SIL OFL; see `src/static/fonts/NOTICE.md`.
