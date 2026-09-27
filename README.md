# DOGFOOD 2026 portal

A self-hostable hackathon submission and judging platform, built for the DOGFOOD 2026
hackathon. **Tier T1 (Core) is complete and verified.** T2, T3 and T4 are not built — see
[Not done yet](#not-done-yet), which is the honest list, not a roadmap.

```
T1  gallery is public ................. PASS
T1  project from fixtures shown ....... PASS
T1  closed event refuses submissions .. PASS
claimed T1, verified T1
```

That block is from [`acceptance-report.txt`](acceptance-report.txt), committed from a real run of
the organizers' own `acceptance/run.py` against a **fresh clone of this repository**. The four T2
lines in that file read **FAIL**, because T2 does not exist. There are no stub endpoints anywhere in
this repository that fake a pass.

[`acceptance-report-offline.txt`](acceptance-report-offline.txt) is the same three checks passing
with both containers on a Docker network that has **no route off it** — no DNS, no PyPI, no CDN.
Reproduce it with `./scripts/offline-check.sh`. Its `portal:` line reads `http://web:8000` rather
than `localhost:8080` because an isolated network cannot publish ports, so the checker runs inside
it; everything else about the two runs is identical.

## Run it

```bash
docker compose up
```

Then open **<http://localhost:8080>**. That is the whole procedure: no `.env` file, no `manage.py`
step, no fixture loading by hand. The entrypoint waits for Postgres, applies migrations, imports the
organizers' fixture data, seeds the demo accounts, and prints the credentials below.

```bash
docker compose down      # stop, keep the data
docker compose down -v   # stop and forget the data (including uploaded images)
```

**The first build needs internet access.** Building the web image pulls the Python base image and
the pinned wheels in `requirements.txt` from PyPI. The *running* portal then uses no network at all:
every CSS and JS asset is vendored in `src/static/vendor/`, there is no CDN link, no web font and no
outbound request. A build with the network off fails at `pip install`; `docker compose up` against
an already-built image works with no network at all, which is what
[`acceptance-report-offline.txt`](acceptance-report-offline.txt) demonstrates.

Requirements: Docker with Compose v2. Nothing else — no Python, no Node, no Postgres on the host.

## Demo credentials

Printed at every boot while `DEMO_MODE=1`, and listed here so a reviewer does not have to read the
logs. **They are public strings. Never run a real event with `DEMO_MODE=1`.**

| Role | Web login | Password |
|---|---|---|
| Platform admin | `admin@example.org` | `dogfood-demo` |
| Organizer | `organizer@example.org` | `dogfood-demo` |
| Judge | `wei.lindqvist@example.org` | `dogfood-demo` |
| Judge | `jonas.vogel@example.org` | `dogfood-demo` |
| Participant | `priya1@example.org` | `dogfood-demo` |

Every account imported from the fixture file uses the same password. API tokens (used by
`.dogfood.toml`) have fixed values so that file survives `docker compose down -v`:

```
Authorization: Bearer dogfood-demo-organizer-token-do-not-use-in-production
Authorization: Bearer dogfood-demo-judge-a-token-do-not-use-in-production
Authorization: Bearer dogfood-demo-judge-b-token-do-not-use-in-production
Authorization: Bearer dogfood-demo-participant-token-do-not-use-in-production
Authorization: Bearer dogfood-demo-admin-token-do-not-use-in-production
```

Tokens are stored only as SHA-256 digests; the plaintext exists in `docker-compose.yml` and nowhere
in the database. There is also a fixed invite link,
`/invite/dogfood-demo-invite-token-not-for-production`, for trying the join flow.

Two events exist after a boot: **Sample Hack 2026** (the organizers' fixture event, whose submission
window closed 2026-03-01 18:00 UTC — this is what the acceptance checker probes) and **DOGFOOD live
demo**, whose window is open, for actually trying submission.

## Configuration

Every variable has a working default inline in `docker-compose.yml`; `.env.example` documents the
full set. The four that matter:

| Variable | Default | What it does |
|---|---|---|
| `DEMO_MODE` | `1` | Seeds demo accounts with **published passwords** and the fixed API tokens above, and shows a banner. **Set to `0` for any real event.** |
| `SEED_FIXTURES` | `1` | Imports `acceptance/fixtures.json` on every boot. **Set to `0` for a real deployment** — you do not want 121 invented users and a closed March-2026 event in your production database. |
| `DJANGO_SECRET_KEY` | a known public string | Signs sessions. **Must be changed before production**; the shipped default is shared by every unconfigured deployment. |
| `COOKIE_SECURE` | `0` | `0` because localhost is plain HTTP. Set to `1` behind TLS. |

### Production checklist

`DEMO_MODE=0`, `SEED_FIXTURES=0`, a generated `DJANGO_SECRET_KEY`, `COOKIE_SECURE=1` behind TLS,
`DJANGO_ALLOWED_HOSTS` set to your hostname, and a Postgres password that is not `dogfood`. The
database port is deliberately not published to the host.

The boot-time import is **create-only**: it inserts missing rows and never modifies existing ones, so
leaving `SEED_FIXTURES=1` on cannot revert an organizer's edits. A row that has drifted from the
fixture is reported as `PRESERVED` with the differing field names and left alone.
`manage.py import_fixtures --sync` is the opt-in escape hatch that lets the fixture win, and only a
human runs it.

`pg_trgm` and `btree_gist` are created by migrations, which needs a database role permitted to create
extensions. The bundled Postgres runs migrations as its superuser, so this is automatic; on a managed
database a DBA may need to create those two extensions first.

## What works today

**Accounts and roles.** Email-based signup and login, sessions, password change, API tokens. Roles
are per event (`participant`, `judge`, `organizer`) plus a platform-admin flag. In one event a user
cannot be both a competitor and a judge or organizer — enforced in the service layer *and* by a
Postgres exclusion constraint, not by a form.

**Events.** Creation and editing with configurable dates, tracks, prizes and custom submission
questions. An organizer dashboard with counts, the audit trail, and duplicate flagging.

**Teams.** Creation, invite links (hashed, expiring, optionally use-limited), join, leave, captaincy
transfer, and a team-size cap.

**Projects.** Draft with only a name; submit when name, tagline, description, track, repository URL
and every required custom question are present. Editing after submitting is allowed until the
deadline and never rewrites the submission time. Markdown descriptions rendered through
`markdown-it-py` and sanitized with `nh3`. Image uploads verified by decoding with Pillow — never by
extension — and re-encoded to strip EXIF. Uploaded files are served only by a view that re-applies
the project's visibility rules, so a draft's screenshot is not a public URL.

**Deadline enforcement that holds.** One implementation, `core.deadlines.assert_submissions_open`,
called by every participant write path. The window is half-open: a deadline of 18:00 refuses 18:00:00
itself. A refused write answers HTTP 409 with `{"error": "submissions_closed", "closed_at": "…Z"}`
through the API and the same message in a banner through the UI, and every refusal is written to the
audit log.

**Public gallery.** `/projects` and `/events/<slug>/projects`, server-rendered, with Postgres
full-text search, filters by event, track and tag, two sorts, and pagination — all state in the query
string, so any view is a link you can paste. It shows the **same rows to everyone**: a team does not
see its own draft there, because a team that thought its draft was public would never press submit.
`GET /api/projects` returns the same rows through the same code.

**Audit trail.** Every consequential action and every refusal, with the actor, the event, the target
and the client IP.

All timestamps are stored in UTC and labelled UTC in every view. Time comes from one injectable
clock, so tests pin an instant without freezing the process clock.

## Not done yet

Stated plainly, because a claimed feature that does not work is worse than an absent one.

**T2 (Judging) — none of it.** No judge assignment, no configurable weighted rubric, no organizer
progress dashboard, no cross-judge normalization, no CSV export, and no enforcement that a judge
cannot see another judge's scores. The `scoring` tables exist and the fixture's 126 reviews are
imported into them, but nothing reads them: `assert_judging_open` raises `NotImplementedError` and
`can_view_project_scores` returns `False` for everyone. See [JUDGING.md](JUDGING.md). The four T2
routes in `.dogfood.toml` point at the paths T2 *will* implement and return 404 today.

**T3 (Public) — none of it.** No community voting, no project comments, no voting window, no
randomized ballot ordering. (Rate limiting and the audit trail, listed under T3's anti-abuse bullet,
do exist: login throttling is DB-backed per (email, IP), and the audit log is complete.)

**T4 (Stretch) — none of it.** No webhooks, no certificate or record generation, no signed judge
participation records, no embeddable widget, no bulk import/export beyond the fixture importer.

**Within T1, these specific gaps:**

- **The API cannot edit or submit a project** — `POST /api/events/<slug>/projects` (create) is the
  only write endpoint. Editing and submitting are UI-only.
- **No submission withdrawal.** Nothing moves a project from `submitted` back to `draft`, for anyone,
  including organizers. The last member of a team with a submitted project cannot leave it; they are
  told to invite someone else.
- **Orphaned media files are possible.** A file is unlinked in `transaction.on_commit`, so a rollback
  can never leave a row pointing at a missing file — but a crash between the commit and the unlink
  leaves an unreferenced file on the media volume, and there is no cleanup command. The failure was
  chosen in this direction deliberately.
- **No live duplicate detection on submit.** The fixture importer flags duplicates, and the
  one-active-project-per-team index covers the realistic case; a team copying another team's repo URL
  is flagged by an organizer by hand from the dashboard.
- **No password reset and no outbound email.** There is no mail provider — the one-command, no-runtime-network
  rule forbids one. Invites are copyable links rather than emailed ones.
- **Custom-question answers are stored as text for every question kind**, validated per kind in the
  service layer rather than in typed columns.
- **Search relevance looks odd on fixture data.** Many fixture projects share an identical summary,
  so text ranking cannot discriminate between them. The gallery sorts by newest or name rather than
  by relevance, which is also why.
- **No production web server config.** Gunicorn behind nothing; no TLS termination, no reverse-proxy
  example, no `TRUST_PROXY_HEADERS` guidance beyond the setting existing.

## Development

```bash
./scripts/test.sh                        # the full suite in the container (556 tests)
./scripts/test.sh tests/test_gallery.py -vv
./scripts/acceptance.sh                  # the organizers' checker -> acceptance-report.txt
./scripts/offline-check.sh               # the same checks with the network sealed off
```

Tests run in the container so the Python version, the pinned dependencies and the database are the
ones the portal actually runs on. Nothing to install on the host but Docker.

Generating a migration needs no database:

```bash
docker compose run --rm --no-deps --entrypoint python -v "${PWD}/src:/app/src" \
    web /app/src/manage.py makemigrations <app>
```

[CLAUDE.md](CLAUDE.md) documents the conventions that must not be broken — the service layer, the
single clock, the check order, the viewer-independent gallery scoper — and why each one is there.

### Windows and Git Bash

Two things will waste an afternoon otherwise:

- **Set `MSYS_NO_PATHCONV=1` before any command with a `/path` argument.** MSYS rewrites anything
  that looks like a POSIX path, so `--data-urlencode "next=/profile"` is silently sent as
  `next=C:/Program Files/Git/profile`, and `docker compose exec web python /app/src/manage.py`
  becomes `C:/Program Files/Git/app/src/manage.py`. Both produce failures that look like application
  bugs. One session was spent chasing a "redirect bug" that was entirely this.
- **Line endings are normalized to LF by `.gitattributes`**, because `docker/entrypoint.sh` runs in a
  Linux container and a CRLF shebang makes it die with "no such file or directory". The Dockerfile
  also strips carriage returns from the entrypoint as a second line of defence, so a ZIP download
  still boots. If you add a shell script, check `git ls-files --eol` shows `i/lf`.

PowerShell users: `docker compose up` and `docker compose down -v` work unchanged; the `scripts/*.sh`
helpers need Git Bash or WSL.

## Layout

```
acceptance/          the organizers' checker and fixture data -- READ ONLY
src/config/          settings, urls, wsgi
src/core/            clock, deadlines, permissions, errors, audit log, token auth
src/accounts/        custom User (email login), ApiToken, signup/login/profile
src/events/          Event, Track, Prize, EventMembership, JudgeTrack, CustomQuestion
src/teams/           Team, TeamMember, TeamInvite
src/projects/        Project, ProjectImage, Tag, CustomAnswer, protected media serving
src/gallery/         the public gallery: one query, used by the HTML and JSON doors
src/scoring/         Criterion, Score, ScoreItem -- models and fixture import only (T2 will use them)
src/api/             DRF views, serializers, urls
src/seed/            import_fixtures, seed_demo
tests/               pytest suite
```

Further reading: [ARCHITECTURE.md](ARCHITECTURE.md), [DATA-MODEL.md](DATA-MODEL.md),
[JUDGING.md](JUDGING.md), [PLAN.md](PLAN.md) (the build log, including what went wrong).

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Anurag V Rao.

`acceptance/run.py` and `acceptance/fixtures.json` belong to the DOGFOOD 2026 organizers and are
included unmodified. Vendored front-end assets keep their own licences, recorded in
`src/static/vendor/NOTICE.md`.
