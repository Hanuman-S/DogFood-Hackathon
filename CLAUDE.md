# CLAUDE.md

Guidance for working in this repo. Read README.md for what the portal does, ARCHITECTURE.md for
why, DATA-MODEL.md for the schema, JUDGING.md for the state of T2. PLAN.md is the phase log for the
current work; docs/t2-scoring-plan.md is the approved plan for the scoring engine.

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
- **Where things live.** There is no `core/clock.py` or `core/permissions.py` (both were removed
  in `c6f381c`). The clock is `core.deadlines.db_now()`: the database's `statement_timestamp()`,
  used for every deadline and window decision. Permissions are `accounts/roles.py` (who holds
  which role in which event), `accounts.guards.portal_required` (who may enter a portal) and
  `events.services.get_managed_event` (the event an organizer may manage; 404 otherwise).
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
- **Scoring engine** (`src/scoring/engine/`) is pure: no Django import, no clock, randomness
  only from `np.random.default_rng(<seed from config>)`, and the same input and config give
  byte-identical JSON. `tests/test_engine_purity.py` enforces it. A new scoring method is one
  file in `scoring/engine/methods/` with `@register`; the contract test covers it automatically.
  Never import from `../scoring-lab` (reference only; copy code in).
- **Scoring results** go through `scoring/services.py`. `judging_closed(event, now)` is the only
  place that compares against `judging_ends_at`. `compute_snapshot` opens its own transaction
  whose first statement is `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ` (permission,
  override and window checks run before it opens), and it **raises if called inside another
  transaction**. `ATOMIC_REQUESTS` is off (unset) in this project; any view that calls
  `compute_snapshot` must be `@transaction.non_atomic_requests`. Tests that call it use
  `@pytest.mark.django_db(transaction=True)`; never weaken that check to suit a test.
  `ResultSnapshot` is immutable and `Publication` append-only (Postgres triggers, in
  `scoring/migrations/0005`).
- **Honest claims.** `.dogfood.toml` claims only what `acceptance/run.py` verifies. A T2 route
  must 404 until it is real (`tests/test_acceptance_contract.py`; today only the CSV export). `acceptance/` is the
  organizers' and is read-only.

## Commands

```bash
./scripts/test.sh                # pytest in the web image against Postgres (forces config.settings_test)
./scripts/acceptance.sh          # organizers' checker -> acceptance-report.txt (stack must be up)
./scripts/offline-check.sh       # boot on a sealed network, run the checker inside it
docker compose down -v           # reset; needed after model changes (migrations were squashed)
PYTHONPATH=src python -m scoring.engine.cli acceptance/fixtures.json   # engine on a file, no DB
```

On Windows Git Bash, prefix `./scripts/test.sh` with `MSYS_NO_PATHCONV=1`, or the `/workspace`
mount path is rewritten and the container refuses it.

Running pytest by hand inside the image: pass `--ds=config.settings_test`. The image sets
`DJANGO_SETTINGS_MODULE=config.settings`, which otherwise overrides `pytest.ini`.
