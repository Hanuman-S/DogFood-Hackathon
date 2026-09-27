# CLAUDE.md

Guidance for working in this repo. Read README.md for what the portal does, ARCHITECTURE.md for
why, DATA-MODEL.md for the schema, JUDGING.md for the state of T2.

## Stack and layout

Django 5.2 LTS + Postgres 16, server-rendered templates, plain CSS (`src/static/css/crt.css`), no
build step. `docker compose up --build` is the product; it must stay one command, seeded, offline.

- Shared domain apps hold **models and rules only**: `accounts`, `events`, `teams`, `projects`,
  `scoring`, `imports`, `core`.
- One app per audience holds the pages: `public`, `participant`, `judge`, `organizer`,
  `platform_admin`.

## Rules that keep the guarantees true

- **Roles are per event.** `events.EventMembership(user, event, role)`. Never add a global role
  field. The only platform-wide powers are `User.is_platform_admin` and `User.can_create_events`.
  Everything about roles goes through `accounts/roles.py` (`roles_in`, `is_organizer_of`,
  `can_compete_in`, `PORTAL_ACCESS`).
- **Two gates.** Every portal view has `@portal_required("<portal>")`. Every view that acts on
  one event then checks the caller's role *in that event* (`events.services.get_managed_event`,
  `can_compete_in`). A hidden button is never the check.
- **Every write goes through a `services.py` function.** Pages and the JSON API must not enforce
  different rules. Services write the audit row.
- **Deadline first.** Participant writes call `core.deadlines.check_submission_window` before
  permission checks and validation, so a late write is a 409 `submissions_closed`, never a 403 or
  400. The Postgres trigger is the backstop; code that must write after the close uses the
  audited `deadline_bypass()`.
- **Conflict of interest** is refused by `can_compete_in` and by the exclusion constraint
  `membership_no_competitor_and_staff`. Joining or leaving a team must keep the participant
  membership in step (`teams/services.py::_register` / `_unregister`).
- **Postgres-only SQL** (trigger, exclusion constraint) lives in `RunPython` migrations that
  no-op on SQLite. Keep that pattern.
- **Offline.** No template, stylesheet or script may reference another host
  (`tests/test_platform.py` checks). Vendor assets under `src/static/`.
- **Design.** Use the tokens and components in `crt.css` (`.frame`, `.kv`, `.table`, `.check`,
  `_field.html` prompts). No inline scripts: the CSP forbids them.
- **Honest claims.** `.dogfood.toml` claims only what `acceptance/run.py` verifies. The T2 routes
  must 404 until T2 is real (`tests/test_acceptance_contract.py`). `acceptance/` is the
  organizers' and is read-only.

## Commands

```bash
./scripts/test.sh                # pytest in the web image against Postgres (forces config.settings_test)
./scripts/acceptance.sh          # organizers' checker -> acceptance-report.txt (stack must be up)
./scripts/offline-check.sh       # boot on a sealed network, run the checker inside it
docker compose down -v           # reset; needed after model changes (migrations were squashed)
```

Running pytest by hand inside the image: pass `--ds=config.settings_test`. The image sets
`DJANGO_SETTINGS_MODULE=config.settings`, which otherwise overrides `pytest.ini`.
