# CLAUDE.md

Guidance for working in this repo. Read README.md for what the portal does, ARCHITECTURE.md for
why, DATA-MODEL.md for the schema, JUDGING.md for the state of T2. PLAN.md is the phase log for the
current work; docs/t2-scoring-plan.md is the approved plan for the scoring engine.

## Stack and layout

Django 5.2 LTS + Postgres 16, server-rendered templates, plain CSS (`src/static/css/crt.css`), no
build step. `docker compose up --build` is the product; it must stay one command, seeded, offline.

- Shared domain apps hold **models and rules only**: `accounts`, `events`, `teams`, `projects`,
  `scoring`, `voting`, `imports`, `records`, `core`. Shared templates (used by several portals) go in
  `src/templates/` (e.g. `_results.html`, `vote.html`), not in a domain app.
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
  different rules. Services write the audit row. New services take the actor and an
  `audit.Origin` (`audit.origin_of(request)`: ip_hash + user agent), **never the request**; the view
  extracts them. Refusals are audited too, and each carries `status` and `code` (see
  `scoring/errors.py`, `voting/errors.py`) so pages and API answer the same.
- **No IP address is stored.** `core.net.hash_ip` (HMAC under a derived key) is the only form an
  address takes in the database: `AuditLog.ip_hash`, `UserSession.ip_hash`, `Ballot.ip_hash`.
- **Keys.** SECRET_KEY comes from `DJANGO_SECRET_KEY` or the file the entrypoint generates in the
  `secrets` volume (`config/secret_key.py`); a known key is refused outside DEMO_MODE. Anything
  keyed from it uses `core.keys.derived_key(purpose)` ("ip-hash", "voter-links", "open-link"), never
  SECRET_KEY directly.
- **Rate limits** are counted from audit rows (`core/ratelimit.py`, the login throttle's pattern):
  a limited write must leave one audit row per attempt, carrying the ip_hash it is limited by.
- **Audit rows that decide something** (a rate limit, a throttle, a cap, a flag) are read through
  `AuditLog.objects.live()`, which leaves out history an event bundle imported
  (`detail.source_history`). `audit.record()` refuses that key; only the bundle import sets it.
- **CSV** is written only by `core.csvfile.to_csv` (formula-escaped); downloads are audited.
- **Deadline first.** Participant writes call `core.deadlines.check_submission_window` before
  permission checks and validation, so a late write is a 409 `submissions_closed`, never a 403 or
  400. The Postgres trigger is the backstop; code that must write after the close uses the
  audited `deadline_bypass()`.
- **Conflict of interest** is refused by `can_compete_in` and by the exclusion constraint
  `membership_no_competitor_and_staff`. Joining or leaving a team must keep the participant
  membership in step (`teams/services.py::_register` / `_unregister`).
- **Postgres-only SQL** (trigger, exclusion constraint) lives in `RunPython` migrations that
  no-op on SQLite. Keep that pattern. A migration that updates rows and then ALTERs the same table
  must be split in two (Postgres refuses the ALTER while deferred FK checks are pending); test data
  migrations against a database that has rows, not only the empty test one.
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
  must 404 until it is real (`tests/test_acceptance_contract.py`; none is left unimplemented). A
  probe must test what it names: `peer_scores` asks for judge_a's real account. `acceptance/` is
  the organizers' and is read-only.
- **Results.** `scoring/services.py` also holds `publish_results` / `unpublish_results` /
  `set_result_settings`; `scoring/results.py` is the read side (who sees what, shared ranks, tie
  groups, winners). The public page 404s unless a publication exists with a public visibility, or
  the caller organizes the event. No per-judge data on any public page.
- **Voting** (`voting/`): every write is `voting/services.py`. Order on a vote write: no vote 404,
  window 409 (`voting_not_open` / `voting_closed`), rate limit 429, access mode / link 403, voter
  rules 403 (`staff_cannot_vote`, `account_too_new`, `own_project`), then 400. The window trigger
  (`voting/migrations/0002`, `0004`) refuses ballot writes outside the window and any DELETE once
  voting opened, and guards the config row; the only way past it is the audited
  `voting.services.voting_bypass`. Voiding (an update of the void columns only) is allowed at any
  time. FKs into ballots are PROTECT (pinned by a test). Tallies are organizers and admins only;
  a GET never writes (a ballot is created by a POST).
- **Final score** (judges + community): `scoring/engine/combine.py` is pure (percentile mid-ranks,
  Fractions, no float tie-breaks). Weights live on `EventScoringConfig` (integers summing to 100),
  are written only by `set_final_weights`, and lock once judging or voting opens (service + trigger
  `scoring/migrations/0011`; the audited `weights_bypass` is for the seed only). A
  `VoteTallySnapshot` is frozen only inside `compute_snapshot` for a final, with the VotingConfig row
  locked and its counter bumped (a concurrent final gets 409 `final_in_progress`); tallies are
  immutable and append-only. With community_weight 0 the combined ranking is the M2 ranking.
- **Comments** (`projects/comments.py`): only on projects in the anonymous gallery
  (`visible_projects()`, no viewer). Order on a post: 401, then no project 404, then comments off
  409, then rate limit 429, then body 400, then duplicate 409 (same author, project and body within
  10 minutes, with the author row locked). Anonymous attempts go in their own audit bucket, capped.
  Hidden and deleted comments are shown only to organizers and admins.
- **Event bundles** (`imports/bundle*.py`). Every id inside a JSON column is declared in
  `imports/bundle_ids.TABLE` with a kind:
  - `plain`: remapped to a bundle id;
  - `composite`: `<judge>:<project>`, both halves remapped;
  - `external`: kept byte for byte.

  An undeclared id-like key or digit key fails the export, so a new engine output with ids must be
  declared first. Engine prose that names ids is listed in `TEXT_TEMPLATES`, and
  `tests/test_bundle_ids_prose.py` fails on an unlisted one.

  The import is one transaction, always creates a new unpublished event, generates fresh secrets,
  and gives non-admins placeholder accounts. Imported audit rows carry `detail.source_history`.
- **Audit readers that decide** (rate limits, throttles, caps, flags) use
  `AuditLog.objects.live()`, which leaves out imported history. `audit.record()` refuses
  `source_history`.
- **Signing keys** (`records/keys.py`): the active key is the one `SigningKey` row with
  `retired_at` NULL, read on every signature. It is never chosen by which PEM files exist, and a
  stray PEM is never adopted. The private keys live only in the secrets volume. Rotation retires,
  then inserts, in one transaction. Only `records/services.py` issues or revokes. Records and keys
  are immutable by trigger, except for revoking (with a category) and retiring once.
- **Framing**: `/embed/` is the only route that may be framed (its own CSP has
  `frame-ancestors *`, and it is `xframe_options_exempt`). It is rendered without the request, and
  `EmbedMiddleware` (outermost) strips every cookie from it. Every other route keeps
  `frame-ancestors 'none'` and DENY.
- **Permanent events**: `Score.project` and `Score.judge` are RESTRICT, `Ballot.event` and
  `IssuedRecord.event` are PROTECT. So an event with reviews, ballots or issued records cannot be
  deleted through the ORM. Keep new such tables PROTECT, and never "fix" this by cascading.
  - `remove_judge` refuses a judge with submitted reviews.
  - The submission and judging-start dates freeze once judging has started with assignments or
    scores.
- **Never touch the user's dev database or running stack** (migrations, `down -v`, restarts)
  without asking. Use a throwaway compose project (`-p <name>`) for fresh boots.

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
